import pytest
import requests

from jevbot import jev

Q = {"regime": {"type": "choice", "instructions": "Regime?",
                "criteria": {"up": None, "down": None, "none_of_these": "other"}}}

NESTED = {"answers": {"regime": {"choice": "up", "confidence": 0.9,
          "probabilities": {"up": 0.9, "down": 0.05, "none_of_these": 0.05}}}, "model": "jev-1.13.0"}
FLAT = {"choice": "up", "confidence": 0.9,
        "probabilities": {"up": 0.9, "down": 0.05, "none_of_these": 0.05}, "model": "jev-1.13.0"}


class Resp:
    def __init__(self, body, status=200):
        self._b, self.status_code = body, status

    def json(self):
        return self._b

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "k_test")


def patch(monkeypatch, resp=None, exc=None):
    calls = []

    def fake(url, headers=None, json=None, timeout=None):
        calls.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        if exc:
            raise exc
        return resp

    monkeypatch.setattr(jev.requests, "post", fake)
    return calls


def test_nested_shape(monkeypatch):
    calls = patch(monkeypatch, Resp(NESTED))
    r = jev.ask({"x": 1}, Q)
    assert r.answers["regime"]["choice"] == "up"
    assert r.model == "jev-1.13.0"
    assert r.latency_s >= 0
    assert calls[0]["headers"]["Authorization"] == "Bearer k_test"
    assert calls[0]["url"] == "https://api.typesafe.ai/v1/systemone"
    assert calls[0]["json"]["model"] == "jev-latest"


def test_flat_shape_single_question(monkeypatch):
    patch(monkeypatch, Resp(FLAT))
    r = jev.ask({"x": 1}, Q)
    assert r.answers["regime"]["confidence"] == 0.9


@pytest.mark.parametrize("resp,exc", [
    (Resp({}, 500), None),
    (None, requests.Timeout()),
    (Resp({"answers": {"regime": {"confidence": 0.9}}, "model": "m"}), None),   # no choice
    (Resp({"answers": {"regime": {"choice": "up", "confidence": 0.9}}, "model": "m"}), None),  # no probabilities
    (Resp({"answers": {}, "model": "m"}), None),                                # missing question
    (Resp(["not", "a", "dict"]), None),
])
def test_failures_raise_jev_error(monkeypatch, resp, exc):
    patch(monkeypatch, resp, exc)
    with pytest.raises(jev.JevError):
        jev.ask({"x": 1}, Q)


def test_missing_key_raises(monkeypatch):
    monkeypatch.delenv("TYPESAFE_API_KEY")
    with pytest.raises(jev.JevError):
        jev.ask({"x": 1}, Q)


def test_question_without_none_of_these_rejected():
    bad = {"q": {"type": "choice", "instructions": "?", "criteria": {"a": None, "b": None}}}
    with pytest.raises(jev.JevError):
        jev.ask({}, bad)


def test_probabilities_must_be_numeric_in_range(monkeypatch):
    body = {"answers": {"regime": {"choice": "up", "confidence": 0.9,
            "probabilities": {"up": "high", "none_of_these": 0.1}}}, "model": "m"}
    patch(monkeypatch, Resp(body))
    with pytest.raises(jev.JevError):
        jev.ask({}, Q)
