"""Pure indicator functions. Every value at index t uses data up to and including t only."""
from __future__ import annotations

import pandas as pd


def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).mean()


def rsi(s: pd.Series, n: int = 14) -> pd.Series:
    """Wilder RSI. A flat series is neutral (50); all-up is 100; all-down is 0."""
    d = s.diff()
    gain = d.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    loss = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    out = 100 - 100 / (1 + gain / loss)
    out = out.where(~((loss == 0) & (gain > 0)), 100.0)
    out = out.where(~((loss == 0) & (gain == 0)), 50.0)
    return out


def rolling_max_prior(s: pd.Series, n: int) -> pd.Series:
    """Max of the previous n values, excluding today."""
    return s.shift(1).rolling(n, min_periods=n).max()


def rolling_min_prior(s: pd.Series, n: int) -> pd.Series:
    return s.shift(1).rolling(n, min_periods=n).min()


def momentum(s: pd.Series, n: int) -> pd.Series:
    return s / s.shift(n) - 1


def vol_stop_pct(s: pd.Series, n: int = 20, k: float = 2.0) -> float:
    """Stop distance as a fraction of price: k * stdev of daily returns, floored at 1%, capped at 15%."""
    r = s.pct_change().dropna().tail(n)
    if len(r) < 2:
        return 0.05
    return float(min(max(k * r.std(), 0.01), 0.15))
