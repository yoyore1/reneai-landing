"""
Strategy 3 Inverse: Same rules as S3 but flipped — buy the UNDERDOG (30c or below).

Logic (all thresholds flipped from S3):
  - Buy when one side is 30c or below (underdog), not 70c+.
  - Skip when NOT choppy (only buy when choppy): both sides must have hit 40c or below in analysis.
  - Hard stop: sell when our side goes to 10c or below.
  - Take profit: sell at 3x entry (e.g. entry 30c → sell at 90c); when our side is 33c or above, hold until resolution.
  - Entry band: only buy when ask is between 25c and 35c (0.25–0.35).
  - Same time window (12:20am–5am), same trade size; manipulation logic mirrored where needed.
"""

import asyncio
import logging
import math
import time
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Set

from bot.time_util import date_key_est, hour_key_est
from bot.polymarket import PolymarketClient, Market, Position
from bot.config import cfg
from bot import notify

log = logging.getLogger("strategy3_inverse")

# === FLIPPED from strategy3: 70→30, choppy uses 40c, reversed (buy when choppy) ===
BUY_THRESHOLD = 0.30       # side must be 30c or below to buy (underdog)
CHOPPY_THRESHOLD = 0.40    # choppy = BOTH sides hit 40c or below; we only buy when choppy
ANALYSIS_START = 210.0
BUY_AT_REMAINING = 180.0
SKIP_NO_LEADER_AT = 60.0   # at 1:00: if any side 30c or below, skip (reversed: only buy when both >30c at 1:00)
MANIPULATION_FAVOR_CENTS = 0.60   # market favors one side 60c+
MANIPULATION_HARD_SELL_CENTS = 0.30  # when manipulation detected, sell when our side <= 30c
S3_HARD_STOP_CENTS = 0.10   # hard stop when our side <= 10c
S3_TAKE_PROFIT_MULTIPLE = 3.0   # sell at 3x entry (e.g. 30c -> 90c); hold 33c+ to resolution
S3_HOLD_ABOVE_CENTS = 0.33   # when our side >= 33c, hold until resolution (no TP)
S3_MIN_BUY_CENTS = 0.25      # only buy if ask >= 25c
S3_MAX_BUY_CENTS = 0.35      # only buy if ask <= 35c


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
    manipulation_detected: bool = False
    sell_order_id: Optional[str] = None


@dataclass
class S3WindowTracker:
    """Tracks price lows for inverse (both sides low = skip)."""
    market: Market
    up_low: float = 1.0      # lowest Up price in analysis window
    down_low: float = 1.0    # lowest Down price in analysis window
    analyzing: bool = False
    decision_made: bool = False
    no_leader_at_1min: bool = False   # at 1:00 any side 30c or below → skip (reversed)
    checked_no_leader_1min: bool = False


class Strategy3Inverse:

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
            "Strategy 3 INVERSE started | buy underdog (30c or below); skip if both 40c- or no underdog at 1:00 | threshold=%.2f",
            BUY_THRESHOLD,
        )
        while self._running:
            try:
                await self._tick()
            except Exception as exc:
                log.error("Strategy 3 Inverse tick error: %s", exc, exc_info=True)
            has_open = any(p.status == "open" for p in self._positions)
            await asyncio.sleep(0.5 if has_open else 1.0)

    def stop(self):
        self._running = False

    async def _tick(self):
        now = time.time()
        if not hasattr(self, "_last_disc") or now - self._last_disc > 30:
            await self._discover()
            self._last_disc = now

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

            # Analysis: track LOWS (inverse of S3 highs)
            if remaining <= ANALYSIS_START and remaining > 0 and not tracker.decision_made:
                if not tracker.analyzing:
                    tracker.analyzing = True
                    log.info("S3inv: Analyzing %s (%.0fs left)", mkt.question[:40], remaining)
                await self.poly.get_market_prices(mkt)
                if mkt.yes_ask > 0:
                    up_bid = await self.poly._get_best_bid(mkt.yes_token_id)
                    down_bid = await self.poly._get_best_bid(mkt.no_token_id)
                    if up_bid is not None and up_bid < tracker.up_low:
                        tracker.up_low = up_bid
                    if down_bid is not None and down_bid < tracker.down_low:
                        tracker.down_low = down_bid

            # Buy window: buy first side that is 30c or below (underdog)
            if remaining <= BUY_AT_REMAINING and remaining >= SKIP_NO_LEADER_AT and not tracker.decision_made:
                if not self._allowed_to_trade_now():
                    tracker.decision_made = True
                    self._decided_cids.add(cid)
                    self.stats.markets_analyzed += 1
                    continue
                # Only buy when CHOPPY (both sides hit 40c or below). Skip when NOT choppy.
                # Choppy = (up_low <= 0.40 AND down_low <= 0.40). So skip when NOT that.
                is_choppy = (tracker.up_low <= CHOPPY_THRESHOLD and tracker.down_low <= CHOPPY_THRESHOLD)
                if not is_choppy:
                    tracker.decision_made = True
                    self._decided_cids.add(cid)
                    self.stats.markets_analyzed += 1
                    self.stats.skipped_choppy += 1
                    self.stats.last_action = f"SKIP NOT CHOPPY (Up low={tracker.up_low:.2f} Down low={tracker.down_low:.2f} — need both ≤{CHOPPY_THRESHOLD*100:.0f}c to buy)"
                    log.info("S3inv SKIP: Not choppy (up_low=%.2f down_low=%.2f) — need BOTH ≤%.0fc to buy", tracker.up_low, tracker.down_low, CHOPPY_THRESHOLD * 100)
                    continue

                await self.poly.get_market_prices(mkt)
                up_bid = await self.poly._get_best_bid(mkt.yes_token_id)
                down_bid = await self.poly._get_best_bid(mkt.no_token_id)
                up_now = up_bid if up_bid is not None else 1.0
                down_now = down_bid if down_bid is not None else 1.0

                buy_side = None
                buy_price = 0
                buy_token = ""
                if up_now <= BUY_THRESHOLD and (down_now > BUY_THRESHOLD or up_now <= down_now):
                    buy_side = "Up"
                    buy_price = up_now
                    buy_token = mkt.yes_token_id
                elif down_now <= BUY_THRESHOLD and (up_now > BUY_THRESHOLD or down_now <= up_now):
                    buy_side = "Down"
                    buy_price = down_now
                    buy_token = mkt.no_token_id

                if buy_side is not None:
                    await self.poly.get_market_prices(mkt)  # refresh right before buy
                    ask = mkt.yes_ask if buy_side == "Up" else mkt.no_ask
                    if ask <= 0 or ask >= 1.0:
                        ask = buy_price
                    if ask < S3_MIN_BUY_CENTS or ask > S3_MAX_BUY_CENTS:
                        log.info("S3inv: skip buy — ask %.2f outside entry band [%.2f, %.2f]", ask, S3_MIN_BUY_CENTS, S3_MAX_BUY_CENTS)
                        continue
                    tracker.decision_made = True
                    self._decided_cids.add(cid)
                    self.stats.markets_analyzed += 1
                    poly_side = "YES" if buy_side == "Up" else "NO"
                    real_pos = await self.poly.buy(mkt, poly_side, cfg.s3_usdc_per_trade)
                    if not real_pos.filled:
                        log.warning("[S3inv] Buy not filled — skip")
                        continue
                    entry = real_pos.avg_entry or ask
                    if entry < S3_MIN_BUY_CENTS or entry > S3_MAX_BUY_CENTS:
                        log.warning("[S3inv] Fill outside band (entry=%.2f); unwinding", entry)
                        p = Position(market=mkt, side=poly_side, token_id=real_pos.token_id, qty=real_pos.qty, avg_entry=entry)
                        await self.poly.sell(p, market_order=True)
                        continue
                    qty = real_pos.qty or math.floor((cfg.s3_usdc_per_trade / ask) * 100) / 100
                    pos = S3Position(
                        market=mkt, side=buy_side, token_id=buy_token,
                        entry_price=real_pos.avg_entry or ask, qty=qty, spent=cfg.s3_usdc_per_trade,
                        entry_time=time.time(),
                    )
                    self._positions.append(pos)
                    self.stats.trades += 1
                    self.stats.last_action = f"BUY {buy_side} (underdog) @ ${ask:.3f} | {mkt.question[:30]}"
                    log.info("[S3inv] BUY %s %.1f @ $%.3f ($%.2f) | %s", buy_side, qty, ask, cfg.s3_usdc_per_trade, mkt.question[:45])

        await self._check_positions()
        self._hourly_report()

    async def _s3_sell(self, pos: S3Position, market_order: bool = False, min_price: float = 0.01) -> bool:
        p = Position(market=pos.market, side=pos.side, token_id=pos.token_id, qty=pos.qty, avg_entry=pos.entry_price)
        sold = await self.poly.sell(p, market_order=market_order, min_price=min_price)
        if sold:
            pos.exit_price = p.exit_price
            pos.pnl = p.pnl
            pos.status = "resolved"
            pnl_val = pos.pnl or 0
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
                # Hard stop: 10c or below
                if our_bid is None or our_bid <= S3_HARD_STOP_CENTS:
                    if pos.sell_order_id:
                        self.poly.cancel_order(pos.sell_order_id)
                        pos.sell_order_id = None
                    sold = await self._s3_sell(pos, market_order=True)
                    if sold:
                        self.stats.losses += 1
                        bid_display = (our_bid or 0) * 100
                        self.stats.last_action = f"S3inv HARD STOP {pos.side} @ {bid_display:.0f}c"
                        log.info("[S3inv] HARD STOP: %s @ %.0fc", pos.side, bid_display)
                        notify.send_loss_email("S3 Inverse Loss: Hard stop", "HARD STOP %s @ %.0fc | PnL $%.2f | %s" % (pos.side, bid_display, pos.pnl or 0, mkt.question[:60]))
                        continue
                    else:
                        log.warning("[S3inv] HARD STOP sell failed — retry")
                # Take profit at 3x entry (e.g. 30c -> 90c); when our side >= 33c hold to resolution
                tp_price = S3_TAKE_PROFIT_MULTIPLE * pos.entry_price
                tp_price = min(tp_price, 0.99)  # cap at 99c
                if our_bid is not None and our_bid >= tp_price:
                    if pos.sell_order_id:
                        self.poly.cancel_order(pos.sell_order_id)
                        pos.sell_order_id = None
                    sold = await self._s3_sell(pos, market_order=False, min_price=min(tp_price * 0.98, 0.99))
                    if sold:
                        self.stats.wins += 1
                        self.stats.last_action = f"S3inv 3x TP {pos.side} @ {our_bid*100:.0f}c"
                        log.info("[S3inv] 3x TAKE PROFIT: %s @ %.0fc (entry %.0fc)", pos.side, our_bid * 100, pos.entry_price * 100)
                        continue

            # Manipulation: same as S3 — favor 60c+, hard sell our side at 30c
            if mkt.window_end and now < mkt.window_end and self.feed and getattr(self.feed, "current_price", None):
                btc = self.feed.current_price
                strike = mkt.reference_price
                if strike is not None:
                    up_bid = await self.poly._get_best_bid(mkt.yes_token_id)
                    down_bid = await self.poly._get_best_bid(mkt.no_token_id)
                    up_bid = up_bid or 0
                    down_bid = down_bid or 0
                    if not pos.manipulation_detected and up_bid >= MANIPULATION_FAVOR_CENTS and btc < strike and pos.side == "Up":
                        pos.manipulation_detected = True
                        log.info("[S3inv] MANIPULATION DETECTED: Up favored, we're long Up")
                    elif not pos.manipulation_detected and down_bid >= MANIPULATION_FAVOR_CENTS and btc > strike and pos.side == "Down":
                        pos.manipulation_detected = True
                        log.info("[S3inv] MANIPULATION DETECTED: Down favored, we're long Down")
                    if pos.manipulation_detected:
                        our_bid = up_bid if pos.side == "Up" else down_bid
                        if our_bid is None or our_bid <= MANIPULATION_HARD_SELL_CENTS:
                            if pos.sell_order_id:
                                self.poly.cancel_order(pos.sell_order_id)
                                pos.sell_order_id = None
                            sold = await self._s3_sell(pos, market_order=True)
                            if sold:
                                self.stats.losses += 1
                                self.stats.last_action = f"S3inv MANIP HARD SELL {pos.side} @ {our_bid*100:.0f}c"
                                log.info("[S3inv] MANIPULATION HARD SELL: %s @ %.0fc", pos.side, our_bid * 100)
                                notify.send_loss_email("S3 Inverse Loss: Manipulation", "MANIP HARD SELL %s @ %.0fc | PnL $%.2f | %s" % (pos.side, our_bid*100, pos.pnl or 0, mkt.question[:60]))
                                continue
                            else:
                                log.warning("[S3inv] MANIP sell failed — retry")

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
                notify.send_loss_email("S3 Inverse Loss: Resolved", "RESOLVED %s (lost) | PnL $%.2f | %s" % (pos.side, pos.pnl, pos.market.question[:60]))

            pos.status = "resolved"
            self.stats.total_pnl += pos.pnl
            self.stats.daily_pnl += pos.pnl
            self._record_hourly_pnl(pos.pnl)
            self.stats.last_action = f"RESOLVED {pos.side} ${pos.pnl:+.2f}"
            self._closed.append(pos)
            log.info("[S3inv] RESOLVED %s: $%.2f → PnL $%+.2f | %s", pos.side, pos.exit_price, pos.pnl, pos.market.question[:45])

    def _record_hourly_pnl(self, pnl: float):
        from bot.pnl_history import append_pnl_inverse
        hour_key = hour_key_est()
        self.stats.hourly_pnl[hour_key] = self.stats.hourly_pnl.get(hour_key, 0) + pnl
        append_pnl_inverse(date_key_est(), hour_key, pnl)

    def _hourly_report(self):
        hour_key = hour_key_est()
        today = date_key_est()
        if self._last_day != today:
            if self._last_day:
                log.info("═══ S3inv NEW DAY ═══")
            self.stats.hourly_pnl = {}
            self.stats.daily_pnl = 0.0
            self._last_day = today
        if hour_key != self._last_hour_key and self._last_hour_key:
            prev_pnl = self.stats.hourly_pnl.get(self._last_hour_key, 0)
            log.info("═══ S3inv HOURLY [%s] PnL $%+.2f | Total $%+.2f | W:%d L:%d", self._last_hour_key, prev_pnl, self.stats.total_pnl, self.stats.wins, self.stats.losses)
        if hour_key not in self.stats.hourly_pnl:
            self.stats.hourly_pnl[hour_key] = 0.0
        self._last_hour_key = hour_key

    @property
    def open_positions(self) -> List[S3Position]:
        return [p for p in self._positions if p.status == "open"]

    @property
    def closed_positions(self) -> List[S3Position]:
        return self._closed
