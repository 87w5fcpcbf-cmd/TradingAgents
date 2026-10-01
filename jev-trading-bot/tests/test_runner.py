import numpy as np
import pandas as pd
import pytest

from jevbot import jev
from jevbot.alerts import Alerts
from jevbot.broker import BrokerError
from jevbot.config import Config
from jevbot.db import Db
from jevbot.risk import Approvals, Risk
from jevbot.runner import Deps, process_approvals, tick
from jevbot.strategy import Strategy

NOW = pd.Timestamp("2026-10-01")
STRAT = Strategy(name="trend_ma", symbols=["SPY"], timeframe="1D", entry="e", exit="x",
                 stop_loss_pct=0.05, take_profit_pct=0.15,
                 jev_thresholds={"regime": 0.8, "headline": 0.8, "buying_pressure": 0.8})


def uptrend(end=NOW, n=300):
    return pd.DataFrame({"SPY": np.linspace(300, 500, n)}, index=pd.bdate_range(end=end, periods=n))


def good_answers():
    def mk(o):
        return {"choice": o, "confidence": 0.9, "probabilities": {o: 0.9, "none_of_these": 0.1}}
    return {"regime": mk("trending_up"), "headline": mk("bullish"), "buying_pressure": mk("yes")}


class FakeBroker:
    def __init__(self, equity=100_000, last_equity=100_000, fail=False):
        self.equity, self.last_equity, self.fail = equity, last_equity, fail
        self.orders, self.held = [], []

    def account(self):
        if self.fail:
            raise BrokerError("down")
        return {"equity": self.equity, "last_equity": self.last_equity}

    def exposure(self):
        return 0.0

    def positions(self):
        return [{"symbol": s, "qty": "10", "avg_entry_price": "480", "current_price": "500", "market_value": "5000"}
                for s in self.held]

    def close_position(self, symbol):
        if self.fail:
            raise BrokerError("down")
        self.closed = getattr(self, "closed", []) + [symbol]
        self.held.remove(symbol)
        return {"id": "x"}

    def bracket_order(self, symbol, qty, stop_price, take_profit_price):
        if self.fail:
            raise BrokerError("down")
        self.orders.append(dict(symbol=symbol, qty=qty, stop=stop_price, tp=take_profit_price))
        self.held.append(symbol)
        return {"id": "o1", "status": "accepted"}


@pytest.fixture
def mk(tmp_path):
    def build(cfg=None, broker=None, data=None, ask=None):
        db = Db(str(tmp_path / "t.db"))
        cfg = cfg or Config()
        risk, ap = Risk(db, cfg), Approvals(db, cfg, now=lambda: 1000.0)
        sent = []
        al = Alerts(db, risk, ap, status_fn=lambda: "ok", send_fn=sent.append)
        calls = []

        def default_ask(state, questions, model_name="jev-latest"):
            calls.append(state)
            return jev.JevResult(answers=good_answers(), model="jev-1.13.0", latency_s=0.3)

        d = Deps(db=db, cfg=cfg, risk=risk, approvals=ap, alerts=al, broker=broker or FakeBroker(),
                 strategy=STRAT, data_fn=lambda syms: data if data is not None else uptrend(),
                 ask=ask or default_ask, now=lambda: NOW)
        return d, sent, calls
    return build


def test_end_to_end_fire_places_bracket_and_logs_everything(mk):
    d, sent, calls = mk()
    out = tick(d)
    b = d.broker
    assert len(b.orders) == 1
    o = b.orders[0]
    assert o["symbol"] == "SPY" and o["stop"] == pytest.approx(500 * 0.95, rel=0.01)
    assert o["qty"] * 500 <= 0.05 * 100_000 + 1
    s = d.db.signals()[0]
    assert s["fired"] and s["probabilities"]["regime"] == 0.9 and s["model"] == "jev-1.13.0"
    assert d.db.jev_calls()[0]["model"] == "jev-1.13.0" and d.db.jev_calls()[0]["latency_s"] == 0.3
    assert len(d.db.trades()) == 1
    assert any("SPY" in m for m in sent)
    assert out["orders"] == 1


def test_kill_switch_means_no_jev_call_and_no_orders(mk):
    d, _, calls = mk()
    d.risk.trip("manual")
    assert tick(d)["status"] == "killed"
    assert d.broker.orders == [] and calls == []


def test_already_held_symbol_not_bought_again(mk):
    d, _, calls = mk()
    d.broker.held.append("SPY")
    tick(d)
    assert d.broker.orders == [] and calls == []


def test_second_tick_same_day_does_not_double_buy(mk):
    d, _, _ = mk()
    tick(d); tick(d)
    assert len(d.broker.orders) == 1


def test_jev_failure_means_no_trade_and_five_failures_trip_kill(mk):
    def down(*a, **k):
        raise jev.JevError("down")
    d, _, _ = mk(ask=down)
    for _ in range(5):
        tick(d)
    assert d.broker.orders == [] and d.risk.is_killed()


def test_stale_data_blocks_and_three_stale_ticks_trip_kill(mk):
    stale = uptrend(end=pd.Timestamp("2026-09-01"))
    d, _, calls = mk(data=stale)
    for _ in range(3):
        tick(d)
    assert d.broker.orders == [] and calls == [] and d.risk.is_killed()


def test_broker_errors_recorded_and_trip_kill(mk):
    d, _, _ = mk(broker=FakeBroker(fail=True))
    for _ in range(3):
        tick(d)
    assert d.risk.is_killed()


def test_daily_loss_limit_blocks_new_entry(mk):
    d, _, _ = mk(broker=FakeBroker(equity=97_000, last_equity=100_000))
    tick(d)
    assert d.broker.orders == []
    assert "daily" in d.db.signals()[0]["result"]


def test_large_order_waits_for_approval_then_executes_when_approved(mk):
    d, sent, _ = mk(cfg=Config(approval_usd=1000))
    out = tick(d)
    assert d.broker.orders == [] and out["pending"] == 1
    assert any("/approve" in m for m in sent)
    aid = d.db.pending_approvals()[0]["id"]
    d.approvals.resolve(aid, True)
    assert process_approvals(d)["executed"] == 1
    assert len(d.broker.orders) == 1


def test_rejected_or_expired_approval_never_trades(mk):
    d, _, _ = mk(cfg=Config(approval_usd=1000))
    tick(d)
    aid = d.db.pending_approvals()[0]["id"]
    d.approvals.resolve(aid, False)
    process_approvals(d)
    t = [1000.0]
    d2, _, _ = mk(cfg=Config(approval_usd=1000))
    d2.approvals.now = lambda: t[0]
    tick(d2)
    t[0] += 601
    process_approvals(d2)
    assert d.broker.orders == [] and d2.broker.orders == []


def test_approved_but_kill_tripped_before_execution_is_blocked(mk):
    d, _, _ = mk(cfg=Config(approval_usd=1000))
    tick(d)
    aid = d.db.pending_approvals()[0]["id"]
    d.approvals.resolve(aid, True)
    d.risk.trip("manual")
    process_approvals(d)
    assert d.broker.orders == []


def test_no_rule_signal_means_no_jev_call(mk):
    down = pd.DataFrame({"SPY": np.linspace(500, 300, 300)}, index=pd.bdate_range(end=NOW, periods=300))
    d, _, calls = mk(data=down)
    tick(d)
    assert calls == [] and d.broker.orders == []


def test_rule_exit_closes_held_position_without_calling_jev(mk):
    down = pd.DataFrame({"SPY": np.linspace(500, 300, 300)}, index=pd.bdate_range(end=NOW, periods=300))
    d, sent, calls = mk(data=down)
    d.broker.held.append("SPY")
    d.db.log_trade({"symbol": "SPY", "qty": 10, "price": 480, "pnl": None, "order_id": "o1"})
    out = tick(d)
    assert d.broker.closed == ["SPY"] and calls == [] and out["exits"] == 1
    assert d.db.trades()[0]["pnl"] == pytest.approx(200.0)       # (500-480)*10
    assert any("CLOSED" in m for m in sent)


def test_held_position_kept_while_rule_still_long(mk):
    d, _, _ = mk()
    d.broker.held.append("SPY")
    tick(d)
    assert getattr(d.broker, "closed", []) == []


def test_no_exits_when_killed(mk):
    down = pd.DataFrame({"SPY": np.linspace(500, 300, 300)}, index=pd.bdate_range(end=NOW, periods=300))
    d, _, _ = mk(data=down)
    d.broker.held.append("SPY")
    d.risk.trip("manual")
    tick(d)
    assert getattr(d.broker, "closed", []) == []


def test_failed_exit_is_recorded_as_broker_error_and_alerted(mk):
    down = pd.DataFrame({"SPY": np.linspace(500, 300, 300)}, index=pd.bdate_range(end=NOW, periods=300))
    b = FakeBroker()
    b.held.append("SPY")
    d, sent, _ = mk(data=down, broker=b)
    b.fail = False
    orig = b.close_position
    b.close_position = lambda s: (_ for _ in ()).throw(BrokerError("nope"))
    tick(d)
    assert d.db.get_int("cnt:broker") == 1 and any("EXIT FAILED" in m for m in sent)
