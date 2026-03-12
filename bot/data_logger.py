"""
Data Logger — Centralized tick-level data collection for strategy analysis.

Collects:
  1. Analysis window ticks (pre-buy): both sides + depth + BTC price
  2. Position ticks (while holding): both sides + depth + BTC price
  3. Skipped/missed markets: reason + highs + BTC price

All timestamps include EDT for timezone-aware analysis.
BTC price fetched from Binance with 10-second cache.
"""

import logging
import os
import time
from datetime import datetime, timezone, timedelta

import aiohttp

log = logging.getLogger("data_logger")

EDT = timezone(timedelta(hours=-4))


class DataLogger:

    def __init__(self, bot_name: str):
        self._bot_name = bot_name
        self._dir = "history"
        os.makedirs(self._dir, exist_ok=True)

        self._analysis_file = os.path.join(self._dir, f"{bot_name}_analysis.csv")
        self._position_file = os.path.join(self._dir, f"{bot_name}_ticks.csv")
        self._skipped_file = os.path.join(self._dir, f"{bot_name}_skipped.csv")

        self._init_csv(self._analysis_file,
            "timestamp,edt_time,edt_hour,market,yes_bid,no_bid,"
            "yes_depth,no_depth,btc_price,seconds_left\n")
        self._init_csv(self._position_file,
            "timestamp,edt_time,edt_hour,market,side,entry_price,"
            "yes_bid,no_bid,yes_depth,no_depth,btc_price,seconds_left\n")
        self._init_csv(self._skipped_file,
            "timestamp,edt_time,edt_hour,market,reason,"
            "yes_high,no_high,btc_price\n")

        self._btc_price: float = 0.0
        self._btc_last_fetch: float = 0
        self._http_session: aiohttp.ClientSession = None

        self._last_analysis_log: dict = {}
        self._analysis_interval = 3.0

    def _init_csv(self, path: str, header: str):
        if not os.path.exists(path):
            with open(path, "w") as f:
                f.write(header)

    async def _ensure_session(self):
        if self._http_session is None or self._http_session.closed:
            self._http_session = aiohttp.ClientSession()

    async def fetch_btc_price(self) -> float:
        now = time.time()
        if now - self._btc_last_fetch < 10 and self._btc_price > 0:
            return self._btc_price
        try:
            await self._ensure_session()
            async with self._http_session.get(
                "https://api.binance.com/api/v3/ticker/price",
                params={"symbol": "BTCUSDT"},
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                data = await resp.json()
                self._btc_price = float(data["price"])
                self._btc_last_fetch = now
        except Exception:
            pass
        return self._btc_price

    def _ts(self):
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    def _edt_str(self):
        return datetime.now(EDT).strftime("%I:%M:%S %p")

    def _edt_hour(self):
        return datetime.now(EDT).hour

    def _clean(self, name: str) -> str:
        return name[:60].replace(",", ";")

    def should_log_analysis(self, market_id: str) -> bool:
        now = time.time()
        last = self._last_analysis_log.get(market_id, 0)
        if now - last >= self._analysis_interval:
            self._last_analysis_log[market_id] = now
            return True
        return False

    def log_analysis_tick(self, market_name: str, yes_bid: float, no_bid: float,
                          yes_depth: float, no_depth: float, btc_price: float,
                          seconds_left: float):
        try:
            with open(self._analysis_file, "a") as f:
                f.write(
                    f"{self._ts()},{self._edt_str()},{self._edt_hour()},"
                    f"{self._clean(market_name)},{yes_bid:.3f},{no_bid:.3f},"
                    f"{yes_depth:.1f},{no_depth:.1f},"
                    f"{btc_price:.2f},{seconds_left:.0f}\n"
                )
        except Exception:
            pass

    def log_position_tick(self, market_name: str, side: str, entry_price: float,
                          yes_bid: float, no_bid: float,
                          yes_depth: float, no_depth: float,
                          btc_price: float, seconds_left: float):
        try:
            with open(self._position_file, "a") as f:
                f.write(
                    f"{self._ts()},{self._edt_str()},{self._edt_hour()},"
                    f"{self._clean(market_name)},{side},{entry_price:.3f},"
                    f"{yes_bid:.3f},{no_bid:.3f},"
                    f"{yes_depth:.1f},{no_depth:.1f},"
                    f"{btc_price:.2f},{seconds_left:.0f}\n"
                )
        except Exception:
            pass

    def log_skipped(self, market_name: str, reason: str,
                    yes_high: float, no_high: float, btc_price: float):
        try:
            with open(self._skipped_file, "a") as f:
                f.write(
                    f"{self._ts()},{self._edt_str()},{self._edt_hour()},"
                    f"{self._clean(market_name)},{reason},"
                    f"{yes_high:.3f},{no_high:.3f},{btc_price:.2f}\n"
                )
        except Exception:
            pass

    async def close(self):
        if self._http_session and not self._http_session.closed:
            await self._http_session.close()
