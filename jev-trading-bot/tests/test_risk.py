import datetime as dt

import pytest

from jevbot.config import Config
from jevbot.db import Db
from jevbot.risk import Account, Approvals, Order, Risk


@pytest.fixture
def db(tmp_path):
    return Db(str(tmp_path / "r.db"))


@pytest.fixture
def risk(db):
    return Risk(db, Config())


def acct(equity=100_000, exposure=0, sod=100_000, peak=100_000):
    return Account(equity=equity, exposure=exposure, start_of_day_equity=sod, peak_equity=peak)


def order(notional=3000, leveraged=False):
    return Order(symbol="SPY", qty=notional / 500, price=500.0, leveraged=leveraged)


def test_small_order_allowed(risk):
    assert risk.check(order(3000), acct()).action == "allow"


def test_over_approval_size_needs_approval(risk):
    assert risk.check(order(6000), acct()).action == "needs_approval"


def test_position_cap_blocks(risk):
    d = risk.check(order(11_000), acct())
    assert d.action == "block" and "position" in d.reason
    d = risk.check(order(3_500, leveraged=True), acct())  # 3% cap = 3000
    assert d.action == "block"


def test_exposure_cap_blocks(risk):
    assert risk.check(order(4000), acct(exposure=22_000)).action == "block"


def test_daily_loss_limit_blocks_new_entries(risk):
    d = risk.check(order(1000), acct(equity=97_900, sod=100_000))
    assert d.action == "block" and "daily" in d.reason
    assert not risk.is_killed()


def test_daily_limit_two_days_in_a_row_trips_kill(risk):
    day1, day2 = dt.date(2026, 10, 1), dt.date(2026, 10, 2)
    risk.check(order(1000), acct(equity=97_000), today=day1)
    assert not risk.is_killed()
    risk.check(order(1000), acct(equity=97_000), today=day2)
    assert risk.is_killed()


def test_drawdown_trips_kill(risk):
    d = risk.check(order(1000), acct(equity=91_000, peak=100_000, sod=91_000))
    assert d.action == "block" and risk.is_killed()


def test_three_broker_errors_trip_kill_and_success_resets(risk):
    risk.record_broker_error(); risk.record_broker_error()
    risk.record_broker_ok()
    risk.record_broker_error(); risk.record_broker_error()
    assert not risk.is_killed()
    risk.record_broker_error()
    assert risk.is_killed()


def test_stale_data_and_jev_failures_trip_kill(risk):
    for _ in range(3):
        risk.record_stale_data()
    assert risk.is_killed()
    risk.reset()
    for _ in range(5):
        risk.record_jev_failure()
    assert risk.is_killed()


def test_killed_blocks_everything_until_manual_reset(risk):
    risk.trip("manual")
    assert risk.check(order(100), acct()).action == "block"
    risk.reset()
    assert risk.check(order(100), acct()).action == "allow"


def test_kill_state_persists_across_restart(tmp_path):
    p = str(tmp_path / "x.db")
    Risk(Db(p), Config()).trip("manual")
    assert Risk(Db(p), Config()).is_killed()


def test_counters_persist_across_restart(tmp_path):
    p = str(tmp_path / "x.db")
    r = Risk(Db(p), Config()); r.record_broker_error(); r.record_broker_error()
    r2 = Risk(Db(p), Config()); r2.record_broker_error()
    assert r2.is_killed()


# approvals ------------------------------------------------------------------
def test_approval_flow(db):
    t = [1000.0]
    ap = Approvals(db, Config(), now=lambda: t[0])
    aid = ap.request(order(6000), {"why": "test"})
    assert ap.status(aid) == "pending"
    assert ap.resolve(aid, True) is True
    assert ap.status(aid) == "approved"


def test_approval_expires_after_timeout_as_reject(db):
    t = [1000.0]
    ap = Approvals(db, Config(), now=lambda: t[0])
    aid = ap.request(order(6000), {})
    t[0] += 601
    assert ap.status(aid) == "expired"
    assert ap.resolve(aid, True) is False        # too late: cannot approve
    assert ap.status(aid) == "expired"


def test_cannot_resolve_twice(db):
    ap = Approvals(db, Config(), now=lambda: 1.0)
    aid = ap.request(order(6000), {})
    assert ap.resolve(aid, False) is True
    assert ap.resolve(aid, True) is False
    assert ap.status(aid) == "rejected"


def test_unknown_approval_id(db):
    assert Approvals(db, Config()).resolve(999, True) is False
    assert Approvals(db, Config()).status(999) == "unknown"


def test_approved_order_still_blocked_if_kill_tripped(db):
    risk = Risk(db, Config())
    ap = Approvals(db, Config(), now=lambda: 1.0)
    aid = ap.request(order(6000), {})
    ap.resolve(aid, True)
    risk.trip("manual")
    assert risk.guard(order(6000), acct(), approved=(ap.status(aid) == "approved")).action == "block"
