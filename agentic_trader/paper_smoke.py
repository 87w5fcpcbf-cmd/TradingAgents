"""
paper_smoke.py — end-to-end PAPER wiring smoke test (deterministic, offline).

Runs ONE real orchestrator cycle through the real RiskGuard + PaperExecutor +
StateStore + JSON logging, using a deterministic STUB brain and STUB quotes in
place of the live LLM brain and yfinance. Use it to confirm the plumbing —
right sign, right size, conviction gating, guard clamping, logs written —
without needing LLM API keys, network, or market hours.

This is NOT the real brain. A real paper run additionally needs:
  • an LLM API key (e.g. ANTHROPIC_API_KEY) + network to the provider,
  • network access to Yahoo Finance for yfinance quotes / analyst data,
  • the run to happen during market hours (the loop gates on it).

Nothing here touches live trading: execution_mode stays "paper".
"""

from __future__ import annotations

import os

from orchestrator import Orchestrator
from risk_guard import Order


def stub_decide(symbols, acct, cfg):
    """What decide() would return after parsing the PM's STRUCTURED_OUTPUT JSON.

    Three intentions that exercise the integration path:
      • NVDA — in-cap buy that should fill,
      • AMD  — below min_conviction_to_enter, should be gated out,
      • TSLA — oversized (50% target) that risk_guard should clamp to 30%.
    """
    return [
        Order("NVDA", "buy", 0.25 * acct.equity, 130.0, "breakout + catalyst (stub)", 0.82),
        Order("AMD",  "buy", 0.20 * acct.equity, 160.0, "thin thesis (stub)",         0.40),
        Order("TSLA", "buy", 0.50 * acct.equity, 240.0, "high conviction, oversized", 0.90),
    ]


_STUB_PX = {
    "NVDA": (130.0, 129.0, 30.0),
    "AMD":  (160.0, 159.0, 30.0),
    "TSLA": (240.0, 238.0, 30.0),
}


def stub_quote(symbol: str):
    return _STUB_PX.get(symbol, (100.0, 100.0, 30.0))


def main():
    # Start from a clean slate so the demo output is deterministic.
    for p in ("logs/decisions.jsonl", "logs/trades.jsonl", "state.json"):
        if os.path.exists(p):
            os.remove(p)

    orch = Orchestrator(
        config_path="config.yaml",
        watchlist=["NVDA", "AMD", "TSLA"],
        decide_fn=stub_decide,
    )
    # Offline substitutes for the two blocked externals.
    orch.exe._quote_fn = stub_quote                       # yfinance is unreachable here
    orch.guard.market_open_now = lambda now=None: True    # bypass the clock for the demo

    print("=== one paper cycle (stub brain + stub quotes; execution_mode=paper) ===")
    orch.run_cycle()

    for label, path in (
        ("logs/decisions.jsonl", "logs/decisions.jsonl"),
        ("logs/trades.jsonl", "logs/trades.jsonl"),
        ("state.json", "state.json"),
    ):
        print(f"\n=== {label} ===")
        try:
            print(open(path).read().strip() or "(empty)")
        except FileNotFoundError:
            print("(not written)")


if __name__ == "__main__":
    main()
