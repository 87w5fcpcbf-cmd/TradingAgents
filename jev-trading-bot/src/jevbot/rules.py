"""Candidate strategies. Each takes a DataFrame of closes (columns = symbols) and returns a
DataFrame of target weights (same index, 0..1). The weight on row t is decided with data <= t;
the backtester applies it from t+1, so there is no lookahead."""
from __future__ import annotations

from typing import Callable

import numpy as np
import pandas as pd

from .indicators import momentum, rolling_max_prior, rolling_min_prior, rsi, sma

SECTORS = ["XLB", "XLC", "XLE", "XLF", "XLI", "XLK", "XLP", "XLRE", "XLU", "XLV", "XLY"]


def _weights(closes: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame(0.0, index=closes.index, columns=closes.columns)


def _month_end_mask(index: pd.DatetimeIndex) -> np.ndarray:
    nxt = pd.Series(index[1:].append(pd.DatetimeIndex([index[-1] + pd.Timedelta(days=40)])), index=index)
    return (nxt.dt.month != index.month).to_numpy()


def _monthly_hold(daily: pd.DataFrame) -> pd.DataFrame:
    """Keep only month-end decisions and carry them forward (rebalance monthly)."""
    mask = _month_end_mask(daily.index)
    held = daily.where(pd.Series(mask, index=daily.index), np.nan).ffill()
    return held.fillna(0.0)


def trend_ma(closes: pd.DataFrame, symbol: str = "SPY", fast: int = 50, slow: int = 200) -> pd.DataFrame:
    w = _weights(closes)
    s = closes[symbol]
    w[symbol] = (sma(s, fast) > sma(s, slow)).astype(float)
    return w


def rsi2(closes: pd.DataFrame, symbol: str = "SPY", entry: float = 10, exit_: float = 70) -> pd.DataFrame:
    w = _weights(closes)
    s = closes[symbol]
    r, trend = rsi(s, 2).to_numpy(), sma(s, 200).to_numpy()
    px = s.to_numpy()
    pos, out = 0.0, np.zeros(len(s))
    for i in range(len(s)):
        if np.isnan(r[i]) or np.isnan(trend[i]):
            pos = 0.0
        elif pos == 0.0 and r[i] < entry and px[i] > trend[i]:
            pos = 1.0
        elif pos == 1.0 and r[i] > exit_:
            pos = 0.0
        out[i] = pos
    w[symbol] = out
    return w


def breakout20(closes: pd.DataFrame, symbol: str = "SPY", enter_n: int = 20, exit_n: int = 10) -> pd.DataFrame:
    w = _weights(closes)
    s = closes[symbol]
    hi, lo = rolling_max_prior(s, enter_n).to_numpy(), rolling_min_prior(s, exit_n).to_numpy()
    px = s.to_numpy()
    pos, out = 0.0, np.zeros(len(s))
    for i in range(len(s)):
        if np.isnan(hi[i]) or np.isnan(lo[i]):
            pos = 0.0
        elif pos == 0.0 and px[i] > hi[i]:
            pos = 1.0
        elif pos == 1.0 and px[i] < lo[i]:
            pos = 0.0
        out[i] = pos
    w[symbol] = out
    return w


def sector_rotation(closes: pd.DataFrame, sectors: list[str] | None = None, bench: str = "SPY",
                    top_n: int = 3, lookback: int = 126) -> pd.DataFrame:
    sectors = [c for c in (sectors or SECTORS) if c in closes.columns]
    w = _weights(closes)
    mom = pd.DataFrame({c: momentum(closes[c], lookback) for c in sectors})
    ranks = mom.rank(axis=1, ascending=False, method="first")
    pick = ((ranks <= top_n) & mom.notna()).astype(float) / top_n
    pick = _monthly_hold(pick)
    risk_on = (closes[bench] > sma(closes[bench], 200)).astype(float)
    for c in sectors:
        w[c] = pick[c] * risk_on
    return w


def dual_momentum(closes: pd.DataFrame, risky: tuple[str, ...] = ("SPY", "EFA"), safe: str = "AGG",
                  lookback: int = 252) -> pd.DataFrame:
    w = _weights(closes)
    mom = pd.DataFrame({c: momentum(closes[c], lookback) for c in risky})
    best = mom.fillna(-np.inf).idxmax(axis=1)  # warmup rows are all-NaN; idxmax would raise
    best_ret = mom.max(axis=1)
    daily = _weights(closes)
    for c in risky:
        daily[c] = ((best == c) & (best_ret > 0)).astype(float)
    daily[safe] = ((best_ret <= 0) & mom.notna().all(axis=1)).astype(float)
    held = _monthly_hold(daily)
    for c in [*risky, safe]:
        w[c] = held[c]
    return w


def leveraged_trend(closes: pd.DataFrame, lev: str = "TQQQ", index: str = "QQQ", slow: int = 200) -> pd.DataFrame:
    w = _weights(closes)
    w[lev] = (closes[index] > sma(closes[index], slow)).astype(float)
    return w


# name -> (function, default kwargs, symbols needed, is_leveraged)
CANDIDATES: dict[str, Callable[..., pd.DataFrame]] = {
    "trend_ma": trend_ma,
    "rsi2": rsi2,
    "breakout20": breakout20,
    "sector_rotation": sector_rotation,
    "dual_momentum": dual_momentum,
    "leveraged_trend": leveraged_trend,
}

SYMBOLS: dict[str, list[str]] = {
    "trend_ma": ["SPY"], "rsi2": ["SPY"], "breakout20": ["SPY"],
    "sector_rotation": ["SPY", *SECTORS], "dual_momentum": ["SPY", "EFA", "AGG"],
    "leveraged_trend": ["QQQ", "TQQQ"],
}
LEVERAGED = {"leveraged_trend"}
