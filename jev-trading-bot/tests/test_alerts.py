import pytest
import requests

from jevbot import alerts as am
from jevbot.config import Config
from jevbot.db import Db
from jevbot.risk import Approvals, Order, Risk


class Resp:
    def __init__(self, body, status=200):
        self._b, self.status_code = body, status

    def json(self):
        return self._b

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_TOKEN", "123:secret-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")


@pytest.fixture
def stack(tmp_path, env):
    db = Db(str(tmp_path / "a.db"))
    cfg = Config()
    risk, ap = Risk(db, cfg), Approvals(db, cfg, now=lambda: 1.0)
    sent = []
    a = am.Alerts(db, risk, ap, status_fn=lambda: "equity 100k", send_fn=lambda t: sent.append(t))
    return db, risk, ap, a, sent


def upd(text, chat=42, uid=1):
    return {"update_id": uid, "message": {"chat": {"id": chat}, "text": text}}


def test_kill_from_allowed_chat_trips_switch(stack):
    _, risk, _, a, sent = stack
    a.handle(upd("/kill"))
    assert risk.is_killed()
    assert any("kill" in s.lower() for s in sent)


def test_foreign_chat_ignored_for_every_command(stack):
    _, risk, ap, a, sent = stack
    aid = ap.request(Order("SPY", 12, 500), {})
    for cmd in ("/kill", f"/approve {aid}", "/status"):
        a.handle(upd(cmd, chat=999))
    assert not risk.is_killed() and ap.status(aid) == "pending" and sent == []


def test_approve_and_reject(stack):
    _, _, ap, a, _ = stack
    a1 = ap.request(Order("SPY", 12, 500), {})
    a.handle(upd(f"/approve {a1}"))
    assert ap.status(a1) == "approved"
    a2 = ap.request(Order("QQQ", 12, 500), {})
    a.handle(upd(f"/reject {a2}"))
    assert ap.status(a2) == "rejected"


def test_approve_without_id_only_when_exactly_one_pending(stack):
    _, _, ap, a, sent = stack
    a1 = ap.request(Order("SPY", 12, 500), {})
    a.handle(upd("/approve"))
    assert ap.status(a1) == "approved"
    a2, a3 = ap.request(Order("SPY", 12, 500), {}), ap.request(Order("QQQ", 12, 500), {})
    a.handle(upd("/approve"))
    assert ap.status(a2) == "pending" and ap.status(a3) == "pending"
    assert any("which" in s.lower() for s in sent)


def test_garbage_and_non_message_updates_do_not_crash(stack):
    _, risk, _, a, _ = stack
    for u in ({}, {"update_id": 5}, {"update_id": 6, "message": {}}, upd("/approve abc"), upd("hello"), upd("/nope")):
        a.handle(u)
    assert not risk.is_killed()


def test_status_replies(stack):
    _, _, _, a, sent = stack
    a.handle(upd("/status"))
    assert "equity 100k" in sent[-1]


def test_send_failure_never_raises_and_never_leaks_token(tmp_path, env, monkeypatch, capsys):
    db = Db(str(tmp_path / "b.db"))
    cfg = Config()
    a = am.Alerts(db, Risk(db, cfg), Approvals(db, cfg), status_fn=lambda: "")

    def boom(*args, **kw):
        raise requests.ConnectionError("https://api.telegram.org/bot123:secret-token/sendMessage")
    monkeypatch.setattr(am.requests, "post", boom)
    a.send("hello")  # must not raise
    out = capsys.readouterr()
    assert "secret-token" not in out.out + out.err
    assert all("secret-token" not in str(e) for e in db.events("alert_error"))


def test_unconfigured_is_a_noop(tmp_path, monkeypatch):
    monkeypatch.delenv("TELEGRAM_TOKEN", raising=False)
    db = Db(str(tmp_path / "c.db"))
    cfg = Config()
    a = am.Alerts(db, Risk(db, cfg), Approvals(db, cfg), status_fn=lambda: "")
    a.send("hi")
    assert a.poll() == 0


def test_poll_processes_updates_and_advances_offset(stack, monkeypatch):
    _, risk, _, a, _ = stack
    body = {"ok": True, "result": [upd("/kill", uid=10)]}
    seen = {}

    def fake_get(url, params=None, timeout=None):
        seen["offset"] = (params or {}).get("offset")
        return Resp(body)

    monkeypatch.setattr(am.requests, "get", fake_get)
    assert a.poll() == 1 and risk.is_killed()
    a.poll()
    assert seen["offset"] == 11
