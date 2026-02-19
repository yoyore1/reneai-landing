"""
Strategy 4: Momentum Pro — Smarter entries, fewer losses

Same core as S1 (detect BTC spike → buy on Polymarket) but with
4 additional filters that dramatically improve win rate:

1. VOLUME CONFIRMATION
   Spike must happen on heavy volume (>2 BTC in 5s).
   A $20 move on thin volume = noise. On heavy volume = real.

2. TIME-OF-WINDOW SCALING
   Spike threshold adapts to how much time is left:
   - First 2 min: need $25 move (more time to reverse)
   - 2-3 min in: need $20 move
   - 3-4 min in: need $15 move (less time to reverse = safer)

3. VOLATILITY FILTER
   Only trade when BTC has been moving. If the 10-min range is < $30,
   the market is dead and any spike is likely a fake-out.

4. COOLDOWN AFTER LOSSES
   After 2 consecutive losses, wait 10 minutes before trading again.
   Choppy markets cause streaks — sit them out.
"""

import asyncio
import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, List, Dict

from bot.polymarket import PolymarketClient, Market, Position
from bot.binance_feed import BinanceFeed

log = logging.getLogger("strategy4")

# Filters
MIN_VOLUME_BTC = 2.0         # need this many BTC traded in last 5s
MIN_RANGE_10M = 30.0          # BTC must have moved $30+ in last 10 min
COOLDOWN_LOSSES = 2           # consecutive losses before cooldown
COOLDOWN_SEC = 600            # 10 minutes

# Spike thresholds by time-in-window (seconds elapsed → $ threshold)
SPIKE_BY_TIME = [
    (0,   25.0),   # 0-120s into window: need $25
    (120, 20.0),   # 120-180s: need $20
    (180, 15.0),   # 180-240s: need $15
]
SPIKE_WINDOW_SEC = 3.0

# Exits
PROFIT_TARGET_PCT = 5.0
MOONBAG_PCT = 15.0
HARD_CAP_PCT = 20.0
MAX_POSITION_USDC = 50.0


@dataclass
class S4Stats:
    total_signals: int = 0
    total_trades: int = 0
    total_exits: int = 0
    total_pnl: float = 0.0
    wins: int = 0
    losses: int = 0
    rejected_volume: int = 0
    rejected_volatility: int = 0
    rejected_trend: int = 0
    rejected_cooldown: int = 0
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

    def __init__(self, feed: BinanceFeed, poly: PolymarketClient):
        self.feed = feed
        self.poly = poly
        self.stats = S4Stats()
        self._windows: Dict[str, S4Window] = {}
        self._open_positions: List[Position] = []
        self._closed_positions: List[Position] = []
        self._running = False
        self._last_day = ""
        self._consecutive_losses = 0
        self._cooldown_until = 0.0

    async def run(self):
        self._running = True
        log.info("Strategy 4 (Momentum Pro) started | volume>%.1f BTC | range>$%.0f | cooldown=%ds",
                 MIN_VOLUME_BTC, MIN_RANGE_10M, COOLDOWN_SEC)
        while self._running:
            try:
                await self._tick()
            except Exception as exc:
                log.error("S4 tick error: %s", exc, exc_info=True)
            await asyncio.sleep(0.5)

    def stop(self):
        self._running = False

    async def _tick(self):
        if not self.feed.is_live:
            return

        btc = self.feed.current_price
        now = time.time()
        self._daily_reset()

        if not hasattr(self, "_last_disc") or now - self._last_disc > 30:
            await self._discover()
            self._last_disc = now

        for cid, ws in list(self._windows.items()):
            if ws.market.window_end and now > ws.market.window_end:
                self._windows.pop(cid, None)
                continue

            # Set baseline 10s after window start
            if ws.open_price is None:
                ready = (ws.market.window_start or 0) + 10
                if now >= ready:
                    ws.open_price = btc
                    log.info("S4 Pro: baseline $%.2f for %s", btc, ws.market.question[:40])

            if ws.open_price is None or ws.signal_fired:
                continue

            left = (ws.market.window_end - now) if ws.market.window_end else 999
            if left <= 20:
                continue

            # ── Filter 1: Cooldown ──
            if now < self._cooldown_until:
                self.stats.current_signal = f"COOLDOWN ({int(self._cooldown_until - now)}s left)"
                continue

            # ── Filter 2: Volatility — is BTC actually moving? ──
            btc_range = self.feed.get_price_range(600)  # 10-min range
            if btc_range < MIN_RANGE_10M:
                continue

            # ── Time-scaled spike threshold ──
            elapsed = now - (ws.market.window_start or now)
            threshold = SPIKE_BY_TIME[-1][1]  # default to lowest
            for min_elapsed, thresh in SPIKE_BY_TIME:
                if elapsed >= min_elapsed:
                    threshold = thresh

            # ── Detect momentum ──
            spike = self.feed.detect_momentum(threshold, SPIKE_WINDOW_SEC)
            if spike is None:
                continue

            spike_dir = "YES" if spike > 0 else "NO"

            # ── Filter 3: Window trend ──
            window_move = btc - ws.open_price
            window_dir = "YES" if window_move >= 0 else "NO"
            if spike_dir != window_dir:
                self.stats.rejected_trend += 1
                self.stats.current_signal = f"REJECTED trend (${spike:+.0f} spike but ${window_move:+.0f} from open)"
                log.info("S4 Pro REJECTED trend: $%+.0f spike but $%+.0f from open", spike, window_move)
                continue

            # ── Filter 4: Volume — is the move backed by real trades? ──
            vol = self.feed.get_volume_btc(5.0)
            if vol < MIN_VOLUME_BTC:
                self.stats.rejected_volume += 1
                self.stats.current_signal = f"REJECTED volume ({vol:.1f} BTC < {MIN_VOLUME_BTC})"
                log.info("S4 Pro REJECTED volume: %.1f BTC < %.1f minimum", vol, MIN_VOLUME_BTC)
                continue

            # ── ALL FILTERS PASSED → BUY ──
            side = spike_dir
            ws.signal_fired = True
            ws.signal_side = side
            self.stats.total_signals += 1
            self.stats.current_signal = f"{'UP' if side == 'YES' else 'DOWN'} ${spike:+.0f} vol={vol:.1f}BTC"
            log.info(
                "S4 Pro SIGNAL: $%+.0f in %.0fs | vol=%.1fBTC | range=$%.0f | thresh=$%.0f | BTC $%+.0f from open → %s | %s",
                spike, SPIKE_WINDOW_SEC, vol, btc_range, threshold,
                window_move, side, ws.market.question[:35],
            )

            await self.poly.get_market_prices(ws.market)
            pos = await self.poly.buy(ws.market, side, MAX_POSITION_USDC)
            if pos.filled:
                ws.position = pos
                self._open_positions.append(pos)
                self.stats.total_trades += 1
                self.stats.last_action = f"BUY {side} @${pos.avg_entry:.3f} (vol={vol:.1f}BTC)"

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

            # Trend reversal check — only if position is negative AND BTC is $10+ wrong
            REVERSAL_BUFFER = 10.0
            btc_now = self.feed.current_price
            ws = self._windows.get(pos.market.condition_id)
            if btc_now and ws and ws.open_price and gain < 0:
                wrong = (pos.side == "YES" and btc_now < ws.open_price - REVERSAL_BUFFER) or \
                        (pos.side == "NO" and btc_now > ws.open_price + REVERSAL_BUFFER)
                if wrong:
                    log.warning("S4 Pro REVERSAL: %s flipped → selling", pos.side)
                    sold = await self.poly.sell(pos)
                    if sold:
                        self._record_exit(pos, loss=True, reason="REVERSAL")
                    else:
                        still_open.append(pos)
                    continue

            if gain > pos.peak_gain:
                pos.peak_gain = gain

            if not pos.moonbag_mode and gain >= MOONBAG_PCT:
                pos.moonbag_mode = True
                log.info("S4 Pro MOONBAG: %s +%.1f%%", pos.side, gain)

            should_sell = False
            reason = ""

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
                log.info("S4 Pro EXIT [%s]: %s gain=%.1f%%", reason, pos.side, gain)
                sold = await self.poly.sell(pos)
                if sold:
                    is_win = (pos.pnl or 0) >= 0
                    self._record_exit(pos, loss=not is_win, reason=reason)
                else:
                    still_open.append(pos)
            elif ended:
                pos.exit_price = bid
                pos.pnl = (bid - pos.avg_entry) * pos.qty
                is_win = pos.pnl >= 0
                self._record_exit(pos, loss=not is_win, reason="SETTLED")
            else:
                still_open.append(pos)

        self._open_positions = still_open

    def _record_exit(self, pos, loss: bool, reason: str):
        self.stats.total_exits += 1
        self.stats.total_pnl += pos.pnl or 0
        self._record_hourly(pos.pnl or 0)
        if loss:
            self.stats.losses += 1
            self._consecutive_losses += 1
            if self._consecutive_losses >= COOLDOWN_LOSSES:
                self._cooldown_until = time.time() + COOLDOWN_SEC
                log.warning(
                    "S4 Pro COOLDOWN: %d consecutive losses → pausing %ds",
                    self._consecutive_losses, COOLDOWN_SEC,
                )
                self.stats.last_action = f"COOLDOWN ({self._consecutive_losses} losses)"
        else:
            self.stats.wins += 1
            self._consecutive_losses = 0
        if "COOLDOWN" not in (self.stats.last_action or ""):
            self.stats.last_action = f"{'SELL' if not loss else 'LOSS'} {pos.side} [{reason}]"
        self._closed_positions.append(pos)

    def _record_hourly(self, pnl):
        key = datetime.now(timezone.utc).strftime("%H:00")
        self.stats.hourly_pnl[key] = self.stats.hourly_pnl.get(key, 0) + pnl

    def _daily_reset(self):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self._last_day != today:
            if self._last_day:
                log.info("═══ S4 Pro NEW DAY ═══")
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
