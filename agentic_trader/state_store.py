"""
state_store.py — durable peak_equity / day_start_equity across restarts.

The catastrophic drawdown halt (vs all-time peak) and the daily-loss pause (vs
this session's opening equity) are only meaningful if their baselines survive a
process restart. Holding them in memory means every reboot silently resets the
drawdown to 0% and re-baselines the day — exactly when you least want the halts
to forget. This persists both to a small JSON file and re-applies them each
cycle.

  • peak_equity     — all-time high; monotonic, never decreases.
  • day_start_equity— equity at the first cycle of the current calendar day
                      (in the configured trading timezone); re-baselined when
                      the day rolls over, otherwise preserved across restarts.

Writes are atomic (temp file + os.replace) and a missing/corrupt file is
treated as a fresh start rather than an error.
"""

from __future__ import annotations

import json
import os
import tempfile


class StateStore:
    def __init__(self, path: str = "state.json"):
        self.path = path
        self.data: dict | None = self._load()

    def _load(self) -> dict | None:
        try:
            with open(self.path) as f:
                d = json.load(f)
            # minimal validation: the keys we depend on must be present + numeric
            float(d["peak_equity"])
            float(d["day_start_equity"])
            str(d["day"])
            return d
        except (FileNotFoundError, KeyError, ValueError, json.JSONDecodeError):
            return None

    def _write(self) -> None:
        directory = os.path.dirname(self.path) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".state-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(self.data, f, indent=2)
            os.replace(tmp, self.path)  # atomic on POSIX
        except Exception:
            if os.path.exists(tmp):
                os.remove(tmp)
            raise

    def sync(self, equity: float, day: str) -> tuple[float, float]:
        """Fold the latest equity into persisted state and return the baselines.

        Returns ``(peak_equity, day_start_equity)`` to stamp onto the
        AccountState before the halt checks run.
        """
        loaded = self.data
        if loaded is None or loaded.get("day") != day:
            day_start = equity                       # new session (or first ever)
        else:
            day_start = float(loaded["day_start_equity"])

        peak = max(equity, float(loaded["peak_equity"])) if loaded else equity

        self.data = {
            "peak_equity": peak,
            "day_start_equity": day_start,
            "day": day,
        }
        self._write()
        return peak, day_start
