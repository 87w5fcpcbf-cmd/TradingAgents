import pytest

from jevbot.config import Config
from jevbot.strategy import Strategy, StrategyError, load, render

GOOD = dict(name="trend_ma", symbols=["SPY"], timeframe="1D", entry="50d SMA > 200d SMA",
            exit="50d SMA < 200d SMA", stop_loss_pct=0.05, take_profit_pct=0.15,
            jev_thresholds={"regime": 0.8, "headline": 0.8, "buying_pressure": 0.8})


def test_roundtrip(tmp_path):
    s = Strategy(**GOOD)
    p = tmp_path / "strategy.md"
    p.write_text(render(s, {"max_drawdown": 0.09, "profit_factor": 1.6}))
    s2 = load(str(p))
    assert s2 == s
    assert "max_drawdown" in p.read_text()


@pytest.mark.parametrize("bad", [
    {"jev_thresholds": {"regime": 1.2, "headline": 0.8, "buying_pressure": 0.8}},
    {"jev_thresholds": {"regime": 0.0, "headline": 0.8, "buying_pressure": 0.8}},
    {"jev_thresholds": {"regime": 0.8}},                 # missing questions
    {"stop_loss_pct": 0},
    {"stop_loss_pct": 1.5},
    {"take_profit_pct": -0.1},
    {"name": "nonexistent"},
    {"symbols": []},
])
def test_invalid_rejected(bad):
    with pytest.raises(StrategyError):
        Strategy(**{**GOOD, **bad})


def test_missing_or_garbled_file(tmp_path):
    with pytest.raises(StrategyError):
        load(str(tmp_path / "nope.md"))
    p = tmp_path / "s.md"
    p.write_text("# no json block here")
    with pytest.raises(StrategyError):
        load(str(p))
    p.write_text("```json\n{not json}\n```")
    with pytest.raises(StrategyError):
        load(str(p))


def test_leveraged_lowers_position_cap():
    cfg = Config()
    assert Strategy(**GOOD).position_cap(cfg) == 0.10
    lev = Strategy(**{**GOOD, "name": "leveraged_trend", "symbols": ["QQQ", "TQQQ"]})
    assert lev.leveraged and lev.position_cap(cfg) == 0.03
