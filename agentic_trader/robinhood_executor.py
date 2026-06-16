"""
robinhood_executor.py — broker adapter.

Two implementations behind one interface:
  • PaperExecutor  — simulated fills, zero real orders. For wiring validation.
  • LiveExecutor   — places real orders via the Robinhood Trading MCP.

IMPORTANT HONESTY NOTE
----------------------
Robinhood's Agentic Trading MCP went live in late May 2026 and the exact
tool names / argument schemas are NOT fully published yet (Robinhood has said
it will add more tools over time). So the LiveExecutor below is written against
the *documented capabilities* (read accounts, read positions, quote, place
order with order types, cancel) with clearly marked TODOs where you must drop
in the exact tool names from the official docs:

    https://robinhood.com/us/en/support/articles/agentic-trading-overview/
    https://robinhood.com/us/en/support/agentic-trading   (API docs / MCP URL)

You connect by pointing an MCP client at Robinhood's Trading MCP server URL
and authenticating per their flow. Any MCP-capable client works; below uses a
generic async MCP client call. Do NOT hardcode credentials — load from env.
"""

from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime
import os
import json
import time
from risk_guard import Order, Position, AccountState


@dataclass
class Fill:
    symbol: str
    side: str
    qty: float
    price: float
    notional: float
    ts: datetime


# --------------------------------------------------------------------------- #
# Real market data (paper mode marks to market against live prices)
# --------------------------------------------------------------------------- #
_QUOTE_TTL_SECONDS = 15
_quote_cache: dict[str, tuple[float, tuple[float, float, float]]] = {}


def yfinance_quote(symbol: str) -> tuple[float, float, float]:
    """Return ``(last_price, previous_close, data_age_seconds)`` from yfinance.

    Replaces the constant placeholder so the PaperExecutor marks positions to
    *live* prices. ``previous_close`` feeds risk_guard's price-deviation check
    and ``data_age_seconds`` feeds its staleness check, so the same guard rails
    apply in paper as in live.

    Resilient by design: tries intraday (for a fresh price + a real timestamp),
    then ``fast_info``, then daily bars. Raises ``RuntimeError`` only when no
    usable price can be found — callers skip the symbol rather than act on a
    zero/garbage mark. Results are cached briefly to avoid hammering Yahoo when
    a single cycle quotes the same symbol several times.
    """
    now_mono = time.monotonic()
    cached = _quote_cache.get(symbol)
    if cached and now_mono - cached[0] < _QUOTE_TTL_SECONDS:
        return cached[1]

    import pandas as pd
    import yfinance as yf

    t = yf.Ticker(symbol)
    last: float | None = None
    prev_close: float | None = None
    age = float("inf")

    def _age_from(ts) -> float:
        now = pd.Timestamp.now(tz=ts.tz) if ts.tz is not None else pd.Timestamp.utcnow()
        return float((now - ts).total_seconds())

    # 1) Intraday: freshest price and a real timestamp to age it.
    try:
        intraday = t.history(period="1d", interval="1m", auto_adjust=False)
        if not intraday.empty:
            last = float(intraday["Close"].iloc[-1])
            age = _age_from(intraday.index[-1])
    except Exception:
        pass

    # 2) fast_info: previous close (+ last-price fallback).
    try:
        fi = t.fast_info
        pc = fi.get("previous_close") if isinstance(fi, dict) else getattr(fi, "previous_close", None)
        if pc:
            prev_close = float(pc)
        if last is None:
            lp = fi.get("last_price") if isinstance(fi, dict) else getattr(fi, "last_price", None)
            if lp:
                last = float(lp)
    except Exception:
        pass

    # 3) Daily fallback for anything still missing.
    if last is None or prev_close is None:
        try:
            daily = t.history(period="5d", auto_adjust=False)
            if not daily.empty:
                if last is None:
                    last = float(daily["Close"].iloc[-1])
                if prev_close is None:
                    prev_close = float(daily["Close"].iloc[-2]) if len(daily) >= 2 else last
                if age == float("inf"):
                    age = _age_from(daily.index[-1])
        except Exception:
            pass

    if last is None or last <= 0:
        raise RuntimeError(f"no usable quote for {symbol}")
    if prev_close is None or prev_close <= 0:
        prev_close = last  # neutral: no deviation signal beats a false one

    result = (last, prev_close, age)
    _quote_cache[symbol] = (now_mono, result)
    return result


class BrokerExecutor:
    """Interface. Both paper and live implement these."""
    def get_account(self) -> AccountState: ...
    def get_quote(self, symbol: str) -> tuple[float, float, float]:
        """returns (last_price, last_close, data_age_seconds)"""
        ...
    def place_order(self, order: Order) -> Fill | None: ...
    def cancel_all(self) -> None: ...


# --------------------------------------------------------------------------- #
# Paper executor — start here
# --------------------------------------------------------------------------- #
class PaperExecutor(BrokerExecutor):
    def __init__(self, starting_cash: float, quote_fn=None):
        self.cash = starting_cash
        self.peak = starting_cash
        self.day_start = starting_cash
        self.positions: dict[str, Position] = {}
        self.trades_today = 0
        self.orders_per_symbol: dict[str, int] = {}
        self.last_order_time = None
        # quote_fn(symbol)->(last, last_close, age). Plug a real data source in.
        self._quote_fn = quote_fn or (lambda s: (100.0, 100.0, 1.0))

    def _equity(self) -> float:
        return self.cash + sum(p.market_value for p in self.positions.values())

    def get_account(self) -> AccountState:
        eq = self._equity()
        self.peak = max(self.peak, eq)
        return AccountState(
            equity=eq, cash=self.cash, peak_equity=self.peak,
            day_start_equity=self.day_start, positions=self.positions,
            trades_today=self.trades_today,
            orders_per_symbol_today=self.orders_per_symbol,
            last_order_time=self.last_order_time,
        )

    def get_quote(self, symbol: str):
        return self._quote_fn(symbol)

    def place_order(self, order: Order) -> Fill | None:
        last, _, _ = self.get_quote(order.symbol)
        px = last
        qty = order.notional_usd / px
        if order.side == "buy":
            self.cash -= order.notional_usd
            if order.symbol in self.positions:
                p = self.positions[order.symbol]
                new_qty = p.qty + qty
                p.avg_price = (p.avg_price * p.qty + px * qty) / new_qty
                p.qty = new_qty
                p.last_price = px
                p.high_water_price = max(p.high_water_price, px)
            else:
                self.positions[order.symbol] = Position(order.symbol, qty, px, px, px)
        else:  # sell (full close at market for simplicity)
            if order.symbol in self.positions:
                p = self.positions.pop(order.symbol)
                self.cash += p.qty * px
                qty = p.qty
        self.trades_today += 1
        self.orders_per_symbol[order.symbol] = self.orders_per_symbol.get(order.symbol, 0) + 1
        self.last_order_time = datetime.now()
        return Fill(order.symbol, order.side, qty, px, qty * px, datetime.now())

    def mark_to_market(self):
        """Refresh last/high-water for open positions (call each cycle)."""
        for p in self.positions.values():
            try:
                last, _, _ = self.get_quote(p.symbol)
            except Exception:
                continue  # transient fetch failure: keep the last good mark
            p.last_price = last
            p.high_water_price = max(p.high_water_price, last)

    def cancel_all(self):
        pass


# --------------------------------------------------------------------------- #
# Live executor — Robinhood Trading MCP
# --------------------------------------------------------------------------- #
class LiveExecutor(BrokerExecutor):
    def __init__(self, mcp_client):
        """
        mcp_client: an initialized MCP client already connected & authenticated
        to the Robinhood Trading MCP server. See README for connection setup.
        """
        self.mcp = mcp_client

    # ---- TODO: replace tool names with the exact ones from Robinhood docs ---
    def get_account(self) -> AccountState:
        # TODO tool name, e.g. "get_account" / "get_buying_power"
        acct = self.mcp.call_tool("get_account", {})              # <-- VERIFY NAME
        positions_raw = self.mcp.call_tool("get_positions", {})   # <-- VERIFY NAME
        positions = {}
        for r in positions_raw.get("positions", []):
            positions[r["symbol"]] = Position(
                symbol=r["symbol"], qty=float(r["quantity"]),
                avg_price=float(r["average_buy_price"]),
                last_price=float(r["last_price"]),
                high_water_price=float(r.get("high_water_price", r["last_price"])),
            )
        eq = float(acct["equity"])
        return AccountState(
            equity=eq, cash=float(acct["buying_power"]),
            peak_equity=eq,            # persist real peak yourself (see README/state.json)
            day_start_equity=eq,       # snapshot this at session open and persist
            positions=positions,
        )

    def get_quote(self, symbol: str):
        q = self.mcp.call_tool("get_quote", {"symbol": symbol})   # <-- VERIFY NAME
        last = float(q["last_price"])
        last_close = float(q.get("previous_close", last))
        age = float(q.get("age_seconds", 0))
        return last, last_close, age

    def place_order(self, order: Order) -> Fill | None:
        last, _, _ = self.get_quote(order.symbol)
        qty = round(order.notional_usd / last, 6)
        # Prefer notional orders if Robinhood's MCP supports them; else qty.
        resp = self.mcp.call_tool(                                # <-- VERIFY NAME/ARGS
            "place_order",
            {
                "symbol": order.symbol,
                "side": order.side,
                "type": "limit",
                "limit_price": round(order.limit_price, 2),
                "quantity": qty,
                "time_in_force": "gfd",
            },
        )
        if not resp or resp.get("state") in ("rejected", "cancelled"):
            return None
        fill_px = float(resp.get("average_price", last))
        return Fill(order.symbol, order.side, qty, fill_px, qty * fill_px, datetime.now())

    def cancel_all(self):
        self.mcp.call_tool("cancel_all_orders", {})               # <-- VERIFY NAME


def build_executor(cfg, mcp_client=None) -> BrokerExecutor:
    if cfg["mode"]["execution_mode"] == "paper":
        return PaperExecutor(
            cfg["account"]["allocated_capital_usd"], quote_fn=yfinance_quote
        )
    if mcp_client is None:
        raise RuntimeError("LIVE mode needs a connected Robinhood MCP client.")
    return LiveExecutor(mcp_client)
