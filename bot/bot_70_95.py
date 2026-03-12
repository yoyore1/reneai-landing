"""
Bot 1 — Official 70/95/28 (isolated).
BTC 5-min markets. Buy 70¢+ leader (ask ≤ 91¢), TP 95¢+, hard stop 28¢.
Buy window ≤3:00; 70¢+ in last 1 min; choppy skip 4:00–1:00; no leader at 1:00.
All sell/exit logic is in this file only.
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

log = logging.getLogger("bot_70_95")

# ─── Bot 1 constants (this bot only) ───
BUY_THRESHOLD = 0.70
TP_CENTS = 0.95
HARD_STOP_CENTS = 0.28
MAX_BUY_CENTS = 0.91
NO_LEADER_SEC = 60.0
CHOPPY_SKIP = 0.65
ANALYSIS_START = 240.0
CHOPPY_END_REMAINING = 60.0
BUY_AT_REMAINING = 180.0
SKIP_NO_LEADER_AT = 60.0
USDC_PER_TRADE = 30.0


@dataclass
class Bot1Stats:
    markets_analyzed: int = 0
    trades: int = 0
    skipped_choppy: int = 0
    skipped_no_leader: int = 0
    total_pnl: float = 0.0
    wins: int = 0
    losses: int = 0
    last_action: str = ""
    hourly_pnl: dict = field(default_factory=dict)
    daily_pnl: float = 0.0


@dataclass
class Bot1Position:
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
class Bot1Tracker:
    market: Market
    up_high: float = 0.0
    down_high: float = 0.0
    analyzing: bool = False
    decision_made: bool = False
    no_leader_at_1min: bool = False
    checked_no_leader_1min: bool = False
    last_seen_leader_at: float = 0.0


class Bot70_95:
    """Isolated Bot 1: 70¢ buy, 95¢ TP, 28¢ hard stop. No shared strategy code."""

    def __init__(self, poly: PolymarketClient, feed=None):
        self.poly = poly
        self.feed = feed
        self.stats = Bot1Stats()
        self._positions: List[Bot1Position] = []
        self._closed: List[Bot1Position] = []
        self._trackers: Dict[str, Bot1Tracker] = {}
        self._decided_cids: Set[str] = set()
        self._running = False
        self._last_hour_key = ""
        self._last_day = ""

    def _allowed_to_trade_now(self) -> bool:
        """Only used for BUYING. Selling (TP/stop) is never gated — we can always sell."""
        from bot.time_util import now_est
        now = now_est()
        hour, minute = now.hour, now.minute
        start_h = getattr(cfg, "s3_trade_start_hour_est", 0)
        start_m = getattr(cfg, "s3_trade_start_minute_est", 0)
        end = getattr(cfg, "s3_trade_end_hour_est", 24)
        past_start = (hour > start_h) or (hour == start_h and minute >= start_m)
        in_window = (past_start and (hour < end)) if start_h <= end else (past_start or (hour < end))
        if not in_window:
            return False
        target = getattr(cfg, "s3_daily_profit_target_usdc", 100.0)
        if target <= 0:
            return True
        return getattr(self.stats, "daily_pnl", 0) < target

    async def run(self):
        self._running = True
        log.info(
            "[Bot1] 70/95/28 | buy 70c+ (ask≤91c) | TP 95c | hard stop 28c | window ≤3:00 | choppy skip | no leader 1:00 skip"
        )
        while self._running:
            try:
                await self._tick()
            except Exception as exc:
                log.error("[Bot1] tick error: %s", exc, exc_info=True)
            # Check positions more often when we have open positions so we don't miss TP/stop
            delay = 0.4 if self.open_positions else 1.0
            await asyncio.sleep(delay)

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

            if remaining <= ANALYSIS_START and remaining > CHOPPY_END_REMAINING:
                tracker.analyzing = True
                await self.poly.get_market_prices(mkt)
                up_bid = await self.poly._get_best_bid(mkt.yes_token_id)
                down_bid = await self.poly._get_best_bid(mkt.no_token_id)
                if up_bid and up_bid > tracker.up_high:
                    tracker.up_high = up_bid
                if down_bid and down_bid > tracker.down_high:
                    tracker.down_high = down_bid
                if up_bid and up_bid >= BUY_THRESHOLD:
                    tracker.last_seen_leader_at = now
                if down_bid and down_bid >= BUY_THRESHOLD:
                    tracker.last_seen_leader_at = now

            if remaining <= SKIP_NO_LEADER_AT and not tracker.checked_no_leader_1min:
                tracker.checked_no_leader_1min = True
                await self.poly.get_market_prices(mkt)
                up_1m = (await self.poly._get_best_bid(mkt.yes_token_id)) or 0
                down_1m = (await self.poly._get_best_bid(mkt.no_token_id)) or 0
                if up_1m < BUY_THRESHOLD and down_1m < BUY_THRESHOLD:
                    tracker.no_leader_at_1min = True
                    log.info("[Bot1] At 1:00 left neither side 70c+ → skip")

            if remaining <= BUY_AT_REMAINING and not tracker.decision_made:
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
                    continue
                if tracker.up_high >= CHOPPY_SKIP and tracker.down_high >= CHOPPY_SKIP:
                    tracker.decision_made = True
                    self._decided_cids.add(cid)
                    self.stats.markets_analyzed += 1
                    self.stats.skipped_choppy += 1
                    continue
                if now - tracker.last_seen_leader_at > NO_LEADER_SEC:
                    continue
                await self.poly.get_market_prices(mkt)
                up_now = (await self.poly._get_best_bid(mkt.yes_token_id)) or 0
                down_now = (await self.poly._get_best_bid(mkt.no_token_id)) or 0
                if up_now >= BUY_THRESHOLD:
                    tracker.last_seen_leader_at = now
                if down_now >= BUY_THRESHOLD:
                    tracker.last_seen_leader_at = now

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
                    if ask > MAX_BUY_CENTS:
                        continue
                    tracker.decision_made = True
                    self._decided_cids.add(cid)
                    self.stats.markets_analyzed += 1
                    poly_side = "YES" if buy_side == "Up" else "NO"
                    trade_usdc = getattr(cfg, "s3_usdc_per_trade", USDC_PER_TRADE)
                    real_pos = await self.poly.buy(mkt, poly_side, trade_usdc)
                    qty = real_pos.qty or math.floor((trade_usdc / ask) * 100) / 100
                    pos = Bot1Position(
                        market=mkt,
                        side=buy_side,
                        token_id=buy_token,
                        entry_price=real_pos.avg_entry or ask,
                        qty=qty,
                        spent=trade_usdc,
                        entry_time=time.time(),
                    )
                    self._positions.append(pos)
                    self.stats.trades += 1
                    self.stats.last_action = f"BUY {buy_side} @ ${ask:.3f}"
                    log.info("[Bot1] BUY %s @ %.2fc | %s", buy_side, ask * 100, mkt.question[:45])

        await self._check_positions()
        self._hourly_report()

    async def _sell(self, pos: Bot1Position, bid_price: float, min_price: Optional[float] = None) -> bool:
        """Sell at market (this bot only). Uses shared poly.sell with explicit params."""
        p = Position(
            market=pos.market, side=pos.side, token_id=pos.token_id,
            qty=pos.qty, avg_entry=pos.entry_price,
        )
        ok = await self.poly.sell(p, bid_price=bid_price, market_order=True, min_price=min_price)
        if ok:
            pos.exit_price = p.exit_price
            pos.pnl = p.pnl
            pos.status = "resolved"
            self.stats.total_pnl += pos.pnl or 0
            self.stats.daily_pnl += pos.pnl or 0
            self._record_hourly_pnl(pos.pnl or 0)
            self._closed.append(pos)
            try:
                self._positions.remove(pos)
            except ValueError:
                pass
        return ok

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
                    self._trackers[cid] = Bot1Tracker(market=mkt)

    async def _check_positions(self):
        """Check open positions for TP (95c) or hard stop (28c). No trading-window gate: we are always allowed to sell."""
        now = time.time()
        for pos in list(self._positions):
            if pos.status != "open":
                continue
            mkt = pos.market
            # While market is still open: try to TP or hard stop (no _allowed_to_trade_now check — selling is always allowed)
            if mkt.window_end and now < mkt.window_end:
                await self.poly.get_market_prices(mkt)
                our_bid = await self.poly._get_best_bid(pos.token_id)
                # If book empty, infer our bid from other side (binary: our_bid ≈ 1 - other_bid)
                if our_bid is None:
                    other_tid = mkt.no_token_id if pos.token_id == mkt.yes_token_id else mkt.yes_token_id
                    other_bid = await self.poly._get_best_bid(other_tid)
                    if other_bid is not None:
                        implied = round(1.0 - other_bid, 2)
                        if implied >= TP_CENTS:
                            our_bid = max(TP_CENTS, implied)
                            log.info("[Bot1] No direct bid; inferred our_bid=%.2f from other side, selling at TP", our_bid * 100)
                        elif implied <= HARD_STOP_CENTS:
                            our_bid = implied
                            log.info("[Bot1] No direct bid; inferred our_bid=%.2f (hard stop), selling", our_bid * 100)
                    if our_bid is None:
                        await asyncio.sleep(0.3)
                        our_bid = await self.poly._get_best_bid(pos.token_id)
                if our_bid is not None:
                    if our_bid <= HARD_STOP_CENTS:
                        log.info("[Bot1] Hard stop: bid=%.2f <= 28c, selling", our_bid * 100)
                        if await self._sell(pos, our_bid, min_price=None):
                            self.stats.losses += 1
                            self.stats.last_action = f"Bot1 HARD STOP {pos.side} @ 28c"
                            log.info("[Bot1] HARD STOP %s @ 28c", pos.side)
                        else:
                            log.warning("[Bot1] HARD STOP sell returned False")
                        continue
                    elif our_bid >= TP_CENTS:
                        log.info("[Bot1] TP: bid=%.2f >= 95c, selling", our_bid * 100)
                        if await self._sell(pos, our_bid, min_price=TP_CENTS):
                            self.stats.wins += 1
                            c = (pos.exit_price or our_bid) * 100
                            self.stats.last_action = f"Bot1 SELL {pos.side} @ {c:.0f}c"
                            log.info("[Bot1] SELL %s @ %.0fc TP", pos.side, c)
                        else:
                            log.warning("[Bot1] TP sell returned False")
                        continue
                else:
                    log.debug("[Bot1] No bid for %s", pos.token_id[:12])
                    continue

            if not mkt.window_end or now <= mkt.window_end:
                continue
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
            self.stats.daily_pnl += pos.pnl
            self._record_hourly_pnl(pos.pnl)
            self.stats.last_action = f"RESOLVED {pos.side} ${pos.pnl:+.2f}"
            self._closed.append(pos)
            try:
                self._positions.remove(pos)
            except ValueError:
                pass
            log.info("[Bot1] RESOLVED %s PnL $%+.2f", pos.side, pos.pnl)

    def _record_hourly_pnl(self, pnl: float):
        try:
            from bot.pnl_history import append_pnl
            append_pnl(date_key_est(), hour_key_est(), pnl)
        except Exception:
            pass
        hour_key = hour_key_est()
        self.stats.hourly_pnl[hour_key] = self.stats.hourly_pnl.get(hour_key, 0) + pnl

    def _hourly_report(self):
        hour_key = hour_key_est()
        today = date_key_est()
        if self._last_day != today:
            if self._last_day:
                log.info("═══ Bot1 NEW DAY ═══")
            self.stats.hourly_pnl = {}
            self.stats.daily_pnl = 0.0
            self._last_day = today
        if hour_key != self._last_hour_key and self._last_hour_key:
            prev = self.stats.hourly_pnl.get(self._last_hour_key, 0)
            log.info("═══ Bot1 HOURLY [%s] PnL $%+.2f", self._last_hour_key, prev)
        if hour_key not in self.stats.hourly_pnl:
            self.stats.hourly_pnl[hour_key] = 0.0
        self._last_hour_key = hour_key

    @property
    def open_positions(self) -> List[Bot1Position]:
        return [p for p in self._positions if p.status == "open"]

    @property
    def closed_positions(self) -> List[Bot1Position]:
        return self._closed
