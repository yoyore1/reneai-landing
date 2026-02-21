"""
Strategy 4: Buy both sides (real arbitrage).

When yes_ask + no_ask < 1, buy both sides in equal share size.
At resolution one side pays $1, the other $0 → you always get $1 per share.
Profit per share = 1 - (yes_ask + no_ask). No directional bet.
"""

import asyncio
import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, List, Dict, Set

from bot.polymarket import PolymarketClient, Market

log = logging.getLogger("strategy4")

# Only buy both sides when combined ask is below this (lock in min edge)
ARB_MAX_SUM = 0.98   # yes_ask + no_ask < 0.98 → at least 2c profit per share
USDC_PER_TRADE = 50.0  # max total spend per arb (both sides combined)


@dataclass
class S4Stats:
    markets_checked: int = 0
    trades: int = 0
    skipped_no_edge: int = 0
    total_pnl: float = 0.0
    wins: int = 0  # arb trades are always "wins" when filled
    losses: int = 0
    last_action: str = ""
    hourly_pnl: dict = field(default_factory=dict)


@dataclass
class S4ArbPosition:
    """One arb = we bought both Yes and No in equal shares; hold to resolution."""
    market: Market
    qty: float           # shares of each side
    yes_entry: float
    no_entry: float
    spent_yes: float
    spent_no: float
    entry_time: float
    status: str = "open"  # open | resolved
    pnl: Optional[float] = None


class Strategy4:
    """Buy both sides when yes_ask + no_ask < 1; hold to resolution for locked-in profit."""

    def __init__(self, poly: PolymarketClient):
        self.poly = poly
        self.stats = S4Stats()
        self._positions: List[S4ArbPosition] = []
        self._closed: List[S4ArbPosition] = []
        self._traded_cids: Set[str] = set()
        self._trackers: Dict[str, Market] = {}
        self._running = False
        self._last_hour_key = ""
        self._last_day = ""
        self._last_discovery = 0.0

    async def run(self):
        self._running = True
        log.info(
            "Strategy 4 started (buy both sides) | arb when yes_ask+no_ask < %.2f | max $%.0f per trade",
            ARB_MAX_SUM, USDC_PER_TRADE,
        )
        while self._running:
            try:
                await self._tick()
            except Exception as exc:
                log.error("Strategy 4 tick error: %s", exc, exc_info=True)
            await asyncio.sleep(1)

    def stop(self):
        self._running = False

    async def _tick(self):
        now = time.time()
        if now - self._last_discovery > 30:
            await self._discover()
            self._last_discovery = now

        for cid, mkt in list(self._trackers.items()):
            if cid in self._traded_cids:
                continue
            if not mkt.window_end or now >= mkt.window_end:
                self._trackers.pop(cid, None)
                self._traded_cids.discard(cid)
                continue

            self.stats.markets_checked += 1
            await self.poly.get_market_prices(mkt)
            yes_ask = mkt.yes_ask if mkt.yes_ask > 0 else 0
            no_ask = mkt.no_ask if mkt.no_ask > 0 else 0
            if yes_ask <= 0 or no_ask <= 0:
                continue

            total_ask = yes_ask + no_ask
            if total_ask >= ARB_MAX_SUM:
                self.stats.skipped_no_edge += 1
                self.stats.last_action = f"S4 SKIP (yes+no=%.2f >= %.2f)" % (total_ask, ARB_MAX_SUM)
                continue

            # Edge: 1 - total_ask profit per share. Buy equal shares of both.
            qty = math.floor((USDC_PER_TRADE / total_ask) * 100) / 100
            if qty <= 0:
                continue

            spent_yes = qty * yes_ask
            spent_no = qty * no_ask
            # Execute both legs
            pos_yes = await self.poly.buy(mkt, "YES", spent_yes)
            pos_no = await self.poly.buy(mkt, "NO", spent_no)
            if not pos_yes.filled or not pos_no.filled:
                self.stats.last_action = "S4 BUY FAILED (one or both legs)"
                log.warning("[S4] One or both legs did not fill for %s", mkt.question[:40])
                continue

            # Use actual filled qty (in case of partial fill use the smaller)
            qty_yes = pos_yes.qty
            qty_no = pos_no.qty
            qty_actual = min(qty_yes, qty_no)
            if qty_actual <= 0:
                continue

            arb = S4ArbPosition(
                market=mkt,
                qty=qty_actual,
                yes_entry=pos_yes.avg_entry,
                no_entry=pos_no.avg_entry,
                spent_yes=qty_actual * pos_yes.avg_entry,
                spent_no=qty_actual * pos_no.avg_entry,
                entry_time=time.time(),
            )
            self._positions.append(arb)
            self._traded_cids.add(cid)
            self.stats.trades += 1
            edge = 1.0 - (arb.yes_entry + arb.no_entry)
            self.stats.last_action = f"S4 ARB {arb.qty:.2f} shares each | edge {edge*100:.1f}c"
            log.info(
                "[S4] ARB BUY BOTH | yes=%.3f no=%.3f sum=%.3f | qty=%.2f each | edge=%.1fc | %s",
                arb.yes_entry, arb.no_entry, arb.yes_entry + arb.no_entry,
                arb.qty, edge * 100, mkt.question[:45],
            )

        await self._check_positions()
        self._hourly_report()

    async def _discover(self):
        markets = await self.poly.find_active_btc_5min_markets()
        for mkt in markets:
            cid = mkt.condition_id
            if cid in self._traded_cids:
                continue
            if mkt.window_start and mkt.window_end:
                remaining = mkt.window_end - time.time()
                if 0 < remaining <= 300:
                    self._trackers[cid] = mkt

    async def _check_positions(self):
        now = time.time()
        still_open: List[S4ArbPosition] = []
        for arb in self._positions:
            if arb.status != "open":
                continue
            if not arb.market.window_end or now < arb.market.window_end:
                still_open.append(arb)
                continue

            # Resolution: one side pays 1, one pays 0 → we get arb.qty * 1
            arb.status = "resolved"
            total_spent = arb.spent_yes + arb.spent_no
            arb.pnl = arb.qty * 1.0 - total_spent
            self.stats.total_pnl += arb.pnl
            self.stats.wins += 1
            self._record_hourly_pnl(arb.pnl)
            self._closed.append(arb)
            self.stats.last_action = f"S4 RESOLVED +${arb.pnl:.2f}"
            log.info(
                "[S4] RESOLVED | PnL $%+.2f (qty=%.2f spent=%.2f) | %s",
                arb.pnl, arb.qty, total_spent, arb.market.question[:45],
            )

        self._positions = still_open

    def _record_hourly_pnl(self, pnl: float):
        hour_key = datetime.now(timezone.utc).strftime("%H:00")
        self.stats.hourly_pnl[hour_key] = self.stats.hourly_pnl.get(hour_key, 0) + pnl

    def _hourly_report(self):
        now = datetime.now(timezone.utc)
        hour_key = now.strftime("%H:00")
        today = now.strftime("%Y-%m-%d")
        if self._last_day != today:
            if self._last_day:
                log.info("═══ S4 NEW DAY — resetting hourly P&L ═══")
            self.stats.hourly_pnl = {}
            self._last_day = today
        if hour_key != self._last_hour_key and self._last_hour_key:
            prev = self.stats.hourly_pnl.get(self._last_hour_key, 0)
            log.info("═══ S4 HOURLY [%s] PnL: $%+.2f | Total: $%+.2f | Trades: %d",
                     self._last_hour_key, prev, self.stats.total_pnl, self.stats.trades)
        if hour_key not in self.stats.hourly_pnl:
            self.stats.hourly_pnl[hour_key] = 0.0
        self._last_hour_key = hour_key

    @property
    def open_positions(self) -> List[S4ArbPosition]:
        return [p for p in self._positions if p.status == "open"]

    @property
    def closed_positions(self) -> List[S4ArbPosition]:
        return self._closed
