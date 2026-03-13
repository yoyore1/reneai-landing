"""
Data Logger — Centralized tick-level data collection for strategy analysis.

Collects:
  1. Analysis window ticks (pre-buy): bids + asks + depth + spread + BTC
  2. Position ticks (while holding): bids + asks + depth + spread + BTC
  3. Skipped/missed markets: reason + highs + BTC price
  4. BTC rolling volatility per market (swing tracking)
  5. Market resolutions: actual outcome independent of bot position

All timestamps include EDT for timezone-aware analysis.
BTC price fetched from Binance with 10-second cache.
"""

import logging
import os
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta

import aiohttp

log = logging.getLogger("data_logger")

EDT = timezone(timedelta(hours=-4))

ANALYSIS_HEADER = (
    "timestamp,edt_time,edt_hour,market,"
    "yes_bid,no_bid,yes_ask,no_ask,"
    "yes_depth,no_depth,yes_ask_depth,no_ask_depth,"
    "yes_spread,no_spread,"
    "btc_price,btc_swing_this_mkt,seconds_left\n"
)

POSITION_HEADER = (
    "timestamp,edt_time,edt_hour,market,side,entry_price,"
    "yes_bid,no_bid,yes_ask,no_ask,"
    "yes_depth,no_depth,yes_ask_depth,no_ask_depth,"
    "yes_spread,no_spread,"
    "btc_price,seconds_left\n"
)

SKIPPED_HEADER = (
    "timestamp,edt_time,edt_hour,market,reason,"
    "yes_high,no_high,btc_price\n"
)

RESOLUTION_HEADER = (
    "timestamp,edt_time,edt_hour,market,"
    "resolution,btc_open,btc_close,btc_swing\n"
)

POST_EXIT_HEADER = (
    "timestamp,edt_time,market,side,exit_reason,"
    "entry_price,exit_price,exit_bid,"
    "resolution,held_pnl,"
    "our_side_max_after,our_side_min_after,"
    "other_side_max_after,"
    "recovered_above_entry,seconds_left_at_exit\n"
)


class DataLogger:

    def __init__(self, bot_name: str):
        self._bot_name = bot_name
        self._dir = "history"
        os.makedirs(self._dir, exist_ok=True)

        self._analysis_file = os.path.join(self._dir, f"{bot_name}_analysis.csv")
        self._position_file = os.path.join(self._dir, f"{bot_name}_ticks.csv")
        self._skipped_file = os.path.join(self._dir, f"{bot_name}_skipped.csv")
        self._resolution_file = os.path.join(self._dir, f"{bot_name}_resolutions.csv")

        self._upgrade_csv(self._analysis_file, ANALYSIS_HEADER)
        self._upgrade_csv(self._position_file, POSITION_HEADER)
        self._init_csv(self._skipped_file, SKIPPED_HEADER)
        self._init_csv(self._resolution_file, RESOLUTION_HEADER)

        self._post_exit_file = os.path.join(self._dir, f"{bot_name}_post_exit.csv")
        self._init_csv(self._post_exit_file, POST_EXIT_HEADER)

        self._btc_price: float = 0.0
        self._btc_last_fetch: float = 0
        self._http_session: aiohttp.ClientSession = None

        self._last_analysis_log: dict = {}
        self._analysis_interval = 3.0

        self._btc_per_market: dict[str, list[float]] = defaultdict(list)

    def _init_csv(self, path: str, header: str):
        if not os.path.exists(path):
            with open(path, "w") as f:
                f.write(header)

    def _upgrade_csv(self, path: str, new_header: str):
        """Create CSV or rename old file if header changed (new columns added)."""
        if not os.path.exists(path):
            with open(path, "w") as f:
                f.write(new_header)
            return
        with open(path, "r") as f:
            existing_header = f.readline()
        if existing_header.strip() != new_header.strip():
            bak = path + ".pre_upgrade"
            if not os.path.exists(bak):
                os.rename(path, bak)
                log.info("Upgraded CSV header: renamed %s → %s", path, bak)
            with open(path, "w") as f:
                f.write(new_header)

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

    def track_btc_for_market(self, market_id: str, btc_price: float):
        """Record a BTC price sample for a given market (for swing calculation)."""
        if btc_price > 0:
            self._btc_per_market[market_id].append(btc_price)

    def get_btc_swing(self, market_id: str) -> float:
        """Return the BTC swing (high - low) for a given market so far."""
        prices = self._btc_per_market.get(market_id, [])
        if len(prices) < 2:
            return 0.0
        return max(prices) - min(prices)

    def finalize_market(self, market_id: str) -> float:
        """Finalize BTC tracking for a market, return swing, clean up."""
        swing = self.get_btc_swing(market_id)
        self._btc_per_market.pop(market_id, None)
        return swing

    def should_log_analysis(self, market_id: str) -> bool:
        now = time.time()
        last = self._last_analysis_log.get(market_id, 0)
        if now - last >= self._analysis_interval:
            self._last_analysis_log[market_id] = now
            return True
        return False

    def log_analysis_tick(self, market_name: str,
                          yes_bid: float, no_bid: float,
                          yes_ask: float, no_ask: float,
                          yes_depth: float, no_depth: float,
                          yes_ask_depth: float, no_ask_depth: float,
                          btc_price: float, seconds_left: float,
                          market_id: str = ""):
        self.track_btc_for_market(market_id or market_name, btc_price)
        yes_spread = round(yes_ask - yes_bid, 3) if yes_ask > 0 and yes_bid > 0 else 0
        no_spread = round(no_ask - no_bid, 3) if no_ask > 0 and no_bid > 0 else 0
        btc_swing = self.get_btc_swing(market_id or market_name)
        try:
            with open(self._analysis_file, "a") as f:
                f.write(
                    f"{self._ts()},{self._edt_str()},{self._edt_hour()},"
                    f"{self._clean(market_name)},"
                    f"{yes_bid:.3f},{no_bid:.3f},{yes_ask:.3f},{no_ask:.3f},"
                    f"{yes_depth:.1f},{no_depth:.1f},"
                    f"{yes_ask_depth:.1f},{no_ask_depth:.1f},"
                    f"{yes_spread:.3f},{no_spread:.3f},"
                    f"{btc_price:.2f},{btc_swing:.2f},{seconds_left:.0f}\n"
                )
        except Exception:
            pass

    def log_position_tick(self, market_name: str, side: str, entry_price: float,
                          yes_bid: float, no_bid: float,
                          yes_ask: float, no_ask: float,
                          yes_depth: float, no_depth: float,
                          yes_ask_depth: float, no_ask_depth: float,
                          btc_price: float, seconds_left: float):
        yes_spread = round(yes_ask - yes_bid, 3) if yes_ask > 0 and yes_bid > 0 else 0
        no_spread = round(no_ask - no_bid, 3) if no_ask > 0 and no_bid > 0 else 0
        try:
            with open(self._position_file, "a") as f:
                f.write(
                    f"{self._ts()},{self._edt_str()},{self._edt_hour()},"
                    f"{self._clean(market_name)},{side},{entry_price:.3f},"
                    f"{yes_bid:.3f},{no_bid:.3f},{yes_ask:.3f},{no_ask:.3f},"
                    f"{yes_depth:.1f},{no_depth:.1f},"
                    f"{yes_ask_depth:.1f},{no_ask_depth:.1f},"
                    f"{yes_spread:.3f},{no_spread:.3f},"
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

    def log_resolution(self, market_name: str, resolution: str,
                       btc_open: float, btc_close: float, btc_swing: float):
        try:
            with open(self._resolution_file, "a") as f:
                f.write(
                    f"{self._ts()},{self._edt_str()},{self._edt_hour()},"
                    f"{self._clean(market_name)},{resolution},"
                    f"{btc_open:.2f},{btc_close:.2f},{btc_swing:.2f}\n"
                )
        except Exception:
            pass

    def log_post_exit(self, market_name: str, side: str, exit_reason: str,
                      entry_price: float, exit_price: float, exit_bid: float,
                      resolution: str, held_pnl: float,
                      our_side_max: float, our_side_min: float,
                      other_side_max: float,
                      recovered_above_entry: bool,
                      seconds_left_at_exit: float):
        try:
            with open(self._post_exit_file, "a") as f:
                f.write(
                    f"{self._ts()},{self._edt_str()},"
                    f"{self._clean(market_name)},{side},{exit_reason},"
                    f"{entry_price:.3f},{exit_price:.3f},{exit_bid:.3f},"
                    f"{resolution},{held_pnl:.2f},"
                    f"{our_side_max:.3f},{our_side_min:.3f},"
                    f"{other_side_max:.3f},"
                    f"{recovered_above_entry},{seconds_left_at_exit:.0f}\n"
                )
        except Exception:
            pass

    async def close(self):
        if self._http_session and not self._http_session.closed:
            await self._http_session.close()
