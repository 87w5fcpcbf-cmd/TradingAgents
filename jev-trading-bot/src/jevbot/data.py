"""Market data: Alpaca daily bars (with pagination), CSV cache loader, staleness checks."""
from __future__ import annotations

import os

import pandas as pd
import requests

DATA_URL = "https://data.alpaca.markets/v2/stocks/bars"


class DataError(Exception):
    pass


def alpaca_bars(symbols: list[str], start: str, end: str, feed: str = "iex", timeout: int = 20) -> pd.DataFrame:
    key, secret = os.environ.get("ALPACA_KEY"), os.environ.get("ALPACA_SECRET")
    if not key or not secret:
        raise DataError("ALPACA_KEY / ALPACA_SECRET not set")
    h = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
    rows: dict[str, dict[str, float]] = {s: {} for s in symbols}
    token = None
    while True:
        params = {"symbols": ",".join(symbols), "timeframe": "1Day", "start": start, "end": end,
                  "adjustment": "all", "feed": feed, "limit": 10000}
        if token:
            params["page_token"] = token
        try:
            r = requests.get(DATA_URL, headers=h, params=params, timeout=timeout)
            r.raise_for_status()
            body = r.json()
        except (requests.RequestException, ValueError) as e:
            raise DataError(f"bars request failed: {type(e).__name__}") from None
        for sym, bars in (body.get("bars") or {}).items():
            for b in bars:
                rows.setdefault(sym, {})[pd.Timestamp(b["t"]).tz_localize(None).normalize()] = float(b["c"])
        token = body.get("next_page_token")
        if not token:
            break
    df = pd.DataFrame({s: pd.Series(v, dtype=float) for s, v in rows.items() if v}).sort_index()
    return df[[s for s in symbols if s in df.columns]]


def load_csv_dir(path: str, symbols: list[str]) -> pd.DataFrame:
    """Load <path>/<SYMBOL>.csv with columns date,close into one close-price frame."""
    cols = {}
    for s in symbols:
        f = os.path.join(path, f"{s}.csv")
        if not os.path.exists(f):
            raise DataError(f"missing {f}")
        d = pd.read_csv(f, parse_dates=["date"]).set_index("date")["close"].astype(float)
        cols[s] = d
    return pd.DataFrame(cols).sort_index().dropna(how="all")


def save_csv_dir(df: pd.DataFrame, path: str) -> None:
    os.makedirs(path, exist_ok=True)
    for s in df.columns:
        out = df[s].dropna().rename("close").rename_axis("date").reset_index()
        out.to_csv(os.path.join(path, f"{s}.csv"), index=False)


def is_stale(df: pd.DataFrame, now: pd.Timestamp, max_age_days: int = 4) -> bool:
    """Stale if empty or the newest bar is older than max_age_days (tolerates weekends/holidays)."""
    if df is None or df.empty:
        return True
    return (now.normalize() - df.index.max().normalize()).days > max_age_days


def require_fresh(df: pd.DataFrame, now: pd.Timestamp) -> pd.DataFrame:
    if is_stale(df, now):
        raise DataError("market data is empty or stale")
    return df
