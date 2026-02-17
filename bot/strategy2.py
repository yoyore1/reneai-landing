"""
Strategy 2: Both-Sides Passive Limit Order Strategy

For each upcoming 5-minute market (before it starts):
  - Buy BOTH Up and Down at $0.50-0.51
  - Place limit sell on BOTH at $0.60
  - One side will always move toward $0.60+ as the market picks a direction

The edge: you pay ~$1.00 total for both sides, one pays out $0.60+
giving you profit. If one side hits $0.60 you make ~$0.10 per share.
"""

import asyncio
import logging
import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, List, Set

from bot.polymarket import PolymarketClient, Market

log = logging.getLogger("strategy2")

BUY_MIN = 0.50
BUY_MAX = 0.51          # tight range — only buy at 50-51c
SELL_TARGET = 0.60
MAX_MARKETS = 5          # buy into the next 5 upcoming markets
USDC_PER_SIDE = 25.0     # $25 per side = $50 total per market


@dataclass
class S2Stats:
    """Running stats for strategy 2."""
    markets_bought: int = 0
    total_positions: int = 0
    sells_filled: int = 0
    total_pnl: float = 0.0
    wins: int = 0
    losses: int = 0
    last_action: str = ""
    # Hourly tracking
    hourly_pnl: dict = None  # {hour_key: pnl}
    current_hour_pnl: float = 0.0
    last_hour_report: str = ""

    def __post_init__(self):
        if self.hourly_pnl is None:
            self.hourly_pnl = {}


@dataclass
class S2Position:
    """A position in strategy 2 (one side of a market)."""
    market: Market
    side: str              # "Up" or "Down"
    token_id: str
    entry_price: float
    qty: float
    spent: float
    sell_target: float
    entry_time: float
    filled: bool = False
    exit_price: Optional[float] = None
    pnl: Optional[float] = None
    status: str = "open"   # open, sold, resolved


class Strategy2:
    """Both-sides passive limit order strategy."""

    def __init__(self, poly: PolymarketClient):
        self.poly = poly
        self.stats = S2Stats()
        self._positions: List[S2Position] = []
        self._closed: List[S2Position] = []
        self._bought_cids: Set[str] = set()
        self._running = False
        self._last_hour_key = ""

    async def run(self):
        self._running = True
        log.info(
            "Strategy 2 started | buy BOTH sides @ $%.2f-$%.2f | sell @ $%.2f | max=%d markets",
            BUY_MIN, BUY_MAX, SELL_TARGET, MAX_MARKETS,
        )

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

        # ── 1. Discover upcoming markets and buy both sides ──
        if not hasattr(self, "_last_disc") or now - self._last_disc > 60:
            await self._buy_upcoming()
            self._last_disc = now

        # ── 2. Check open positions for sell fills / resolution ──
        await self._check_positions()

        # ── 3. Hourly P&L report ──
        self._hourly_report()

    # ------------------------------------------------------------------
    # Buy both sides of upcoming markets
    # ------------------------------------------------------------------

    async def _buy_upcoming(self):
        markets = await self.poly.find_active_btc_5min_markets()
        now = time.time()

        # Only markets that haven't started yet
        upcoming = [m for m in markets
                    if m.window_start and m.window_start > now
                    and m.condition_id not in self._bought_cids]
        upcoming.sort(key=lambda m: m.window_start)

        # How many more markets can we buy into
        open_market_count = len(self._bought_cids) - len([
            cid for cid in self._bought_cids
            if all(p.status != "open" for p in self._positions if p.market.condition_id == cid)
        ])
        slots = MAX_MARKETS - max(0, open_market_count)

        for mkt in upcoming[:max(0, slots)]:
            await self._try_buy_both(mkt)

    async def _try_buy_both(self, market: Market):
        """Buy BOTH Up and Down sides of a market at 50-51c each."""
        await self.poly.get_market_prices(market)
        up_ask = market.yes_ask
        down_ask = market.no_ask

        # Both must be in the 50-51c range
        if not (BUY_MIN <= up_ask <= BUY_MAX):
            log.info("S2: %s Up ask=$%.3f outside $%.2f-$%.2f, skipping",
                     market.question[:35], up_ask, BUY_MIN, BUY_MAX)
            return
        if not (BUY_MIN <= down_ask <= BUY_MAX):
            log.info("S2: %s Down ask=$%.3f outside $%.2f-$%.2f, skipping",
                     market.question[:35], down_ask, BUY_MIN, BUY_MAX)
            return

        self._bought_cids.add(market.condition_id)
        self.stats.markets_bought += 1

        # Buy Up side
        up_qty = math.floor((USDC_PER_SIDE / up_ask) * 100) / 100
        up_pos = S2Position(
            market=market, side="Up", token_id=market.yes_token_id,
            entry_price=up_ask, qty=up_qty, spent=USDC_PER_SIDE,
            sell_target=SELL_TARGET, entry_time=time.time(), filled=True,
        )
        self._positions.append(up_pos)
        self.stats.total_positions += 1

        # Buy Down side
        dn_qty = math.floor((USDC_PER_SIDE / down_ask) * 100) / 100
        dn_pos = S2Position(
            market=market, side="Down", token_id=market.no_token_id,
            entry_price=down_ask, qty=dn_qty, spent=USDC_PER_SIDE,
            sell_target=SELL_TARGET, entry_time=time.time(), filled=True,
        )
        self._positions.append(dn_pos)
        self.stats.total_positions += 1

        total_spent = USDC_PER_SIDE * 2
        self.stats.last_action = f"BUY BOTH @ Up=${up_ask:.3f} Down=${down_ask:.3f} | {market.question[:35]}"
        log.info(
            "[S2] BUY BOTH | Up %.1f@$%.3f + Down %.1f@$%.3f = $%.2f | sell@$%.2f | %s",
            up_qty, up_ask, dn_qty, down_ask, total_spent, SELL_TARGET, market.question[:45],
        )

    # ------------------------------------------------------------------
    # Check positions
    # ------------------------------------------------------------------

    async def _check_positions(self):
        now = time.time()

        for pos in self._positions:
            if pos.status != "open":
                continue

            window_ended = pos.market.window_end and now > pos.market.window_end
            bid = await self.poly._get_best_bid(pos.token_id)

            if bid and bid >= pos.sell_target:
                # Limit sell hit at $0.60!
                pos.exit_price = pos.sell_target
                pos.pnl = (pos.sell_target - pos.entry_price) * pos.qty
                pos.status = "sold"
                self.stats.sells_filled += 1
                self.stats.total_pnl += pos.pnl
                self.stats.wins += 1
                self._record_hourly_pnl(pos.pnl)
                self.stats.last_action = f"SELL {pos.side} @${pos.sell_target:.2f} +${pos.pnl:.2f}"
                self._closed.append(pos)
                log.info(
                    "[S2] SELL %s @ $%.2f | PnL: +$%.2f | %s",
                    pos.side, pos.sell_target, pos.pnl, pos.market.question[:45],
                )

            elif window_ended:
                # Market resolved
                if bid and bid > 0.5:
                    pos.exit_price = 1.0
                    pos.pnl = (1.0 - pos.entry_price) * pos.qty
                    self.stats.wins += 1
                elif bid is not None:
                    pos.exit_price = 0.0
                    pos.pnl = -pos.spent
                    self.stats.losses += 1
                else:
                    pos.exit_price = 0.0
                    pos.pnl = -pos.spent
                    self.stats.losses += 1

                pos.status = "resolved"
                self.stats.total_pnl += pos.pnl
                self._record_hourly_pnl(pos.pnl)
                self.stats.last_action = f"RESOLVED {pos.side} ${pos.pnl:+.2f}"
                self._closed.append(pos)
                log.info(
                    "[S2] RESOLVED %s: exit=$%.2f PnL=$%.2f | %s",
                    pos.side, pos.exit_price, pos.pnl, pos.market.question[:45],
                )

    # ------------------------------------------------------------------
    # Hourly P&L tracking
    # ------------------------------------------------------------------

    def _record_hourly_pnl(self, pnl: float):
        hour_key = datetime.now(timezone.utc).strftime("%H:00")
        self.stats.hourly_pnl[hour_key] = self.stats.hourly_pnl.get(hour_key, 0) + pnl

    def _hourly_report(self):
        now = datetime.now(timezone.utc)
        hour_key = now.strftime("%H:00")
        today = now.strftime("%Y-%m-%d")

        # Reset at midnight (new day)
        if not hasattr(self, "_last_day") or self._last_day != today:
            if hasattr(self, "_last_day") and self._last_day:
                log.info("═══ S2 NEW DAY — resetting hourly P&L ═══")
            self.stats.hourly_pnl = {}
            self._last_day = today

        # Log report when a new hour starts
        if hour_key != self._last_hour_key and self._last_hour_key:
            prev_pnl = self.stats.hourly_pnl.get(self._last_hour_key, 0)
            log.info(
                "═══ S2 HOURLY [%s] ═══  PnL: $%+.2f  |  Day total: $%+.2f  |  "
                "Sells: %d  W: %d  L: %d",
                self._last_hour_key, prev_pnl, self.stats.total_pnl,
                self.stats.sells_filled, self.stats.wins, self.stats.losses,
            )
            self.stats.last_hour_report = (
                f"{self._last_hour_key}: ${prev_pnl:+.2f} (day ${self.stats.total_pnl:+.2f})"
            )

        # Make sure current hour exists in the dict even with $0
        if hour_key not in self.stats.hourly_pnl:
            self.stats.hourly_pnl[hour_key] = 0.0

        self._last_hour_key = hour_key

    @property
    def open_positions(self) -> List[S2Position]:
        return [p for p in self._positions if p.status == "open"]

    @property
    def closed_positions(self) -> List[S2Position]:
        return self._closed
