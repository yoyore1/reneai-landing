"""
Volatility Guard — pauses or reduces trading when market conditions are poor.

Checks every tick:
  1. BTC range over last 60 minutes (from Binance klines)
  2. Rolling win rate over last N trades
  3. Choppy rate over last N markets

If 2 of 3 flags are active -> trading paused until conditions improve.
"""

import asyncio
import logging
import time
from collections import deque
from typing import Optional

import httpx

log = logging.getLogger("vol_guard")

# Thresholds
BTC_RANGE_MIN = 150.0        # BTC must have moved >= $150 in last 60 min
WIN_RATE_MIN = 0.60           # rolling win rate must be >= 60%
CHOPPY_RATE_MAX = 0.50        # choppy skip rate must be < 50%
ROLLING_WINDOW = 10           # look at last 10 trades / markets
CHECK_INTERVAL = 300          # re-check BTC range every 5 minutes


class VolatilityGuard:

    def __init__(self):
        self._btc_prices: deque = deque(maxlen=120)  # 60 min at 30s intervals
        self._trade_results: deque = deque(maxlen=ROLLING_WINDOW)
        self._market_results: deque = deque(maxlen=ROLLING_WINDOW)

        self._btc_range: float = 0.0
        self._rolling_wr: float = 1.0
        self._choppy_rate: float = 0.0
        self._flags_active: int = 0
        self._paused: bool = False
        self._last_btc_check: float = 0.0
        self._reason: str = ""

    @property
    def is_paused(self) -> bool:
        return self._paused

    @property
    def reason(self) -> str:
        return self._reason

    @property
    def status_dict(self) -> dict:
        return {
            "paused": self._paused,
            "btc_range_60m": round(self._btc_range, 2),
            "rolling_wr": round(self._rolling_wr * 100, 1),
            "choppy_rate": round(self._choppy_rate * 100, 1),
            "flags": self._flags_active,
            "reason": self._reason,
            "trades_tracked": len(self._trade_results),
            "markets_tracked": len(self._market_results),
        }

    def record_trade(self, won: bool):
        """Record a trade result (win or loss)."""
        self._trade_results.append(won)
        self._update_state()

    def record_market(self, was_choppy: bool):
        """Record a market outcome (choppy or not)."""
        self._market_results.append(was_choppy)
        self._update_state()

    async def check_btc(self):
        """Fetch BTC price from Binance and update the rolling range."""
        now = time.time()
        if now - self._last_btc_check < 30:
            return
        self._last_btc_check = now

        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(
                    "https://api.binance.com/api/v3/klines",
                    params={"symbol": "BTCUSDT", "interval": "1m", "limit": 60},
                )
                if resp.status_code == 200:
                    klines = resp.json()
                    highs = [float(k[2]) for k in klines]
                    lows = [float(k[3]) for k in klines]
                    if highs and lows:
                        self._btc_range = max(highs) - min(lows)

                    current_price = float(klines[-1][4]) if klines else 0
                    if current_price > 0:
                        self._btc_prices.append(current_price)
        except Exception as e:
            log.debug("BTC price fetch failed: %s", e)

        self._update_state()

    def _update_state(self):
        """Recalculate flags and pause state."""
        flags = 0
        reasons = []

        # Flag 1: BTC range
        if self._btc_range > 0 and self._btc_range < BTC_RANGE_MIN:
            flags += 1
            reasons.append(f"BTC_range=${self._btc_range:.0f}<${BTC_RANGE_MIN:.0f}")

        # Flag 2: Rolling win rate
        if len(self._trade_results) >= 5:
            wins = sum(1 for w in self._trade_results if w)
            self._rolling_wr = wins / len(self._trade_results)
            if self._rolling_wr < WIN_RATE_MIN:
                flags += 1
                reasons.append(f"WR={self._rolling_wr*100:.0f}%<{WIN_RATE_MIN*100:.0f}%")
        else:
            self._rolling_wr = 1.0

        # Flag 3: Choppy rate
        if len(self._market_results) >= 5:
            choppy_count = sum(1 for c in self._market_results if c)
            self._choppy_rate = choppy_count / len(self._market_results)
            if self._choppy_rate > CHOPPY_RATE_MAX:
                flags += 1
                reasons.append(f"choppy={self._choppy_rate*100:.0f}%>{CHOPPY_RATE_MAX*100:.0f}%")
        else:
            self._choppy_rate = 0.0

        self._flags_active = flags
        was_paused = self._paused
        self._paused = flags >= 2

        if self._paused:
            self._reason = " | ".join(reasons)
        else:
            self._reason = ""

        if self._paused and not was_paused:
            log.warning(
                "VOL GUARD PAUSED TRADING: %s | btc_range=$%.0f wr=%.0f%% choppy=%.0f%%",
                self._reason, self._btc_range, self._rolling_wr * 100, self._choppy_rate * 100,
            )
        elif not self._paused and was_paused:
            log.info(
                "VOL GUARD RESUMED TRADING: btc_range=$%.0f wr=%.0f%% choppy=%.0f%%",
                self._btc_range, self._rolling_wr * 100, self._choppy_rate * 100,
            )
