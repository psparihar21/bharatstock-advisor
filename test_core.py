"""Offline self-check: python test_core.py (no network needed). Uses a throwaway DB, never bharatstock.db."""
import os
import sqlite3
import tempfile
from datetime import datetime, timedelta

import numpy as np
import pandas as pd

import core

core.DB_PATH = os.path.join(tempfile.mkdtemp(), "test.db")

assert core.inr(12345678.9) == "1,23,45,678.90"
assert core.inr(999) == "999.00"
assert core.inr(-1234.5) == "-1,234.50"
assert core.inr(None) == "unavailable"
assert core.rupee(-3798) == "-₹3,798.00" and core.rupee(1234567) == "₹12,34,567.00"

ist = core.IST
assert core.market_session(datetime(2026, 10, 28, 9, 5, tzinfo=ist)) == "pre-open"
assert core.market_session(datetime(2026, 10, 28, 11, 0, tzinfo=ist)) == "open"
assert core.market_session(datetime(2026, 10, 28, 16, 0, tzinfo=ist)) == "closed"
assert core.market_session(datetime(2026, 10, 31, 11, 0, tzinfo=ist)).startswith("closed")

assert core.sentiment("TCS beats estimates, shares surge") == "bullish"
assert core.sentiment("Infosys falls after downgrade") == "bearish"
assert "results" in core.flags_for("TCS Q2 results today")

# Synthetic steady uptrend -> uptrend label, CONSIDER BUY or WAIT FOR DIP, never AVOID
idx = pd.date_range("2025-01-01", periods=260, freq="B", tz=ist)
close = pd.Series(np.linspace(100, 200, 260) + np.sin(np.arange(260)) * 2, index=idx)
df = pd.DataFrame({"Open": close, "High": close + 2, "Low": close - 2, "Close": close, "Volume": 1e6}, index=idx)
t = core.technicals(df)
assert t["trend"] == "uptrend", t["trend"]
inf = {"returnOnEquity": 0.3, "debtToEquity": 10, "trailingPE": 25}
fund = {"profit_growth": 12, "flags": []}
parts = core.score(t, inf, fund, [], 1)
b = core.buy_suggestion(parts, t, inf, [])
assert b["action"] in ("CONSIDER BUY", "WAIT FOR DIP"), b
assert 1 <= b["confidence"] <= 4
h = core.hold_suggestion(parts, t, inf, {"qty": 10, "avg_price": 100}, [])
assert abs(h["pnl"] - (t["price"] - 100) * 10) < 1e-6
lo, hi = h["stop"]
assert lo < hi < t["price"]

# Downtrend + weak fundamentals -> AVOID / EXIT
down = df.copy()
down["Close"] = close.values[::-1]
down["High"], down["Low"] = down["Close"] + 2, down["Close"] - 2
td = core.technicals(down)
bad = {"returnOnEquity": 0.04, "debtToEquity": 200, "trailingPE": 80}
pd_ = core.score(td, bad, {"profit_growth": -20, "flags": []}, [], -1)
assert core.buy_suggestion(pd_, td, bad, [])["action"] == "AVOID"
assert core.hold_suggestion(pd_, td, bad, {"qty": 1, "avg_price": 200}, [])["action"] == "EXIT"

z = core.zones(t)
assert z["stop"][1] < z["buy"][0] < z["buy"][1] <= t["price"] < z["profit"][0], z
pro, con = core.pros_cons(t, inf, fund, [], "risk-on", 10.0, 5.0)
assert pro and "Broad market is risk-on" in pro
pro, con = core.pros_cons(td, bad, {"profit_growth": -20, "revenue_growth": -5}, [], "risk-off", -10.0, 0.0)
assert len(con) > len(pro)

# Penny stock (~Rs 1): zones must not collapse when rounded; stop strictly below buy
pidx = pd.date_range("2025-01-01", periods=120, freq="B", tz=ist)
pc = pd.Series(1.0 + np.sin(np.arange(120)) * 0.02, index=pidx)
pdf = pd.DataFrame({"Open": pc, "High": pc + 0.02, "Low": pc - 0.04, "Close": pc, "Volume": 1e6}, index=pidx)
pz = core.zones(core.technicals(pdf))
assert 0 < pz["stop"][0] < pz["stop"][1] < pz["buy"][0] <= pz["buy"][1], pz

# db() closes its connection (the file can be removed/reopened) and rolls back on error
with core.db() as con:
    con.execute("INSERT INTO watchlist VALUES('ZZTEST','2026-10-28',0)")
try:
    with core.db() as con:
        con.execute("DELETE FROM watchlist WHERE symbol='ZZTEST'")
        raise RuntimeError
except RuntimeError:
    pass
with core.db() as con:
    assert con.execute("SELECT 1 FROM watchlist WHERE symbol='ZZTEST'").fetchone()
    held = con
try:
    held.execute("SELECT 1")
    raise AssertionError("connection left open")
except sqlite3.ProgrammingError:
    pass

# Cached news fallback ignores headlines older than the window
old, new = core.now_ist() - timedelta(days=90), core.now_ist() - timedelta(days=2)
core.cache_news("ZZTEST.NS", [{"date": d, "headline": h, "source": "t", "link": h, "sentiment": "noise", "flags": []}
                              for d, h in ((old, "old"), (new, "new"))])
assert [i["headline"] for i in core.cached_news("ZZTEST.NS")] == ["new"]

# Verdict memory: first sight has no previous; a change returns the old verdict and keeps it until the next change
assert core.verdict_change("ZZTEST", "WATCH", {"technical": 0}) is None
assert core.verdict_change("ZZTEST", "WATCH", {"technical": 1}) is None
prev = core.verdict_change("ZZTEST", "CONSIDER BUY", {"technical": 3})
assert prev["action"] == "WATCH" and prev["scores"] == {"technical": 1}, prev
assert core.verdict_change("ZZTEST", "CONSIDER BUY", {"technical": 3})["action"] == "WATCH"

print("all checks passed")
