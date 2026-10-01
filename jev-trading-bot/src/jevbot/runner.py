"""Orchestration: data -> rules -> Jev gate -> risk -> (approval) -> broker -> alerts -> db.

Every order, approved or not, passes through Risk.guard. Exits are handled by the bracket orders
that live at the broker, so nothing here depends on Jev to get out of a position.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable

import pandas as pd

from . import jev as jev_mod
from .alerts import Alerts
from .broker import BrokerError
from .config import Config
from .data import DataError, is_stale
from .db import Db
from .indicators import rsi, sma
from .risk import Account, Approvals, Order, Risk
from .rules import CANDIDATES, SYMBOLS
from .signal import gate
from .strategy import Strategy


@dataclass
class Deps:
    db: Db
    cfg: Config
    risk: Risk
    approvals: Approvals
    alerts: Alerts
    broker: Any
    strategy: Strategy
    data_fn: Callable[[list[str]], pd.DataFrame]
    ask: Callable[..., jev_mod.JevResult] = jev_mod.ask
    now: Callable[[], pd.Timestamp] = pd.Timestamp.now
    headlines_fn: Callable[[str], list[str]] = field(default=lambda sym: [])


def _account(d: Deps) -> Account:
    a = d.broker.account()
    exposure = d.broker.exposure()
    d.db.log_equity(a["equity"], d.now().timestamp())
    peak = max([a["equity"], *[e for _, e in d.db.equity_series()]])
    return Account(equity=a["equity"], exposure=exposure, start_of_day_equity=a["last_equity"], peak_equity=peak)


def _state(sym: str, closes: pd.DataFrame, d: Deps) -> dict[str, Any]:
    s = closes[sym].dropna()
    tail = s.tail(30)
    return {
        "symbol": sym, "as_of": str(s.index[-1].date()),
        "bars": [{"date": str(i.date()), "close": round(float(v), 4)} for i, v in tail.items()],
        "indicators": {"sma50": round(float(sma(s, 50).iloc[-1]), 4), "sma200": round(float(sma(s, 200).iloc[-1]), 4),
                       "rsi2": round(float(rsi(s, 2).iloc[-1]), 2)},
        "headlines": d.headlines_fn(sym),  # data only; never interpolated into question text
    }


def _place(d: Deps, o: Order, stop: float, tp: float, probs: dict[str, float]) -> str:
    try:
        resp = d.broker.bracket_order(o.symbol, o.qty, stop_price=stop, take_profit_price=tp)
    except BrokerError as e:
        d.risk.record_broker_error()
        d.alerts.send(f"ERROR placing {o.symbol} order: {e}")
        return f"broker error: {e}"
    d.risk.record_broker_ok()
    d.db.log_trade({"symbol": o.symbol, "qty": o.qty, "price": o.price, "stop": stop, "take_profit": tp,
                    "order_id": resp.get("id"), "status": resp.get("status"), "probabilities": probs, "pnl": None})
    d.alerts.send(f"ORDER SUBMITTED (paper): BUY {int(o.qty)} {o.symbol} ~${o.price:.2f}, stop {stop:.2f}, target {tp:.2f}")
    return "ordered"


def tick(d: Deps) -> dict[str, Any]:
    out = {"status": "ok", "orders": 0, "pending": 0}
    if d.risk.is_killed():
        return {**out, "status": "killed"}
    st = d.strategy
    data_syms = sorted(set(SYMBOLS.get(st.name, [])) | set(st.symbols))
    try:
        closes = d.data_fn(data_syms)
        stale = is_stale(closes, d.now())
    except DataError:
        stale = True
    if stale:
        d.risk.record_stale_data()
        d.alerts.send("WARNING: market data empty or stale; no trading this tick.")
        return {**out, "status": "stale"}
    d.risk.record_fresh_data()
    try:
        acct = _account(d)
        held = {p["symbol"] for p in d.broker.positions()}
    except BrokerError as e:
        d.risk.record_broker_error()
        d.alerts.send(f"ERROR reading account: {e}")
        return {**out, "status": "broker_error"}
    d.risk.record_broker_ok()

    today = d.now().date().isoformat()
    decided = {(e["date"], e["symbol"]) for e in d.db.events("decided")}
    waiting = {a["order"]["symbol"] for a in d.db.pending_approvals()}
    last = CANDIDATES[st.name](closes).iloc[-1]
    for sym in st.symbols:
        if last.get(sym, 0) <= 0 or sym in held or sym in waiting or (today, sym) in decided:
            continue
        res = gate(1, _state(sym, closes, d), st, ask=d.ask)
        d.db.log_jev({"symbol": sym, "model": res.model, "latency_s": res.latency_s, "cost_usd": res.cost_usd,
                      "ok": not res.jev_error, "probabilities": res.probabilities})
        row = {"symbol": sym, "direction": 1, "fired": res.fire, "probabilities": res.probabilities, "model": res.model}
        if res.jev_error:
            d.risk.record_jev_failure()
            d.db.log_signal({**row, "result": res.reason})
            d.alerts.send(f"WARNING: {res.reason}")
            continue
        d.risk.record_jev_ok()
        d.db.log_event("decided", {"date": today, "symbol": sym})
        if not res.fire:
            d.db.log_signal({**row, "result": res.reason})
            continue
        price = float(closes[sym].dropna().iloc[-1])
        pct = min(d.cfg.target_position_pct, st.position_cap(d.cfg))
        qty = math.floor(pct * acct.equity / price)
        if qty < 1:
            d.db.log_signal({**row, "result": "position too small for one share"})
            continue
        order = Order(sym, qty, price, st.leveraged)
        stop, tp = price * (1 - st.stop_loss_pct), price * (1 + st.take_profit_pct)
        dec = d.risk.check(order, acct, today=d.now().date())
        if dec.action == "block":
            d.db.log_signal({**row, "result": f"blocked: {dec.reason}"})
            d.alerts.send(f"BLOCKED {sym}: {dec.reason}")
        elif dec.action == "needs_approval":
            aid = d.approvals.request(order, {"stop": stop, "take_profit": tp, "probabilities": res.probabilities})
            d.db.log_signal({**row, "result": f"awaiting approval #{aid}"})
            d.alerts.send(
                f"APPROVAL NEEDED #{aid}: BUY {qty} {sym} ~${order.notional:,.0f} (stop {stop:.2f}, target {tp:.2f}). "
                f"Jev: {res.probabilities}. Reply /approve {aid} or /reject {aid} within {d.cfg.approval_timeout_s // 60} min.")
            out["pending"] += 1
        else:
            result = _place(d, order, stop, tp, res.probabilities)
            d.db.log_signal({**row, "result": result})
            out["orders"] += result == "ordered"
    return out


def process_approvals(d: Deps) -> dict[str, int]:
    """Execute approved orders (after re-checking risk and the kill switch); expire stale ones."""
    res = {"executed": 0, "expired": 0}
    for a in d.db.pending_approvals():
        if d.approvals.status(a["id"]) == "expired":
            res["expired"] += 1
            d.alerts.send(f"Approval #{a['id']} expired; treated as REJECTED.")
    for a in d.db.approvals_with_status("approved"):
        o = Order(**a["order"])
        try:
            acct = _account(d)
        except BrokerError as e:
            d.risk.record_broker_error()
            d.alerts.send(f"ERROR executing approval #{a['id']}: {e}")
            continue
        dec = d.risk.guard(o, acct, approved=True, today=d.now().date())
        if dec.action != "allow":
            d.db.mark_approval(a["id"], "blocked")
            d.alerts.send(f"Approved order #{a['id']} BLOCKED by risk: {dec.reason}")
            continue
        ctx = a["context"]
        result = _place(d, o, ctx["stop"], ctx["take_profit"], ctx.get("probabilities", {}))
        d.db.mark_approval(a["id"], "executed" if result == "ordered" else "failed")
        res["executed"] += result == "ordered"
    return res
