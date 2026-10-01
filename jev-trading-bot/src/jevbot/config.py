"""Configuration read from the environment only. Secrets are never defaulted or logged."""
from __future__ import annotations

import os
from dataclasses import dataclass

PAPER_URL = "https://paper-api.alpaca.markets"


@dataclass(frozen=True)
class Config:
    db_path: str = "jevbot.db"
    approval_usd: float = 5000.0
    approval_timeout_s: int = 600
    max_position_pct: float = 0.10
    max_position_pct_leveraged: float = 0.03
    max_exposure_pct: float = 0.25
    daily_loss_pct: float = 0.02
    drawdown_kill_pct: float = 0.08
    broker_error_kill: int = 3
    alpaca_base_url: str = PAPER_URL

    @classmethod
    def from_env(cls) -> "Config":
        e = os.environ
        return cls(
            db_path=e.get("JEVBOT_DB", "jevbot.db"),
            approval_usd=float(e.get("APPROVAL_USD", "5000")),
        )


def secret(name: str) -> str | None:
    """Read a secret from the environment; return None if unset or empty."""
    v = os.environ.get(name)
    return v or None
