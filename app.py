"""BharatStock Advisor — Streamlit UI. Run: streamlit run app.py"""

import pandas as pd
import streamlit as st

import core
from core import DISCLAIMER, NA, crore, inr, pct, rupee

st.set_page_config(page_title="BharatStock Advisor", page_icon=":material/candlestick_chart:", layout="wide")

# action -> (color, icon, plain-English meaning)
ACTIONS = {
    "CONSIDER BUY": ("green", "trending_up", "Signals line up. Consider buying inside the buy zone, in parts, "
                                             "with a stop below the sell zone."),
    "WAIT FOR DIP": ("orange", "hourglass_top", "Decent on data, but the price is stretched. Wait for it to come "
                                                "back into the buy zone."),
    "WATCH": ("blue", "visibility", "No clear edge right now. Keep it on the watchlist."),
    "AVOID": ("red", "block", "Weak trend and/or weak fundamentals. Better to stay away for now."),
    "ACCUMULATE": ("green", "add_circle", "Trend and fundamentals support adding more, ideally inside the buy zone."),
    "HOLD": ("blue", "pan_tool", "Nothing says sell. Keep holding; act only if price closes below the sell zone."),
    "TRIM": ("orange", "content_cut", "Consider booking part of the profit, especially inside the profit zone."),
    "EXIT": ("red", "logout", "Trend broken and data weak. Consider exiting, or at least cutting size."),
    "WAIT": ("orange", "schedule", "Wait before acting: a results/news event is close, or there isn't enough data yet."),
}
TREND_COLOR = {"uptrend": "green", "range": "gray", "downtrend": "red"}


@st.cache_data(ttl=600, show_spinner=False)
def cached_report(query, holding_key):
    holding = dict(zip(("qty", "avg_price"), holding_key)) if holding_key else None
    return core.analyse(query, holding)


@st.cache_data(ttl=300, show_spinner=False)
def cached_context():
    return core.market_context()


@st.cache_data(ttl=300, show_spinner=False)
def cached_quick(symbol):
    """Price + technical-only signal for list pages (no news, so it stays fast)."""
    df = core.history(symbol, "1y")
    if df.empty and symbol.endswith(".NS"):  # BSE-only scrip
        df = core.history(symbol[:-3] + ".BO", "1y")
    q = core.quote_from_history(df)
    t = core.technicals(df) if not df.empty else {}
    return q, t, core.zones(t)


@st.cache_data(ttl=86400, show_spinner=False)
def cached_stock_list():
    return core.stock_list()


def holding_for(symbol_base):
    with core.db() as con:
        rows = con.execute("SELECT qty, avg_price FROM holdings WHERE symbol=?", (symbol_base,)).fetchall()
    if not rows:
        return None
    qty = sum(r["qty"] for r in rows)
    return (qty, sum(r["qty"] * r["avg_price"] for r in rows) / qty)


def option_for(sym):
    name = dict(cached_stock_list()).get(sym)
    return f"{sym} — {name}" if name else sym


def to_symbol(text):
    """Selectbox value (picked or free-typed) -> NSE base symbol, or None if it can't be resolved."""
    text = text.split(" — ")[0].strip()
    if text.upper() in dict(cached_stock_list()):
        return text.upper()
    res = core.resolve(text)
    return res[0].split(".")[0] if res else None


def open_report(query):
    # set the search box's own state before switching, so the report page opens on this stock
    st.session_state.query, st.session_state.search_pick = query, option_for(query)
    st.switch_page(search_page)


def band(lo_hi):
    return f"₹{inr(lo_hi[0])} – ₹{inr(lo_hi[1])}" if lo_hi else NA


def market_strip():
    ctx = cached_context()
    n = ctx.get("Nifty 50")
    move = (f" :{'green' if n['change_pct'] >= 0 else 'red'}[{n['change_pct']:+.2f}%]"
            if n and n["change_pct"] is not None else "")
    label = f"Market: Nifty 50 {inr(n['price']) if n else NA}{move} · NSE {core.market_session()} (delayed)"
    with st.expander(label, icon=":material/monitoring:"):
        with st.container(horizontal=True):
            for name, q in ctx.items():
                st.metric(name, inr(q["price"]) if q else NA,
                          f"{q['change_pct']:+.2f}%" if q and q["change_pct"] is not None else None, border=True)
        st.caption(f"Index data delayed (Yahoo Finance). Checked {core.now_ist():%d %b %Y, %H:%M} IST. "
                   "FII/DII flows: not available in v1.")


def verdict(title, s, subtitle):
    color, icon, meaning = ACTIONS[s["action"]]
    with st.container(border=True, height="stretch"):
        st.caption(title)
        st.markdown(f"## :{color}[:material/{icon}: {s['action']}]")
        st.markdown(f"Confidence **{s['confidence']}/5** " + ":material/star:" * s["confidence"]
                    + ":material/star_border:" * (5 - s["confidence"]))
        st.write(meaning)
        st.caption(subtitle)


def scorecard(parts):
    labels = {2: ("Strong", "green"), 1: ("Positive", "green"), 0: ("Neutral", "gray"),
              -1: ("Weak", "orange"), -2: ("Poor", "red")}
    icons = {"market": "public", "technical": "show_chart", "fundamental": "account_balance", "news": "newspaper"}
    cols = st.columns(4)
    for col, (k, (v, why)) in zip(cols, parts.items()):
        label, color = labels[max(-2, min(2, v))]
        with col.container(border=True, height="stretch"):
            st.markdown(f"**:material/{icons[k]}: {k.title()}**")
            st.badge(f"{label} ({v:+d})", color=color)
            st.caption(" · ".join(why))


def report(query):
    with st.spinner(f"Researching {query}: prices, news, fundamentals…"):
        rep = cached_report(query, holding_for(query.strip().upper()))
    if "error" in rep:
        st.error(rep["error"], icon=":material/search_off:")
        return
    base = rep["nse"].split(".")[0]
    if not rep["held"] and (h := holding_for(base)):
        rep = cached_report(query, h)  # a typed name resolved to a symbol you hold
    inf, q, t, f, z = rep["info"], rep["quote"], rep["tech"], rep["fund"], rep["zones"]

    # ---- header ----
    head, act = st.columns([4, 1], vertical_alignment="bottom")
    head.title(rep["name"])
    with head.container(horizontal=True):
        st.badge(rep["nse"], icon=":material/tag:", color="blue")
        st.badge(rep["exchange"] + " data", color="gray")
        st.badge(inf.get("sector", "sector unavailable"), color="violet")
        st.badge("delayed price", icon=":material/schedule:", color="orange")
        if rep["held"]:
            st.badge("in your holdings", icon=":material/work:", color="green")
    if act.button("Add to watchlist", icon=":material/bookmark_add:", width="stretch"):
        with core.db() as con:
            con.execute("INSERT OR IGNORE INTO watchlist VALUES(?,?,0)", (base, core.now_ist().date().isoformat()))
        st.toast(f"{base} added to watchlist", icon=":material/check:")
    st.markdown(f"*{rep['thesis']}*")
    if prev := core.verdict_change(base, rep["buy"]["action"], {k: v for k, (v, _) in rep["parts"].items()}):
        moved = [f"{k} {prev['scores'].get(k, 0):+d} → {v:+d}" for k, (v, _) in rep["parts"].items()
                 if prev["scores"].get(k) != v]
        st.info(f"Buy-side view changed: was **{prev['action']}** (as of {prev['date']}), now **{rep['buy']['action']}**. "
                + ("What moved: " + ", ".join(moved) + "." if moved else "Scores unchanged; thresholds decided it."),
                icon=":material/history:")

    # ---- 1. the decision ----
    with st.container(border=True):
        price_col, buy_col, own_col = st.columns([1.1, 1.4, 1.4])
        with price_col:
            st.metric("Last price (delayed)", f"₹{inr(q['price'])}" if q else NA,
                      f"{q['change_pct']:+.2f}%" if q and q["change_pct"] is not None else None)
            lo, hi = inf.get("fiftyTwoWeekLow"), inf.get("fiftyTwoWeekHigh")
            if q and lo and hi and hi > lo:
                st.progress(min(1.0, max(0.0, (q["price"] - lo) / (hi - lo))),
                            text=f"52-week range ₹{inr(lo, 0)} – ₹{inr(hi, 0)}")
            st.caption(f"NSE session: {rep['session']}")
        with buy_col:
            verdict("If you don't own it", rep["buy"], "Buy-side view: WATCH · WAIT FOR DIP · CONSIDER BUY · AVOID")
        with own_col:
            h, mine = rep["hold"], holding_for(base)
            sub = (f"Your P/L: {rupee((q['price'] - mine[1]) * mine[0])} ({(q['price'] / mine[1] - 1) * 100:+.2f}%)"
                   if rep["held"] and q and mine else "Owner view if you bought at today's price")
            verdict("If you own it", h, sub)

        if z:
            b, s, p = st.columns(3)
            with b.container(border=True, height="stretch"):
                st.markdown(":green[:material/shopping_cart: **When to buy**]")
                st.markdown(f"### {band(z['buy'])}")
                st.caption("Buy zone: recent support up to the 20-day average. "
                           + ("**Downtrend — wait for the trend to turn before buying here.**" if z["falling"]
                              else "**Price is inside it now.**" if z["in_buy"] else "Price is above it — wait for a pullback.")
                           + f" Or on a close above ₹{inr(t['resistance'][0])} with strong volume (breakout).")
            with s.container(border=True, height="stretch"):
                st.markdown(":red[:material/sell: **When to sell / stop-loss**]")
                st.markdown(f"### {band(z['stop'])}")
                st.caption("A daily close below this zone means support failed — consider selling. Set 0.5–1.5× ATR "
                           "under the buy zone; a zone, not an exact tick.")
            with p.container(border=True, height="stretch"):
                st.markdown(":blue[:material/savings: **When to book profit**]")
                st.markdown(f"### {band(z['profit'])}")
                st.caption("Near recent highs / resistance. "
                           + ("**Price is close to it now — consider partial profit.**" if z["near_profit"]
                              else "Consider booking part profit if price reaches here."))
        else:
            st.caption(f"Buy, stop-loss and profit zones need 30+ trading days of price history; "
                       f"{len(rep['history'])} available.")

    # ---- 2. reasons ----
    st.subheader("Why", divider="gray")
    pro_col, con_col = st.columns(2)
    with pro_col.container(border=True, height="stretch"):
        st.markdown(":green[**:material/thumb_up: Reasons to buy / hold**]")
        for x in rep["pros"] or ["None found in available data"]:
            st.markdown(f":green[:material/check_circle:] {x}")
    with con_col.container(border=True, height="stretch"):
        st.markdown(":red[**:material/thumb_down: Reasons for caution / selling**]")
        for x in rep["cons"] or ["None found in available data"]:
            st.markdown(f":red[:material/cancel:] {x}")
    if rep["held"]:
        st.markdown("**What would change the owner view:** " + " · ".join(rep["hold"]["invalidation"]))
    else:
        if rep["buy"]["better"]:
            st.markdown("**Better entry if:** " + " · ".join(rep["buy"]["better"]))
        st.markdown("**Worse if:** " + " · ".join(rep["buy"]["worse"]))
    scorecard(rep["parts"])
    st.caption(f"Combined score {rep['buy']['total']:+d} = market + technical + fundamental + news. "
               "Rules-based on public data — a suggestion, never a guarantee.")

    # ---- 3. details ----
    tab_chart, tab_news, tab_fund, tab_risk = st.tabs(
        [":material/show_chart: Chart & technicals", f":material/newspaper: News ({len(rep['news'])})",
         ":material/account_balance: Fundamentals", ":material/warning: Risks & data"])
    with tab_chart:
        if t:
            full = rep["history"]["Close"]
            chart = pd.DataFrame({"Close": full, "SMA 20": full.rolling(20).mean(), "SMA 50": full.rolling(50).mean(),
                                  "SMA 200": full.rolling(200).mean()}).iloc[-126:]
            st.line_chart(chart, height=340, y_label="₹")

            def sig(above):
                return "Bullish" if above else "Bearish"
            rows = [("Daily trend", t["trend"], ""), ("Weekly trend", t["weekly_trend"], ""),
                    ("RSI 14 daily", f"{t['rsi']:.0f}",
                     "Overbought" if t["rsi"] > 70 else "Oversold" if t["rsi"] < 30 else "Neutral"),
                    ("RSI 14 weekly", f"{t['weekly_rsi']:.0f}" if t["weekly_rsi"] else NA, ""),
                    ("MACD vs signal", f"{t['macd']:.1f} / {t['macd_signal']:.1f}", sig(t["macd"] > t["macd_signal"])),
                    ("ATR 14", f"₹{inr(t['atr'])}", "typical daily move"),
                    ("Volume vs 20-day avg", f"{t['vol_ratio']:.2f}×" if t["vol_ratio"] else NA,
                     "Spike" if t["vol_ratio"] and t["vol_ratio"] > 2 else ""),
                    ("Support 20d / 60d", f"₹{inr(t['support'][0])} / ₹{inr(t['support'][1])}", ""),
                    ("Resistance 20d / 60d", f"₹{inr(t['resistance'][0])} / ₹{inr(t['resistance'][1])}", "")]
            for n in (200, 50, 20):
                v = t[f"sma{n}"]
                rows.insert(2, (f"Price vs SMA {n}", f"₹{inr(v)}" if v else NA, sig(t["price"] > v) if v else ""))
            st.table(pd.DataFrame(rows, columns=["Indicator", "Value", "Signal"]).set_index("Indicator"))
        else:
            n = len(rep["history"])
            st.info(f"Only {n} trading day{'s' if n != 1 else ''} of price history (new listing?) — technicals need 30+."
                    if n else "Price history unavailable — technicals skipped.", icon=":material/info:")

    with tab_news:
        with st.container(border=True):
            st.markdown("**Digest**")
            for b in rep["digest"]:
                st.markdown(f"- {b}")
        sent_color = {"bullish": "green", "bearish": "red", "mixed": "orange", "noise": "gray"}
        for i in rep["news"][:25]:
            flags = " ".join(f":violet-badge[{x}]" for x in i["flags"])
            headline = i["headline"].replace("[", "(").replace("]", ")")
            st.markdown(f":{sent_color[i['sentiment']]}-badge[{i['sentiment']}] {flags} [{headline}]({i['link']})  \n"
                        f":gray[{i['source']} · {core.fmt_date(i['date'])}]")
        if rep["news_errors"]:
            st.caption("Feeds that failed this time: " + "; ".join(rep["news_errors"]))
        st.caption("Sentiment is keyword-based and rough. NSE/BSE filings and YouTube titles are not wired in v1.")

    with tab_fund:
        with st.container(horizontal=True):
            for label, val in [("Market cap", crore(inf.get("marketCap"))),
                               ("PE (TTM)", f"{inf['trailingPE']:.1f}" if inf.get("trailingPE") else NA),
                               ("PB", f"{inf['priceToBook']:.1f}" if inf.get("priceToBook") else NA),
                               ("ROE", pct(inf.get("returnOnEquity"), 100)),
                               ("Debt/equity", f"{inf['debtToEquity']:.0f}%" if inf.get("debtToEquity") is not None else NA),
                               ("Dividend yield", f"{inf['dividendYield']:.2f}%" if inf.get("dividendYield") is not None else NA),
                               ("Insider/promoter %*", pct(inf.get("heldPercentInsiders"), 100)),
                               ("Promoter pledge", NA)]:
                st.metric(label, val, border=True)
        g1, g2 = st.columns([2, 1])
        if f["revenue"]:
            trend = pd.DataFrame({"Revenue": {y: v / 1e7 for y, v in f["revenue"]},
                                  "Net profit": {y: v / 1e7 for y, v in f["profit"]}})
            trend.index = trend.index.astype(str)
            g1.bar_chart(trend, height=260, stack=False, y_label="₹ Cr")
        else:
            g1.info("Revenue/profit history unavailable.", icon=":material/info:")
        with g2.container(border=True):
            st.metric("Revenue growth (latest FY)", pct(f.get("revenue_growth")))
            st.metric("Net profit growth (latest FY)", pct(f.get("profit_growth")))
            st.metric("Net / operating margin",
                      f"{pct(inf.get('profitMargins'), 100)} / {pct(inf.get('operatingMargins'), 100)}")
        if f["flags"]:
            for x in f["flags"]:
                st.error(x, icon=":material/flag:")
        else:
            st.success("No fundamental red flags spotted in available data.", icon=":material/verified:")
        st.caption("*Yahoo 'insiders' holding is a rough proxy for promoter holding. Pledge is not in free feeds — "
                   "check the NSE shareholding pattern filing.")

    with tab_risk:
        st.markdown(f"**Market regime:** {rep['regime']}")
        if rep["sector_index"]:
            st.markdown(f"**Vs sector** ({rep['sector_index']}, 3 months): stock {pct(rep['stock_3m'])} · "
                        f"sector {pct(rep['sector_3m'])}")
        for r in rep["risks"]:
            st.markdown(f":orange[:material/warning:] {r}")
        st.caption(f"Report built {rep['fetched_at']:%d %b %Y, %H:%M:%S} IST (cached up to 10 min) · "
                   + (f"last daily price bar {q['as_of']:%d %b %Y}, delayed — not real-time · " if q else "")
                   + (f"newest headline {core.fmt_date(rep['news'][0]['date'])}" if rep["news"] else "no news found"))


def search():
    st.title("Research a stock")
    options = [f"{sym} — {name}" for sym, name in cached_stock_list()]
    if "search_pick" not in st.session_state:  # widget state is dropped on other pages; restore last stock
        st.session_state.search_pick = option_for(st.session_state.get("query", "TCS"))
    picked = st.selectbox("Search NSE stocks", options, index=None, key="search_pick",
                          placeholder="Start typing a name or symbol, e.g. reli, hdfc, infosys",
                          accept_new_options=True,
                          help="Matches all ~2,600 NSE listed equities. Pick one, or type any name and press Enter.")
    if picked:
        st.session_state.query = picked.split(" — ")[0].strip()
        report(st.session_state.query)


def watchlist():
    st.title("Watchlist")
    with st.form("add_watch", clear_on_submit=True, border=False):
        with st.container(horizontal=True, vertical_alignment="bottom"):
            sym = st.selectbox("Add a stock", [s for s, _ in cached_stock_list()], index=None,
                               placeholder="Type a symbol", accept_new_options=True)
            if st.form_submit_button("Add", icon=":material/add:") and sym:
                if base := to_symbol(sym):
                    with core.db() as con:
                        con.execute("INSERT OR IGNORE INTO watchlist VALUES(?,?,0)", (base, core.now_ist().date().isoformat()))
                    st.rerun()
                st.error(f"Couldn't find an NSE listing for '{sym}'.", icon=":material/search_off:")
    with core.db() as con:
        rows = con.execute("SELECT * FROM watchlist ORDER BY symbol").fetchall()
    if not rows:
        st.info("Your watchlist is empty.", icon=":material/bookmark:")
    cols = st.columns(3)
    for n, r in enumerate(rows):
        q, t, z = cached_quick(r["symbol"] + ".NS")
        with cols[n % 3].container(border=True):
            st.markdown(f"**{r['symbol']}**" + (" :gray[(sample)]" if r["sample"] else ""))
            st.metric("Price (delayed)", f"₹{inr(q['price'])}" if q else NA,
                      f"{q['change_pct']:+.2f}%" if q and q["change_pct"] is not None else None)
            if t:
                with st.container(horizontal=True):
                    st.badge(t["trend"], color=TREND_COLOR[t["trend"]])
                    st.badge(f"RSI {t['rsi']:.0f}", color="gray")
                    if z["in_buy"]:
                        st.badge("in buy zone", icon=":material/shopping_cart:", color="green")
                    if z["falling"]:
                        st.badge("falling — wait", icon=":material/trending_down:", color="red")
                    if z["near_profit"]:
                        st.badge("near resistance", icon=":material/savings:", color="blue")
                st.caption(f"Buy zone {band(z['buy'])} · stop {band(z['stop'])}")
            with st.container(horizontal=True):
                if st.button("Full report", key="a" + r["symbol"], icon=":material/analytics:"):
                    open_report(r["symbol"])
                if st.button("Remove", key="r" + r["symbol"], icon=":material/delete:", type="tertiary"):
                    with core.db() as con:
                        con.execute("DELETE FROM watchlist WHERE symbol=?", (r["symbol"],))
                    st.rerun()
    st.caption("Badges are a quick technical-only read. Open the full report for the buy/sell suggestion with news "
               "and fundamentals.")


ALERTS = {"below stop": ("red", "error", "closed below its stop zone — open the full report and review a SELL"),
          "near stop": ("orange", "warning", "is at or near its stop zone — watch the next daily close"),
          "near profit": ("blue", "savings", "is near its profit zone — consider booking part profit")}


def action_needed(table):
    """Holdings-page summary: which positions need a look today (technical-only, delayed prices)."""
    flagged = {t["Symbol"]: t["Alert"] for t in table if t["Alert"]}
    with st.container(border=True):
        st.markdown("**:material/notifications_active: Action needed**")
        for sym, alert in sorted(flagged.items(), key=lambda x: list(ALERTS).index(x[1])):
            color, icon, text = ALERTS[alert]
            st.markdown(f":{color}[:material/{icon}: **{sym}**] {text}")
        if not flagged:
            st.markdown(":green[:material/check_circle:] Nothing near a stop or profit zone right now.")


def holdings():
    st.title("My holdings")
    with st.expander("Add a holding", icon=":material/add_circle:"):
        with st.form("add_holding", clear_on_submit=True, border=False):
            c = st.columns(4)
            sym = c[0].selectbox("Stock", [s for s, _ in cached_stock_list()], index=None,
                                 placeholder="Type a symbol", accept_new_options=True)
            qty = c[1].number_input("Quantity", min_value=0.0, step=1.0)
            avg = c[2].number_input("Average buy price ₹", min_value=0.0, step=1.0)
            bdate = c[3].date_input("Buy date", value=core.now_ist().date(), max_value=core.now_ist().date())
            if st.form_submit_button("Save holding", icon=":material/save:"):
                base = to_symbol(sym) if sym else None
                if not base or qty <= 0 or avg <= 0:
                    st.error("A valid NSE stock, quantity > 0 and average price > 0 are required.")
                else:
                    with core.db() as con:
                        con.execute("INSERT INTO holdings(symbol,qty,avg_price,buy_date) VALUES(?,?,?,?)",
                                    (base, qty, avg, bdate.isoformat()))
                    st.rerun()
    with core.db() as con:
        rows = con.execute("SELECT * FROM holdings ORDER BY symbol").fetchall()
    if not rows:
        st.info("No holdings saved yet.", icon=":material/work_off:")
        return
    total_cost = total_value = 0.0
    table = []
    for r in rows:
        q, t, z = cached_quick(r["symbol"] + ".NS")
        cost = r["qty"] * r["avg_price"]
        value = r["qty"] * q["price"] if q else None
        total_cost += cost
        total_value += value if value is not None else cost
        table.append({"id": r["id"], "Symbol": r["symbol"] + (" (sample)" if r["sample"] else ""),
                      "Qty": r["qty"], "Avg ₹": r["avg_price"], "LTP ₹": q["price"] if q else None,
                      "Invested ₹": cost, "Value ₹": value,
                      "P/L ₹": value - cost if value is not None else None,
                      "P/L %": (value / cost - 1) * 100 if value is not None else None,
                      "Trend": t.get("trend", NA), "Sell below (stop zone)": band(z["stop"]) if z else NA,
                      "Alert": ("below stop" if q["price"] < z["stop"][0] else "near stop"
                                if q["price"] < z["stop"][1] * 1.02 else "near profit" if z["near_profit"] else "")
                      if q and z else "",
                      "Bought": r["buy_date"]})
    action_needed(table)
    k = st.columns(3)
    k[0].metric("Invested", f"₹{inr(total_cost)}", border=True)
    k[1].metric("Current value", f"₹{inr(total_value)}", border=True)
    k[2].metric("Unrealised P/L", rupee(total_value - total_cost),
                f"{(total_value / total_cost - 1) * 100:+.2f}%", border=True)
    if missing := [t["Symbol"] for t in table if t["LTP ₹"] is None]:
        st.caption(f"No delayed price for {', '.join(missing)} — counted at cost in the totals above.")

    def pl_color(v):
        return "" if v is None or pd.isna(v) else f"color: {'#34D399' if v >= 0 else '#F87171'}"
    df = pd.DataFrame(table)
    styled = (df.drop(columns="id").style.map(pl_color, subset=["P/L ₹", "P/L %"])
              .format({"Avg ₹": inr, "LTP ₹": inr, "Invested ₹": inr, "Value ₹": inr, "P/L ₹": inr,
                       "P/L %": lambda v: NA if pd.isna(v) else f"{v:+.2f}%", "Qty": "{:g}"}))
    st.dataframe(styled, hide_index=True)
    st.caption("Prices delayed. Trend and stop zone are technical-only; open a holding for the full HOLD/SELL view. "
               "Sample rows are seeded on first run — delete them below.")
    with st.container(horizontal=True, vertical_alignment="bottom"):
        pick = st.selectbox("Holding", [f"{t['id']}: {t['Symbol']}" for t in table])
        if st.button("HOLD / SELL suggestion", icon=":material/analytics:", type="primary"):
            open_report(pick.split(": ")[1].split(" ")[0])
        # keyed per holding so the confirm closes after a delete instead of offering the next row
        with st.popover("Delete", icon=":material/delete:", key="del_" + pick.split(":")[0]):
            if st.button(f"Yes, delete {pick.split(': ')[1]}", type="primary"):
                with core.db() as con:
                    con.execute("DELETE FROM holdings WHERE id=?", (int(pick.split(":")[0]),))
                st.rerun()


search_page = st.Page(search, title="Search & report", icon=":material/search:", default=True)
nav = st.navigation([search_page,
                     st.Page(watchlist, title="Watchlist", icon=":material/bookmarks:"),
                     st.Page(holdings, title="My holdings", icon=":material/work:")])
with st.sidebar:
    st.caption(DISCLAIMER)
market_strip()
nav.run()
st.warning(DISCLAIMER, icon=":material/gavel:")
