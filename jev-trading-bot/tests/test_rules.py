import numpy as np
import pandas as pd
import pytest

from jevbot import indicators as ind
from jevbot import rules


def idx(n):
    return pd.bdate_range("2020-01-01", periods=n)


def series(vals, col="SPY"):
    return pd.DataFrame({col: np.asarray(vals, dtype=float)}, index=idx(len(vals)))


def test_rsi_bounded_and_extremes():
    up = pd.Series(np.arange(1, 60, dtype=float))
    down = pd.Series(np.arange(60, 1, -1, dtype=float))
    assert ind.rsi(up, 2).dropna().between(0, 100).all()
    assert ind.rsi(up, 2).iloc[-1] > 95
    assert ind.rsi(down, 2).iloc[-1] < 5


def test_rsi_flat_series_is_neutral_not_nan_crash():
    flat = pd.Series([10.0] * 30)
    out = ind.rsi(flat, 2).dropna()
    assert (out == 50).all()


def test_trend_ma_long_in_uptrend_flat_in_downtrend():
    up = series(np.linspace(100, 300, 400))
    down = series(np.linspace(300, 100, 400))
    assert rules.trend_ma(up, "SPY")["SPY"].iloc[-1] == 1
    assert rules.trend_ma(down, "SPY")["SPY"].iloc[-1] == 0


def test_no_lookahead_future_does_not_change_past_weights():
    base = np.linspace(100, 200, 300)
    a = series(base)
    b = series(np.concatenate([base[:250], base[250:][::-1] * 0.1]))  # different future
    wa = rules.trend_ma(a, "SPY")["SPY"].iloc[:250]
    wb = rules.trend_ma(b, "SPY")["SPY"].iloc[:250]
    pd.testing.assert_series_equal(wa, wb)


def test_rsi2_enters_on_oversold_dip_in_uptrend_and_exits():
    p = list(np.linspace(100, 300, 260)) + [296, 290, 284]  # sharp dip in uptrend
    w = rules.rsi2(series(p), "SPY")["SPY"]
    assert w.iloc[-1] == 1
    p2 = p + [300, 310, 320]
    assert rules.rsi2(series(p2), "SPY")["SPY"].iloc[-1] == 0


def test_breakout20_enters_on_new_high():
    flat = [100.0] * 40
    w = rules.breakout20(series(flat + [110.0]), "SPY")["SPY"]
    assert w.iloc[-1] == 1
    assert w.iloc[30] == 0


def test_sector_rotation_picks_top_3_equal_weight_and_cash_when_bench_weak():
    n = 400
    d = idx(n)
    df = pd.DataFrame({"SPY": np.linspace(100, 300, n)}, index=d)
    for i, g in enumerate([0.9, 0.8, 0.7, 0.2, 0.1]):
        df[f"S{i}"] = 100 * (1 + g * np.linspace(0, 1, n))
    w = rules.sector_rotation(df, sectors=[f"S{i}" for i in range(5)], bench="SPY")
    last = w.iloc[-1]
    assert last[["S0", "S1", "S2"]].sum() == pytest.approx(1.0)
    assert last[["S3", "S4"]].sum() == 0
    df["SPY"] = np.linspace(300, 100, n)
    assert rules.sector_rotation(df, sectors=[f"S{i}" for i in range(5)], bench="SPY").iloc[-1].sum() == 0


def test_dual_momentum_falls_back_to_safe_when_all_negative():
    n = 400
    d = idx(n)
    df = pd.DataFrame({"SPY": np.linspace(300, 200, n), "EFA": np.linspace(300, 250, n),
                       "AGG": np.linspace(100, 101, n)}, index=d)
    w = rules.dual_momentum(df, risky=("SPY", "EFA"), safe="AGG").iloc[-1]
    assert w["AGG"] == 1 and w["SPY"] == 0 and w["EFA"] == 0


def test_leveraged_trend_follows_index_filter():
    n = 400
    up = pd.DataFrame({"QQQ": np.linspace(100, 300, n), "TQQQ": np.linspace(10, 90, n)}, index=idx(n))
    assert rules.leveraged_trend(up, lev="TQQQ", index="QQQ")["TQQQ"].iloc[-1] == 1
    dn = pd.DataFrame({"QQQ": np.linspace(300, 100, n), "TQQQ": np.linspace(90, 10, n)}, index=idx(n))
    assert rules.leveraged_trend(dn, lev="TQQQ", index="QQQ")["TQQQ"].iloc[-1] == 0


def test_registry_has_all_six_candidates():
    assert set(rules.CANDIDATES) == {"trend_ma", "rsi2", "breakout20", "sector_rotation", "dual_momentum", "leveraged_trend"}
