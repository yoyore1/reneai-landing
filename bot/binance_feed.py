"""
Real-time BTC/USDT price feed from Binance via WebSocket.

Uses the public trade stream -- no API key required.
Exposes a shared `price_state` dict that the strategy reads.
"""

import asyncio
import collections
import json
import time
import logging
from typing import Optional, Tuple

import aiohttp
import websockets

from bot.config import cfg

log = logging.getLogger("binance")

# Rolling buffer of (timestamp, price) for spike detection
PriceTick = Tuple[float, float]


class BinanceFeed:
    """Connects to Binance WS and keeps the latest BTC/USDT price up to date."""

    def __init__(self):
        self.current_price: Optional[float] = None
        self.last_update: float = 0.0
        self._running = False
        self._ws = None
        # Rolling price buffer: last 10 seconds of ticks (timestamp, price)
        self.price_buffer: collections.deque = collections.deque(maxlen=500)
        # Volume buffer: (timestamp, qty_btc) for volume analysis
        self.volume_buffer: collections.deque = collections.deque(maxlen=2000)
        # Price range buffer: (timestamp, price) for longer-term range calc
        self.range_buffer: collections.deque = collections.deque(maxlen=5000)

    def get_price_n_seconds_ago(self, n: float) -> Optional[float]:
        """Return the price from approximately `n` seconds ago."""
        cutoff = time.time() - n
        for ts, px in self.price_buffer:
            if ts >= cutoff:
                return px
        # If buffer is empty or all ticks are newer than n seconds
        if self.price_buffer:
            return self.price_buffer[0][1]
        return None

    def detect_spike(self, move_usd: float, window_sec: float) -> Optional[float]:
        """
        Check if price moved >= move_usd within the last window_sec seconds.
        Returns the signed dollar move if spike detected, None otherwise.
        """
        old_price = self.get_price_n_seconds_ago(window_sec)
        if old_price is None or self.current_price is None:
            return None
        delta = self.current_price - old_price
        if abs(delta) >= move_usd:
            return delta
        return None

    def detect_momentum(self, move_usd: float, window_sec: float) -> Optional[float]:
        """
        Detect a momentum spike with instant confirmation (no delay).

        Checks:
          1. Price moved $move_usd+ over the last window_sec seconds
          2. The midpoint price (halfway through the window) was BETWEEN
             the start and end — meaning consistent direction, not a V-shape

        Returns signed dollar move if momentum confirmed, None otherwise.
        """
        if self.current_price is None:
            return None

        price_start = self.get_price_n_seconds_ago(window_sec)
        price_mid = self.get_price_n_seconds_ago(window_sec / 2.0)

        if price_start is None or price_mid is None:
            return None

        delta = self.current_price - price_start
        if abs(delta) < move_usd:
            return None

        # Check midpoint is between start and end (consistent direction)
        if delta > 0:
            # Up move: mid should be above start and below current
            if price_mid > price_start and price_mid < self.current_price:
                return delta
        else:
            # Down move: mid should be below start and above current
            if price_mid < price_start and price_mid > self.current_price:
                return delta

        return None

    def get_volume_btc(self, window_sec: float) -> float:
        """Total BTC volume traded in the last `window_sec` seconds."""
        cutoff = time.time() - window_sec
        return sum(qty for ts, qty in self.volume_buffer if ts >= cutoff)

    def get_price_range(self, window_sec: float) -> float:
        """High - Low price range over the last `window_sec` seconds."""
        cutoff = time.time() - window_sec
        prices = [px for ts, px in self.range_buffer if ts >= cutoff]
        if len(prices) < 2:
            return 0.0
        return max(prices) - min(prices)

    # ------------------------------------------------------------------
    # bootstrap: grab a REST snapshot so we have a price before WS fires
    # ------------------------------------------------------------------
    async def _seed_price(self):
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(cfg.binance_rest_url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    data = await resp.json()
                    self.current_price = float(data["price"])
                    self.last_update = time.time()
                    log.info("Seeded BTC price from REST: $%.2f", self.current_price)
        except Exception as exc:
            log.warning("REST seed failed (%s), will wait for WS", exc)

    # ------------------------------------------------------------------
    # main loop
    # ------------------------------------------------------------------
    async def run(self):
        """Long-running coroutine -- call as a task."""
        self._running = True
        await self._seed_price()

        while self._running:
            try:
                async with websockets.connect(cfg.binance_ws_url, ping_interval=20) as ws:
                    self._ws = ws
                    log.info("Connected to Binance WebSocket")
                    async for raw in ws:
                        if not self._running:
                            break
                        msg = json.loads(raw)
                        now = time.time()
                        self.current_price = float(msg["p"])
                        self.last_update = now
                        self.price_buffer.append((now, self.current_price))
                        self.range_buffer.append((now, self.current_price))
                        qty = float(msg.get("q", 0))
                        if qty > 0:
                            self.volume_buffer.append((now, qty))
            except (websockets.ConnectionClosed, ConnectionError, OSError) as exc:
                log.warning("Binance WS disconnected (%s), reconnecting in 2s...", exc)
                await asyncio.sleep(2)
            except Exception as exc:
                log.error("Binance WS unexpected error: %s", exc, exc_info=True)
                await asyncio.sleep(5)

    def stop(self):
        self._running = False

    @property
    def is_live(self) -> bool:
        return self.current_price is not None and (time.time() - self.last_update) < 10
