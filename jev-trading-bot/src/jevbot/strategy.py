"""strategy.md: the human-readable rulebook plus a machine-readable JSON block that the bot loads."""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field

from .config import Config
from .rules import CANDIDATES, LEVERAGED

QUESTIONS = ("regime", "headline", "buying_pressure")


class StrategyError(ValueError):
    pass


@dataclass
class Strategy:
    name: str
    symbols: list[str]
    timeframe: str
    entry: str
    exit: str
    stop_loss_pct: float
    take_profit_pct: float
    jev_thresholds: dict[str, float] = field(default_factory=dict)

    def __post_init__(self):
        if self.name not in CANDIDATES:
            raise StrategyError(f"unknown strategy {self.name!r}")
        if not self.symbols:
            raise StrategyError("symbols must not be empty")
        if not 0 < self.stop_loss_pct < 1:
            raise StrategyError("stop_loss_pct must be in (0,1)")
        if not self.take_profit_pct > 0:
            raise StrategyError("take_profit_pct must be > 0")
        for q in QUESTIONS:
            t = self.jev_thresholds.get(q)
            if t is None or isinstance(t, bool) or not 0 < float(t) < 1:
                raise StrategyError(f"jev threshold for {q!r} must be in (0,1)")

    @property
    def leveraged(self) -> bool:
        return self.name in LEVERAGED

    def position_cap(self, cfg: Config) -> float:
        return min(cfg.max_position_pct, cfg.max_position_pct_leveraged) if self.leveraged else cfg.max_position_pct


_BLOCK = re.compile(r"```json\s*(.*?)```", re.S)


def load(path: str) -> Strategy:
    try:
        text = open(path, encoding="utf-8").read()
    except OSError as e:
        raise StrategyError(f"cannot read {path}: {e.strerror}") from None
    m = _BLOCK.search(text)
    if not m:
        raise StrategyError("strategy.md has no ```json block")
    try:
        data = json.loads(m.group(1))
        return Strategy(**data)
    except (json.JSONDecodeError, TypeError) as e:
        raise StrategyError(f"strategy.md block invalid: {e}") from None


def render(s: Strategy, stats: dict | None = None) -> str:
    stats = stats or {}
    lines = [
        f"# Strategy: {s.name}", "",
        "> Not financial advice. Paper trading only. Written by the backtester; edit the JSON block to change rules.", "",
        "| Rule | Value |", "|---|---|",
        f"| Symbols | {', '.join(s.symbols)} |", f"| Timeframe | {s.timeframe} |",
        f"| Entry | {s.entry} |", f"| Exit | {s.exit} |",
        f"| Stop loss | {s.stop_loss_pct:.1%} |", f"| Take profit | {s.take_profit_pct:.1%} |",
        f"| Leveraged | {'yes (reduced position cap)' if s.leveraged else 'no'} |", "",
        "Jev must clear every threshold before a trade fires:", "",
    ]
    lines += [f"- {k}: >= {v}" for k, v in s.jev_thresholds.items()]
    if stats:
        lines += ["", "## Backtest (out-of-sample)", ""] + [f"- {k}: {v}" for k, v in stats.items()]
    lines += ["", "## Machine-readable rules", "", "```json", json.dumps(asdict(s), indent=2), "```", ""]
    return "\n".join(lines)
