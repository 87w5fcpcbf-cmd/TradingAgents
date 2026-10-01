"""Hard limits Jev cannot override: position/exposure caps, daily loss limit, kill switch, approvals.

The kill switch is persisted in SQLite so it survives restarts, and stays tripped until reset() is
called by a person. `guard()` is the single choke point every order must pass, approved or not.
"""
from __future__ import annotations

import datetime as dt
import time
from dataclasses import asdict, dataclass
from typing import Callable

from .config import Config
from .db import Db

KILL = "kill_switch"
JEV_FAILURE_KILL = 5
STALE_KILL = 3


@dataclass(frozen=True)
class Order:
    symbol: str
    qty: float
    price: float
    leveraged: bool = False
    side: str = "buy"

    @property
    def notional(self) -> float:
        return self.qty * self.price


@dataclass(frozen=True)
class Account:
    equity: float
    exposure: float
    start_of_day_equity: float
    peak_equity: float


@dataclass(frozen=True)
class Decision:
    action: str  # "allow" | "needs_approval" | "block"
    reason: str = ""


def _prev_business_day(d: dt.date) -> dt.date:
    d -= dt.timedelta(days=1)
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d


class Risk:
    def __init__(self, db: Db, cfg: Config):
        self.db, self.cfg = db, cfg

    # kill switch -----------------------------------------------------------
    def is_killed(self) -> bool:
        return self.db.get_flag(KILL)

    def kill_reason(self) -> str | None:
        return self.db.flag_reason(KILL)

    def trip(self, reason: str) -> None:
        self.db.set_flag(KILL, True, reason)
        self.db.log_event("kill_switch", {"reason": reason})

    def reset(self) -> None:
        self.db.set_flag(KILL, False, None)
        for c in ("broker", "stale", "jev"):
            self.db.set_int(f"cnt:{c}", 0)
        self.db.log_event("kill_reset", {})

    # failure counters (persisted) -----------------------------------------
    def _bump(self, name: str, limit: int, reason: str) -> None:
        n = self.db.get_int(f"cnt:{name}") + 1
        self.db.set_int(f"cnt:{name}", n)
        if n >= limit:
            self.trip(f"{reason} ({n} in a row)")

    def record_broker_error(self): self._bump("broker", self.cfg.broker_error_kill, "broker errors")
    def record_broker_ok(self): self.db.set_int("cnt:broker", 0)
    def record_stale_data(self): self._bump("stale", STALE_KILL, "stale market data")
    def record_fresh_data(self): self.db.set_int("cnt:stale", 0)
    def record_jev_failure(self): self._bump("jev", JEV_FAILURE_KILL, "Jev failures")
    def record_jev_ok(self): self.db.set_int("cnt:jev", 0)

    # checks ----------------------------------------------------------------
    def _daily_limit_hit(self, today: dt.date) -> None:
        iso = today.isoformat()
        seen = {e["date"] for e in self.db.events("daily_limit")}
        if iso not in seen:
            self.db.log_event("daily_limit", {"date": iso})
        if _prev_business_day(today).isoformat() in seen:
            self.trip("daily loss limit hit two sessions in a row")

    def check(self, o: Order, a: Account, today: dt.date | None = None) -> Decision:
        c = self.cfg
        if self.is_killed():
            return Decision("block", f"kill switch tripped: {self.kill_reason()}")
        if a.equity <= 0:
            return Decision("block", "no equity")
        if a.peak_equity > 0 and (a.peak_equity - a.equity) / a.peak_equity >= c.drawdown_kill_pct:
            self.trip(f"drawdown >= {c.drawdown_kill_pct:.0%} from peak")
            return Decision("block", "drawdown kill switch tripped")
        if a.start_of_day_equity > 0 and (a.start_of_day_equity - a.equity) / a.start_of_day_equity >= c.daily_loss_pct:
            self._daily_limit_hit(today or dt.date.today())
            if self.is_killed():
                return Decision("block", f"kill switch tripped: {self.kill_reason()}")
            return Decision("block", f"daily loss limit {c.daily_loss_pct:.0%} reached")
        cap = c.max_position_pct_leveraged if o.leveraged else c.max_position_pct
        if o.notional > cap * a.equity:
            return Decision("block", f"position {o.notional:.0f} exceeds {cap:.0%} cap")
        if a.exposure + o.notional > c.max_exposure_pct * a.equity:
            return Decision("block", f"total exposure would exceed {c.max_exposure_pct:.0%}")
        if o.notional > c.approval_usd:
            return Decision("needs_approval", f"notional {o.notional:.0f} above approval size {c.approval_usd:.0f}")
        return Decision("allow")

    def guard(self, o: Order, a: Account, approved: bool = False, today: dt.date | None = None) -> Decision:
        """Final gate before any order. Kill switch and hard caps apply even to approved orders."""
        d = self.check(o, a, today)
        if d.action == "needs_approval" and approved:
            return Decision("allow", "approved by user")
        return d


class Approvals:
    def __init__(self, db: Db, cfg: Config, now: Callable[[], float] = time.time):
        self.db, self.cfg, self.now = db, cfg, now

    def request(self, o: Order, context: dict) -> int:
        return self.db.add_approval({"order": asdict(o), "context": context}, self.now() + self.cfg.approval_timeout_s)

    def status(self, aid: int) -> str:
        row = self.db.get_approval(aid)
        if not row:
            return "unknown"
        if row["status"] == "pending" and self.now() > row["expires"]:
            self.db.set_approval_status(aid, "expired")
            return "expired"
        return row["status"]

    def resolve(self, aid: int, approve: bool) -> bool:
        if self.status(aid) != "pending":
            return False
        self.db.set_approval_status(aid, "approved" if approve else "rejected")
        return True
