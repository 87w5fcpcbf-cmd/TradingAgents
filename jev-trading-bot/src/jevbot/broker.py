"""Alpaca PAPER broker. The only module that can place orders. Refuses any non-paper endpoint."""
from __future__ import annotations

import os
import re
from typing import Any

import requests

from .config import PAPER_URL


class BrokerError(Exception):
    pass


class Broker:
    def __init__(self, base_url: str = PAPER_URL, timeout: int = 15):
        if base_url.rstrip("/") != PAPER_URL:
            raise BrokerError("only the Alpaca paper endpoint is allowed (live trading is disabled)")
        key, secret = os.environ.get("ALPACA_KEY"), os.environ.get("ALPACA_SECRET")
        if not key or not secret:
            raise BrokerError("ALPACA_KEY / ALPACA_SECRET not set")
        self._h = {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret}
        self.base, self.timeout = PAPER_URL, timeout

    def _req(self, method: str, path: str, **kw) -> Any:
        try:
            r = requests.request(method, self.base + path, headers=self._h, timeout=self.timeout, **kw)
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as e:
            raise BrokerError(f"{method} {path} failed: {type(e).__name__}") from None

    def account(self) -> dict[str, float]:
        a = self._req("GET", "/v2/account")
        try:
            return {"equity": float(a["equity"]), "last_equity": float(a["last_equity"])}
        except (KeyError, TypeError, ValueError):
            raise BrokerError("malformed account response") from None

    def positions(self) -> list[dict[str, Any]]:
        return self._req("GET", "/v2/positions")

    def exposure(self) -> float:
        try:
            return sum(abs(float(p["market_value"])) for p in self.positions())
        except (KeyError, TypeError, ValueError):
            raise BrokerError("malformed positions response") from None

    def bracket_order(self, symbol: str, qty: float, stop_price: float, take_profit_price: float) -> dict[str, Any]:
        if qty <= 0 or int(qty) != qty:
            raise BrokerError("bracket orders need a positive whole number of shares")
        body = {
            "symbol": symbol, "qty": str(int(qty)), "side": "buy", "type": "market",
            "time_in_force": "day", "order_class": "bracket",
            "stop_loss": {"stop_price": f"{stop_price:.2f}"},
            "take_profit": {"limit_price": f"{take_profit_price:.2f}"},
        }
        return self._req("POST", "/v2/orders", json=body)

    def closed_orders(self) -> list[dict[str, Any]]:
        """Recent closed parent orders with their bracket legs (used to compute realized P&L)."""
        return self._req("GET", "/v2/orders", params={"status": "closed", "nested": "true", "limit": 100, "direction": "desc"})

    def close_position(self, symbol: str) -> dict[str, Any]:
        """Cancel the symbol's open bracket legs, then close the position at market."""
        if not re.fullmatch(r"[A-Z][A-Z.]{0,5}", symbol):
            raise BrokerError("invalid symbol")
        for o in self._req("GET", "/v2/orders", params={"status": "open", "symbols": symbol, "nested": "true"}):
            self._req("DELETE", f"/v2/orders/{o['id']}")
        return self._req("DELETE", f"/v2/positions/{symbol}")

    def is_session_today(self, day: str) -> bool:
        """True if the market has a regular session on `day` (YYYY-MM-DD); False on holidays/weekends."""
        cal = self._req("GET", "/v2/calendar", params={"start": day, "end": day})
        return any(c.get("date") == day for c in cal)
