"""BharatStock Advisor core: data, technicals, news, scoring, storage. No Streamlit imports here."""
import io
import json
import os
import re
import sqlite3
import time
import warnings
from contextlib import contextmanager
from datetime import datetime, timedelta
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo

import feedparser
import pandas as pd
import requests
import yfinance as yf
from dotenv import load_dotenv

load_dotenv()
warnings.filterwarnings("ignore", module="yfinance")  # pandas deprecation noise from inside yfinance
IST = ZoneInfo("Asia/Kolkata")
DB_PATH = os.getenv("DB_PATH", os.path.join(os.path.dirname(__file__), "bharatstock.db"))
UA = {"User-Agent": "BharatStockAdvisor/0.1 (personal research tool)"}
DISCLAIMER = ("Educational research tool only. Not SEBI-registered advice. Markets can lose money. "
              "Do your own due diligence. Suggestions are based on public data and can be wrong.")
NA = "unavailable"

# Common names -> NSE symbol. Anything else goes through Yahoo search.
ALIASES = {
    "reliance": "RELIANCE", "tcs": "TCS", "tata consultancy": "TCS", "infosys": "INFY", "infy": "INFY",
    "hdfc bank": "HDFCBANK", "hdfcbank": "HDFCBANK", "icici bank": "ICICIBANK", "sbi": "SBIN",
    "state bank": "SBIN", "itc": "ITC", "wipro": "WIPRO", "hcl": "HCLTECH", "l&t": "LT",
    "larsen": "LT", "airtel": "BHARTIARTL", "bharti airtel": "BHARTIARTL", "kotak": "KOTAKBANK",
    "axis bank": "AXISBANK", "maruti": "MARUTI", "asian paints": "ASIANPAINT",
    "bajaj finance": "BAJFINANCE", "sun pharma": "SUNPHARMA", "hul": "HINDUNILVR",
    "hindustan unilever": "HINDUNILVR", "adani enterprises": "ADANIENT",
}

MARKET_TICKERS = {"Nifty 50": "^NSEI", "Sensex": "^BSESN", "Nifty Bank": "^NSEBANK",
                  "India VIX": "^INDIAVIX", "USD/INR": "INR=X"}
# Yahoo sector name -> Nifty sector index
SECTOR_INDEX = {"Technology": "^CNXIT", "Financial Services": "^NSEBANK", "Healthcare": "^CNXPHARMA",
                "Consumer Defensive": "^CNXFMCG", "Consumer Cyclical": "^CNXAUTO", "Energy": "^CNXENERGY",
                "Basic Materials": "^CNXMETAL", "Real Estate": "^CNXREALTY", "Utilities": "^CNXENERGY",
                "Industrials": "^CNXINFRA", "Communication Services": "^CNXMEDIA"}

RSS_FEEDS = {
    "Economic Times Markets": "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
    "Moneycontrol": "https://www.moneycontrol.com/rss/business.xml",
    "Livemint Markets": "https://www.livemint.com/rss/markets",
    "Business Standard Markets": "https://www.business-standard.com/rss/markets-106.rss",
}

# ---------- small helpers ----------

_last_call = {}


def throttle(key, min_gap=1.0):
    """Polite rate limit: at most one call per `min_gap` seconds per source."""
    wait = _last_call.get(key, 0) + min_gap - time.time()
    if wait > 0:
        time.sleep(wait)
    _last_call[key] = time.time()


def now_ist():
    return datetime.now(IST)


def inr(x, decimals=2):
    """Indian digit grouping: 12345678.9 -> '1,23,45,678.90'."""
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return NA
    neg, x = x < 0, abs(float(x))
    whole, frac = f"{x:.{decimals}f}".split(".") if decimals else (f"{x:.0f}", "")
    head, tail = whole[:-3], whole[-3:]
    head = re.sub(r"(\d)(?=(\d\d)+$)", r"\1,", head)
    s = (head + "," + tail) if head else tail
    return ("-" if neg else "") + s + ("." + frac if frac else "")


def rupee(x, decimals=2):
    """Signed rupee amount: -3798 -> '-₹3,798.00' (not '₹-3,798.00')."""
    s = inr(x, decimals)
    return s if s == NA else ("-₹" + s[1:] if s.startswith("-") else "₹" + s)


def crore(x):
    return NA if x is None else f"₹{inr(x / 1e7, 0)} Cr"


def pct(x, mult=1.0):
    return NA if x is None or pd.isna(x) else f"{x * mult:.2f}%"


def market_session(t=None):
    """NSE equity session in IST. Holidays come from NSE_HOLIDAYS in .env."""
    t = t or now_ist()
    holidays = {d.strip() for d in os.getenv("NSE_HOLIDAYS", "").split(",") if d.strip()}
    if t.weekday() >= 5:
        return "closed (weekend)"
    if t.strftime("%Y-%m-%d") in holidays:
        return "holiday"
    hm = t.hour * 60 + t.minute
    if 9 * 60 <= hm < 9 * 60 + 15:
        return "pre-open"
    if 9 * 60 + 15 <= hm < 15 * 60 + 30:
        return "open"
    return "closed"

# ---------- symbol resolution & market data ----------


def resolve(query):
    """Return (nse_symbol, bse_symbol, name) for a name or ticker, or None."""
    q = query.strip()
    base = ALIASES.get(q.lower())
    if not base and re.fullmatch(r"[A-Za-z0-9&\-]+(\.(NS|BO))?", q):
        base = q.upper().split(".")[0]
        for ex in (".NS", ".BO"):  # BSE-only scrips have no .NS listing
            if not yf.Ticker(base + ex).history(period="5d").empty:
                return base + ".NS", base + ".BO", base
        base = None
    if not base:
        throttle("yahoo")
        try:
            quotes = yf.Search(q, max_results=10).quotes
        except Exception:
            quotes = []
        for item in quotes:
            sym = item.get("symbol", "")
            if sym.endswith((".NS", ".BO")):
                base = sym.split(".")[0]
                return base + ".NS", base + ".BO", item.get("longname") or item.get("shortname") or base
        return None
    return base + ".NS", base + ".BO", base


NSE_LIST_URL = "https://archives.nseindia.com/content/equities/EQUITY_L.csv"


def stock_list():
    """All NSE equities as (symbol, name) for type-ahead search. Public CSV, refreshed weekly, kept in SQLite."""
    with db() as con:
        row = con.execute("SELECT v FROM meta WHERE k='stocks_at'").fetchone()
        fresh = row and datetime.fromisoformat(row["v"]) > now_ist() - timedelta(days=7)
        if not fresh:
            try:
                throttle("nse", 1.0)
                r = requests.get(NSE_LIST_URL, headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
                r.raise_for_status()
                df = pd.read_csv(io.StringIO(r.text))
                rows = [(sym.strip(), name.strip()) for sym, name in zip(df["SYMBOL"], df["NAME OF COMPANY"])]
                if len(rows) > 500:  # guard against an error page parsed as CSV
                    con.execute("DELETE FROM stocks")
                    con.executemany("INSERT OR REPLACE INTO stocks VALUES(?,?)", rows)
                    con.execute("INSERT OR REPLACE INTO meta VALUES('stocks_at',?)", (now_ist().isoformat(),))
            except Exception:
                pass  # keep whatever list we had; aliases below are the last fallback
        rows = con.execute("SELECT symbol, name FROM stocks ORDER BY symbol").fetchall()
    return [(r["symbol"], r["name"]) for r in rows] or sorted({(v, k.title()) for k, v in ALIASES.items()})


def history(symbol, period="1y", interval="1d"):
    throttle("yahoo", 0.5)
    try:
        df = yf.Ticker(symbol).history(period=period, interval=interval, auto_adjust=False)
    except Exception:
        return pd.DataFrame()
    return df.dropna(subset=["Close"]) if not df.empty else df


def info(symbol):
    throttle("yahoo", 0.5)
    try:
        return yf.Ticker(symbol).info or {}
    except Exception:
        return {}


def quote_from_history(df):
    if df.empty:
        return None
    last = df.iloc[-1]
    prev = df["Close"].iloc[-2] if len(df) > 1 else None
    return {"price": float(last["Close"]),
            "change_pct": (float(last["Close"]) / prev - 1) * 100 if prev else None,
            "as_of": df.index[-1].tz_convert(IST) if df.index.tz is not None else df.index[-1]}


def market_context():
    out = {}
    for name, sym in MARKET_TICKERS.items():
        df = history(sym, "1y")
        q = quote_from_history(df)
        if q:
            q["above_sma50"] = bool(df["Close"].iloc[-1] > df["Close"].rolling(50).mean().iloc[-1]) \
                if len(df) >= 50 else None
        out[name] = q
    return out


def regime(ctx):
    nifty, vix = ctx.get("Nifty 50"), ctx.get("India VIX")
    if not nifty or nifty.get("above_sma50") is None:
        return "unknown", 0
    risk_on = nifty["above_sma50"]
    high_vix = bool(vix and vix["price"] > 20)
    if risk_on and not high_vix:
        return "risk-on (Nifty above 50-DMA, VIX calm)", 1
    if not risk_on and high_vix:
        return "risk-off (Nifty below 50-DMA, VIX elevated)", -1
    return "mixed (" + ("Nifty above" if risk_on else "Nifty below") + " 50-DMA, VIX " + \
        ("elevated" if high_vix else "calm") + ")", 0


def ret_3m(df):
    return (df["Close"].iloc[-1] / df["Close"].iloc[-63] - 1) * 100 if len(df) > 63 else None

# ---------- technicals ----------


def rsi(close, n=14):
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + gain / loss)


def technicals(df):
    """Daily + weekly indicators. Returns {} when there is not enough history."""
    if len(df) < 30:
        return {}
    c = df["Close"]
    ema12, ema26 = c.ewm(span=12, adjust=False).mean(), c.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False).mean()
    tr = pd.concat([df["High"] - df["Low"], (df["High"] - c.shift()).abs(), (df["Low"] - c.shift()).abs()],
                   axis=1).max(axis=1)
    sma = {n: (c.rolling(n).mean().iloc[-1] if len(c) >= n else None) for n in (20, 50, 200)}
    price = float(c.iloc[-1])
    avg_vol = df["Volume"].iloc[-21:-1].mean()
    t = {
        "price": price, "sma20": sma[20], "sma50": sma[50], "sma200": sma[200],
        "rsi": float(rsi(c).iloc[-1]), "macd": float(macd.iloc[-1]), "macd_signal": float(signal.iloc[-1]),
        "atr": float(tr.rolling(14).mean().iloc[-1]),
        "vol_ratio": float(df["Volume"].iloc[-1] / avg_vol) if avg_vol else None,
        "support": (float(df["Low"].iloc[-20:].min()), float(df["Low"].iloc[-60:].min())),
        "resistance": (float(df["High"].iloc[-20:].max()), float(df["High"].iloc[-60:].max())),
    }
    t["trend"] = trend_label(price, sma[20], sma[50], sma[200])
    wk = df.resample("W-FRI").agg({"Close": "last"}).dropna()["Close"]
    t["weekly_rsi"] = float(rsi(wk).iloc[-1]) if len(wk) > 15 else None
    w20 = wk.rolling(20).mean().iloc[-1] if len(wk) >= 20 else None
    w10 = wk.rolling(10).mean().iloc[-1] if len(wk) >= 10 else None
    t["weekly_trend"] = trend_label(float(wk.iloc[-1]), w10, w20, None) if w20 else NA
    return t


def trend_label(price, fast, mid, slow):
    if fast is None or mid is None:
        return "range"
    if price > fast > mid and (slow is None or mid > slow):
        return "uptrend"
    if price < fast < mid and (slow is None or mid < slow):
        return "downtrend"
    return "range"

# ---------- fundamentals ----------


def fundamentals(symbol, inf):
    """Annual revenue/profit trend from Yahoo statements plus ratio red flags. Missing stays missing."""
    out = {"revenue": [], "profit": [], "flags": []}
    try:
        throttle("yahoo", 0.5)
        inc = yf.Ticker(symbol).income_stmt
        for row, key in (("Total Revenue", "revenue"), ("Net Income", "profit")):
            if inc is not None and row in inc.index:
                s = inc.loc[row].dropna().sort_index()
                out[key] = [(d.year, float(v)) for d, v in s.items()]
    except Exception:
        pass
    for key in ("revenue", "profit"):
        vals = [v for _, v in out[key]]
        out[key + "_growth"] = (vals[-1] / vals[-2] - 1) * 100 if len(vals) >= 2 and vals[-2] > 0 else None
    de, roe, pe = inf.get("debtToEquity"), inf.get("returnOnEquity"), inf.get("trailingPE")
    if de is not None and de > 150:
        out["flags"].append(f"High debt/equity ({de:.0f}%)")
    if roe is not None and roe < 0.08:
        out["flags"].append(f"Low ROE ({roe * 100:.1f}%)")
    if pe is not None and pe > 60:
        out["flags"].append(f"Rich valuation (PE {pe:.0f})")
    if out["profit_growth"] is not None and out["profit_growth"] < -10:
        out["flags"].append(f"Net profit fell {abs(out['profit_growth']):.0f}% last year")
    if inf.get("profitMargins") is not None and inf["profitMargins"] < 0:
        out["flags"].append("Loss-making (negative net margin)")
    return out

# ---------- news ----------

BULL = ["beats", "surge", "jumps", "rises", "record", "upgrade", "buy rating", "order win", "wins order",
        "rally", "gains", "strong", "growth", "profit up", "outperform", "raises", "bags", "approval"]
BEAR = ["misses", "falls", "slumps", "plunges", "downgrade", "sell rating", "loss", "weak", "probe",
        "penalty", "fraud", "raid", "decline", "cuts", "lawsuit", "resigns", "default", "pledge", "slips"]
FLAGS = {"results": ["result", "q1", "q2", "q3", "q4", "quarterly", "earnings"],
         "guidance": ["guidance", "outlook"], "promoter pledge": ["pledge"],
         "block deal": ["block deal", "bulk deal", "stake sale"], "legal": ["court", "sebi", "lawsuit", "probe", "penalty"],
         "rating change": ["upgrade", "downgrade", "target price", "rating"],
         "policy": ["government", "policy", "rbi", "budget", "tariff", "regulation"]}


def sentiment(text):
    t = text.lower()
    b, s = sum(w in t for w in BULL), sum(w in t for w in BEAR)
    if b and s:
        return "mixed"
    return "bullish" if b else "bearish" if s else "noise"


def flags_for(text):
    t = text.lower()
    return [f for f, words in FLAGS.items() if any(re.search(r"\b" + re.escape(w) + r"\b", t) for w in words)]


def _entry(e, source):
    ts = e.get("published_parsed") or e.get("updated_parsed")
    dt = datetime(*ts[:6], tzinfo=ZoneInfo("UTC")).astimezone(IST) if ts else None
    title = re.sub(r"\s+", " ", e.get("title", "")).strip()
    return {"date": dt, "headline": title, "source": source, "link": e.get("link", ""),
            "sentiment": sentiment(title), "flags": flags_for(title)}


def fetch_news(name, base, days=30):
    """Google News India RSS search + major Indian market RSS feeds, filtered to this company."""
    items, errors = [], []
    q = quote_plus(f'"{name}" OR {base} share NSE')
    gn = f"https://news.google.com/rss/search?q={q}+when:{days}d&hl=en-IN&gl=IN&ceid=IN:en"
    feeds = {"Google News (India)": gn, **RSS_FEEDS}
    keys = {k.lower() for k in (name, base) if k and len(k) >= 3}
    for source, url in feeds.items():
        throttle("rss:" + source, 1.0)
        try:
            r = requests.get(url, headers=UA, timeout=10)
            r.raise_for_status()
            parsed = feedparser.parse(r.content)
        except Exception as ex:
            errors.append(f"{source}: {type(ex).__name__}")
            continue
        for e in parsed.entries:
            item = _entry(e, source)
            if source.startswith("Google"):
                # Google titles end with " - Publisher"; surface the real publisher
                head, _, pub = item["headline"].rpartition(" - ")
                if head:
                    item.update(headline=head, source=pub + " via Google News",
                                sentiment=sentiment(head), flags=flags_for(head))
            elif not any(k in item["headline"].lower() for k in keys):
                continue
            items.append(item)
    items += fetch_api_news(name, errors)
    cutoff = now_ist() - timedelta(days=days)
    seen, out = set(), []
    for it in sorted(items, key=lambda i: i["date"] or cutoff, reverse=True):
        k = it["headline"].lower()[:80]
        if k in seen or (it["date"] and it["date"] < cutoff):
            continue
        seen.add(k)
        out.append(it)
    return out[:40], errors


def fetch_api_news(name, errors):
    """Optional NewsAPI / GNews, only if keys are set in .env."""
    out = []
    if key := os.getenv("NEWSAPI_KEY"):
        try:
            throttle("newsapi", 1.0)
            r = requests.get("https://newsapi.org/v2/everything", timeout=10, params={
                "q": name, "language": "en", "sortBy": "publishedAt", "pageSize": 20, "apiKey": key})
            for a in r.json().get("articles", []):
                dt = datetime.fromisoformat(a["publishedAt"].replace("Z", "+00:00")).astimezone(IST)
                out.append({"date": dt, "headline": a["title"], "source": a["source"]["name"] + " (NewsAPI)",
                            "link": a["url"], "sentiment": sentiment(a["title"]), "flags": flags_for(a["title"])})
        except Exception as ex:
            errors.append(f"NewsAPI: {type(ex).__name__}")
    if key := os.getenv("GNEWS_KEY"):
        try:
            throttle("gnews", 1.0)
            r = requests.get("https://gnews.io/api/v4/search", timeout=10, params={
                "q": name, "lang": "en", "country": "in", "max": 20, "apikey": key})
            for a in r.json().get("articles", []):
                dt = datetime.fromisoformat(a["publishedAt"].replace("Z", "+00:00")).astimezone(IST)
                out.append({"date": dt, "headline": a["title"], "source": a["source"]["name"] + " (GNews)",
                            "link": a["url"], "sentiment": sentiment(a["title"]), "flags": flags_for(a["title"])})
        except Exception as ex:
            errors.append(f"GNews: {type(ex).__name__}")
    return out


def news_digest(items):
    if not items:
        return ["No matching news found in the feeds checked. This is not a signal by itself."]
    counts = {s: sum(i["sentiment"] == s for i in items) for s in ("bullish", "bearish", "mixed", "noise")}
    bullets = [f"{len(items)} headlines found: {counts['bullish']} bullish, {counts['bearish']} bearish, "
               f"{counts['mixed']} mixed, {counts['noise']} neutral/noise (keyword-based, rough)."]
    flagged = {}
    for i in items:
        for f in i["flags"]:
            flagged.setdefault(f, i)
    for f, i in list(flagged.items())[:5]:
        bullets.append(f"Flag — {f}: \"{i['headline']}\" ({i['source']}, {fmt_date(i['date'])})")
    for i in [i for i in items if i["sentiment"] != "noise" and i not in flagged.values()][:max(0, 7 - len(bullets))]:
        bullets.append(f"{i['sentiment'].title()}: \"{i['headline']}\" ({i['source']}, {fmt_date(i['date'])})")
    return bullets[:8]


def news_score(items):
    """-2..+2 from bullish minus bearish share. Needs at least 3 non-noise items to count."""
    sig = [i for i in items if i["sentiment"] in ("bullish", "bearish")]
    if len(sig) < 3:
        return 0
    net = (sum(i["sentiment"] == "bullish" for i in sig) - sum(i["sentiment"] == "bearish" for i in sig)) / len(sig)
    return 2 if net > 0.4 else 1 if net > 0.15 else -2 if net < -0.4 else -1 if net < -0.15 else 0


def fmt_date(d):
    return d.strftime("%d %b %Y") if d else "date unknown"

# ---------- suggestion engine ----------


def score(tech, inf, fund, news_items, regime_score):
    """Signed component scores. Each component explains itself."""
    parts = {"market": (regime_score, [f"Market regime score {regime_score:+d}"])}
    t, why = 0, []
    if tech:
        t += {"uptrend": 2, "range": 0, "downtrend": -2}[tech["trend"]]
        why.append(f"Daily trend: {tech['trend']}")
        if tech["sma200"] is not None:
            above = tech["price"] > tech["sma200"]
            t += 1 if above else -1
            why.append("Price above 200-DMA" if above else "Price below 200-DMA")
        if tech["macd"] > tech["macd_signal"]:
            t += 1
            why.append("MACD above signal")
        else:
            why.append("MACD below signal")
        why.append(f"RSI {tech['rsi']:.0f}")
    parts["technical"] = (t, why or ["Not enough price history (needs 30+ trading days)"])
    f, why = 0, []
    roe, de, pe = inf.get("returnOnEquity"), inf.get("debtToEquity"), inf.get("trailingPE")
    if roe is not None:
        f += 1 if roe >= 0.15 else -1 if roe < 0.08 else 0
        why.append(f"ROE {roe * 100:.1f}%")
    if de is not None:
        f += -1 if de > 150 else 1 if de < 50 else 0
        why.append(f"Debt/equity {de:.0f}%")
    if pe is not None:
        f += -1 if pe > 60 else 0
        why.append(f"PE {pe:.1f}")
    if fund.get("profit_growth") is not None:
        f += 1 if fund["profit_growth"] > 10 else -1 if fund["profit_growth"] < -10 else 0
        why.append(f"Net profit growth {fund['profit_growth']:.1f}% (latest year)")
    parts["fundamental"] = (f, why or ["Fundamentals unavailable"])
    n = news_score(news_items)
    parts["news"] = (n, [f"News tone score {n:+d} from {len(news_items)} headlines"])
    return parts


def confidence(parts, tech, inf, n_news):
    """1-5: how much data we have and how much the components agree. Capped at 4 — public data is never certain."""
    available = sum([bool(tech), inf.get("returnOnEquity") is not None, n_news > 0, True])
    signs = [v for v, _ in parts.values() if v != 0]
    agree = abs(sum(1 if v > 0 else -1 for v in signs)) / len(signs) if signs else 0
    return max(1, min(4 if tech else 2, round(available * 0.5 + agree * 2)))


def buy_suggestion(parts, tech, inf, news_items):
    total = sum(v for v, _ in parts.values())
    conf = confidence(parts, tech, inf, len(news_items))
    overbought = tech and (tech["rsi"] > 70 or (tech["sma20"] and tech["price"] > tech["sma20"] * 1.08))
    if not tech:
        action = "WATCH"
    elif total <= -3 or (tech["trend"] == "downtrend" and parts["fundamental"][0] < 0):
        action = "AVOID"
    elif total >= 4 and overbought:
        action = "WAIT FOR DIP"
    elif total >= 4 and parts["technical"][0] >= 1:
        action = "CONSIDER BUY"
    elif total >= 2 and tech["trend"] != "downtrend":
        action = "WAIT FOR DIP"
    else:
        action = "WATCH"
    better, worse = [], []
    if tech:
        s1 = tech["support"][0]
        better.append(f"A pullback toward support ₹{inr(s1)}–₹{inr(tech['sma50'] or s1)} that holds")
        better.append("Fresh results confirming profit growth")
        worse.append(f"Daily close below ₹{inr(tech['support'][1])} (60-day low)")
        if tech["sma200"]:
            worse.append(f"Breaking below the 200-DMA (~₹{inr(tech['sma200'])})")
    worse.append("Negative results, guidance cut, promoter pledge increase, or regulatory action")
    return {"action": action, "confidence": conf, "total": total, "better": better, "worse": worse}


def buy_band(tech):
    """Pullback zone: from 20-day support (at most 3 ATR down) up to the 20-day average, never above price."""
    price, atr = tech["price"], tech["atr"]
    lo = max(tech["support"][0], price - 3 * atr)
    hi = max(lo + 0.5 * atr, min(price, tech["sma20"] or price - atr))
    return round(lo, 2), round(hi, 2)


def stop_zone(tech):
    """Range, not a tick: 0.5–1.5 ATR under the buy zone, so a close there means support has failed."""
    if not tech:
        return None
    lo = buy_band(tech)[0]
    return round(max(0.01, lo - 1.5 * tech["atr"]), 2), round(max(0.01, lo - 0.5 * tech["atr"]), 2)


def zones(tech):
    """Three price bands for the plan: where to buy, where the view breaks (sell), where to book profit."""
    if not tech:
        return None
    price, atr, r20 = tech["price"], tech["atr"], tech["resistance"][0]
    buy = buy_band(tech)
    profit = (round(r20, 2), round(r20 + atr, 2)) if r20 > price * 1.01 else         (round(price + atr, 2), round(price + 2 * atr, 2))
    return {"buy": buy, "stop": stop_zone(tech), "profit": profit,
            # a falling stock sitting in its "buy zone" is not a buy signal
            "in_buy": buy[0] <= price <= buy[1] * 1.01 and tech["trend"] != "downtrend",
            "falling": tech["trend"] == "downtrend", "near_profit": price >= profit[0] * 0.97}


def pros_cons(tech, inf, fund, news_items, regime_label, stock_3m, sector_3m):
    """Screener-style reasons for and against, only from data we actually have."""
    pro, con = [], []
    if tech:
        (pro if tech["trend"] == "uptrend" else con if tech["trend"] == "downtrend" else []).append(
            f"Daily trend is {tech['trend']} (price vs 20/50/200-day averages)")
        if tech["sma200"]:
            (pro if tech["price"] > tech["sma200"] else con).append(
                f"Price is {'above' if tech['price'] > tech['sma200'] else 'below'} the 200-day average "
                f"(₹{inr(tech['sma200'])})")
        (pro if tech["macd"] > tech["macd_signal"] else con).append(
            "MACD momentum is " + ("rising" if tech["macd"] > tech["macd_signal"] else "falling"))
        if tech["rsi"] > 70:
            con.append(f"RSI {tech['rsi']:.0f} — overbought, chasing here is risky")
        elif tech["rsi"] < 30:
            pro.append(f"RSI {tech['rsi']:.0f} — oversold, a bounce is possible")
    roe, de, pe = inf.get("returnOnEquity"), inf.get("debtToEquity"), inf.get("trailingPE")
    if roe is not None:
        (pro if roe >= 0.15 else con if roe < 0.08 else []).append(f"ROE {roe * 100:.1f}%")
    if de is not None:
        (pro if de < 50 else con if de > 150 else []).append(f"Debt/equity {de:.0f}%")
    if pe is not None and pe > 60:
        con.append(f"Expensive at PE {pe:.0f}")
    for key, label in (("revenue_growth", "Revenue"), ("profit_growth", "Net profit")):
        g = fund.get(key)
        if g is not None:
            (pro if g > 10 else con if g < 0 else []).append(f"{label} {'grew' if g >= 0 else 'fell'} "
                                                             f"{abs(g):.1f}% in the latest year")
    if stock_3m is not None and sector_3m is not None:
        (pro if stock_3m > sector_3m else con).append(
            f"3-month return {stock_3m:+.1f}% vs sector {sector_3m:+.1f}%")
    n = news_score(news_items)
    if n > 0:
        pro.append("Recent news tone leans positive")
    elif n < 0:
        con.append("Recent news tone leans negative")
    if "risk-off" in regime_label:
        con.append("Broad market is risk-off")
    elif "risk-on" in regime_label:
        pro.append("Broad market is risk-on")
    return pro, con


def hold_suggestion(parts, tech, inf, holding, news_items):
    total = sum(v for v, _ in parts.values())
    conf = confidence(parts, tech, inf, len(news_items))
    if not tech:
        return {"action": "WAIT", "confidence": 1, "total": total, "pnl": None, "pnl_pct": None,
                "stop": None, "invalidation": ["Not enough price history to judge (new listing or data gap) — "
                                               "revisit after ~30 trading days"]}
    price = tech["price"]
    cost = holding["avg_price"] * holding["qty"]
    pnl = price * holding["qty"] - cost
    pnl_pct = (price / holding["avg_price"] - 1) * 100
    results_soon = any("results" in i["flags"] for i in news_items[:10])
    below_200 = tech["sma200"] is not None and price < tech["sma200"]
    if tech["trend"] == "downtrend" and below_200 and total <= -3:
        action = "EXIT"
    elif (pnl_pct > 30 and tech["rsi"] > 72) or (pnl_pct > 15 and total <= -2):
        action = "TRIM"
    elif results_soon and abs(total) <= 2:
        action = "WAIT"
    elif total >= 4 and tech["rsi"] < 68 and tech["trend"] == "uptrend":
        action = "ACCUMULATE"
    else:
        action = "HOLD"
    inval = [f"Close below the stop zone (₹{inr(stop_zone(tech)[0])}–₹{inr(stop_zone(tech)[1])})"]
    if action in ("EXIT", "TRIM"):
        inval.append(f"Reclaiming the 50-DMA (~₹{inr(tech['sma50'])}) with volume would soften this view"
                     if tech["sma50"] else "A trend reversal would soften this view")
    else:
        inval.append("Weak results / guidance cut or a break of the 200-DMA would turn this negative")
    if results_soon:
        inval.append("Results-related news in the last few days — volatility likely")
    return {"action": action, "confidence": conf, "total": total, "pnl": pnl, "pnl_pct": pnl_pct,
            "stop": stop_zone(tech), "invalidation": inval}


def thesis(name, tech, parts, action):
    if not tech:
        return f"{name}: not enough price history for technicals yet, so no view beyond watching."
    tone = "constructive" if parts["fundamental"][0] > 0 else "weak" if parts["fundamental"][0] < 0 else "neutral"
    return (f"{name} is in a {tech['trend']} with {tone} fundamentals on available data; "
            f"suggestion based on public data: {action}.")


def risks(tech, inf, fund, ctx_regime, news_items):
    r = list(fund.get("flags", []))
    if tech and tech["rsi"] > 70:
        r.append(f"Overbought (RSI {tech['rsi']:.0f}) — short-term pullback risk")
    if tech and tech["vol_ratio"] and tech["vol_ratio"] > 2:
        r.append(f"Volume spike ({tech['vol_ratio']:.1f}x average) — check for news")
    if "risk-off" in ctx_regime:
        r.append("Broad market is risk-off")
    for f in ("legal", "promoter pledge", "block deal"):
        if any(f in i["flags"] for i in news_items):
            r.append(f"News mentions {f} — read the source")
    r.append("Promoter pledge data not available from free sources in v1 — check NSE shareholding filings")
    r.append("Data is delayed and may be incomplete; Yahoo ratios can lag the latest quarter")
    return r

# ---------- full report ----------


def analyse(query, holding=None):
    res = resolve(query)
    if not res:
        return {"error": f"Could not find an NSE/BSE listing for '{query}'."}
    nse, bse, name = res
    df = history(nse, "1y")
    exch = "NSE"
    if df.empty:
        df, exch = history(bse, "1y"), "BSE"
    inf = info(nse if exch == "NSE" else bse)
    name = inf.get("longName") or name
    tech = technicals(df) if not df.empty else {}
    ctx = market_context()
    reg_label, reg_score = regime(ctx)
    fund = fundamentals(nse if exch == "NSE" else bse, inf)
    news_items, news_errors = fetch_news(inf.get("shortName") or name, nse.split(".")[0])
    if news_items:
        cache_news(nse, news_items)
    else:
        news_items = cached_news(nse)
    parts = score(tech, inf, fund, news_items, reg_score)
    sector_idx = SECTOR_INDEX.get(inf.get("sector"))
    sector_df = history(sector_idx, "1y") if sector_idx else pd.DataFrame()
    rep = {
        "name": name, "nse": nse, "bse": bse, "exchange": exch, "info": inf, "history": df,
        "quote": quote_from_history(df), "tech": tech, "fund": fund, "ctx": ctx, "regime": reg_label,
        "session": market_session(), "news": news_items, "news_errors": news_errors,
        "digest": news_digest(news_items), "parts": parts,
        "sector_index": sector_idx, "stock_3m": ret_3m(df) if not df.empty else None,
        "sector_3m": ret_3m(sector_df) if not sector_df.empty else None,
        "fetched_at": now_ist(),
    }
    rep["buy"] = buy_suggestion(parts, tech, inf, news_items)
    # Not held: still show what an owner would do, priced at today's level (P/L = 0)
    rep["held"] = bool(holding)
    rep["hold"] = hold_suggestion(parts, tech, inf, holding or {"qty": 0, "avg_price": tech.get("price", 1)},
                                  news_items)
    rep["zones"] = zones(tech)
    rep["pros"], rep["cons"] = pros_cons(tech, inf, fund, news_items, reg_label, rep["stock_3m"], rep["sector_3m"])
    rep["thesis"] = thesis(name, tech, parts, (rep["hold"] if holding else rep["buy"])["action"])
    rep["risks"] = risks(tech, inf, fund, reg_label, news_items)
    return rep

# ---------- SQLite: holdings, watchlist, news cache ----------


@contextmanager
def db():
    """`with db() as con:` commits on success, rolls back on error, and always closes the file."""
    con = sqlite3.connect(DB_PATH)
    try:
        with con:
            yield _ready(con)
    finally:
        con.close()


def _ready(con):
    con.row_factory = sqlite3.Row
    con.executescript("""
        CREATE TABLE IF NOT EXISTS holdings(id INTEGER PRIMARY KEY, symbol TEXT NOT NULL, qty REAL NOT NULL,
            avg_price REAL NOT NULL, buy_date TEXT, sample INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS watchlist(symbol TEXT PRIMARY KEY, added TEXT, sample INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS news_cache(symbol TEXT, link TEXT, date TEXT, headline TEXT, source TEXT,
            sentiment TEXT, flags TEXT, PRIMARY KEY(symbol, link));
        CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
        CREATE TABLE IF NOT EXISTS stocks(symbol TEXT PRIMARY KEY, name TEXT);
    """)
    if not con.execute("SELECT 1 FROM meta WHERE k='seeded'").fetchone():
        # Sample rows so the UI is not empty on first run. Marked sample=1; delete freely.
        con.executemany("INSERT INTO holdings(symbol,qty,avg_price,buy_date,sample) VALUES(?,?,?,?,1)",
                        [("TCS", 10, 2475.0, "2026-04-27"), ("INFY", 25, 1180.0, "2026-04-27"),
                         ("HDFCBANK", 40, 770.0, "2026-04-27")])
        con.executemany("INSERT OR IGNORE INTO watchlist VALUES(?,?,1)",
                        [(s, "2026-10-28") for s in ("TCS", "RELIANCE", "ICICIBANK", "ITC")])
        con.execute("INSERT INTO meta VALUES('seeded','1')")
        con.commit()
    return con


def cache_news(symbol, items):
    with db() as con:
        con.executemany("INSERT OR REPLACE INTO news_cache VALUES(?,?,?,?,?,?,?)",
                        [(symbol, i["link"], i["date"].isoformat() if i["date"] else None, i["headline"],
                          i["source"], i["sentiment"], ",".join(i["flags"])) for i in items])


def verdict_change(symbol, action, parts):
    """Remember the buy-side verdict per symbol. Returns the last different one ({action, scores, date}) or None."""
    key, cur = "verdict:" + symbol, {"action": action, "scores": parts, "date": now_ist().strftime("%d %b %Y")}
    with db() as con:
        row = con.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
        saved = json.loads(row["v"]) if row else {}
        # prev = the old verdict as last seen, so its scores show what moved
        saved = {"cur": cur, "prev": saved.get("cur") if saved.get("cur", {}).get("action") != action
                 else saved.get("prev")}
        con.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (key, json.dumps(saved)))
    return saved.get("prev")


def cached_news(symbol, days=30):
    """Fallback when feeds fail: only cached headlines from the same window, so old news never counts as fresh."""
    since = (now_ist() - timedelta(days=days)).isoformat()
    with db() as con:
        rows = con.execute("SELECT * FROM news_cache WHERE symbol=? AND date>=? ORDER BY date DESC LIMIT 40",
                           (symbol, since)).fetchall()
    return [{"date": datetime.fromisoformat(r["date"]) if r["date"] else None, "headline": r["headline"],
             "source": r["source"] + " (cached)", "link": r["link"], "sentiment": r["sentiment"],
             "flags": [f for f in (r["flags"] or "").split(",") if f]} for r in rows]
