"""
state_store.py — durable session state the orchestrator owns, not the broker.

A live broker tells you balances and positions, but NOT the things this agent
must track itself across cycles and restarts:

  • peak_equity      — all-time high; the catastrophic-drawdown baseline.
  • day_start_equity — equity at the day's first cycle; the daily-loss baseline.
  • high_water[sym]  — highest price seen while a position is held; the TRAILING
                       stop needs this. Robinhood does not report it, so in live
                       a position's high-water would otherwise reset to last
                       price every fetch and the trailing stop would never fire.
  • trades_today, orders_per_symbol, last_order_time — the churn counters the
                       turnover limits gate on. The live AccountState is rebuilt
                       from the broker each call with these blank, so without
                       persistence the turnover limits are silently disabled in
                       live (and reset every restart).

The orchestrator folds each cycle's truth in via begin_cycle() and records every
fill via record_fill(); both persist atomically. A missing/corrupt file, or one
written by an older version, is upgraded to defaults rather than erroring.
"""

from __future__ import annotations

import copy
import json
import os
import tempfile
from datetime import datetime, timezone

_DEFAULTS: dict = {
    "peak_equity": None,
    "day_start_equity": None,
    "day": None,
    "high_water": {},        # symbol -> highest last price seen while held
    "trades_today": 0,
    "orders_per_symbol": {},  # symbol -> fills today
    "last_order_time": None,  # ISO-8601 UTC, or None
}


class StateStore:
    def __init__(self, path: str = "state.json"):
        self.path = path
        self.data: dict = self._load()

    def _load(self) -> dict:
        try:
            with open(self.path) as f:
                raw = json.load(f)
            if not isinstance(raw, dict):
                raise ValueError("state root is not an object")
            return {**copy.deepcopy(_DEFAULTS), **raw}  # old files upgrade cleanly
        except (FileNotFoundError, ValueError, json.JSONDecodeError):
            return copy.deepcopy(_DEFAULTS)

    def _write(self) -> None:
        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".state-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(self.data, f, indent=2)
            os.replace(tmp, self.path)  # atomic on POSIX
        except Exception:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise

    def begin_cycle(
        self, equity: float, day: str, position_last_prices: dict[str, float]
    ) -> dict:
        """Fold this cycle's equity + position marks into state; return a snapshot.

        Rolls the day over (resetting the daily baseline and churn counters) when
        ``day`` changes, lifts the all-time peak, and updates per-symbol high-water
        marks (pruning symbols no longer held). Counters are NOT touched here —
        only record_fill() advances them — so it is safe to call several times per
        cycle.
        """
        d = self.data
        if d.get("day") != day:                     # new session (or first ever)
            d["day"] = day
            d["day_start_equity"] = equity
            d["trades_today"] = 0
            d["orders_per_symbol"] = {}
            d["last_order_time"] = None
        if d.get("day_start_equity") is None:
            d["day_start_equity"] = equity

        pe = d.get("peak_equity")
        d["peak_equity"] = equity if pe is None else max(float(pe), equity)

        hw = {
            s: float(v)
            for s, v in d.get("high_water", {}).items()
            if s in position_last_prices            # drop closed positions
        }
        for sym, last in position_last_prices.items():
            hw[sym] = max(hw.get(sym, last), last)
        d["high_water"] = hw

        self._write()
        return self.snapshot()

    def record_fill(self, symbol: str, when: datetime) -> None:
        """Advance the churn counters after a fill, and persist."""
        d = self.data
        d["trades_today"] = int(d.get("trades_today", 0)) + 1
        ops = dict(d.get("orders_per_symbol", {}))
        ops[symbol] = ops.get(symbol, 0) + 1
        d["orders_per_symbol"] = ops
        d["last_order_time"] = when.astimezone(timezone.utc).isoformat()
        self._write()

    def snapshot(self) -> dict:
        """Current durable state, with last_order_time as an aware datetime."""
        d = self.data
        lot = d.get("last_order_time")
        return {
            "peak_equity": d["peak_equity"],
            "day_start_equity": d["day_start_equity"],
            "high_water": dict(d.get("high_water", {})),
            "trades_today": int(d.get("trades_today", 0)),
            "orders_per_symbol": dict(d.get("orders_per_symbol", {})),
            "last_order_time": datetime.fromisoformat(lot) if lot else None,
        }


# --------------------------------------------------------------------------- #
# Self-test
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import tempfile as _tf

    path = os.path.join(_tf.mkdtemp(), "state.json")

    s = StateStore(path)
    # Day 1: open at 1000, buy NVDA (rises to 130 then we'll trail it)
    snap = s.begin_cycle(1000.0, "2026-06-16", {"NVDA": 120.0})
    print("c1 peak/day_start:", snap["peak_equity"], snap["day_start_equity"])
    s.record_fill("NVDA", datetime.now(timezone.utc))
    # price climbs -> high-water lifts; equity climbs -> peak lifts
    s.begin_cycle(1300.0, "2026-06-16", {"NVDA": 150.0})
    snap = s.begin_cycle(1200.0, "2026-06-16", {"NVDA": 138.0})  # pulled back
    print("high-water NVDA (should be 150):", snap["high_water"]["NVDA"])
    print("peak (should be 1300):", snap["peak_equity"])
    print("trades_today (should be 1):", snap["trades_today"])

    # *** restart from disk: everything must survive ***
    s2 = StateStore(path)
    snap = s2.begin_cycle(1200.0, "2026-06-16", {"NVDA": 138.0})
    assert snap["peak_equity"] == 1300.0, "peak must survive restart"
    assert snap["day_start_equity"] == 1000.0, "day_start must survive restart"
    assert snap["high_water"]["NVDA"] == 150.0, "high-water must survive restart"
    assert snap["trades_today"] == 1, "churn count must survive restart"
    assert snap["last_order_time"] is not None and snap["last_order_time"].tzinfo
    print("RESTART OK: peak/day_start/high_water/trades/last_order_time all persisted")

    # close the position -> its high-water is pruned (re-entry starts fresh)
    snap = s2.begin_cycle(1200.0, "2026-06-16", {})
    assert "NVDA" not in snap["high_water"], "closed position's high-water must be pruned"
    print("PRUNE OK: closed position's high-water dropped")

    # new day -> daily baseline + churn counters reset, peak + high_water persist
    snap = s2.begin_cycle(1200.0, "2026-06-17", {"NVDA": 138.0})
    assert snap["day_start_equity"] == 1200.0 and snap["trades_today"] == 0
    assert snap["peak_equity"] == 1300.0
    print("ROLLOVER OK: day_start + counters reset, peak persists")

    # corrupt file -> fresh defaults, no crash
    with open(path, "w") as f:
        f.write("{ not json")
    s3 = StateStore(path)
    snap = s3.begin_cycle(900.0, "2026-06-18", {})
    assert snap["peak_equity"] == 900.0
    print("CORRUPT-RECOVERY OK")
    print("ALL ASSERTIONS PASSED")
