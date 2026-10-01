"""Telegram alerts and commands. Only the allow-listed chat id is ever obeyed.

Commands: /status /kill /approve [id] /reject [id]. Resetting the kill switch is deliberately
NOT a Telegram command; it needs `jevbot reset` on the machine. Errors never include the token.
"""
from __future__ import annotations

import os
from typing import Any, Callable

import requests

from .db import Db
from .risk import Approvals, Risk

API = "https://api.telegram.org/bot{token}/{method}"


class Alerts:
    def __init__(self, db: Db, risk: Risk, approvals: Approvals, status_fn: Callable[[], str],
                 send_fn: Callable[[str], None] | None = None):
        self.db, self.risk, self.approvals, self.status_fn = db, risk, approvals, status_fn
        self.token = os.environ.get("TELEGRAM_TOKEN") or None
        self.chat_id = os.environ.get("TELEGRAM_CHAT_ID") or None
        self._send_override = send_fn

    @property
    def configured(self) -> bool:
        return bool(self.token and self.chat_id)

    def send(self, text: str) -> None:
        if self._send_override:
            self._send_override(text)
            return
        if not self.configured:
            return
        try:
            r = requests.post(API.format(token=self.token, method="sendMessage"),
                              json={"chat_id": self.chat_id, "text": text}, timeout=10)
            r.raise_for_status()
        except Exception as e:  # alerts must never crash trading; log the type only (URL contains the token)
            self.db.log_event("alert_error", {"error": type(e).__name__})

    # incoming --------------------------------------------------------------
    def handle(self, update: dict[str, Any]) -> None:
        msg = update.get("message") if isinstance(update, dict) else None
        if not isinstance(msg, dict):
            return
        chat = (msg.get("chat") or {}).get("id")
        if self.chat_id is None or str(chat) != str(self.chat_id):
            return  # unknown chat: ignore silently
        parts = str(msg.get("text") or "").strip().split()
        if not parts:
            return
        cmd, args = parts[0].split("@")[0].lower(), parts[1:]
        if cmd == "/status":
            self.send(self.status_fn())
        elif cmd == "/kill":
            self.risk.trip("manual /kill via Telegram")
            self.send("Kill switch TRIPPED. All trading halted until reset on the machine (`jevbot reset`).")
        elif cmd in ("/approve", "/reject"):
            self._resolve(cmd == "/approve", args)

    def _resolve(self, approve: bool, args: list[str]) -> None:
        pending = [p["id"] for p in self.db.pending_approvals()]
        if args:
            if not args[0].isdigit():
                self.send("Usage: /approve <id> or /reject <id>")
                return
            aid = int(args[0])
        elif len(pending) == 1:
            aid = pending[0]
        else:
            self.send(f"Which one? Pending ids: {pending or 'none'}. Use /approve <id> or /reject <id>.")
            return
        ok = self.approvals.resolve(aid, approve)
        self.send(f"#{aid} {'approved' if approve else 'rejected'}." if ok else f"#{aid} is not pending (expired, done or unknown).")

    def poll(self) -> int:
        """Fetch and handle new updates; return how many were processed."""
        if not self.configured:
            return 0
        offset = self.db.get_int("tg_offset", 0)
        try:
            r = requests.get(API.format(token=self.token, method="getUpdates"),
                             params={"offset": offset, "timeout": 0}, timeout=10)
            r.raise_for_status()
            updates = r.json().get("result", [])
        except Exception as e:
            self.db.log_event("alert_error", {"error": type(e).__name__})
            return 0
        n = 0
        for u in updates:
            self.handle(u)
            n += 1
            self.db.set_int("tg_offset", int(u.get("update_id", offset)) + 1)
        return n
