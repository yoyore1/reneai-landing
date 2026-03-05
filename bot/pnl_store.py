"""
Persistent PnL storage — saves daily/hourly PnL to a JSON file.

Structure:
{
  "2026-03-04": {
    "total": 12.50,
    "trades": 5,
    "wins": 3,
    "losses": 2,
    "hours": { "00": 3.50, "01": -1.00, ... }
  }
}
"""

import json
import logging
import os
import threading
from datetime import datetime, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

EST = ZoneInfo("America/New_York")

log = logging.getLogger("pnl_store")


class PnLStore:

    def __init__(self, filepath: str = "pnl_data.json"):
        self._filepath = filepath
        self._lock = threading.Lock()
        self._data: dict = {}
        self._load()

    def _load(self):
        if os.path.exists(self._filepath):
            try:
                with open(self._filepath, "r") as f:
                    self._data = json.load(f)
                log.info("Loaded PnL data from %s (%d days)", self._filepath, len(self._data))
            except Exception as exc:
                log.warning("Failed to load PnL data: %s", exc)
                self._data = {}

    def _save(self):
        try:
            with open(self._filepath, "w") as f:
                json.dump(self._data, f, indent=2)
        except Exception as exc:
            log.warning("Failed to save PnL data: %s", exc)

    def record_trade(self, pnl: float, is_win: bool):
        now = datetime.now(EST)
        day_key = now.strftime("%Y-%m-%d")
        hour_key = now.strftime("%H")

        with self._lock:
            if day_key not in self._data:
                self._data[day_key] = {"total": 0, "trades": 0, "wins": 0, "losses": 0, "hours": {}}

            day = self._data[day_key]
            day["total"] = round(day["total"] + pnl, 2)
            day["trades"] += 1
            if is_win:
                day["wins"] += 1
            else:
                day["losses"] += 1

            day["hours"][hour_key] = round(day["hours"].get(hour_key, 0) + pnl, 2)
            self._save()

    def get_all(self) -> dict:
        with self._lock:
            return dict(self._data)

    def get_month(self, year: int, month: int) -> dict:
        prefix = f"{year:04d}-{month:02d}"
        with self._lock:
            return {k: v for k, v in self._data.items() if k.startswith(prefix)}

    def get_day(self, date_str: str) -> dict:
        with self._lock:
            return self._data.get(date_str, {})
