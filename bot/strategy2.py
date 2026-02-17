"""
Strategy 2: Passive Limit Order Strategy

Buy the next 5 upcoming 5-minute markets at $0.50-0.53 (before they start),
then immediately place a limit sell at $0.60. Let it sit.

Simple: buy cheap, sell at 60c, repeat.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Set

from bot.config import cfg
from bot.polymarket import PolymarketClient, Market, Position

log = logging.getLogger("strategy2")

BUY_MIN = 0.50
BUY_MAX = 0.53
SELL_TARGET = 0.60
MAX_MARKETS = 5         # buy into the next 5 upcoming markets
USDC_PER_MARKET = 50.0  # spend per market


@dataclass
class S2Stats:
    """Running stats for strategy 2."""
    markets_bought: int = 0
    sells_placed: int = 0
    sells_filled: int = 0
    total_pnl: float = 0.0
    wins: int = 0
    losses: int = 0
    last_action: str = ""


@dataclass
class S2Position:
    """A position in strategy 2."""
    market: Market
    side: str          # always "YES" (buying Up side at ~50c)
    token_id: str
    entry_price: float
    qty: float
    spent: float
    sell_target: float
    entry_time: float
    filled: bool = False
    exit_price: Optional[float] = None
    pnl: Optional[float] = None
    status: str = "open"  # open, sold, resolved


class Strategy2:
    """Passive limit order strategy."""

    def __init__(self, poly: PolymarketClient):
        self.poly = poly
        self.stats = S2Stats()
        self._positions: List[S2Position] = []
        self._closed: List[S2Position] = []
        self._bought_cids: Set[str] = set()
        self._running = False

    async def run(self):
        self._running = True
        log.info("Strategy 2 started | buy=$%.2f-$%.2f sell=$%.2f max_markets=%d",
                 BUY_MIN, BUY_MAX, SELL_TARGET, MAX_MARKETS)

        while self._running:
            try:
                await self._tick()
            except Exception as exc:
                log.error("Strategy 2 tick error: %s", exc, exc_info=True)
            await asyncio.sleep(2)

    def stop(self):
        self._running = False

    async def _tick(self):
        now = time.time()

        # ── 1. Discover upcoming markets and buy into them ──
        if not hasattr(self, "_last_disc") or now - self._last_disc > 60:
            await self._buy_upcoming()
            self._last_disc = now

        # ── 2. Check open positions for sells / resolution ──
        await self._check_positions()

    async def _buy_upcoming(self):
        """Find upcoming markets and buy Up side at 50-53c."""
        markets = await self.poly.find_active_btc_5min_markets()
        now = time.time()

        # Sort by start time, pick ones that haven't started yet
        upcoming = [m for m in markets
                    if m.window_start and m.window_start > now
                    and m.condition_id not in self._bought_cids]
        upcoming.sort(key=lambda m: m.window_start)

        # Only buy up to MAX_MARKETS total open positions
        open_count = len([p for p in self._positions if p.status == "open"])
        slots = MAX_MARKETS - open_count

        for mkt in upcoming[:slots]:
            await self._try_buy(mkt)

    async def _try_buy(self, market: Market):
        """Try to buy the Up side of a market at 50-53c."""
        # Fetch the order book
        await self.poly.get_market_prices(market)
        ask = market.yes_ask

        if ask <= 0:
            return

        # Check if ask is in our buy range
        if ask < BUY_MIN or ask > BUY_MAX:
            log.info("S2: %s ask=$%.3f outside range $%.2f-$%.2f, skipping",
                     market.question[:40], ask, BUY_MIN, BUY_MAX)
            return

        # Buy!
        qty = USDC_PER_MARKET / ask
        import math
        qty = math.floor(qty * 100) / 100

        pos = S2Position(
            market=market,
            side="Up",
            token_id=market.yes_token_id,
            entry_price=ask,
            qty=qty,
            spent=USDC_PER_MARKET,
            sell_target=SELL_TARGET,
            entry_time=time.time(),
            filled=True,
            status="open",
        )

        self._positions.append(pos)
        self._bought_cids.add(market.condition_id)
        self.stats.markets_bought += 1
        self.stats.last_action = f"BUY Up @ ${ask:.3f} | {market.question[:40]}"
        log.info(
            "[S2 DRY] BUY Up %.1f shares @ $%.3f ($%.2f) | sell target $%.2f | %s",
            qty, ask, USDC_PER_MARKET, SELL_TARGET, market.question[:50],
        )

    async def _check_positions(self):
        """Check if any positions hit the sell target or resolved."""
        now = time.time()

        for pos in self._positions:
            if pos.status != "open":
                continue

            # Check if market window ended (resolved)
            window_ended = pos.market.window_end and now > pos.market.window_end

            # Get current bid
            bid = await self.poly._get_best_bid(pos.token_id)

            if bid and bid >= pos.sell_target:
                # Sell target hit!
                pos.exit_price = pos.sell_target
                pos.pnl = (pos.sell_target - pos.entry_price) * pos.qty
                pos.status = "sold"
                self.stats.sells_filled += 1
                self.stats.total_pnl += pos.pnl
                self.stats.wins += 1
                self.stats.last_action = f"SELL @ ${pos.sell_target:.2f} +${pos.pnl:.2f} | {pos.market.question[:30]}"
                self._closed.append(pos)
                log.info(
                    "[S2 DRY] SELL @ $%.2f | PnL: $%.2f | %s",
                    pos.sell_target, pos.pnl, pos.market.question[:50],
                )

            elif window_ended:
                # Market resolved -- check if Up won
                # In dry run, simulate based on whether bid is > 0.5
                if bid and bid > 0.5:
                    pos.exit_price = 1.0
                    pos.pnl = (1.0 - pos.entry_price) * pos.qty
                    self.stats.wins += 1
                else:
                    pos.exit_price = 0.0
                    pos.pnl = -pos.spent
                    self.stats.losses += 1

                pos.status = "resolved"
                self.stats.total_pnl += pos.pnl
                self.stats.last_action = f"RESOLVED PnL=${pos.pnl:+.2f} | {pos.market.question[:30]}"
                self._closed.append(pos)
                log.info(
                    "[S2] RESOLVED: exit=$%.2f PnL=$%.2f | %s",
                    pos.exit_price, pos.pnl, pos.market.question[:50],
                )

    @property
    def open_positions(self) -> List[S2Position]:
        return [p for p in self._positions if p.status == "open"]

    @property
    def closed_positions(self) -> List[S2Position]:
        return self._closed
