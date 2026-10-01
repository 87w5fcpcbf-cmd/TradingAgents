import numpy as np
import pandas as pd
import pytest

from jevbot import backtest as bt


def df(vals, col="SPY"):
    return pd.DataFrame({col: np.asarray(vals, float)}, index=pd.bdate_range("2021-01-01", periods=len(vals)))


def test_buy_and_hold_exact_return_no_costs():
    closes = df([100, 110, 121])
    w = pd.DataFrame(1.0, index=closes.index, columns=closes.columns)
    r = bt.run(closes, w, cost_bps=0)
    # weights decided on day t are earned from day t+1: both +10% days are earned
    assert r.total_return == pytest.approx(0.21)
    assert r.n_trades == 1


def test_weights_are_lagged_one_day_no_lookahead():
    closes = df([100, 100, 200, 200])
    w = pd.DataFrame({"SPY": [0.0, 1.0, 0.0, 0.0]}, index=closes.index)  # long decided on day1 close
    r = bt.run(closes, w, cost_bps=0)
    assert r.total_return == pytest.approx(1.0)  # earns the day1->day2 doubling, applied from day2


def test_costs_reduce_return():
    closes = df(np.linspace(100, 150, 50))
    w = pd.DataFrame(1.0, index=closes.index, columns=closes.columns)
    assert bt.run(closes, w, cost_bps=20).total_return < bt.run(closes, w, cost_bps=0).total_return


def test_flat_data_no_trades_rejected_without_zero_division():
    closes = df([100.0] * 300)
    w = pd.DataFrame(0.0, index=closes.index, columns=closes.columns)
    r = bt.run(closes, w, cost_bps=5)
    assert r.n_trades == 0 and r.profit_factor == 0 and r.max_drawdown == 0
    ok, reasons = bt.evaluate(r)
    assert not ok and any("trades" in x for x in reasons)


def test_max_drawdown_computed():
    closes = df([100, 100, 50, 100])
    w = pd.DataFrame(1.0, index=closes.index, columns=closes.columns)
    r = bt.run(closes, w, cost_bps=0)
    assert r.max_drawdown == pytest.approx(0.5)


def test_evaluate_rejects_drawdown_and_few_trades_and_low_pf():
    base = dict(equity=pd.Series([1.0]), total_return=0.5, win_rate=0.6, trade_returns=[])
    good = bt.Result(max_drawdown=0.10, profit_factor=2.0, n_trades=40, **base)
    assert bt.evaluate(good)[0]
    assert not bt.evaluate(bt.Result(max_drawdown=0.20, profit_factor=2.0, n_trades=40, **base))[0]
    assert not bt.evaluate(bt.Result(max_drawdown=0.10, profit_factor=2.0, n_trades=10, **base))[0]
    assert not bt.evaluate(bt.Result(max_drawdown=0.10, profit_factor=1.0, n_trades=40, **base))[0]
    assert bt.evaluate(bt.Result(max_drawdown=0.4, profit_factor=2.0, n_trades=40, **base), max_dd=0.5)[0]


def test_profit_factor_and_win_rate():
    # two trades: +10% then -5%
    closes = df([100, 100, 110, 110, 110, 104.5])
    w = pd.DataFrame({"SPY": [0, 1, 1, 0, 1, 0]}, index=closes.index, dtype=float)
    r = bt.run(closes, w, cost_bps=0)
    assert r.n_trades == 2
    assert r.win_rate == pytest.approx(0.5)
    assert r.profit_factor == pytest.approx(0.10 / 0.05, rel=0.05)


def test_split_is_chronological_and_disjoint():
    closes = df(np.arange(100) + 1.0)
    a, b = bt.split(closes, 0.6)
    assert len(a) == 60 and len(b) == 40 and a.index[-1] < b.index[0]
