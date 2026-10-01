import pandas as pd
import pytest
import requests

from jevbot import broker as bm
from jevbot import data as dm
from jevbot.config import PAPER_URL


class Resp:
    def __init__(self, body, status=200):
        self._b, self.status_code = body, status

    def json(self):
        return self._b

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


@pytest.fixture(autouse=True)
def keys(monkeypatch):
    monkeypatch.setenv("ALPACA_KEY", "pk")
    monkeypatch.setenv("ALPACA_SECRET", "sk")


def test_refuses_non_paper_url():
    with pytest.raises(bm.BrokerError):
        bm.Broker(base_url="https://api.alpaca.markets")
    bm.Broker(base_url=PAPER_URL)


def test_requires_keys(monkeypatch):
    monkeypatch.delenv("ALPACA_KEY")
    with pytest.raises(bm.BrokerError):
        bm.Broker()


def test_account_and_positions(monkeypatch):
    def fake(method, url, headers=None, json=None, params=None, timeout=None):
        assert headers["APCA-API-KEY-ID"] == "pk" and url.startswith(PAPER_URL)
        if url.endswith("/v2/account"):
            return Resp({"equity": "100500.5", "last_equity": "100000"})
        return Resp([{"symbol": "SPY", "qty": "10", "market_value": "5000"}])

    monkeypatch.setattr(bm.requests, "request", fake)
    b = bm.Broker()
    a = b.account()
    assert a["equity"] == 100500.5 and a["last_equity"] == 100000.0
    assert b.exposure() == 5000.0


def test_bracket_order_payload(monkeypatch):
    sent = {}

    def fake(method, url, headers=None, json=None, params=None, timeout=None):
        sent.update(method=method, url=url, json=json)
        return Resp({"id": "abc", "status": "accepted"})

    monkeypatch.setattr(bm.requests, "request", fake)
    out = bm.Broker().bracket_order("SPY", 10, stop_price=475.0, take_profit_price=575.0)
    j = sent["json"]
    assert sent["method"] == "POST" and sent["url"].endswith("/v2/orders")
    assert j["order_class"] == "bracket" and j["side"] == "buy" and j["qty"] == "10"
    assert j["stop_loss"]["stop_price"] == "475.00" and j["take_profit"]["limit_price"] == "575.00"
    assert out["id"] == "abc"


def test_bracket_requires_whole_positive_shares():
    with pytest.raises(bm.BrokerError):
        bm.Broker().bracket_order("SPY", 0, stop_price=1, take_profit_price=2)
    with pytest.raises(bm.BrokerError):
        bm.Broker().bracket_order("SPY", 2.5, stop_price=1, take_profit_price=2)


def test_http_and_network_errors_become_broker_error(monkeypatch):
    monkeypatch.setattr(bm.requests, "request", lambda *a, **k: Resp({}, 500))
    with pytest.raises(bm.BrokerError):
        bm.Broker().account()

    def boom(*a, **k):
        raise requests.ConnectionError("x")
    monkeypatch.setattr(bm.requests, "request", boom)
    with pytest.raises(bm.BrokerError):
        bm.Broker().account()


# data -----------------------------------------------------------------------
def test_alpaca_bars_paginates_and_builds_close_frame(monkeypatch):
    pages = [
        {"bars": {"SPY": [{"t": "2026-09-28T04:00:00Z", "c": 500.0}], "QQQ": [{"t": "2026-09-28T04:00:00Z", "c": 400.0}]},
         "next_page_token": "p2"},
        {"bars": {"SPY": [{"t": "2026-09-29T04:00:00Z", "c": 505.0}], "QQQ": [{"t": "2026-09-29T04:00:00Z", "c": 402.0}]},
         "next_page_token": None},
    ]
    it = iter(pages)
    monkeypatch.setattr(dm.requests, "get", lambda *a, **k: Resp(next(it)))
    df = dm.alpaca_bars(["SPY", "QQQ"], "2026-09-01", "2026-09-30")
    assert list(df.columns) == ["SPY", "QQQ"] and len(df) == 2
    assert df["SPY"].iloc[-1] == 505.0


def test_empty_bars_raise():
    with pytest.raises(dm.DataError):
        dm.require_fresh(pd.DataFrame(), pd.Timestamp("2026-10-01"))


def test_staleness():
    idx = pd.to_datetime(["2026-09-24", "2026-09-25"])
    df = pd.DataFrame({"SPY": [1.0, 2.0]}, index=idx)
    assert dm.is_stale(df, pd.Timestamp("2026-10-05"))
    assert not dm.is_stale(df, pd.Timestamp("2026-09-28"))   # weekend gap tolerated
    assert dm.is_stale(pd.DataFrame(), pd.Timestamp("2026-09-28"))


def test_close_position_cancels_open_orders_then_closes(monkeypatch):
    seen = []

    def fake(method, url, headers=None, json=None, params=None, timeout=None):
        seen.append((method, url.replace(PAPER_URL, ""), params))
        if method == "GET":
            return Resp([{"id": "leg1"}, {"id": "leg2"}])
        return Resp({})

    monkeypatch.setattr(bm.requests, "request", fake)
    bm.Broker().close_position("SPY")
    assert seen[0][0] == "GET" and seen[0][2]["symbols"] == "SPY" and seen[0][2]["status"] == "open"
    assert ("DELETE", "/v2/orders/leg1", None) in seen and ("DELETE", "/v2/orders/leg2", None) in seen
    assert seen[-1][:2] == ("DELETE", "/v2/positions/SPY")


def test_close_position_rejects_odd_symbols():
    with pytest.raises(bm.BrokerError):
        bm.Broker().close_position("../account")
