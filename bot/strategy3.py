"""
Strategy 3: Late Momentum — Buy the Leader at 1:30 Remaining

Logic:
  - Monitor each 5-min window from the start
  - Track the highest price Up and Down reach between 2:15 and 1:30 remaining
  - At exactly 1:30 remaining:
    - If BOTH Up and Down hit $0.65+ during 2:15→1:30 → SKIP (choppy, no edge)
    - Otherwise, if Up OR Down is $0.70+ → BUY that side, hold to resolution
  - Resolution: winning side pays $1.00, losing side pays $0.00

The idea: by 1:30 left the direction is mostly decided. If one side is
at 70c+ it's very likely to win. But if BOTH sides hit 65c during the
analysis window, the market is chopping and we can't trust either side.
"""

import asyncio
import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, List, Dict, Set

from bot.polymarket import PolymarketClient, Market

log = logging.getLogger("strategy3")

BUY_THRESHOLD = 0.70       # side must be 70c+ at the 1:30 mark
SKIP_THRESHOLD = 0.65      # if BOTH sides hit this during analysis window, skip
ANALYSIS_START = 180.0      # start tracking at 3:00 remaining
ANALYSIS_END = 120.0        # trigger buy decision at 2:00 remaining
USDC_PER_TRADE = 50.0


@dataclass
class S3Stats:
    markets_analyzed: int = 0
    trades: int = 0
    skipped_choppy: int = 0
    skipped_no_leader: int = 0
    total_pnl: float = 0.0
    wins: int = 0
    losses: int = 0
    last_action: str = ""
    hourly_pnl: dict = field(default_factory=dict)
    last_hour_report: str = ""


@dataclass
class S3Position:
    market: Market
    side: str
    token_id: str
    entry_price: float
    qty: float
    spent: float
    entry_time: float
    exit_price: Optional[float] = None
    pnl: Optional[float] = None
    status: str = "open"


@dataclass
class S3WindowTracker:
    """Tracks price highs for a window during the analysis period."""
    market: Market
    up_high: float = 0.0      # highest Up price seen during analysis window
    down_high: float = 0.0    # highest Down price seen during analysis window
    analyzing: bool = False   # are we in the analysis window?
    decision_made: bool = False


class Strategy3:

    def __init__(self, poly: PolymarketClient):
        self.poly = poly
        self.stats = S3Stats()
        self._positions: List[S3Position] = []
        self._closed: List[S3Position] = []
        self._trackers: Dict[str, S3WindowTracker] = {}
        self._decided_cids: Set[str] = set()
        self._running = False
        self._last_hour_key = ""
        self._last_day = ""

    async def run(self):
        self._running = True
        log.info(
            "Strategy 3 started | buy_threshold=$%.2f | skip_if_both>=$%.2f | at 1:30 remaining",
            BUY_THRESHOLD, SKIP_THRESHOLD,
        )
        while self._running:
            try:
                await self._tick()
            except Exception as exc:
                log.error("Strategy 3 tick error: %s", exc, exc_info=True)
            await asyncio.sleep(1)

    def stop(self):
        self._running = False

    async def _tick(self):
        now = time.time()

        # Discover markets
        if not hasattr(self, "_last_disc") or now - self._last_disc > 30:
            await self._discover()
            self._last_disc = now

        # Analyze each tracked window
        for cid, tracker in list(self._trackers.items()):
            if tracker.decision_made:
                continue

            mkt = tracker.market
            if not mkt.window_end:
                continue

            remaining = mkt.window_end - now

            # Window ended, clean up
            if remaining <= 0:
                self._trackers.pop(cid, None)
                continue

            # Analysis window: 2:45 to 1:30 remaining
            if remaining <= ANALYSIS_START and remaining > ANALYSIS_END:
                if not tracker.analyzing:
                    tracker.analyzing = True
                    log.info("S3: Analyzing %s (%.0fs left)", mkt.question[:40], remaining)

                # Poll both sides and track highs
                await self.poly.get_market_prices(mkt)
                if mkt.yes_ask > 0:
                    # Use ask as proxy for current price
                    up_price = 1.0 - mkt.no_ask if mkt.no_ask > 0 else mkt.yes_ask
                    down_price = 1.0 - mkt.yes_ask if mkt.yes_ask > 0 else mkt.no_ask
                    # Actually, the bid is what we'd pay — let's get bids
                    up_bid = await self.poly._get_best_bid(mkt.yes_token_id)
                    down_bid = await self.poly._get_best_bid(mkt.no_token_id)

                    if up_bid and up_bid > tracker.up_high:
                        tracker.up_high = up_bid
                    if down_bid and down_bid > tracker.down_high:
                        tracker.down_high = down_bid

            # Decision time: 1:30 remaining
            elif remaining <= ANALYSIS_END and not tracker.decision_made:
                tracker.decision_made = True
                self._decided_cids.add(cid)
                self.stats.markets_analyzed += 1

                # Get current prices
                await self.poly.get_market_prices(mkt)
                up_bid = await self.poly._get_best_bid(mkt.yes_token_id)
                down_bid = await self.poly._get_best_bid(mkt.no_token_id)
                up_now = up_bid or 0
                down_now = down_bid or 0

                log.info(
                    "S3 DECISION: %s | Up now=$%.3f (high=$%.3f) | Down now=$%.3f (high=$%.3f)",
                    mkt.question[:35], up_now, tracker.up_high, down_now, tracker.down_high,
                )

                # Check skip condition: both sides hit 65c+ during analysis
                if tracker.up_high >= SKIP_THRESHOLD and tracker.down_high >= SKIP_THRESHOLD:
                    self.stats.skipped_choppy += 1
                    self.stats.last_action = f"SKIP CHOPPY (Up high={tracker.up_high:.2f} Down high={tracker.down_high:.2f})"
                    log.info(
                        "S3 SKIP: Both sides hit $%.2f+ (Up=%.3f Down=%.3f) — too choppy",
                        SKIP_THRESHOLD, tracker.up_high, tracker.down_high,
                    )
                    continue

                # Check buy condition: one side is 70c+
                buy_side = None
                buy_price = 0
                buy_token = ""

                if up_now >= BUY_THRESHOLD and up_now >= down_now:
                    buy_side = "Up"
                    buy_price = up_now
                    buy_token = mkt.yes_token_id
                elif down_now >= BUY_THRESHOLD and down_now >= up_now:
                    buy_side = "Down"
                    buy_price = down_now
                    buy_token = mkt.no_token_id

                if buy_side is None:
                    self.stats.skipped_no_leader += 1
                    self.stats.last_action = f"SKIP NO LEADER (Up={up_now:.2f} Down={down_now:.2f})"
                    log.info(
                        "S3 SKIP: No side at $%.2f+ (Up=%.3f Down=%.3f)",
                        BUY_THRESHOLD, up_now, down_now,
                    )
                    continue

                # BUY
                # Use the ask price for entry (what we'd actually pay)
                ask = mkt.yes_ask if buy_side == "Up" else mkt.no_ask
                if ask <= 0 or ask >= 1.0:
                    ask = buy_price  # fallback to bid

                qty = math.floor((USDC_PER_TRADE / ask) * 100) / 100
                pos = S3Position(
                    market=mkt, side=buy_side, token_id=buy_token,
                    entry_price=ask, qty=qty, spent=USDC_PER_TRADE,
                    entry_time=time.time(),
                )
                self._positions.append(pos)
                self.stats.trades += 1
                self.stats.last_action = f"BUY {buy_side} @ ${ask:.3f} | {mkt.question[:30]}"
                log.info(
                    "[S3] BUY %s %.1f shares @ $%.3f ($%.2f) | 1:30 left | %s",
                    buy_side, qty, ask, USDC_PER_TRADE, mkt.question[:45],
                )

        # Check positions for resolution
        await self._check_positions()
        self._hourly_report()

    async def _discover(self):
        markets = await self.poly.find_active_btc_5min_markets()
        now = time.time()
        for mkt in markets:
            cid = mkt.condition_id
            if cid in self._trackers or cid in self._decided_cids:
                continue
            # Only track markets that are currently active or about to start
            if mkt.window_start and mkt.window_end:
                remaining = mkt.window_end - now
                if 0 < remaining <= 300:
                    self._trackers[cid] = S3WindowTracker(market=mkt)

    async def _check_positions(self):
        now = time.time()
        for pos in self._positions:
            if pos.status != "open":
                continue
            if not pos.market.window_end or now <= pos.market.window_end:
                continue

            # Window ended — resolve
            bid = await self.poly._get_best_bid(pos.token_id)
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
            self._record_hourly_pnl(pos.pnl)
            self.stats.last_action = f"RESOLVED {pos.side} ${pos.pnl:+.2f}"
            self._closed.append(pos)
            log.info(
                "[S3] RESOLVED %s: $%.2f → PnL $%+.2f | %s",
                pos.side, pos.exit_price, pos.pnl, pos.market.question[:45],
            )

    def _record_hourly_pnl(self, pnl: float):
        hour_key = datetime.now(timezone.utc).strftime("%H:00")
        self.stats.hourly_pnl[hour_key] = self.stats.hourly_pnl.get(hour_key, 0) + pnl

    def _hourly_report(self):
        now = datetime.now(timezone.utc)
        hour_key = now.strftime("%H:00")
        today = now.strftime("%Y-%m-%d")

        if self._last_day != today:
            if self._last_day:
                log.info("═══ S3 NEW DAY — resetting hourly P&L ═══")
            self.stats.hourly_pnl = {}
            self._last_day = today

        if hour_key != self._last_hour_key and self._last_hour_key:
            prev_pnl = self.stats.hourly_pnl.get(self._last_hour_key, 0)
            log.info(
                "═══ S3 HOURLY [%s] ═══  PnL: $%+.2f  |  Total: $%+.2f  |  W:%d L:%d Skip:%d",
                self._last_hour_key, prev_pnl, self.stats.total_pnl,
                self.stats.wins, self.stats.losses,
                self.stats.skipped_choppy + self.stats.skipped_no_leader,
            )

        if hour_key not in self.stats.hourly_pnl:
            self.stats.hourly_pnl[hour_key] = 0.0
        self._last_hour_key = hour_key

    @property
    def open_positions(self) -> List[S3Position]:
        return [p for p in self._positions if p.status == "open"]

    @property
    def closed_positions(self) -> List[S3Position]:
        return self._closed
