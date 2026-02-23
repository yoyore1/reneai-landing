"""
Strategy 3: Late Momentum — Buy the Leader at 1:30 Remaining

Logic:
  - Monitor each 5-min window from the start
  - Track the highest price Up and Down reach between 2:15 and 1:30 remaining
  - Buy at 2:40 remaining or less: keep checking every tick; buy as soon as one side hits 70c+ (and not choppy).
  - If we get to 1:00 remaining and still no side is 70c+ → don't buy at all for the rest of that market.
  - Analysis 3:00→2:40: track highs for choppy check (both 65c+ → skip).
  - Manipulation exit: if market favors one side (e.g. 60c+) but BTC price is on the other side
    of the strike, sell that side immediately to avoid liquidation.
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
from datetime import datetime

from bot.time_util import date_key_est, hour_key_est
from typing import Optional, List, Dict, Set

from bot.polymarket import PolymarketClient, Market, Position
from bot.config import cfg

log = logging.getLogger("strategy3")

BUY_THRESHOLD = 0.70       # side must be 70c+ to buy
SKIP_THRESHOLD = 0.65      # if BOTH sides hit this during analysis window, skip
ANALYSIS_START = 180.0     # start tracking at 3:00 remaining
BUY_AT_REMAINING = 160.0   # actually buy at 2:40 remaining
SKIP_NO_LEADER_AT = 60.0   # at 1:00 remaining: if neither side 70c+, don't buy at all
USDC_PER_TRADE = 50.0
MANIPULATION_FAVOR_CENTS = 0.60   # detect manipulation: one side 60c+ but BTC on opposite side of strike
MANIPULATION_HARD_SELL_CENTS = 0.30  # when manipulation detected, hard sell if our side drops to 30c or less
S3_HARD_STOP_CENTS = 0.30   # for ALL S3 positions: if our side goes to 30c or below, sell immediately (avoid liquidation)
S3_SELL_AT_CENTS = 0.97     # take profit: sell when our side reaches 97c or above
S3_MAX_BUY_CENTS = 0.94     # don't buy if ask is above 94c


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
    daily_pnl: float = 0.0


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
    manipulation_detected: bool = False  # when True, hard sell at 30c or below


@dataclass
class S3WindowTracker:
    """Tracks price highs for a window during the analysis period."""
    market: Market
    up_high: float = 0.0      # highest Up price seen during analysis window
    down_high: float = 0.0    # highest Down price seen during analysis window
    analyzing: bool = False   # are we in the analysis window?
    decision_made: bool = False
    no_leader_at_1min: bool = False   # at 1 min remaining neither side was 70c+ → don't buy
    checked_no_leader_1min: bool = False  # have we run the 1-min check


class Strategy3:

    def __init__(self, poly: PolymarketClient, feed=None):
        self.poly = poly
        self.feed = feed  # Binance feed for BTC price (manipulation check)
        self.stats = S3Stats()
        self._positions: List[S3Position] = []
        self._closed: List[S3Position] = []
        self._trackers: Dict[str, S3WindowTracker] = {}
        self._decided_cids: Set[str] = set()
        self._running = False
        self._last_hour_key = ""
        self._last_day = ""
        self._consecutive_losses = 0
        self._pause_until = 0.0

    async def run(self):
        self._running = True
        log.info(
            "Strategy 3 started | buy at 2:40 or less (first 70c+); if no 70c+ by 1:00 → skip market | threshold=$%.2f",
            BUY_THRESHOLD,
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

            # Analysis window: 3:00 to 2:40 remaining (track highs)
            if remaining <= ANALYSIS_START and remaining > BUY_AT_REMAINING:
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

            # At 1:00 remaining: if still no side 70c+, give up for this market (don't buy rest of window)
            if remaining <= SKIP_NO_LEADER_AT and not tracker.checked_no_leader_1min:
                tracker.checked_no_leader_1min = True
                await self.poly.get_market_prices(mkt)
                up_bid_1m = await self.poly._get_best_bid(mkt.yes_token_id)
                down_bid_1m = await self.poly._get_best_bid(mkt.no_token_id)
                up_1m = up_bid_1m or 0
                down_1m = down_bid_1m or 0
                if up_1m < BUY_THRESHOLD and down_1m < BUY_THRESHOLD:
                    tracker.no_leader_at_1min = True
                    log.info("S3: At 1:00 left neither side 70c+ (Up=%.2f Down=%.2f) → won't buy this market", up_1m, down_1m)

            # Buy window: 2:40 remaining or less. Every tick: buy as soon as one side 70c+; or skip if no leader at 1m / choppy
            if remaining <= BUY_AT_REMAINING and not tracker.decision_made:
                # Already passed 1 min with no leader → give up
                if tracker.no_leader_at_1min:
                    tracker.decision_made = True
                    self._decided_cids.add(cid)
                    self.stats.markets_analyzed += 1
                    self.stats.skipped_no_leader += 1
                    self.stats.last_action = "SKIP NO LEADER (at 1:00 neither 70c+)"
                    log.info("S3 SKIP: No leader at 1:00 → not buying for rest of market")
                    continue

                # Choppy (both hit 65c+ in analysis) → skip
                if tracker.up_high >= SKIP_THRESHOLD and tracker.down_high >= SKIP_THRESHOLD:
                    tracker.decision_made = True
                    self._decided_cids.add(cid)
                    self.stats.markets_analyzed += 1
                    self.stats.skipped_choppy += 1
                    self.stats.last_action = f"SKIP CHOPPY (Up high={tracker.up_high:.2f} Down high={tracker.down_high:.2f})"
                    log.info("S3 SKIP: Both sides hit $%.2f+ — too choppy", SKIP_THRESHOLD)
                    continue

                # Get current prices: buy as soon as one side is 70c+
                await self.poly.get_market_prices(mkt)
                up_bid = await self.poly._get_best_bid(mkt.yes_token_id)
                down_bid = await self.poly._get_best_bid(mkt.no_token_id)
                up_now = up_bid or 0
                down_now = down_bid or 0

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

                if buy_side is not None:
                    if cfg.daily_loss_limit_usdc < 0 and self.stats.daily_pnl <= cfg.daily_loss_limit_usdc:
                        log.info("S3: Skipping buy — daily P&L $%.2f at or below limit $%.2f", self.stats.daily_pnl, cfg.daily_loss_limit_usdc)
                        tracker.decision_made = True
                        self._decided_cids.add(cid)
                        self.stats.markets_analyzed += 1
                        continue
                    if now < self._pause_until:
                        log.info("S3: Skipping buy — cooldown after %d consecutive losses (%.0f min left)", self._consecutive_losses, (self._pause_until - now) / 60)
                        tracker.decision_made = True
                        self._decided_cids.add(cid)
                        self.stats.markets_analyzed += 1
                        continue
                    ask = mkt.yes_ask if buy_side == "Up" else mkt.no_ask
                    if ask <= 0 or ask >= 1.0:
                        ask = buy_price
                    # Don't buy above 94c
                    if ask > S3_MAX_BUY_CENTS:
                        continue  # wait for better price or skip
                    # Buy now (at 2:40 or less; ask <= 94c)
                    tracker.decision_made = True
                    self._decided_cids.add(cid)
                    self.stats.markets_analyzed += 1
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
                    "[S3] BUY %s %.1f shares @ $%.3f ($%.2f) | %.0fs left | %s",
                    buy_side, qty, ask, USDC_PER_TRADE, remaining, mkt.question[:45],
                )
                # else: no side 70c+ yet, keep waiting (don't set decision_made)

        # Check positions for resolution
        await self._check_positions()
        self._hourly_report()

    async def _s3_sell(self, pos: S3Position) -> bool:
        """Sell an S3 position via Polymarket client. Updates pos.exit_price, pos.pnl, pos.status."""
        p = Position(
            market=pos.market, side=pos.side, token_id=pos.token_id,
            qty=pos.qty, avg_entry=pos.entry_price,
        )
        sold = await self.poly.sell(p)
        if sold:
            pos.exit_price = p.exit_price
            pos.pnl = p.pnl
            pos.status = "resolved"
            pnl_val = pos.pnl or 0
            self.stats.total_pnl += pnl_val
            self.stats.daily_pnl += pnl_val
            self._record_hourly_pnl(pnl_val)
            self._closed.append(pos)
        return sold

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

            mkt = pos.market
            # ----- While window open: dollar loss cap, then hard stop 30c, take profit 97c -----
            if mkt.window_end and now < mkt.window_end:
                our_bid = await self.poly._get_best_bid(pos.token_id)
                if our_bid is not None:
                    if our_bid < pos.entry_price and cfg.max_loss_per_trade_usdc > 0:
                        dollar_loss = (pos.entry_price - our_bid) * pos.qty
                        if dollar_loss >= cfg.max_loss_per_trade_usdc:
                            sold = await self._s3_sell(pos)
                            if sold:
                                self.stats.losses += 1
                                self._consecutive_losses += 1
                                if self._consecutive_losses >= cfg.consecutive_losses_to_pause:
                                    self._pause_until = now + cfg.pause_minutes_after_streak * 60
                                    log.info("[S3] %d consecutive losses → pause new buys for %.0f min", self._consecutive_losses, cfg.pause_minutes_after_streak)
                                self.stats.last_action = f"S3 MAX $ LOSS ${dollar_loss:.2f}"
                                log.info("[S3] MAX $ LOSS: %s @ %.0fc (loss $%.2f)", pos.side, our_bid * 100, dollar_loss)
                                continue
                    if our_bid <= S3_HARD_STOP_CENTS:
                        sold = await self._s3_sell(pos)
                        if sold:
                            self.stats.losses += 1
                            self._consecutive_losses += 1
                            if self._consecutive_losses >= cfg.consecutive_losses_to_pause:
                                self._pause_until = now + cfg.pause_minutes_after_streak * 60
                                log.info("[S3] %d consecutive losses → pause new buys for %.0f min", self._consecutive_losses, cfg.pause_minutes_after_streak)
                            self.stats.last_action = f"S3 HARD STOP {pos.side} @ {our_bid*100:.0f}c"
                            log.info("[S3] HARD STOP: %s @ %.0fc (sell to avoid liquidation)", pos.side, our_bid * 100)
                            continue
                    elif our_bid >= S3_SELL_AT_CENTS:
                        sold = await self._s3_sell(pos)
                        if sold:
                            self.stats.wins += 1
                            self._consecutive_losses = 0
                            self.stats.last_action = f"S3 SELL {pos.side} @ {our_bid*100:.0f}c (take profit)"
                            log.info("[S3] SELL %s @ %.0fc (take profit at 97c+)", pos.side, our_bid * 100)
                            continue

            # ----- Manipulation: detect, then hard sell at 30c or below (while window open) -----
            if mkt.window_end and now < mkt.window_end and self.feed and getattr(self.feed, "current_price", None):
                btc = self.feed.current_price
                strike = mkt.reference_price
                if strike is not None:
                    up_bid = await self.poly._get_best_bid(mkt.yes_token_id)
                    down_bid = await self.poly._get_best_bid(mkt.no_token_id)
                    up_bid = up_bid or 0
                    down_bid = down_bid or 0
                    # Detect: market favors Up (60c+) but BTC below strike → we're long Up on wrong side
                    if not pos.manipulation_detected and up_bid >= MANIPULATION_FAVOR_CENTS and btc < strike and pos.side == "Up":
                        pos.manipulation_detected = True
                        self.stats.last_action = f"S3 MANIPULATION DETECTED {pos.side} (Up {up_bid:.2f}c but BTC < strike) — hard sell at 30c"
                        log.info("[S3] MANIPULATION DETECTED: Up favored at %.2fc but BTC $%.0f < strike $%.0f → will hard sell at 30c",
                                 up_bid * 100, btc, strike)
                    # Detect: market favors Down (60c+) but BTC above strike → we're long Down on wrong side
                    elif not pos.manipulation_detected and down_bid >= MANIPULATION_FAVOR_CENTS and btc > strike and pos.side == "Down":
                        pos.manipulation_detected = True
                        self.stats.last_action = f"S3 MANIPULATION DETECTED {pos.side} (Down {down_bid:.2f}c but BTC > strike) — hard sell at 30c"
                        log.info("[S3] MANIPULATION DETECTED: Down favored at %.2fc but BTC $%.0f > strike $%.0f → will hard sell at 30c",
                                 down_bid * 100, btc, strike)
                    # Hard sell: if manipulation detected and our side is at 30c or less, sell
                    if pos.manipulation_detected:
                        our_bid = up_bid if pos.side == "Up" else down_bid
                        if our_bid is not None and our_bid <= MANIPULATION_HARD_SELL_CENTS:
                            sold = await self._s3_sell(pos)
                            if sold:
                                self.stats.losses += 1
                                self._consecutive_losses += 1
                                if self._consecutive_losses >= cfg.consecutive_losses_to_pause:
                                    self._pause_until = now + cfg.pause_minutes_after_streak * 60
                                    log.info("[S3] %d consecutive losses → pause new buys for %.0f min", self._consecutive_losses, cfg.pause_minutes_after_streak)
                                self.stats.last_action = f"S3 MANIPULATION HARD SELL {pos.side} @ {our_bid*100:.0f}c"
                                log.info("[S3] MANIPULATION HARD SELL: %s @ %.0fc (was 30c or below)", pos.side, our_bid * 100)
                                continue

            if not mkt.window_end or now <= mkt.window_end:
                continue

            # Window ended — resolve
            bid = await self.poly._get_best_bid(pos.token_id)
            if bid and bid > 0.5:
                pos.exit_price = 1.0
                pos.pnl = (1.0 - pos.entry_price) * pos.qty
                self.stats.wins += 1
                self._consecutive_losses = 0
            else:
                pos.exit_price = 0.0
                pos.pnl = -pos.spent
                self.stats.losses += 1
                self._consecutive_losses += 1
                if self._consecutive_losses >= cfg.consecutive_losses_to_pause:
                    self._pause_until = now + cfg.pause_minutes_after_streak * 60
                    log.info("[S3] %d consecutive losses → pause new buys for %.0f min", self._consecutive_losses, cfg.pause_minutes_after_streak)

            pos.status = "resolved"
            self.stats.total_pnl += pos.pnl
            self.stats.daily_pnl += pos.pnl
            self._record_hourly_pnl(pos.pnl)
            self.stats.last_action = f"RESOLVED {pos.side} ${pos.pnl:+.2f}"
            self._closed.append(pos)
            log.info(
                "[S3] RESOLVED %s: $%.2f → PnL $%+.2f | %s",
                pos.side, pos.exit_price, pos.pnl, pos.market.question[:45],
            )

    def _record_hourly_pnl(self, pnl: float):
        hour_key = hour_key_est()
        self.stats.hourly_pnl[hour_key] = self.stats.hourly_pnl.get(hour_key, 0) + pnl

    def _hourly_report(self):
        hour_key = hour_key_est()
        today = date_key_est()

        if self._last_day != today:
            if self._last_day:
                log.info("═══ S3 NEW DAY — resetting hourly P&L and daily P&L ═══")
            self.stats.hourly_pnl = {}
            self.stats.daily_pnl = 0.0
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
