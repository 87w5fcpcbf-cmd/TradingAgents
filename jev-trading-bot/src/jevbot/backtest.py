"""Vectorised daily backtester. Weights decided with data <= t are earned from t+1 (no lookahead)."""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd


@dataclass
class Result:
    equity: pd.Series
    total_return: float
    max_drawdown: float          # positive fraction, e.g. 0.12 = 12%
    profit_factor: float
    win_rate: float
    n_trades: int
    trade_returns: list[float] = field(default_factory=list)


def split(df: pd.DataFrame, frac: float = 0.6) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Chronological in-sample / out-of-sample split."""
    k = int(len(df) * frac)
    return df.iloc[:k], df.iloc[k:]


def _segments(pos: np.ndarray) -> list[tuple[int, int]]:
    segs, start = [], None
    for i, p in enumerate(pos):
        if p > 0 and start is None:
            start = i
        elif p <= 0 and start is not None:
            segs.append((start, i - 1))
            start = None
    if start is not None:
        segs.append((start, len(pos) - 1))
    return segs


def run(closes: pd.DataFrame, weights: pd.DataFrame, cost_bps: float = 5.0) -> Result:
    weights = weights.reindex(index=closes.index, columns=closes.columns).fillna(0.0)
    pos = weights.shift(1).fillna(0.0)
    rets = closes.pct_change().fillna(0.0)
    cost = cost_bps / 1e4
    turnover = pos.diff().abs().fillna(pos.abs()).sum(axis=1)
    daily = (pos * rets).sum(axis=1) - turnover * cost
    equity = (1 + daily).cumprod()
    total_return = float(equity.iloc[-1] - 1) if len(equity) else 0.0
    peak = equity.cummax()
    max_dd = float(((peak - equity) / peak).max()) if len(equity) else 0.0

    trade_returns: list[float] = []
    for col in closes.columns:
        p, r = pos[col].to_numpy(), rets[col].to_numpy()
        for a, b in _segments(p):
            gross = float((p[a:b + 1] * r[a:b + 1]).sum())
            trade_returns.append(gross - 2 * cost * float(p[a:b + 1].max()))
    wins = [t for t in trade_returns if t > 0]
    losses = [-t for t in trade_returns if t < 0]
    n = len(trade_returns)
    if n == 0:
        pf = 0.0
    elif not losses:
        pf = float("inf") if wins else 0.0
    else:
        pf = sum(wins) / sum(losses)
    return Result(equity=equity, total_return=total_return, max_drawdown=max_dd, profit_factor=pf,
                  win_rate=(len(wins) / n if n else 0.0), n_trades=n, trade_returns=trade_returns)


def evaluate(r: Result, max_dd: float = 0.15, min_trades: int = 30, min_pf: float = 1.3,
             min_win_rate: float = 0.0) -> tuple[bool, list[str]]:
    reasons = []
    if r.n_trades < min_trades:
        reasons.append(f"only {r.n_trades} trades (< {min_trades})")
    if r.max_drawdown >= max_dd:
        reasons.append(f"max drawdown {r.max_drawdown:.1%} (>= {max_dd:.0%})")
    if r.profit_factor <= min_pf:
        reasons.append(f"profit factor {r.profit_factor:.2f} (<= {min_pf})")
    if r.win_rate < min_win_rate:
        reasons.append(f"win rate {r.win_rate:.1%} (< {min_win_rate:.0%})")
    return (not reasons), reasons
