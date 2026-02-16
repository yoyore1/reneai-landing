"""
Real-time BTC/USDT price feed from Binance via WebSocket.

Uses the public trade stream -- no API key required.
Exposes a shared `price_state` dict that the strategy reads.
"""

import asyncio
import json
import time
import logging
from typing import Optional

import aiohttp
import websockets

from bot.config import cfg

log = logging.getLogger("binance")


class BinanceFeed:
    """Connects to Binance WS and keeps the latest BTC/USDT price up to date."""

    def __init__(self):
        self.current_price: Optional[float] = None
        self.last_update: float = 0.0
        self._running = False
        self._ws = None

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
                        self.current_price = float(msg["p"])
                        self.last_update = time.time()
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
