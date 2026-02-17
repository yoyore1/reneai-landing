"""
Core arbitrage strategy.

Concept
-------
Every 5 minutes Polymarket opens a new binary market:
  "Will BTC be above $X at HH:MM?"

Binance price updates in real time (milliseconds).  Polymarket prices
lag because human traders need time to react.

When Binance shows a clear directional move during a 5-min window the
outcome is essentially known, but Polymarket odds haven't caught up yet.
We:
  1. Detect the Binance spike (price moved > threshold from window open).
  2. Buy the winning side on Polymarket immediately.
  3. Exit rules:
     - MOONBAG:    If gain hits +20%, let it ride.  Trailing stop at +10%.
     - PROFIT:     If gain is between +10% and +20%, sell immediately.
     - WAIT:       If gain is below +10%, keep holding.
     - PROTECTION: If position drops past -15%, enter protection mode.
                   Sell when it recovers to -10% (accept small loss).
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional, List, Dict

from bot.config import cfg
from bot.binance_feed import BinanceFeed
from bot.polymarket import PolymarketClient, Market, Position

log = logging.getLogger("strategy")


@dataclass
class WindowState:
    """Tracks per-window state."""
    market: Market
    window_open_price: Optional[float] = None  # BTC price at window start
    signal_fired: bool = False                   # did we already trade this window?
    signal_side: str = ""                        # YES or NO
    position: Optional[Position] = None
    # Spike confirmation: spike detected but waiting to confirm
    pending_spike_time: float = 0.0              # when spike was first detected
    pending_spike_dir: str = ""                  # "YES" or "NO"
    pending_spike_price: float = 0.0             # BTC price when spike detected


@dataclass
class StrategyStats:
    """Running statistics for the dashboard."""
    total_signals: int = 0
    total_trades: int = 0
    total_exits: int = 0
    total_pnl: float = 0.0
    wins: int = 0
    losses: int = 0
    current_window: str = ""
    current_signal: str = ""
    last_action: str = ""


class Strategy:
    """
    Runs the Binance-Polymarket arbitrage loop.
    """

    def __init__(self, feed: BinanceFeed, poly: PolymarketClient):
        self.feed = feed
        self.poly = poly
        self.stats = StrategyStats()

        # Active window states keyed by condition_id
        self._windows: Dict[str, WindowState] = {}
        # Positions awaiting exit
        self._open_positions: List[Position] = []
        # Closed positions for logging
        self._closed_positions: List[Position] = []

        self._running = False

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def run(self):
        self._running = True
        log.info("Strategy started  |  spike=$%.0f/%0.fs  profit_target=%.1f%%  dry_run=%s",
                 cfg.spike_move_usd, cfg.spike_window_sec, cfg.profit_target_pct, cfg.dry_run)

        while self._running:
            try:
                await self._tick()
            except Exception as exc:
                log.error("Strategy tick error: %s", exc, exc_info=True)
            await asyncio.sleep(cfg.poll_interval_sec)

    def stop(self):
        self._running = False

    # ------------------------------------------------------------------
    # Single tick
    # ------------------------------------------------------------------

    async def _tick(self):
        if not self.feed.is_live:
            return  # no price yet

        btc_price = self.feed.current_price

        # ---- 1. Refresh active markets every ~30 s ----
        now = time.time()
        if not hasattr(self, "_last_discovery") or now - self._last_discovery > 30:
            await self._discover_markets()
            self._last_discovery = now

        # ---- 2. For each active window, check for spike signal ----
        for cid, ws in list(self._windows.items()):
            # Skip if window has ended
            if ws.market.window_end and now > ws.market.window_end:
                self._windows.pop(cid, None)
                continue

            # Record the BTC price 10s after the window opens (let market settle)
            if ws.window_open_price is None:
                ready_time = (ws.market.window_start or 0) + 10
                if now >= ready_time:
                    ws.window_open_price = btc_price
                    log.info("Window baseline set (10s delay): $%.2f for %s",
                             btc_price, ws.market.question[:50])

            if ws.window_open_price is None or ws.signal_fired:
                continue

            self.stats.current_window = ws.market.question[:60]

            # Don't buy in the last 20 seconds of the window
            time_left = (ws.market.window_end - now) if ws.market.window_end else 999
            if time_left <= 20:
                continue

            # ── Spike detection with confirmation ──
            # Step 1: Detect initial spike
            if not ws.pending_spike_dir:
                spike_delta = self.feed.detect_spike(cfg.spike_move_usd, cfg.spike_window_sec)
                if spike_delta is not None:
                    ws.pending_spike_dir = "YES" if spike_delta > 0 else "NO"
                    ws.pending_spike_time = now
                    ws.pending_spike_price = btc_price
                    log.info(
                        "SPIKE DETECTED: $%+.0f in %.0fs → %s | waiting %.0fs to confirm...",
                        spike_delta, cfg.spike_window_sec,
                        ws.pending_spike_dir, cfg.spike_confirm_sec,
                    )
                    self.stats.current_signal = f"CONFIRMING {ws.pending_spike_dir}..."

            # Step 2: After confirm delay, check if BTC held the direction
            elif not ws.signal_fired:
                elapsed = now - ws.pending_spike_time
                if elapsed >= cfg.spike_confirm_sec:
                    # Did BTC hold the move?
                    if ws.pending_spike_dir == "YES":
                        held = btc_price >= ws.pending_spike_price
                    else:
                        held = btc_price <= ws.pending_spike_price

                    if held:
                        side = ws.pending_spike_dir
                        ws.signal_fired = True
                        ws.signal_side = side
                        self.stats.total_signals += 1
                        confirm_move = btc_price - ws.pending_spike_price
                        self.stats.current_signal = f"CONFIRMED {side} ${confirm_move:+.0f}"
                        log.info(
                            "SPIKE CONFIRMED: %s held after %.1fs (BTC $%.2f → $%.2f, %+$.0f) | Buy on %s",
                            side, elapsed, ws.pending_spike_price, btc_price,
                            confirm_move, ws.market.question[:50],
                        )

                        # Execute the buy
                        await self.poly.get_market_prices(ws.market)
                        position = await self.poly.buy(ws.market, side, cfg.max_position_usdc)
                        if position.filled:
                            ws.position = position
                            self._open_positions.append(position)
                            self.stats.total_trades += 1
                            self.stats.last_action = f"BUY {side} @ ${position.avg_entry:.4f}"
                    else:
                        # Reversed — fake-out, skip this window
                        ws.pending_spike_dir = ""
                        ws.pending_spike_time = 0
                        log.info(
                            "SPIKE REJECTED: BTC reversed after %.1fs ($%.2f → $%.2f) — fake-out",
                            elapsed, ws.pending_spike_price, btc_price,
                        )
                        self.stats.current_signal = "REJECTED (fake-out)"

        # ---- 3. Monitor open positions for exit ----
        await self._check_exits()

    # ------------------------------------------------------------------
    # Market discovery
    # ------------------------------------------------------------------

    async def _discover_markets(self):
        markets = await self.poly.find_active_btc_5min_markets()
        for mkt in markets:
            if mkt.condition_id not in self._windows:
                self._windows[mkt.condition_id] = WindowState(market=mkt)
                log.info("Tracking new market: %s", mkt.question[:70])

    # ------------------------------------------------------------------
    # Exit management
    # ------------------------------------------------------------------

    async def _check_exits(self):
        still_open: List[Position] = []
        for pos in self._open_positions:
            if pos.exit_price is not None:
                continue  # already closed

            # Get current bid price for our token
            bid = await self.poly._get_best_bid(pos.token_id)

            if bid is None:
                still_open.append(pos)
                continue

            gain_pct = ((bid - pos.avg_entry) / pos.avg_entry) * 100
            now = time.time()
            window_ended = pos.market.window_end and now > pos.market.window_end

            # Track peak gain
            if gain_pct > pos.peak_gain:
                pos.peak_gain = gain_pct

            # --- Mode transitions ---

            # Moonbag: gain hits 20%+ → let it ride, trailing stop at 10%
            if (not pos.moonbag_mode and not pos.protection_mode
                    and gain_pct >= cfg.moonbag_pct):
                pos.moonbag_mode = True
                log.info(
                    "MOONBAG MODE: %s hit +%.1f%%! Letting it ride, "
                    "trailing stop at +%.1f%%",
                    pos.side, gain_pct, cfg.profit_target_pct,
                )
                self.stats.last_action = f"MOONBAG {pos.side} +{gain_pct:.1f}%"

            # Protection: drops past -15% → damage control
            if not pos.protection_mode and gain_pct <= cfg.drawdown_trigger_pct:
                pos.protection_mode = True
                pos.moonbag_mode = False
                log.info(
                    "PROTECTION MODE: %s dropped to %.1f%% | will sell at %.1f%%",
                    pos.side, gain_pct, cfg.protection_exit_pct,
                )
                self.stats.last_action = f"PROTECT {pos.side} @{gain_pct:.1f}%"

            # --- Exit decisions ---
            should_sell = False
            sell_reason = ""

            # HARD STOP — no exceptions, sell immediately
            if gain_pct <= cfg.hard_stop_pct:
                should_sell = True
                sell_reason = f"HARD STOP {gain_pct:.1f}% (limit={cfg.hard_stop_pct}%)"
                log.warning(
                    "HARD STOP: %s at %.1f%% — emergency sell",
                    pos.side, gain_pct,
                )

            elif pos.moonbag_mode:
                # Dynamic trailing stop: floor = half the peak gain
                # Peak +20% → stop +10%, peak +30% → stop +15%, peak +50% → stop +25%
                trailing_floor = pos.peak_gain / 2.0
                if gain_pct <= trailing_floor:
                    should_sell = True
                    sell_reason = (
                        f"MOONBAG TRAIL +{gain_pct:.1f}% "
                        f"(peak +{pos.peak_gain:.1f}%, floor +{trailing_floor:.1f}%)"
                    )
            elif pos.protection_mode:
                # Was below -15%, sell when recovers to -10%
                if gain_pct >= cfg.protection_exit_pct:
                    should_sell = True
                    sell_reason = f"PROTECTION EXIT {gain_pct:+.1f}%"
            else:
                # Normal: sell between 10% and 20%
                if gain_pct >= cfg.profit_target_pct:
                    should_sell = True
                    sell_reason = f"PROFIT +{gain_pct:.1f}%"

            if should_sell:
                log.info(
                    "EXIT [%s]: %s | entry=%.4f bid=%.4f gain=%.1f%%",
                    sell_reason, pos.side, pos.avg_entry, bid, gain_pct,
                )
                sold = await self.poly.sell(pos)
                if sold:
                    self.stats.total_exits += 1
                    self.stats.total_pnl += pos.pnl or 0
                    if (pos.pnl or 0) >= 0:
                        self.stats.wins += 1
                    else:
                        self.stats.losses += 1
                    self.stats.last_action = f"SELL {pos.side} [{sell_reason}]"
                    self._closed_positions.append(pos)
                else:
                    still_open.append(pos)
            elif window_ended:
                # Window over -- settles on-chain
                log.info(
                    "WINDOW ENDED: %s | entry=%.4f | will settle on-chain",
                    pos.side, pos.avg_entry,
                )
                pos.exit_price = bid
                pos.pnl = (bid - pos.avg_entry) * pos.qty
                self.stats.total_exits += 1
                self.stats.total_pnl += pos.pnl
                if pos.pnl >= 0:
                    self.stats.wins += 1
                else:
                    self.stats.losses += 1
                self.stats.last_action = f"SETTLED {pos.side} PnL=${pos.pnl:.2f}"
                self._closed_positions.append(pos)
            else:
                still_open.append(pos)

        self._open_positions = still_open
