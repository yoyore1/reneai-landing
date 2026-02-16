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
        log.info("Strategy started  |  spike_threshold=%.2f%%  profit_target=%.1f%%  dry_run=%s",
                 cfg.spike_threshold_pct, cfg.profit_target_pct, cfg.dry_run)

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

            # Calculate move from window open
            move_pct = ((btc_price - ws.window_open_price) / ws.window_open_price) * 100
            self.stats.current_window = ws.market.question[:60]

            # Don't buy in the last 20 seconds of the window
            time_left = (ws.market.window_end - now) if ws.market.window_end else 999
            if abs(move_pct) >= cfg.spike_threshold_pct and time_left > 20:
                # Determine side: if BTC went UP → YES wins, if DOWN → NO wins
                side = "YES" if move_pct > 0 else "NO"
                ws.signal_fired = True
                ws.signal_side = side
                self.stats.total_signals += 1
                self.stats.current_signal = f"{'UP' if side == 'YES' else 'DOWN'} {move_pct:+.3f}%"
                log.info(
                    "SIGNAL: BTC %+.3f%% from $%.2f → $%.2f  |  Buy %s on %s",
                    move_pct, ws.window_open_price, btc_price, side, ws.market.question[:50],
                )

                # Fetch latest Polymarket prices
                await self.poly.get_market_prices(ws.market)

                # Execute the buy
                position = await self.poly.buy(ws.market, side, cfg.max_position_usdc)
                if position.filled:
                    ws.position = position
                    self._open_positions.append(position)
                    self.stats.total_trades += 1
                    self.stats.last_action = f"BUY {side} @ ${position.avg_entry:.4f}"

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

            if pos.moonbag_mode:
                # Was above 20%, trailing stop: sell if drops back to 10%
                if gain_pct <= cfg.profit_target_pct:
                    should_sell = True
                    sell_reason = (
                        f"MOONBAG STOP +{gain_pct:.1f}% "
                        f"(peak +{pos.peak_gain:.1f}%)"
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
