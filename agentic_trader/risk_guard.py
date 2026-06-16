"""
risk_guard.py — deterministic, LLM-proof risk enforcement.

Every proposed order passes through check_order() before it can reach the
broker. The LLM produces *intentions*; this module decides what is allowed.
Nothing here trusts the model. Limits come from config.yaml.

Two jobs:
  1. check_order()        -> gate every new entry/add against all limits.
  2. stop_loss_exits()    -> generate exit orders for the calculated core
                             (cut losers at hard stop, trail winners).
"""

from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Optional
import yaml


# --------------------------------------------------------------------------- #
# Data structures
# --------------------------------------------------------------------------- #
@dataclass
class Order:
    symbol: str
    side: str                # "buy" | "sell"
    notional_usd: float      # dollar size of the order
    limit_price: float       # intended price (used for sanity + sizing)
    reason: str = ""         # the agent's rationale (for the log)
    conviction: float = 0.0  # 0..1 from the portfolio manager


@dataclass
class Position:
    symbol: str
    qty: float
    avg_price: float
    last_price: float
    high_water_price: float  # highest price seen since entry (for trailing stop)

    @property
    def market_value(self) -> float:
        return self.qty * self.last_price

    @property
    def unrealized_pct(self) -> float:
        if self.avg_price == 0:
            return 0.0
        return (self.last_price - self.avg_price) / self.avg_price


@dataclass
class AccountState:
    equity: float                       # total agentic-account value now
    cash: float                         # buying power
    peak_equity: float                  # highest equity ever (for drawdown)
    day_start_equity: float             # equity at session open (for daily loss)
    positions: dict[str, Position] = field(default_factory=dict)
    trades_today: int = 0
    orders_per_symbol_today: dict[str, int] = field(default_factory=dict)
    last_order_time: Optional[datetime] = None
    consecutive_losing_days: int = 0
    halted_reason: Optional[str] = None  # sticky catastrophic halt


@dataclass
class Decision:
    allowed: bool
    reason: str
    order: Optional[Order] = None        # possibly size-adjusted


# --------------------------------------------------------------------------- #
# Guard
# --------------------------------------------------------------------------- #
class RiskGuard:
    def __init__(self, config_path: str = "config.yaml"):
        with open(config_path) as f:
            self.cfg = yaml.safe_load(f)
        self.r = self.cfg["risk"]
        self.sched = self.cfg["schedule"]
        self.uni = self.cfg["universe"]
        self.tz = ZoneInfo(self.sched["timezone"])

    # ---- top-level halts (checked before anything else) ----------------- #
    def catastrophic_halt(self, acct: AccountState) -> Optional[str]:
        if acct.halted_reason:
            return acct.halted_reason
        # Drawdown from all-time peak — the bug / black-swan backstop.
        if acct.peak_equity > 0:
            dd = (acct.peak_equity - acct.equity) / acct.peak_equity
            if dd >= self.r["max_drawdown_halt_pct"]:
                return f"CATASTROPHIC: drawdown {dd:.1%} >= {self.r['max_drawdown_halt_pct']:.0%} from peak"
        return None

    def daily_paused(self, acct: AccountState) -> Optional[str]:
        if acct.day_start_equity > 0:
            day_pl = (acct.equity - acct.day_start_equity) / acct.day_start_equity
            if day_pl <= -self.r["daily_loss_limit_pct"]:
                return f"daily loss {day_pl:.1%} <= -{self.r['daily_loss_limit_pct']:.0%}; paused until next session"
        return None

    def market_open_now(self, now: Optional[datetime] = None) -> bool:
        now = (now or datetime.now(self.tz)).astimezone(self.tz)
        if now.weekday() >= 5:            # Sat/Sun
            return False
        o_h, o_m = map(int, self.sched["market_open"].split(":"))
        c_h, c_m = map(int, self.sched["market_close"].split(":"))
        open_t = now.replace(hour=o_h, minute=o_m, second=0, microsecond=0) \
            + timedelta(minutes=self.sched["skip_first_minutes"])
        close_t = now.replace(hour=c_h, minute=c_m, second=0, microsecond=0) \
            - timedelta(minutes=self.sched["skip_last_minutes"])
        return open_t <= now <= close_t

    # ---- the main gate --------------------------------------------------- #
    def check_order(self, order: Order, acct: AccountState,
                    last_close: float, data_age_seconds: float,
                    now: Optional[datetime] = None) -> Decision:
        now = now or datetime.now(self.tz)

        # 0. sticky catastrophic halt
        h = self.catastrophic_halt(acct)
        if h:
            return Decision(False, h)

        # 1. daily loss pause
        p = self.daily_paused(acct)
        if p:
            return Decision(False, p)

        # 2. market hours
        if not self.market_open_now(now):
            return Decision(False, "market closed / outside trading window")

        # 3. universe allow/deny (deny always wins)
        sym = order.symbol.upper()
        if sym in [s.upper() for s in self.uni["blocked_symbols"]]:
            return Decision(False, f"{sym} is on the blocked list")
        allowed = [s.upper() for s in self.uni["allowed_symbols"]]
        if allowed and sym not in allowed:
            return Decision(False, f"{sym} not in allowed_symbols whitelist")

        # 4. data integrity
        if data_age_seconds > self.r["require_fresh_data_seconds"]:
            return Decision(False, f"stale data ({data_age_seconds:.0f}s old)")
        if last_close > 0:
            dev = abs(order.limit_price - last_close) / last_close
            if dev > self.r["max_price_deviation_pct"]:
                return Decision(False, f"price {order.limit_price} is {dev:.1%} off last close {last_close} (bad/stale quote)")

        # 5. order sanity
        if order.notional_usd < self.r["min_order_notional_usd"]:
            return Decision(False, "below minimum order notional")
        if order.limit_price <= 0 or order.notional_usd <= 0:
            return Decision(False, "non-positive price/notional")

        # 6. churn controls
        if acct.trades_today >= self.r["max_trades_per_day"]:
            return Decision(False, f"max_trades_per_day ({self.r['max_trades_per_day']}) reached")
        if acct.orders_per_symbol_today.get(sym, 0) >= self.r["max_orders_per_symbol_per_day"]:
            return Decision(False, f"max orders for {sym} today reached")
        if acct.last_order_time:
            gap = (now - acct.last_order_time).total_seconds() / 60
            if gap < self.r["min_minutes_between_orders"]:
                return Decision(False, f"rate-limited ({gap:.1f}m < {self.r['min_minutes_between_orders']}m)")

        # 7. sizing — only enforced on BUYs / adds
        if order.side == "buy":
            if len(acct.positions) >= self.r["max_open_positions"] and sym not in acct.positions:
                return Decision(False, f"max_open_positions ({self.r['max_open_positions']}) reached")

            max_pos_val = acct.equity * self.r["max_position_pct_of_account"]
            existing_val = acct.positions[sym].market_value if sym in acct.positions else 0.0
            order_notional = order.notional_usd

            # clamp to the per-order ceiling
            max_order = acct.equity * self.r["max_single_order_notional_pct"]
            if order_notional > max_order:
                order_notional = max_order

            # clamp so total position stays under the concentration cap
            if existing_val + order_notional > max_pos_val:
                order_notional = max(0.0, max_pos_val - existing_val)

            if order_notional < self.r["min_order_notional_usd"]:
                return Decision(False, f"{sym} already at/near {self.r['max_position_pct_of_account']:.0%} cap")

            if order_notional > acct.cash:
                order_notional = acct.cash
                if order_notional < self.r["min_order_notional_usd"]:
                    return Decision(False, "insufficient buying power")

            adjusted = Order(sym, "buy", round(order_notional, 2), order.limit_price,
                             order.reason, order.conviction)
            note = "" if adjusted.notional_usd == order.notional_usd else \
                f" (size clamped {order.notional_usd:.0f}->{adjusted.notional_usd:.0f})"
            return Decision(True, "approved" + note, adjusted)

        # sells (manual de-risking) pass the gate; stop logic handles exits
        return Decision(True, "approved (sell)", order)

    # ---- the calculated core: protective exits -------------------------- #
    def stop_loss_exits(self, acct: AccountState) -> list[Order]:
        """Cut losers at the hard stop; trail winners off their high-water mark."""
        exits: list[Order] = []
        hard = self.r["hard_stop_loss_pct"]
        trail = self.r["trailing_stop_pct"]
        for sym, pos in acct.positions.items():
            # hard stop
            if pos.unrealized_pct <= -hard:
                exits.append(Order(sym, "sell", pos.market_value, pos.last_price,
                                   f"HARD STOP {pos.unrealized_pct:.1%}", 1.0))
                continue
            # trailing stop (only once in profit territory)
            if pos.high_water_price > pos.avg_price:
                drop = (pos.high_water_price - pos.last_price) / pos.high_water_price
                if drop >= trail:
                    exits.append(Order(sym, "sell", pos.market_value, pos.last_price,
                                       f"TRAIL STOP -{drop:.1%} off high", 1.0))
        return exits


# --------------------------------------------------------------------------- #
# Self-test
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    from datetime import datetime
    g = RiskGuard("config.yaml")
    tz = g.tz
    # a Wednesday at 11:00 ET -> market open
    now = datetime(2026, 6, 17, 11, 0, tzinfo=tz)

    acct = AccountState(
        equity=1000, cash=1000, peak_equity=1000, day_start_equity=1000,
        positions={}, last_order_time=None,
    )

    print("== check_order tests ==")
    # normal buy, under caps
    d = g.check_order(Order("NVDA", "buy", 200, 130.0, "momentum", 0.8),
                      acct, last_close=129.0, data_age_seconds=60, now=now)
    print("normal buy:", d.allowed, "|", d.reason, "| size", d.order.notional_usd if d.order else None)

    # oversized buy -> clamped to 30% = 300
    d = g.check_order(Order("NVDA", "buy", 900, 130.0, "high conviction", 0.95),
                      acct, last_close=129.0, data_age_seconds=60, now=now)
    print("oversized buy:", d.allowed, "|", d.reason, "| size", d.order.notional_usd if d.order else None)

    # blocked symbol
    g.uni["blocked_symbols"] = ["GME"]
    d = g.check_order(Order("GME", "buy", 100, 25.0, "meme", 0.9),
                      acct, last_close=25.0, data_age_seconds=60, now=now)
    print("blocked symbol:", d.allowed, "|", d.reason)
    g.uni["blocked_symbols"] = []

    # stale data
    d = g.check_order(Order("AAPL", "buy", 100, 200.0, "", 0.7),
                      acct, last_close=200.0, data_age_seconds=9999, now=now)
    print("stale data:", d.allowed, "|", d.reason)

    # bad quote (20% off last close, limit 15%)
    d = g.check_order(Order("AAPL", "buy", 100, 240.0, "", 0.7),
                      acct, last_close=200.0, data_age_seconds=60, now=now)
    print("bad quote:", d.allowed, "|", d.reason)

    # catastrophic drawdown halt
    crashed = AccountState(equity=650, cash=650, peak_equity=1000, day_start_equity=1000)
    d = g.check_order(Order("AAPL", "buy", 50, 200.0, "", 0.7),
                      crashed, last_close=200.0, data_age_seconds=60, now=now)
    print("drawdown halt:", d.allowed, "|", d.reason)

    # daily loss pause (-10%)
    redday = AccountState(equity=890, cash=890, peak_equity=1000, day_start_equity=1000)
    d = g.check_order(Order("AAPL", "buy", 50, 200.0, "", 0.7),
                      redday, last_close=200.0, data_age_seconds=60, now=now)
    print("daily pause:", d.allowed, "|", d.reason)

    print("\n== stop_loss_exits tests ==")
    acct2 = AccountState(
        equity=1000, cash=200, peak_equity=1000, day_start_equity=1000,
        positions={
            "LOSER": Position("LOSER", 10, 100.0, 90.0, 100.0),   # -10% -> hard stop
            "WINR":  Position("WINR", 5, 100.0, 130.0, 150.0),    # +30%, 13% off high -> trail
            "HOLD":  Position("HOLD", 5, 100.0, 104.0, 105.0),    # fine, no exit
        },
    )
    for ex in g.stop_loss_exits(acct2):
        print(f"EXIT {ex.symbol}: {ex.reason}")
