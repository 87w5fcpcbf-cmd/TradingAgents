"""
orchestrator.py — the always-on ReAct loop.

Per cycle, during market hours:
  1. mark positions to market, refresh state
  2. run protective exits (hard/trailing stops) FIRST — capital preservation
  3. ask the agent brain (TradingAgents) for new trade intentions
  4. route EVERY intention through RiskGuard.check_order()
  5. execute survivors via the broker adapter
  6. log everything (decisions + fills + full prompts if enabled)

The agent brain is pluggable. `decide()` shows the integration seam for
TradingAgents (see README). A stub is provided so the loop runs end-to-end in
paper mode before you wire the real graph in.
"""

from __future__ import annotations
import time
import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo
import yaml

from risk_guard import RiskGuard, Order, AccountState
from robinhood_executor import build_executor, PaperExecutor


# --------------------------------------------------------------------------- #
# Agent brain seam
# --------------------------------------------------------------------------- #
def decide(symbols: list[str], acct: AccountState, cfg: dict) -> list[Order]:
    """
    Return a list of proposed Orders (entries/adds). REPLACE THIS with a call
    into TradingAgents' graph. Typical wiring:

        from tradingagents.graph.trading_graph import TradingAgentsGraph
        from tradingagents.default_config import DEFAULT_CONFIG
        ta_cfg = {**DEFAULT_CONFIG,
                  "deep_think_llm": cfg["llm"]["deep_model"],
                  "quick_think_llm": cfg["llm"]["shallow_model"],
                  "max_debate_rounds": cfg["llm"]["max_debate_rounds"]}
        graph = TradingAgentsGraph(debug=False, config=ta_cfg)
        for sym in symbols:
            state, decision = graph.propagate(sym, datetime.now().date().isoformat())
            # decision -> {action: BUY/SELL/HOLD, conviction, target_pct, rationale}
            # convert to Order(...) and append

    Feed the prompts from prompts.py into the graph's agents so it trades the
    calculated-aggressive posture. The portfolio-manager prompt must emit
    structured JSON (see prompts.STRUCTURED_OUTPUT) so this layer can parse it.

    The stub below proposes nothing (safe no-op) so paper runs are clean.
    """
    return []


# --------------------------------------------------------------------------- #
# Loop
# --------------------------------------------------------------------------- #
class Orchestrator:
    def __init__(self, config_path="config.yaml", mcp_client=None,
                 watchlist=None, decide_fn=decide):
        with open(config_path) as f:
            self.cfg = yaml.safe_load(f)
        self.guard = RiskGuard(config_path)
        self.exe = build_executor(self.cfg, mcp_client)
        self.tz = ZoneInfo(self.cfg["schedule"]["timezone"])
        self.watchlist = watchlist or ["NVDA", "AMD", "PLTR", "TSLA", "META"]
        self.decide_fn = decide_fn
        os.makedirs(os.path.dirname(self.cfg["logging"]["decision_log_path"]), exist_ok=True)

    def _log(self, path_key: str, record: dict):
        record["ts"] = datetime.now(self.tz).isoformat()
        with open(self.cfg["logging"][path_key], "a") as f:
            f.write(json.dumps(record) + "\n")

    def _execute(self, order: Order, acct: AccountState):
        last, last_close, age = self.exe.get_quote(order.symbol)
        order.limit_price = order.limit_price or last
        decision = self.guard.check_order(order, acct, last_close, age)
        self._log("decision_log_path", {
            "symbol": order.symbol, "side": order.side,
            "requested_notional": order.notional_usd, "conviction": order.conviction,
            "reason": order.reason, "allowed": decision.allowed,
            "guard_reason": decision.reason,
        })
        if not decision.allowed:
            print(f"  ✗ {order.side} {order.symbol}: {decision.reason}")
            return
        fill = self.exe.place_order(decision.order)
        if fill:
            self._log("trade_log_path", {
                "symbol": fill.symbol, "side": fill.side, "qty": fill.qty,
                "price": fill.price, "notional": fill.notional,
            })
            print(f"  ✓ {fill.side} {fill.symbol} {fill.qty:.4f} @ {fill.price:.2f}")

    def run_cycle(self):
        if isinstance(self.exe, PaperExecutor):
            self.exe.mark_to_market()
        acct = self.exe.get_account()

        # hard catastrophic / daily checks surface immediately
        halt = self.guard.catastrophic_halt(acct) or self.guard.daily_paused(acct)
        if halt:
            print(f"[{datetime.now(self.tz):%H:%M}] HALTED: {halt}")
            return

        if not self.guard.market_open_now():
            print(f"[{datetime.now(self.tz):%H:%M}] market closed — idle")
            return

        # 1) protective exits FIRST
        for ex in self.guard.stop_loss_exits(acct):
            print(f"  ! protective exit: {ex.symbol} ({ex.reason})")
            self._execute(ex, acct)
            acct = self.exe.get_account()

        # 2) new intentions, gated
        for order in self.decide_fn(self.watchlist, acct, self.cfg):
            if order.conviction < self.cfg["strategy"]["min_conviction_to_enter"] and order.side == "buy":
                print(f"  – skip {order.symbol}: conviction {order.conviction} < threshold")
                continue
            self._execute(order, acct)
            acct = self.exe.get_account()

        eq = acct.equity
        print(f"[{datetime.now(self.tz):%H:%M}] equity ${eq:,.2f} | "
              f"positions {len(acct.positions)} | trades {acct.trades_today}")

    def run_forever(self):
        interval = self.cfg["schedule"]["poll_interval_minutes"] * 60
        print(f"Agentic Trader running in {self.cfg['mode']['execution_mode'].upper()} mode. Ctrl-C to stop.")
        while True:
            try:
                self.run_cycle()
            except Exception as e:
                # never let a transient error crash the loop or trade blindly
                print(f"  [error] {e!r} — skipping cycle")
            time.sleep(interval)


if __name__ == "__main__":
    # Paper dry-run with the safe no-op brain: proves scheduler + guard + exec wiring.
    orch = Orchestrator()
    orch.run_cycle()
    print("Single paper cycle complete. Wire decide() to TradingAgents, then run_forever().")
