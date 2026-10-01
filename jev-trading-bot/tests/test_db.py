from jevbot.db import Db


def test_flags_default_false_and_roundtrip(tmp_path):
    db = Db(str(tmp_path / "t.db"))
    assert db.get_flag("kill") is False
    db.set_flag("kill", True, "manual")
    assert db.get_flag("kill") is True
    assert db.flag_reason("kill") == "manual"
    db.set_flag("kill", False)
    assert db.get_flag("kill") is False


def test_signal_trade_jev_rows_persist(tmp_path):
    p = str(tmp_path / "t.db")
    db = Db(p)
    db.log_signal({"symbol": "SPY", "direction": 1, "fired": True, "probabilities": {"a": 0.9}, "result": "filled"})
    db.log_trade({"symbol": "SPY", "qty": 5, "price": 500.0, "pnl": 12.5})
    db.log_jev({"model": "jev-1.13.0", "latency_s": 0.4, "cost_usd": 0.001, "ok": True})
    db2 = Db(p)  # reopen: persisted
    assert db2.signals()[0]["symbol"] == "SPY"
    assert db2.signals()[0]["probabilities"] == {"a": 0.9}
    assert db2.trades()[0]["pnl"] == 12.5
    assert db2.jev_calls()[0]["model"] == "jev-1.13.0"
