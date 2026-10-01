"""Jev (TypeSafe) client. Data in, probabilities out. Fails closed: any problem raises JevError.

The API key is read from TYPESAFE_API_KEY at call time and is never logged or stored.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import requests

URL = "https://api.typesafe.ai/v1/systemone"
TIMEOUT_S = 30


class JevError(Exception):
    """Any failure to obtain a well-formed Jev answer. Callers must treat this as 'no trade'."""


@dataclass
class JevResult:
    answers: dict[str, dict[str, Any]]
    model: str
    latency_s: float
    cost_usd: float | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)


def choice_question(instructions: str, options: dict[str, str | None]) -> dict[str, Any]:
    """Build a choice question; always adds the required none_of_these option."""
    criteria = dict(options)
    criteria.setdefault("none_of_these", "None of the other options apply.")
    return {"type": "choice", "instructions": instructions, "criteria": criteria}


def _validate_questions(questions: dict[str, Any]) -> None:
    for name, q in questions.items():
        if q.get("type") == "choice" and "none_of_these" not in q.get("criteria", {}):
            raise JevError(f"question {name!r} lacks a none_of_these option")


def _validate_answer(name: str, a: Any) -> dict[str, Any]:
    if not isinstance(a, dict) or "choice" not in a:
        raise JevError(f"answer {name!r} missing choice")
    probs = a.get("probabilities")
    if not isinstance(probs, dict) or not probs:
        raise JevError(f"answer {name!r} missing probabilities")
    for k, v in probs.items():
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0.0 <= float(v) <= 1.0:
            raise JevError(f"answer {name!r} has invalid probability for {k!r}")
    return a


def ask(state: dict[str, Any], questions: dict[str, Any], model: str = "jev-latest") -> JevResult:
    import os

    key = os.environ.get("TYPESAFE_API_KEY")
    if not key:
        raise JevError("TYPESAFE_API_KEY is not set")
    _validate_questions(questions)
    t0 = time.monotonic()
    try:
        r = requests.post(
            URL,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={"state": state, "model": model, "questions": questions},
            timeout=TIMEOUT_S,
        )
        r.raise_for_status()
        body = r.json()
    except (requests.RequestException, ValueError) as e:
        raise JevError(f"Jev request failed: {type(e).__name__}") from None  # message only: never echo headers
    latency = time.monotonic() - t0
    if not isinstance(body, dict):
        raise JevError("Jev response is not an object")
    if isinstance(body.get("answers"), dict):
        raw_answers = body["answers"]
    elif len(questions) == 1 and "choice" in body:  # flat shape for a single question
        raw_answers = {next(iter(questions)): body}
    else:
        raise JevError("Jev response has no answers")
    answers = {}
    for name in questions:
        if name not in raw_answers:
            raise JevError(f"answer {name!r} missing from response")
        answers[name] = _validate_answer(name, raw_answers[name])
    cost = body.get("cost_usd") if isinstance(body.get("cost_usd"), (int, float)) else None
    return JevResult(answers=answers, model=str(body.get("model", "unknown")), latency_s=latency, cost_usd=cost, raw=body)
