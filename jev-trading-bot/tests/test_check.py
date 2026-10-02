import pandas as pd
import requests

from jevbot import cli, jev


class FakeBroker:
    def account(self):
        return {"equity": 100000.0, "last_equity": 100000.0}


def good_ask(state, questions, model="jev-latest"):
    return jev.JevResult(answers={"q": {"choice": "no", "confidence": 0.9, "probabilities": {"no": 0.9, "yes": 0.05, "none_of_these": 0.05}}},
                         model="jev-1.13.0", latency_s=0.3)


def patch_all(monkeypatch, broker=FakeBroker, ask=good_ask, bars=None, tg=True):
    monkeypatch.setattr(cli, "Broker", broker)
    monkeypatch.setattr(cli.jev_mod, "ask", ask)
    monkeypatch.setattr(cli, "alpaca_bars", bars or (lambda *a, **k: pd.DataFrame({"SPY": [1.0, 2.0]})))
    if tg:
        monkeypatch.setenv("TELEGRAM_TOKEN", "1:tok")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "5")
        monkeypatch.setattr(cli.requests, "post", lambda *a, **k: type("R", (), {"raise_for_status": lambda s: None})())
    else:
        monkeypatch.delenv("TELEGRAM_TOKEN", raising=False)


def test_all_pass(monkeypatch):
    patch_all(monkeypatch)
    res = cli.check_cmd()
    assert [r[0] for r in res] == ["alpaca account", "alpaca data", "jev", "telegram"]
    assert all(r[1] for r in res)
    assert "jev-1.13.0" in [r for r in res if r[0] == "jev"][0][2]


def test_failures_reported_not_raised_and_never_leak_secrets(monkeypatch):
    def bad_broker():
        raise cli.BrokerError("ALPACA_KEY / ALPACA_SECRET not set")

    def bad_ask(*a, **k):
        raise jev.JevError("Jev request failed: HTTPError")

    def bad_post(*a, **k):
        raise requests.ConnectionError("https://api.telegram.org/bot1:tok/sendMessage")
    patch_all(monkeypatch, broker=bad_broker, ask=bad_ask)
    monkeypatch.setattr(cli.requests, "post", bad_post)
    res = cli.check_cmd()
    assert [r[1] for r in res] == [False, True, False, False]
    assert all("1:tok" not in r[2] for r in res)


def test_telegram_not_configured_is_a_warning_not_a_pass(monkeypatch):
    patch_all(monkeypatch, tg=False)
    t = [r for r in cli.check_cmd() if r[0] == "telegram"][0]
    assert t[1] is False and "not configured" in t[2]
