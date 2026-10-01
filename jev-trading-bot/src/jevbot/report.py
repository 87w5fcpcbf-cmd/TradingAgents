"""Daily plain-text report: trades, P&L, win rate, largest loss, Jev latency and cost per decision."""
from __future__ import annotations

import datetime as dt
from collections import Counter

from .db import Db
from .risk import KILL


def _start_of_day(d: dt.date) -> float:
    return dt.datetime.combine(d, dt.time.min).timestamp()


def daily(db: Db, day: dt.date | None = None) -> str:
    day = day or dt.date.today()
    t0, t1 = _start_of_day(day), _start_of_day(day) + 86400
    trades_today = [t for t in db.trades() if t0 <= t["ts"] < t1]
    closed = [t["pnl"] for t in db.trades() if t.get("pnl") is not None]
    wins = [p for p in closed if p > 0]
    eq = [e for ts, e in db.equity_series() if ts < t1]
    jev = [j for j in db.jev_calls() if t0 <= j["ts"] < t1]
    lat = [j["latency_s"] for j in jev if j.get("latency_s") is not None]
    costs = [j["cost_usd"] for j in jev if j.get("cost_usd") is not None]
    models = Counter(j["model"] for j in jev if j.get("model"))
    lines = [f"Daily report {day.isoformat()} (paper trading, not financial advice)", ""]
    lines.append(f"Trades placed today: {len(trades_today)}")
    if closed:
        lines.append(f"Closed trades: {len(closed)} | win rate {len(wins) / len(closed):.1%} | "
                     f"realized P&L ${sum(closed):.2f} | largest loss ${min(closed + [0]):.2f}")
    else:
        lines.append("Closed trades: 0 | win rate n/a | realized P&L n/a | largest loss n/a")
    if len(eq) >= 2:
        lines.append(f"Equity: ${eq[0]:,.2f} -> ${eq[-1]:,.2f} ({eq[-1] - eq[0]:+,.2f})")
    else:
        lines.append("Equity: n/a")
    lines.append(
        f"Jev: {len(jev)} calls | avg latency {sum(lat) / len(lat):.2f}s | " if lat else f"Jev: {len(jev)} calls | avg latency n/a | "
    )
    lines[-1] += f"avg cost per decision ${sum(costs) / len(costs):.4f}" if costs else "avg cost per decision n/a"
    if models:
        lines.append("Jev models: " + ", ".join(f"{m} x{n}" for m, n in models.items()))
    killed = db.get_flag(KILL)
    lines.append("Kill switch: " + (f"TRIPPED ({db.flag_reason(KILL)})" if killed else "ok"))
    return "\n".join(lines)
