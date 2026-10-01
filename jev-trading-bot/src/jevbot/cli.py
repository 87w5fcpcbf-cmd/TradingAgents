"""Command line: fetch-data, backtest, run, report, status, kill, reset, dashboard."""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
import time
from zoneinfo import ZoneInfo

import requests

from . import backtest as bt
from .alerts import Alerts
from .broker import Broker, BrokerError
from .config import Config
from .data import DataError, alpaca_bars, load_csv_dir, save_csv_dir
from .db import Db
from .report import daily
from .risk import Approvals, Risk
from .rules import CANDIDATES, LEVERAGED, SECTORS, SYMBOLS
from .runner import Deps, process_approvals, sync_trades, tick
from .strategy import Strategy, StrategyError, load, render

ET = ZoneInfo("America/New_York")
MIN_ROWS = 252 * 5  # at least five years of daily bars

DEFAULTS: dict[str, dict] = {
    "trend_ma": dict(symbols=["SPY"], entry="SPY 50-day SMA above 200-day SMA", exit="50-day SMA falls below 200-day SMA", stop=0.05, tp=0.20),
    "rsi2": dict(symbols=["SPY"], entry="RSI(2) < 10 while close > 200-day SMA", exit="RSI(2) > 70", stop=0.05, tp=0.10),
    "breakout20": dict(symbols=["SPY"], entry="close above the prior 20-day high", exit="close below the prior 10-day low", stop=0.06, tp=0.20),
    "sector_rotation": dict(symbols=SECTORS, entry="top 3 of 11 sector ETFs by 6-month return (monthly), only while SPY > 200-day SMA",
                            exit="dropped from the top 3 at month end, or SPY < 200-day SMA", stop=0.08, tp=0.30),
    "dual_momentum": dict(symbols=["SPY", "EFA", "AGG"], entry="best of SPY/EFA by 12-month return if positive, else AGG (monthly)",
                          exit="no longer the monthly pick", stop=0.08, tp=0.30),
    "leveraged_trend": dict(symbols=["TQQQ"], entry="TQQQ while QQQ > 200-day SMA", exit="QQQ falls below 200-day SMA", stop=0.10, tp=0.40),
}


class CliError(Exception):
    pass


def backtest_cmd(data_dir: str, out: str, max_dd: float = 0.15, min_trades: int = 30, min_pf: float = 1.3,
                 min_win_rate: float = 0.0, lev_max_dd: float = 0.5, split_frac: float = 0.6,
                 cost_bps: float = 5.0, allow_leveraged: bool = False):
    """Evaluate every candidate out-of-sample; write strategy.md for the best survivor (if any)."""
    rows, results = [], {}
    for name, fn in CANDIDATES.items():
        need = SYMBOLS[name]
        try:
            closes = load_csv_dir(data_dir, need)[need].dropna()
        except DataError as e:
            rows.append(dict(name=name, passed=False, reasons=[f"missing data: {e}"], oos_return=float("nan"))); continue
        if len(closes) < MIN_ROWS:
            raise CliError(f"{name}: only {len(closes)} daily bars; need at least {MIN_ROWS} (5 years)")
        w = fn(closes)
        cut = int(len(closes) * split_frac)
        is_r = bt.run(closes.iloc[:cut], w.iloc[:cut], cost_bps)
        oos = bt.run(closes.iloc[cut:], w.iloc[cut:], cost_bps)
        cap = lev_max_dd if name in LEVERAGED else max_dd
        ok, reasons = bt.evaluate(oos, cap, min_trades, min_pf, min_win_rate)
        results[name] = (oos, closes.index[cut], closes.index[-1])
        rows.append(dict(name=name, passed=ok, reasons=reasons, is_return=is_r.total_return, oos_return=oos.total_return,
                         oos_dd=oos.max_drawdown, oos_pf=oos.profit_factor, oos_trades=oos.n_trades, oos_win=oos.win_rate))
    eligible = [r for r in rows if r["passed"] and (allow_leveraged or r["name"] not in LEVERAGED)]
    winner = max(eligible, key=lambda r: r["oos_return"] / max(r["oos_dd"], 0.01))["name"] if eligible else None
    if winner:
        oos, start, end = results[winner]
        d = DEFAULTS[winner]
        s = Strategy(name=winner, symbols=d["symbols"], timeframe="1D", entry=d["entry"], exit=d["exit"],
                     stop_loss_pct=d["stop"], take_profit_pct=d["tp"], validated=True,
                     jev_thresholds={"regime": 0.8, "headline": 0.8, "buying_pressure": 0.8})
        stats = {"out_of_sample_period": f"{start.date()} to {end.date()}", "total_return": f"{oos.total_return:.1%}",
                 "max_drawdown": f"{oos.max_drawdown:.1%}", "profit_factor": f"{oos.profit_factor:.2f}",
                 "win_rate": f"{oos.win_rate:.1%}", "trades": oos.n_trades, "cost_bps_per_side": cost_bps,
                 "note": "no parameters were fitted; in-sample is shown for reference only"}
        with open(out, "w", encoding="utf-8") as f:
            f.write(render(s, stats))
    return rows, winner


def check_runnable(path: str, allow_unvalidated: bool) -> Strategy:
    try:
        s = load(path)
    except StrategyError as e:
        raise CliError(str(e)) from None
    if not s.validated and not allow_unvalidated:
        raise CliError("strategy.md is unvalidated. Run `jevbot backtest` on real data first, "
                       "or pass --allow-unvalidated to paper trade it anyway.")
    return s


def due_for_tick(now_et: dt.datetime, last_done: dt.date | None) -> bool:
    """Once per weekday, after the 16:15 ET close."""
    if now_et.weekday() >= 5 or last_done == now_et.date():
        return False
    return (now_et.hour, now_et.minute) >= (16, 15)


def _print_table(rows: list[dict]) -> None:
    print(f"{'candidate':18} {'pass':5} {'IS ret':>8} {'OOS ret':>8} {'OOS dd':>7} {'PF':>6} {'trades':>6} {'win':>6}")
    for r in rows:
        if "oos_dd" not in r:
            print(f"{r['name']:18} {'no':5} {'-':>8} {'-':>8}  {'; '.join(r['reasons'])}"); continue
        print(f"{r['name']:18} {'YES' if r['passed'] else 'no':5} {r['is_return']:8.1%} {r['oos_return']:8.1%} "
              f"{r['oos_dd']:7.1%} {r['oos_pf']:6.2f} {r['oos_trades']:6d} {r['oos_win']:6.1%}"
              + ("" if r["passed"] else "   <- " + "; ".join(r["reasons"])))


def _build_deps(args) -> Deps:
    cfg = Config.from_env()
    db = Db(cfg.db_path)
    risk, ap = Risk(db, cfg), Approvals(db, cfg)
    broker = Broker()
    strategy = check_runnable(args.strategy, args.allow_unvalidated)

    def status() -> str:
        a = broker.account()
        k = f"TRIPPED ({risk.kill_reason()})" if risk.is_killed() else "ok"
        return f"equity ${a['equity']:,.2f} | kill switch {k} | pending approvals {len(db.pending_approvals())}"

    alerts = Alerts(db, risk, ap, status_fn=status)

    def data_fn(syms):
        end = dt.date.today()
        return alpaca_bars(syms, str(end - dt.timedelta(days=500)), str(end))

    return Deps(db=db, cfg=cfg, risk=risk, approvals=ap, alerts=alerts, broker=broker, strategy=strategy, data_fn=data_fn)


def _heartbeat() -> None:
    """Ping an external uptime monitor (e.g. healthchecks.io) so a silent Pi gets noticed."""
    url = os.environ.get("HEALTHCHECK_URL")
    if url:
        try:
            requests.get(url, timeout=10)
        except requests.RequestException:
            pass  # the monitor itself raises the alarm when pings stop


def run_loop(d: Deps, interval_s: int = 15) -> None:
    last_done: dt.date | None = None
    last_beat = 0.0
    d.alerts.send(f"jevbot started (PAPER). Strategy {d.strategy.name}, validated={d.strategy.validated}.")
    while True:
        try:
            d.alerts.poll()
            sync_trades(d)
            process_approvals(d)
            now = dt.datetime.now(ET)
            if due_for_tick(now, last_done) and d.broker.is_session_today(now.date().isoformat()):
                res = tick(d)
                if res["status"] in ("ok", "killed"):
                    last_done = now.date()
                    d.alerts.send(daily(d.db))
            elif due_for_tick(now, last_done):
                last_done = now.date()  # holiday: nothing to do today
        except BrokerError as e:
            d.risk.record_broker_error()
            d.alerts.send(f"ERROR in main loop: {e}")
        except Exception as e:  # never die silently: alert and keep the safety stops in place
            d.alerts.send(f"ERROR in main loop: {type(e).__name__}: {e}")
        if time.monotonic() - last_beat > 300:
            _heartbeat()
            last_beat = time.monotonic()
        time.sleep(interval_s)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="jevbot", description="Approval-gated paper-trading bot. Not financial advice.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch-data", help="download daily bars from Alpaca into data_cache/")
    f.add_argument("--years", type=int, default=7); f.add_argument("--dir", default="data_cache")
    b = sub.add_parser("backtest", help="evaluate all candidates out-of-sample and write strategy.md")
    b.add_argument("--dir", default="data_cache"); b.add_argument("--out", default="strategy.md")
    b.add_argument("--max-dd", type=float, default=0.15); b.add_argument("--min-trades", type=int, default=30)
    b.add_argument("--min-pf", type=float, default=1.3); b.add_argument("--allow-leveraged", action="store_true")
    r = sub.add_parser("run", help="run the paper-trading bot")
    r.add_argument("--strategy", default="strategy.md"); r.add_argument("--allow-unvalidated", action="store_true")
    sub.add_parser("report"); sub.add_parser("status"); sub.add_parser("kill"); sub.add_parser("reset")
    d = sub.add_parser("dashboard"); d.add_argument("--host", default="127.0.0.1"); d.add_argument("--port", type=int, default=8000)
    a = ap.parse_args(argv)
    try:
        if a.cmd == "fetch-data":
            end = dt.date.today()
            syms = sorted({s for v in SYMBOLS.values() for s in v})
            df = alpaca_bars(syms, str(end - dt.timedelta(days=365 * a.years + 30)), str(end))
            save_csv_dir(df, a.dir)
            print(f"saved {len(df.columns)} symbols, {len(df)} bars to {a.dir}")
        elif a.cmd == "backtest":
            rows, winner = backtest_cmd(a.dir, a.out, a.max_dd, a.min_trades, a.min_pf, allow_leveraged=a.allow_leveraged)
            _print_table(rows)
            print(f"\nWinner: {winner}; wrote {a.out}" if winner else "\nNo candidate survived the filter. Nothing written. Do not trade.")
        elif a.cmd == "run":
            run_loop(_build_deps(a))
        else:
            cfg = Config.from_env(); db = Db(cfg.db_path); risk = Risk(db, cfg)
            if a.cmd == "report": print(daily(db))
            elif a.cmd == "status": print(f"kill switch: {'TRIPPED: ' + str(risk.kill_reason()) if risk.is_killed() else 'ok'}")
            elif a.cmd == "kill": risk.trip("manual `jevbot kill`"); print("kill switch TRIPPED")
            elif a.cmd == "reset": risk.reset(); print("kill switch reset")
            elif a.cmd == "dashboard":
                import uvicorn
                from .dashboard import create_app
                uvicorn.run(create_app(db, cfg), host=a.host, port=a.port)
    except (CliError, DataError, BrokerError, StrategyError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
