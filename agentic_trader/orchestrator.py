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
import re
import logging
from datetime import datetime
from zoneinfo import ZoneInfo
import yaml

from risk_guard import RiskGuard, Order, AccountState
from robinhood_executor import build_executor, PaperExecutor
import prompts

logger = logging.getLogger("agentic_trader.orchestrator")


# --------------------------------------------------------------------------- #
# Agent brain seam — TradingAgents integration
# --------------------------------------------------------------------------- #
# Wiring shape:
#   1. Build the TradingAgents graph ONCE, lazily (first decide() call). Keeps
#      module import + a closed-market cycle green and key-free.
#   2. Per watchlist symbol, run the multi-agent graph (fundamentals / sentiment
#      / news / technical -> bull/bear -> trader -> portfolio manager) for the
#      heavy analysis.
#   3. Run a FINAL structured portfolio-manager pass over the brain's reports
#      using prompts.portfolio_manager_prompt() (GLOBAL_GUARDRAILS + PM +
#      RISK_CONSTRAINTS + STRUCTURED_OUTPUT). That pass emits the strict
#      STRUCTURED_OUTPUT JSON — including the 0..1 conviction the loop gates on,
#      which the brain's native 5-tier rating does not provide.
#   4. Parse that JSON into Order objects. BUY size is the DELTA to the target
#      weight (not the raw target) so we never re-buy a position we already hold.
#
# This layer only sets *intent*. risk_guard.check_order() is the sole hard
# enforcement of every limit (CLAUDE.md: never move enforcement into prompts).
# Any brain/parse failure proposes nothing — we never trade on a broken signal.

_MODEL_ALIASES = {
    # config.yaml ships friendly names; map them to real catalog model ids.
    "claude": "claude-opus-4-8",
    "claude-haiku-or-equivalent": "claude-haiku-4-5",
}

_BRAIN = None  # cached TradingAgentsGraph (built on first decide() call)


def _resolve_model(name: str) -> str:
    return _MODEL_ALIASES.get((name or "").strip().lower(), name)


def _infer_provider(model: str) -> str:
    m = (model or "").lower()
    if "claude" in m:
        return "anthropic"
    if "gemini" in m:
        return "google"
    if m.startswith(("gpt", "o1", "o3", "o4")):
        return "openai"
    return "openai"


def _get_brain(cfg: dict):
    """Build the TradingAgents graph once, lazily. Raises if it cannot be built
    (e.g. missing API key); the caller treats that as 'propose nothing'."""
    global _BRAIN
    if _BRAIN is not None:
        return _BRAIN
    from tradingagents.default_config import DEFAULT_CONFIG
    from tradingagents.graph.trading_graph import TradingAgentsGraph

    deep = _resolve_model(cfg["llm"]["deep_model"])
    quick = _resolve_model(cfg["llm"]["shallow_model"])
    rounds = int(cfg["llm"].get("max_debate_rounds", 1))
    ta_cfg = {
        **DEFAULT_CONFIG,
        "llm_provider": _infer_provider(deep),
        "deep_think_llm": deep,
        "quick_think_llm": quick,
        "max_debate_rounds": rounds,
        "max_risk_discuss_rounds": rounds,
        "temperature": cfg["llm"].get("temperature"),
    }
    _BRAIN = TradingAgentsGraph(debug=False, config=ta_cfg)
    return _BRAIN


def _extract_json(text: str) -> dict | None:
    """Pull the first JSON object out of an LLM response (tolerates ``` fences)."""
    if not text:
        return None
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        raw = fenced.group(1)
    else:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            return None
        raw = text[start:end + 1]
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _account_brief(acct: AccountState) -> str:
    if not acct.positions:
        pos = "none"
    else:
        pos = "; ".join(
            f"{s}: {p.qty:.4f}sh @avg {p.avg_price:.2f}, last {p.last_price:.2f}, "
            f"{p.unrealized_pct:+.1%}"
            for s, p in acct.positions.items()
        )
    return (
        f"equity ${acct.equity:,.2f} | buying power ${acct.cash:,.2f} | "
        f"open positions ({len(acct.positions)}): {pos} | trades today {acct.trades_today}"
    )


def _synthesize(brain, sym: str, final_state: dict, acct: AccountState) -> dict | None:
    """Final calculated-aggressive PM pass -> strict STRUCTURED_OUTPUT JSON."""
    reports = "\n\n".join([
        f"## Market / Technical\n{final_state.get('market_report', '')}",
        f"## Sentiment\n{final_state.get('sentiment_report', '')}",
        f"## News / Macro\n{final_state.get('news_report', '')}",
        f"## Fundamentals\n{final_state.get('fundamentals_report', '')}",
        f"## Research-manager plan\n{final_state.get('investment_plan', '')}",
        f"## Trader proposal\n{final_state.get('trader_investment_plan', '')}",
        f"## Brain portfolio-manager decision\n{final_state.get('final_trade_decision', '')}",
    ])
    prompt = "\n\n".join([
        prompts.portfolio_manager_prompt(),
        prompts.STRATEGY_BRIEF,
        f"CURRENT BOOK: {_account_brief(acct)}",
        f"SYMBOL UNDER REVIEW: {sym}",
        "ANALYST + DEBATE OUTPUTS FROM THE BRAIN:\n" + reports,
        f"Emit the STRUCTURED_OUTPUT JSON for {sym} only.",
    ])
    resp = brain.deep_thinking_llm.invoke(prompt)
    text = resp if isinstance(resp, str) else getattr(resp, "content", str(resp))
    return _extract_json(text)


def _to_order(d: dict, acct: AccountState, cfg: dict, min_conv: float) -> Order | None:
    """Convert one STRUCTURED_OUTPUT decision into an Order (or None to skip)."""
    sym = str(d.get("symbol", "")).upper().strip()
    action = str(d.get("action", "HOLD")).upper().strip()
    if not sym or action == "HOLD":
        return None

    conviction = max(0.0, min(1.0, float(d.get("conviction", 0.0) or 0.0)))
    entry = float(d.get("entry_price", 0.0) or 0.0)   # 0.0 -> orchestrator uses last
    rationale = str(d.get("rationale", ""))[:300]
    existing = acct.positions[sym].market_value if sym in acct.positions else 0.0

    if action == "BUY":
        if conviction < min_conv:
            return None  # below entry threshold (orchestrator + guard re-check too)
        # cap the *intent* at the configured concentration; risk_guard enforces hard.
        target_pct = min(
            float(d.get("target_position_pct", 0.0) or 0.0),
            float(cfg["risk"]["max_position_pct_of_account"]),
        )
        delta = target_pct * acct.equity - existing      # only add the shortfall
        if delta <= float(cfg["risk"]["min_order_notional_usd"]):
            return None  # already at/above target weight
        return Order(sym, "buy", round(delta, 2), entry, rationale, conviction)

    if action == "SELL":
        if existing <= 0:
            return None  # nothing to sell
        return Order(sym, "sell", round(existing, 2), entry, rationale, conviction)

    return None


def decide(symbols: list[str], acct: AccountState, cfg: dict) -> list[Order]:
    """Ask the TradingAgents brain for trade intentions, as gated Orders.

    Never raises: on any failure (no API key, network, unparseable output) it
    logs and returns the orders gathered so far — proposing nothing rather than
    trading on a broken signal. Hard limits are enforced later by risk_guard.
    """
    try:
        brain = _get_brain(cfg)
    except Exception as e:
        logger.warning("brain unavailable (%r) — proposing no trades this cycle", e)
        return []

    today = datetime.now().date().isoformat()
    min_conv = float(cfg["strategy"]["min_conviction_to_enter"])
    orders: list[Order] = []

    for sym in symbols:
        try:
            final_state, _rating = brain.propagate(sym, today)
            decision = _synthesize(brain, sym, final_state, acct)
        except Exception as e:
            logger.warning("brain failed on %s (%r) — skipping", sym, e)
            continue
        if not decision:
            logger.warning("no parseable decision for %s — skipping", sym)
            continue
        for d in decision.get("decisions", []):
            order = _to_order(d, acct, cfg, min_conv)
            if order is not None:
                orders.append(order)

    return orders


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
