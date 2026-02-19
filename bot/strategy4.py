"""
Strategy 4: Momentum v2 — $25 in 3 seconds (stricter than S1)

Same logic as S1 but with higher spike threshold:
  - $25 move in 3 seconds (vs S1's $15 in 2s)
  - Same midpoint check, same window trend verification
  - Same exit rules (sell at 5%, moonbag at 15%, hard cap 20%)

Purpose: compare against S1 to see if fewer but higher-conviction
trades outperform more frequent lower-conviction trades.
"""

import asyncio
import collections
import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, List, Dict

from bot.polymarket import PolymarketClient, Market, Position

log = logging.getLogger("strategy4")

SPIKE_MOVE_USD = 25.0
SPIKE_WINDOW_SEC = 3.0
PROFIT_TARGET_PCT = 5.0
MOONBAG_PCT = 15.0
HARD_CAP_PCT = 20.0
MAX_POSITION_USDC = 50.0
POLL_SEC = 0.5


@dataclass
class S4Stats:
    total_signals: int = 0
    total_trades: int = 0
    total_exits: int = 0
    total_pnl: float = 0.0
    wins: int = 0
    losses: int = 0
    rejected: int = 0
    current_window: str = ""
    current_signal: str = ""
    last_action: str = ""
    hourly_pnl: dict = field(default_factory=dict)


@dataclass
class S4Window:
    market: Market
    open_price: Optional[float] = None
    signal_fired: bool = False
    signal_side: str = ""
    position: Optional[Position] = None


class Strategy4:

    def __init__(self, feed, poly: PolymarketClient):
        self.feed = feed
        self.poly = poly
        self.stats = S4Stats()
        self._windows: Dict[str, S4Window] = {}
        self._open_positions: List[Position] = []
        self._closed_positions: List[Position] = []
        self._running = False
        self._last_day = ""

    async def run(self):
        self._running = True
        log.info("Strategy 4 started | spike=$%.0f/%.0fs | sell=+%.0f%% | moonbag=+%.0f%%",
                 SPIKE_MOVE_USD, SPIKE_WINDOW_SEC, PROFIT_TARGET_PCT, MOONBAG_PCT)
        while self._running:
            try:
                await self._tick()
            except Exception as exc:
                log.error("S4 tick error: %s", exc, exc_info=True)
            await asyncio.sleep(POLL_SEC)

    def stop(self):
        self._running = False

    async def _tick(self):
        if not self.feed.is_live:
            return

        btc = self.feed.current_price
        now = time.time()
        self._daily_reset()

        # Discover markets
        if not hasattr(self, "_last_disc") or now - self._last_disc > 30:
            await self._discover()
            self._last_disc = now

        # Check each window for signals
        for cid, ws in list(self._windows.items()):
            if ws.market.window_end and now > ws.market.window_end:
                self._windows.pop(cid, None)
                continue

            # Set open price 10s after window start
            if ws.open_price is None:
                ready = (ws.market.window_start or 0) + 10
                if now >= ready:
                    ws.open_price = btc
                    log.info("S4: Window baseline $%.2f for %s", btc, ws.market.question[:40])

            if ws.open_price is None or ws.signal_fired:
                continue

            # No buys in last 20s
            left = (ws.market.window_end - now) if ws.market.window_end else 999
            if left <= 20:
                continue

            # Detect momentum: $25 in 3s
            spike = self.feed.detect_momentum(SPIKE_MOVE_USD, SPIKE_WINDOW_SEC)
            if spike is None:
                continue

            spike_dir = "YES" if spike > 0 else "NO"
            window_move = btc - ws.open_price
            window_dir = "YES" if window_move >= 0 else "NO"

            # Must match window trend
            if spike_dir != window_dir:
                self.stats.rejected += 1
                self.stats.current_signal = f"REJECTED (${spike:+.0f} spike but ${window_move:+.0f} from open)"
                log.info("S4 REJECTED: $%+.0f spike but BTC $%+.0f from open", spike, window_move)
                continue

            side = spike_dir
            ws.signal_fired = True
            ws.signal_side = side
            self.stats.total_signals += 1
            self.stats.current_signal = f"{'UP' if side == 'YES' else 'DOWN'} ${spike:+.0f}"
            log.info(
                "S4 MOMENTUM: $%+.0f in %.0fs, BTC $%+.0f from open → BUY %s | %s",
                spike, SPIKE_WINDOW_SEC, window_move, side, ws.market.question[:40],
            )

            await self.poly.get_market_prices(ws.market)
            pos = await self.poly.buy(ws.market, side, MAX_POSITION_USDC)
            if pos.filled:
                ws.position = pos
                self._open_positions.append(pos)
                self.stats.total_trades += 1
                self.stats.last_action = f"BUY {side} @ ${pos.avg_entry:.4f}"

        await self._check_exits()

    async def _discover(self):
        markets = await self.poly.find_active_btc_5min_markets()
        for mkt in markets:
            if mkt.condition_id not in self._windows:
                self._windows[mkt.condition_id] = S4Window(market=mkt)

    async def _check_exits(self):
        still_open = []
        for pos in self._open_positions:
            if pos.exit_price is not None:
                continue

            bid = await self.poly._get_best_bid(pos.token_id)
            if bid is None:
                still_open.append(pos)
                continue

            gain = ((bid - pos.avg_entry) / pos.avg_entry) * 100
            now = time.time()
            ended = pos.market.window_end and now > pos.market.window_end

            # Trend reversal: BTC crossed to wrong side of window open
            btc_now = self.feed.current_price
            ws = self._windows.get(pos.market.condition_id)
            if btc_now and ws and ws.open_price:
                wrong_side = (
                    (pos.side == "YES" and btc_now < ws.open_price) or
                    (pos.side == "NO" and btc_now > ws.open_price)
                )
                if wrong_side:
                    log.warning("S4 REVERSAL: %s but BTC flipped → selling", pos.side)
                    sold = await self.poly.sell(pos)
                    if sold:
                        self.stats.total_exits += 1
                        self.stats.total_pnl += pos.pnl or 0
                        self._record_hourly(pos.pnl or 0)
                        self.stats.losses += 1
                        self.stats.last_action = f"REVERSAL {pos.side}"
                        self._closed_positions.append(pos)
                    else:
                        still_open.append(pos)
                    continue

            if gain > pos.peak_gain:
                pos.peak_gain = gain

            # Moonbag mode
            if not pos.moonbag_mode and gain >= MOONBAG_PCT:
                pos.moonbag_mode = True
                log.info("S4 MOONBAG: %s +%.1f%%", pos.side, gain)

            should_sell = False
            reason = ""

            # Max take profit at 96c — never wait for resolution
            if bid >= 0.96:
                should_sell = True
                reason = f"MAX TP @${bid:.2f}"
            elif gain >= HARD_CAP_PCT:
                should_sell = True
                reason = f"HARD CAP +{gain:.1f}%"
            elif pos.moonbag_mode:
                floor = pos.peak_gain / 2.0
                if gain <= floor:
                    should_sell = True
                    reason = f"TRAIL +{gain:.1f}% (peak +{pos.peak_gain:.1f}%)"
            elif gain >= PROFIT_TARGET_PCT:
                should_sell = True
                reason = f"PROFIT +{gain:.1f}%"

            if should_sell:
                log.info("S4 EXIT [%s]: %s gain=%.1f%%", reason, pos.side, gain)
                sold = await self.poly.sell(pos)
                if sold:
                    self.stats.total_exits += 1
                    self.stats.total_pnl += pos.pnl or 0
                    self._record_hourly(pos.pnl or 0)
                    if (pos.pnl or 0) >= 0:
                        self.stats.wins += 1
                    else:
                        self.stats.losses += 1
                    self.stats.last_action = f"SELL {pos.side} [{reason}]"
                    self._closed_positions.append(pos)
                else:
                    still_open.append(pos)
            elif ended:
                pos.exit_price = bid
                pos.pnl = (bid - pos.avg_entry) * pos.qty
                self.stats.total_exits += 1
                self.stats.total_pnl += pos.pnl
                self._record_hourly(pos.pnl)
                if pos.pnl >= 0:
                    self.stats.wins += 1
                else:
                    self.stats.losses += 1
                self.stats.last_action = f"SETTLED {pos.side} ${pos.pnl:+.2f}"
                self._closed_positions.append(pos)
            else:
                still_open.append(pos)

        self._open_positions = still_open

    def _record_hourly(self, pnl):
        key = datetime.now(timezone.utc).strftime("%H:00")
        self.stats.hourly_pnl[key] = self.stats.hourly_pnl.get(key, 0) + pnl

    def _daily_reset(self):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self._last_day != today:
            if self._last_day:
                log.info("═══ S4 NEW DAY — resetting hourly P&L ═══")
            self.stats.hourly_pnl = {}
            self._last_day = today
        key = datetime.now(timezone.utc).strftime("%H:00")
        if key not in self.stats.hourly_pnl:
            self.stats.hourly_pnl[key] = 0.0

    @property
    def open_positions(self):
        return self._open_positions

    @property
    def closed_positions(self):
        return self._closed_positions
