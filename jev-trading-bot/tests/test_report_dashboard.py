import time

import pytest
from fastapi.testclient import TestClient

from jevbot.config import Config
from jevbot.dashboard import create_app
from jevbot.db import Db
from jevbot.report import daily
from jevbot.risk import Risk
from jevbot.runner import sync_trades


@pytest.fixture
def db(tmp_path):
    return Db(str(tmp_path / "d.db"))


def seed(db):
    t0 = time.time()
    db.log_equity(100_000, t0 - 100)
    db.log_equity(100_300, t0)
    db.log_trade({"symbol": "SPY", "qty": 10, "price": 500, "pnl": 120.0, "order_id": "a"})
    db.log_trade({"symbol": "QQQ", "qty": 10, "price": 400, "pnl": -50.0, "order_id": "b"})
    db.log_trade({"symbol": "IWM", "qty": 10, "price": 200, "pnl": None, "order_id": "c"})
    db.log_jev({"model": "jev-1.13.0", "latency_s": 0.4, "cost_usd": 0.002, "ok": True})
    db.log_jev({"model": "jev-1.13.0", "latency_s": 0.6, "cost_usd": None, "ok": True})


def test_daily_report_math(db):
    seed(db)
    r = daily(db)
    assert "Trades placed today: 3" in r
    assert "win rate 50.0%" in r            # 1 win of 2 closed
    assert "realized P&L $70.00" in r
    assert "largest loss $-50.00" in r
    assert "avg latency 0.50s" in r
    assert "avg cost per decision $0.0020" in r   # only calls reporting a cost
    assert "jev-1.13.0" in r


def test_daily_report_empty_db_does_not_crash(db):
    r = daily(db)
    assert "Trades placed today: 0" in r and "n/a" in r


def test_report_shows_kill_switch(db):
    Risk(db, Config()).trip("drawdown")
    assert "TRIPPED" in daily(db) and "drawdown" in daily(db)


class FakeBroker:
    def closed_orders(self):
        return [{"id": "c", "filled_avg_price": "200", "legs": [
            {"status": "canceled", "filled_avg_price": None},
            {"status": "filled", "filled_avg_price": "190"}]}]


def test_sync_trades_fills_pnl_from_filled_leg(db):
    seed(db)

    class D:  # minimal deps
        pass
    d = D(); d.db = db; d.broker = FakeBroker()
    assert sync_trades(d) == 1
    t = [x for x in db.trades() if x["symbol"] == "IWM"][0]
    assert t["pnl"] == pytest.approx(-100.0)    # (190-200)*10


# dashboard -------------------------------------------------------------------
@pytest.fixture
def client(db, monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "hunter2")
    seed(db)
    db.log_signal({"symbol": "<script>alert(1)</script>", "direction": 1, "fired": False,
                   "probabilities": {"regime": 0.5}, "model": "jev-1.13.0", "result": "<b>x</b>"})
    return TestClient(create_app(db, Config()), follow_redirects=False)


def test_requires_login(client):
    assert client.get("/").status_code == 303
    assert client.get("/api/signals").status_code in (303, 401)


def test_wrong_password_rejected_right_one_accepted(client):
    assert client.post("/login", data={"password": "nope"}).status_code == 401
    assert client.get("/").status_code == 303
    r = client.post("/login", data={"password": "hunter2"})
    assert r.status_code == 303
    page = client.get("/")
    assert page.status_code == 200 and "Kill switch" in page.text and "Jev" in page.text


def test_output_is_html_escaped(client):
    client.post("/login", data={"password": "hunter2"})
    html = client.get("/").text
    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;" in html


def test_fails_closed_without_password(db, monkeypatch):
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    c = TestClient(create_app(db, Config()), follow_redirects=False)
    assert c.get("/login").status_code == 503
    assert c.post("/login", data={"password": ""}).status_code == 503
    assert c.get("/").status_code in (303, 503)
