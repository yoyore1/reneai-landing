"""
Strategy 3 PERFECT: Late Momentum with extra chaos filters.

Goal: behave like Strategy 3 (buy the leader late), but:
  - Be more selective on entries.
  - Avoid markets that recently flipped favorites or whipped around.

Key differences from Strategy 3:
  - Higher favorite threshold: BUY at 75c+ instead of 70c+.
  - Chaos filters on the favorite during the analysis+buy window:
      * Flip-count: how many times the favorite switched sides.
      * Range: max - min favorite price.
  - Time-aware strictness: stricter rules in the last 90s.

We never touch the original Strategy3; this is a separate, more conservative variant.
"""

import asyncio
import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, List, Dict, Set

from bot.time_util import date_key_est, hour_key_est
from bot.polymarket import PolymarketClient, Market, Position
from bot.config import cfg
from bot import notify

log = logging.getLogger("strategy3_perfect")


# --- Core thresholds (more conservative than Strategy 3) -----------------------

BUY_THRESHOLD = 0.75       # side must be 75c+ to buy (vs 70c in Strategy 3)
SKIP_THRESHOLD = 0.60      # if BOTH sides hit 60c between 3:30 and end, don't buy (same as Strategy 3)
ANALYSIS_START = 210.0     # start tracking at 3:30 remaining
BUY_AT_REMAINING = 180.0   # buy window 3:00–1:00 remaining
SKIP_NO_LEADER_AT = 60.0   # at 1:00 remaining: if neither side 75c+, don't buy at all
USDC_PER_TRADE = 30.0      # overridden by cfg.s3_usdc_per_trade when config is loaded

MANIPULATION_FAVOR_CENTS = 0.60
MANIPULATION_HARD_SELL_CENTS = 0.30
S3_HARD_STOP_CENTS = 0.30
S3_SELL_AT_CENTS = 0.95
S3_MAX_BUY_CENTS = 0.90

# Chaos filters (favorite behaviour during analysis+buy window)
FAV_FLIP_SKIP = 2          # skip if favorite changed sides 2+ times
FAV_RANGE_SKIP = 0.20      # skip if favorite range (max-min) >= 20c
LATE_STRICT_REMAINING = 90.0  # last 1:30 is extra strict
LATE_FLIP_SKIP = 1         # in last 1:30, skip if any flip
LATE_RANGE_SKIP = 0.15     # in last 1:30, skip if range >= 15c


@dataclass
class S3Stats:
    markets_analyzed: int = 0
    trades: int = 0
    skipped_choppy: int = 0
    skipped_no_leader: int = 0
    skipped_chaos: int = 0
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
    manipulation_detected: bool = False
    sell_order_id: Optional[str] = None


@dataclass
class S3WindowTracker:
    """Tracks price highs and favorite behaviour during the analysis period."""

    market: Market
    up_high: float = 0.0
    down_high: float = 0.0
    analyzing: bool = False
    decision_made: bool = False
    no_leader_at_1min: bool = False
    checked_no_leader_1min: bool = False

    # Chaos-tracking fields (for favorite behaviour)
    fav_last_side: Optional[str] = None  # "Up" or "Down"
    fav_flips: int = 0
    fav_max: float = 0.0
    fav_min: float = 1.0


class Strategy3Perfect:
    """Conservative late-momentum strategy with chaos filters."""

    def __init__(self, poly: PolymarketClient, feed=None):
        self.poly = poly
        self.feed = feed
        self.stats = S3Stats()
        self._positions: List[S3Position] = []
        self._closed: List[S3Position] = []
        self._trackers: Dict[str, S3WindowTracker] = {}
        self._decided_cids: Set[str] = set()
        self._running = False
        self._last_hour_key = ""
        self._last_day = ""

    def _allowed_to_trade_now(self) -> bool:
        """Same calendar gating as Strategy 3."""
        from bot.time_util import now_est

        now = now_est()
        hour, minute = now.hour, now.minute
        start_h = getattr(cfg, "s3_trade_start_hour_est", 0)
        start_m = getattr(cfg, "s3_trade_start_minute_est", 0)
        end = getattr(cfg, "s3_trade_end_hour_est", 5)

        past_start = (hour > start_h) or (hour == start_h and minute >= start_m)
        if start_h <= end:
            in_window = past_start and (hour < end)
        else:
            in_window = past_start or (hour < end)
        if not in_window:
            return False

        target = getattr(cfg, "s3_daily_profit_target_usdc", 100.0)
        if target <= 0:
            return True
        return getattr(self.stats, "daily_pnl", 0) < target

    async def run(self):
        self._running = True
        log.info(
            "Strategy 3 PERFECT started | buy 75c+ leader, 3:00–1:00, skip choppy + recent chaos | threshold=$%.2f",
            BUY_THRESHOLD,
        )
        while self._running:
            try:
                await self._tick()
            except Exception as exc:
                log.error("Strategy 3 PERFECT tick error: %s", exc, exc_info=True)

            has_open = any(p.status == "open" for p in self._positions)
            sleep_sec = 0.5 if has_open else 1.0
            await asyncio.sleep(sleep_sec)

    def stop(self):
        self._running = False

    async def _tick(self):
        await self._discover()
        now = time.time()

        for cid, tracker in list(self._trackers.items()):
            if tracker.decision_made:
                continue
            mkt = tracker.market
            if not mkt.window_end:
                continue

            remaining = mkt.window_end - now

            if remaining <= 0:
                self._trackers.pop(cid, None)
                continue

            # Analysis window: 3:30 till end (track highs and favorite behaviour)
            if remaining <= ANALYSIS_START and remaining > 0 and not tracker.decision_made:
                if not tracker.analyzing:
                    tracker.analyzing = True
                    log.info("S3perf: Analyzing %s (%.0fs left)", mkt.question[:40], remaining)

                await self.poly.get_market_prices(mkt)
                if mkt.yes_ask > 0:
                    up_bid = await self.poly._get_best_bid(mkt.yes_token_id)
                    down_bid = await self.poly._get_best_bid(mkt.no_token_id)

                    if up_bid and up_bid > tracker.up_high:
                        tracker.up_high = up_bid
                    if down_bid and down_bid > tracker.down_high:
                        tracker.down_high = down_bid

                    # --- Chaos tracking: favorite flips + range ---
                    up_val = up_bid or 0.0
                    down_val = down_bid or 0.0
                    fav_side = "Up" if up_val >= down_val else "Down"
                    fav_price = max(up_val, down_val)

                    # Flip-count: count side changes of favorite
                    if tracker.fav_last_side and tracker.fav_last_side != fav_side:
                        tracker.fav_flips += 1
                    tracker.fav_last_side = fav_side

                    # Range: track max/min favorite price over analysis/buy window
                    if fav_price > tracker.fav_max:
                        tracker.fav_max = fav_price
                    if fav_price < tracker.fav_min:
                        tracker.fav_min = fav_price

            # At 1:00 remaining: if still no side 75c+, give up for this market
            if remaining <= SKIP_NO_LEADER_AT and not tracker.checked_no_leader_1min:
                tracker.checked_no_leader_1min = True
                await self.poly.get_market_prices(mkt)
                up_bid_1m = await self.poly._get_best_bid(mkt.yes_token_id)
                down_bid_1m = await self.poly._get_best_bid(mkt.no_token_id)
                up_1m = up_bid_1m or 0.0
                down_1m = down_bid_1m or 0.0
                if up_1m < BUY_THRESHOLD and down_1m < BUY_THRESHOLD:
                    tracker.no_leader_at_1min = True
                    log.info("S3perf: At 1:00 left neither side %.0fc+ (Up=%.2f Down=%.2f) → won't buy this market",
                             BUY_THRESHOLD * 100, up_1m, down_1m)

            # Buy window: 3:00–1:00 remaining. Never buy in last 60s (too late, too risky).
            if remaining <= BUY_AT_REMAINING and remaining >= SKIP_NO_LEADER_AT and not tracker.decision_made:
                if not self._allowed_to_trade_now():
                    tracker.decision_made = True
                    self._decided_cids.add(cid)
                    self.stats.markets_analyzed += 1
                    continue

                if tracker.no_leader_at_1min:
                    tracker.decision_made = True
                    self._decided_cids.add(cid)
                    self.stats.markets_analyzed += 1
                    self.stats.skipped_no_leader += 1
                    self.stats.last_action = "SKIP NO LEADER (at 1:00 neither 75c+)"
                    log.info("S3perf SKIP: No leader at 1:00 → not buying for rest of market")
                    continue

                # Classic choppy: both sides hit 60c+ in analysis
                if tracker.up_high >= SKIP_THRESHOLD and tracker.down_high >= SKIP_THRESHOLD:
                    tracker.decision_made = True
                    self._decided_cids.add(cid)
                    self.stats.markets_analyzed += 1
                    self.stats.skipped_choppy += 1
                    self.stats.last_action = f"SKIP CHOPPY (Up high={tracker.up_high:.2f} Down high={tracker.down_high:.2f})"
                    log.info("S3perf SKIP: Both sides hit 60c+ between 3:30 and end — too choppy")
                    continue

                # --- Chaos filter: favorite flips + range ---
                fav_range = max(0.0, tracker.fav_max - tracker.fav_min)
                # Base rule across whole window
                if tracker.fav_flips >= FAV_FLIP_SKIP or fav_range >= FAV_RANGE_SKIP:
                    tracker.decision_made = True
                    self._decided_cids.add(cid)
                    self.stats.markets_analyzed += 1
                    self.stats.skipped_chaos += 1
                    self.stats.last_action = (
                        f"SKIP CHAOS (flips={tracker.fav_flips} range={fav_range:.2f})"
                    )
                    log.info(
                        "S3perf SKIP: CHAOS flips=%d range=%.2f (need calmer tape to buy)",
                        tracker.fav_flips,
                        fav_range,
                    )
                    continue

                # Extra strict in the last 90s
                if remaining <= LATE_STRICT_REMAINING:
                    if tracker.fav_flips >= LATE_FLIP_SKIP or fav_range >= LATE_RANGE_SKIP:
                        tracker.decision_made = True
                        self._decided_cids.add(cid)
                        self.stats.markets_analyzed += 1
                        self.stats.skipped_chaos += 1
                        self.stats.last_action = (
                            f"SKIP LATE CHAOS (flips={tracker.fav_flips} range={fav_range:.2f})"
                        )
                        log.info(
                            "S3perf SKIP: LATE CHAOS flips=%d range=%.2f (tighter filter in last 90s)",
                            tracker.fav_flips,
                            fav_range,
                        )
                        continue

                # Get current prices: buy as soon as one side is 75c+
                await self.poly.get_market_prices(mkt)
                up_bid = await self.poly._get_best_bid(mkt.yes_token_id)
                down_bid = await self.poly._get_best_bid(mkt.no_token_id)
                up_now = up_bid or 0.0
                down_now = down_bid or 0.0

                buy_side = None
                buy_price = 0.0
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
                    ask = mkt.yes_ask if buy_side == "Up" else mkt.no_ask
                    if ask <= 0 or ask >= 1.0:
                        ask = buy_price
                    if ask > S3_MAX_BUY_CENTS:
                        # Too stretched; wait for better price or skip
                        log.info(
                            "S3perf: %s ask %.2f > max %.2f — skip this tick",
                            buy_side,
                            ask,
                            S3_MAX_BUY_CENTS,
                        )
                        continue

                    tracker.decision_made = True
                    self._decided_cids.add(cid)
                    self.stats.markets_analyzed += 1
                    poly_side = "YES" if buy_side == "Up" else "NO"
                    real_pos = await self.poly.buy(mkt, poly_side, cfg.s3_usdc_per_trade)
                    if not real_pos.filled:
                        log.warning("[S3perf] Buy order not filled (FOK may have been killed) — skipping")
                        continue

                    qty = real_pos.qty or math.floor((cfg.s3_usdc_per_trade / ask) * 100) / 100
                    pos = S3Position(
                        market=mkt,
                        side=buy_side,
                        token_id=buy_token,
                        entry_price=real_pos.avg_entry or ask,
                        qty=qty,
                        spent=cfg.s3_usdc_per_trade,
                        entry_time=time.time(),
                    )
                    self._positions.append(pos)
                    self.stats.trades += 1
                    self.stats.last_action = f"BUY {buy_side} @ ${ask:.3f} | {mkt.question[:30]}"
                    log.info(
                        "[S3perf] BUY %s %.1f shares @ $%.3f ($%.2f) | %.0fs left | %s",
                        buy_side,
                        qty,
                        ask,
                        cfg.s3_usdc_per_trade,
                        remaining,
                        mkt.question[:45],
                    )

        await self._check_positions()
        self._hourly_report()

    async def _s3_sell(self, pos: S3Position, market_order: bool = False, min_price: float = 0.01) -> bool:
        p = Position(
            market=pos.market,
            side=pos.side,
            token_id=pos.token_id,
            qty=pos.qty,
            avg_entry=pos.entry_price,
        )
        sold = await self.poly.sell(p, market_order=market_order, min_price=min_price)
        if sold:
            pos.exit_price = p.exit_price
            pos.pnl = p.pnl
            pos.status = "resolved"
            pnl_val = pos.pnl or 0.0
            self.stats.total_pnl += pnl_val
            self.stats.daily_pnl += pnl_val
            self._record_hourly_pnl(pnl_val)
            self._closed.append(pos)
        elif p.qty < pos.qty:
            pos.qty = p.qty
        return sold

    async def _discover(self):
        markets = await self.poly.find_active_btc_5min_markets()
        now = time.time()
        for mkt in markets:
            cid = mkt.condition_id
            if cid in self._trackers or cid in self._decided_cids:
                continue
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
            if mkt.window_end and now < mkt.window_end:
                our_bid = await self.poly._get_best_bid(pos.token_id)
                if our_bid is None or our_bid <= S3_HARD_STOP_CENTS:
                    if pos.sell_order_id:
                        self.poly.cancel_order(pos.sell_order_id)
                        pos.sell_order_id = None
                    sold = await self._s3_sell(pos, market_order=True)
                    if sold:
                        self.stats.losses += 1
                        bid_display = (our_bid or 0) * 100
                        self.stats.last_action = f"S3perf HARD STOP {pos.side} @ {bid_display:.0f}c"
                        log.info("[S3perf] HARD STOP: %s @ %.0fc (trigger 30c)", pos.side, bid_display)
                        notify.send_loss_email(
                            "S3 PERFECT Loss: Hard stop",
                            "HARD STOP %s @ %.0fc | PnL $%.2f | %s"
                            % (pos.side, bid_display, pos.pnl or 0.0, mkt.question[:60]),
                        )
                        continue
                    else:
                        log.warning(
                            "[S3perf] HARD STOP attempted but sell failed (bid=%.0fc) — retrying next tick",
                            (our_bid or 0) * 100,
                        )
                elif our_bid is not None and our_bid >= S3_SELL_AT_CENTS:
                    if pos.sell_order_id:
                        self.poly.cancel_order(pos.sell_order_id)
                        pos.sell_order_id = None
                    sold = await self._s3_sell(pos, market_order=True, min_price=0.90)
                    if sold:
                        self.stats.wins += 1
                        self.stats.last_action = f"S3perf SELL {pos.side} @ 95c+"
                        log.info("[S3perf] SELL %s @ 95c+ PnL=$%.2f", pos.side, pos.pnl or 0.0)
                        continue
                    else:
                        log.warning(
                            "[S3perf] TAKE PROFIT at %.0fc partial/retry (%.2f left)",
                            our_bid * 100,
                            pos.qty,
                        )

            # Manipulation: same as S3
            if mkt.window_end and now < mkt.window_end and self.feed and getattr(self.feed, "current_price", None):
                btc = self.feed.current_price
                strike = mkt.reference_price
                if strike is not None:
                    up_bid = await self.poly._get_best_bid(mkt.yes_token_id)
                    down_bid = await self.poly._get_best_bid(mkt.no_token_id)
                    up_bid = up_bid or 0.0
                    down_bid = down_bid or 0.0
                    if (
                        not pos.manipulation_detected
                        and up_bid >= MANIPULATION_FAVOR_CENTS
                        and btc < strike
                        and pos.side == "Up"
                    ):
                        pos.manipulation_detected = True
                        self.stats.last_action = (
                            f"S3perf MANIPULATION DETECTED {pos.side} (Up {up_bid:.2f}c but BTC < strike) — hard sell at 30c"
                        )
                        log.info(
                            "[S3perf] MANIPULATION DETECTED: Up favored at %.2fc but BTC $%.0f < strike $%.0f → will hard sell at 30c",
                            up_bid * 100,
                            btc,
                            strike,
                        )
                    elif (
                        not pos.manipulation_detected
                        and down_bid >= MANIPULATION_FAVOR_CENTS
                        and btc > strike
                        and pos.side == "Down"
                    ):
                        pos.manipulation_detected = True
                        self.stats.last_action = (
                            f"S3perf MANIPULATION DETECTED {pos.side} (Down {down_bid:.2f}c but BTC > strike) — hard sell at 30c"
                        )
                        log.info(
                            "[S3perf] MANIPULATION DETECTED: Down favored at %.2fc but BTC $%.0f > strike $%.0f → will hard sell at 30c",
                            down_bid * 100,
                            btc,
                            strike,
                        )
                    if pos.manipulation_detected:
                        our_bid = up_bid if pos.side == "Up" else down_bid
                        if our_bid is None or our_bid <= MANIPULATION_HARD_SELL_CENTS:
                            if pos.sell_order_id:
                                self.poly.cancel_order(pos.sell_order_id)
                                pos.sell_order_id = None
                            sold = await self._s3_sell(pos, market_order=True)
                            if sold:
                                self.stats.losses += 1
                                self.stats.last_action = f"S3perf MANIP HARD SELL {pos.side} @ {our_bid*100:.0f}c"
                                log.info(
                                    "[S3perf] MANIPULATION HARD SELL: %s @ %.0fc (was 30c or below)",
                                    pos.side,
                                    our_bid * 100,
                                )
                                notify.send_loss_email(
                                    "S3 PERFECT Loss: Manipulation exit",
                                    "MANIP HARD SELL %s @ %.0fc | PnL $%.2f | %s"
                                    % (pos.side, our_bid * 100, pos.pnl or 0.0, mkt.question[:60]),
                                )
                                continue
                            else:
                                log.warning("[S3perf] MANIPULATION HARD SELL attempted but failed — retrying next tick")

            if not mkt.window_end or now <= mkt.window_end:
                continue

            if pos.sell_order_id:
                self.poly.cancel_order(pos.sell_order_id)
                pos.sell_order_id = None
            bid = await self.poly._get_best_bid(pos.token_id)
            if bid and bid > 0.5:
                pos.exit_price = 1.0
                pos.pnl = (1.0 - pos.entry_price) * pos.qty
                self.stats.wins += 1
            else:
                pos.exit_price = 0.0
                pos.pnl = -pos.spent
                self.stats.losses += 1
                notify.send_loss_email(
                    "S3 PERFECT Loss: Resolved",
                    "RESOLVED %s (lost) | PnL $%.2f | %s"
                    % (pos.side, pos.pnl or 0.0, pos.market.question[:60]),
                )

            pos.status = "resolved"
            self.stats.total_pnl += pos.pnl or 0.0
            self.stats.daily_pnl += pos.pnl or 0.0
            self._record_hourly_pnl(pos.pnl or 0.0)
            self.stats.last_action = f"RESOLVED {pos.side} ${pos.pnl:+.2f}"
            self._closed.append(pos)
            log.info(
                "[S3perf] RESOLVED %s: $%.2f → PnL $%+.2f | %s",
                pos.side,
                pos.exit_price,
                pos.pnl or 0.0,
                pos.market.question[:45],
            )

    def _record_hourly_pnl(self, pnl: float):
        from bot.pnl_history import append_pnl

        hour_key = hour_key_est()
        self.stats.hourly_pnl[hour_key] = self.stats.hourly_pnl.get(hour_key, 0.0) + pnl
        append_pnl(date_key_est(), hour_key, pnl)

    def _hourly_report(self):
        hour_key = hour_key_est()
        today = date_key_est()
        if self._last_day != today:
            if self._last_day:
                log.info("═══ S3 PERFECT NEW DAY ═══")
            self.stats.hourly_pnl = {}
            self.stats.daily_pnl = 0.0
            self._last_day = today
        if hour_key != self._last_hour_key and self._last_hour_key:
            prev_pnl = self.stats.hourly_pnl.get(self._last_hour_key, 0.0)
            log.info(
                "═══ S3 PERFECT HOURLY [%s] PnL $%+.2f | Total $%+.2f | W:%d L:%d",
                self._last_hour_key,
                prev_pnl,
                self.stats.total_pnl,
                self.stats.wins,
                self.stats.losses,
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

