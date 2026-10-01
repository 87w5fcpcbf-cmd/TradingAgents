import datetime as dt

import numpy as np
import pandas as pd
import pytest

from jevbot import cli
from jevbot.data import save_csv_dir
from jevbot.rules import CANDIDATES, SYMBOLS
from jevbot.strategy import load

ALL = sorted({s for v in SYMBOLS.values() for s in v})


def make_data(tmp_path, years=7, seed=1, drift=0.0004, vol=0.01):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2019-01-01", periods=int(252 * years))
    df = pd.DataFrame({s: 100 * np.exp(np.cumsum(rng.normal(drift, vol, len(idx)))) for s in ALL}, index=idx)
    d = tmp_path / "data"
    save_csv_dir(df, str(d))
    return str(d)


def test_backtest_runs_all_candidates_and_reports(tmp_path):
    d = make_data(tmp_path)
    rows, winner = cli.backtest_cmd(d, str(tmp_path / "strategy.md"), max_dd=0.5, min_trades=1, min_pf=0.0)
    assert {r["name"] for r in rows} == set(CANDIDATES)
    assert all("oos_return" in r and "passed" in r for r in rows)


def test_backtest_writes_validated_strategy_for_a_survivor(tmp_path):
    d = make_data(tmp_path)
    out = tmp_path / "strategy.md"
    rows, winner = cli.backtest_cmd(d, str(out), max_dd=0.9, min_trades=1, min_pf=0.0)
    assert winner is not None and winner != "leveraged_trend"     # never auto-select the leveraged one
    s = load(str(out))
    assert s.validated and s.name == winner


def test_no_survivor_writes_nothing_and_says_so(tmp_path):
    d = make_data(tmp_path)
    out = tmp_path / "strategy.md"
    rows, winner = cli.backtest_cmd(d, str(out), max_dd=0.0001, min_trades=10_000, min_pf=99)
    assert winner is None and not out.exists()
    assert all(not r["passed"] and r["reasons"] for r in rows)


def test_too_little_history_rejected(tmp_path):
    d = make_data(tmp_path, years=2)
    with pytest.raises(cli.CliError):
        cli.backtest_cmd(d, str(tmp_path / "s.md"))


def test_missing_data_candidates_are_skipped_not_crashed(tmp_path):
    d = make_data(tmp_path)
    import os
    os.remove(os.path.join(d, "XLK.csv"))
    rows, _ = cli.backtest_cmd(d, str(tmp_path / "s.md"), max_dd=0.9, min_trades=1, min_pf=0.0)
    sr = [r for r in rows if r["name"] == "sector_rotation"][0]
    assert not sr["passed"] and "missing" in sr["reasons"][0]


def test_run_refuses_unvalidated_strategy(tmp_path):
    from jevbot.strategy import Strategy, render
    s = Strategy(name="trend_ma", symbols=["SPY"], timeframe="1D", entry="e", exit="x", stop_loss_pct=0.05,
                 take_profit_pct=0.1, jev_thresholds={"regime": 0.8, "headline": 0.8, "buying_pressure": 0.8})
    p = tmp_path / "strategy.md"
    p.write_text(render(s))
    with pytest.raises(cli.CliError) as e:
        cli.check_runnable(str(p), allow_unvalidated=False)
    assert "unvalidated" in str(e.value).lower()
    assert cli.check_runnable(str(p), allow_unvalidated=True).name == "trend_ma"


def test_due_for_tick_only_weekdays_after_close_once_per_day():
    et = lambda y, m, d, h, mi: dt.datetime(y, m, d, h, mi, tzinfo=cli.ET)
    assert cli.due_for_tick(et(2026, 10, 1, 16, 30), None)                       # Thursday after close
    assert not cli.due_for_tick(et(2026, 10, 1, 15, 0), None)                     # before close
    assert not cli.due_for_tick(et(2026, 10, 3, 17, 0), None)                     # Saturday
    assert not cli.due_for_tick(et(2026, 10, 1, 17, 0), dt.date(2026, 10, 1))     # already done today
