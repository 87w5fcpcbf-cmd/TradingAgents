"""SQLite store: single source of truth for flags, signals, trades, Jev calls, approvals."""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS flags (name TEXT PRIMARY KEY, value INTEGER NOT NULL, reason TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS signals (id INTEGER PRIMARY KEY, ts REAL, data TEXT);
CREATE TABLE IF NOT EXISTS trades (id INTEGER PRIMARY KEY, ts REAL, data TEXT);
CREATE TABLE IF NOT EXISTS jev_calls (id INTEGER PRIMARY KEY, ts REAL, data TEXT);
CREATE TABLE IF NOT EXISTS approvals (
  id INTEGER PRIMARY KEY, ts REAL, expires REAL, status TEXT, data TEXT);
CREATE TABLE IF NOT EXISTS equity (ts REAL, equity REAL);
CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, ts REAL, kind TEXT, data TEXT);
"""


class Db:
    def __init__(self, path: str):
        self.path = path
        self._c = sqlite3.connect(path, check_same_thread=False)
        self._c.row_factory = sqlite3.Row
        self._c.executescript(SCHEMA)
        self._c.commit()

    # flags -----------------------------------------------------------------
    def get_flag(self, name: str) -> bool:
        r = self._c.execute("SELECT value FROM flags WHERE name=?", (name,)).fetchone()
        return bool(r["value"]) if r else False

    def flag_reason(self, name: str) -> str | None:
        r = self._c.execute("SELECT reason FROM flags WHERE name=?", (name,)).fetchone()
        return r["reason"] if r else None

    def set_flag(self, name: str, value: bool, reason: str | None = None) -> None:
        self._c.execute(
            "INSERT INTO flags(name,value,reason,ts) VALUES(?,?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET value=excluded.value, reason=excluded.reason, ts=excluded.ts",
            (name, int(value), reason, time.time()),
        )
        self._c.commit()

    def get_int(self, name: str, default: int = 0) -> int:
        r = self._c.execute("SELECT value FROM flags WHERE name=?", (name,)).fetchone()
        return int(r["value"]) if r else default

    def set_int(self, name: str, value: int) -> None:
        self.set_flag(name, value)  # flags.value is an INTEGER column; reused for persisted counters

    # json row tables -------------------------------------------------------
    def _log(self, table: str, row: dict[str, Any]) -> int:
        cur = self._c.execute(f"INSERT INTO {table}(ts,data) VALUES(?,?)", (time.time(), json.dumps(row)))
        self._c.commit()
        return cur.lastrowid

    def _all(self, table: str) -> list[dict[str, Any]]:
        out = []
        for r in self._c.execute(f"SELECT id, ts, data FROM {table} ORDER BY id"):
            d = json.loads(r["data"])
            d["id"], d["ts"] = r["id"], r["ts"]
            out.append(d)
        return out

    def log_signal(self, row): return self._log("signals", row)
    def log_trade(self, row): return self._log("trades", row)
    def log_jev(self, row): return self._log("jev_calls", row)
    def log_event(self, kind: str, row: dict[str, Any]) -> None:
        self._c.execute("INSERT INTO events(ts,kind,data) VALUES(?,?,?)", (time.time(), kind, json.dumps(row)))
        self._c.commit()
    def signals(self): return self._all("signals")
    def trades(self): return self._all("trades")
    def jev_calls(self): return self._all("jev_calls")

    def events(self, kind: str | None = None) -> list[dict[str, Any]]:
        q, args = "SELECT ts,kind,data FROM events", ()
        if kind:
            q, args = q + " WHERE kind=?", (kind,)
        return [{"ts": r["ts"], "kind": r["kind"], **json.loads(r["data"])} for r in self._c.execute(q + " ORDER BY id", args)]

    # equity ----------------------------------------------------------------
    def log_equity(self, equity: float, ts: float | None = None) -> None:
        self._c.execute("INSERT INTO equity(ts,equity) VALUES(?,?)", (ts or time.time(), equity))
        self._c.commit()

    def equity_series(self) -> list[tuple[float, float]]:
        return [(r["ts"], r["equity"]) for r in self._c.execute("SELECT ts,equity FROM equity ORDER BY ts")]

    # approvals -------------------------------------------------------------
    def add_approval(self, data: dict[str, Any], expires: float) -> int:
        cur = self._c.execute(
            "INSERT INTO approvals(ts,expires,status,data) VALUES(?,?,?,?)",
            (time.time(), expires, "pending", json.dumps(data)),
        )
        self._c.commit()
        return cur.lastrowid

    def get_approval(self, aid: int) -> dict[str, Any] | None:
        r = self._c.execute("SELECT * FROM approvals WHERE id=?", (aid,)).fetchone()
        if not r:
            return None
        return {"id": r["id"], "expires": r["expires"], "status": r["status"], **json.loads(r["data"])}

    def set_approval_status(self, aid: int, status: str) -> None:
        self._c.execute("UPDATE approvals SET status=? WHERE id=? AND status='pending'", (status, aid))
        self._c.commit()

    def mark_approval(self, aid: int, status: str) -> None:
        self._c.execute("UPDATE approvals SET status=? WHERE id=?", (status, aid))
        self._c.commit()

    def approvals_with_status(self, status: str) -> list[dict[str, Any]]:
        return [self.get_approval(r["id"]) for r in self._c.execute("SELECT id FROM approvals WHERE status=?", (status,))]

    def pending_approvals(self) -> list[dict[str, Any]]:
        return [self.get_approval(r["id"]) for r in self._c.execute("SELECT id FROM approvals WHERE status='pending'")]
