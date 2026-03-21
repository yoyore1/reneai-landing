"""
Strategy Perfected — Data-proven filters only (March 18 analysis, 249 markets)

PHILOSOPHY: Only skip what's PROVEN to lose. No speculative filters.

PROVEN FILTERS (from 8hr + full-day tick-level analysis):
  1. Choppy: both sides > 63c = 32% WR, -$156 in 8hrs. Avg opp=45c, avg entry=87c.
  2. Max opp bid: opp > 40c = 25% WR, -$356 in 8hrs. THE #1 loss predictor.
  3. Entry price: 70-85c = 89% WR. 86c+ bleeds. 91c+ = 44% WR.
  4. Bleed hours: 4AM=33%/-$61, 9AM=67%/-$25, 10AM=42%/-$36, 9PM=33%/-$82.
  5. Bid stability: volatile (>0.15 std) = 54% WR. Bouncy (0.10-0.15) = 80%.
  6. Velocity: only skip manipulation (<30 conf). Was blocking genuine at 80% WR.

REMOVED (hurt on 9006):
  - Min depth ratio (low depth = GOOD, not bad)
  - Depth eroding (blocked 75% WR markets)
  - Down-weak depth (no evidence Down needs more depth)
  - Min opp bid (low opp is fine if not TOO low; max opp is the real filter)
  - BTC swing filter (mixed signal, not reliable enough)

KILLER COMBO: opp 10-40c + entry 70-85c = 89% WR, +$133 in 8hrs
"""

import asyncio
import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, List, Dict, Set

from bot.config import cfg
from bot.polymarket import PolymarketClient, Market
from bot.trade_history import log_s3_trade, log_daily_snapshot
from bot.data_logger import DataLogger
from bot.velocity_scorer import VelocityScorer
from bot.session_tracker import SessionTracker

log = logging.getLogger("strategy_perfected")

# -- Entry thresholds --
BUY_THRESHOLD = 0.70
BUY_MAX_PRICE = 0.85
SKIP_THRESHOLD = 0.63

# -- Windows --
ANALYSIS_START = 240.0
BUY_WINDOW_START = 180.0
BUY_WINDOW_END = 55.0

# -- Exit levels --
TP_PRICE = 0.94
SL_PRICE = 0.35
FORCE_EXIT_SECS = 25.0

# -- Position sizing --
USDC_PER_TRADE = 25.0

# -- Opp bid (THE key filter) --
MAX_AVG_OPP_BID = 0.40

# -- Bid stability --
MAX_BID_VOLATILITY = 0.15

# -- Velocity --
MIN_VELOCITY_CONF = 30

# -- Hour restrictions --
BLEED_HOURS = {4, 9, 10, 21}

RESOLUTION_WAIT = 30
RESOLUTION_BID_WIN = 0.90


@dataclass
class PerfectedStats:
    markets_analyzed: int = 0
    trades: int = 0
    skipped_choppy: int = 0
    skipped_no_leader: int = 0
    skipped_opp_high: int = 0
    skipped_depth_high: int = 0
    skipped_hour: int = 0
    skipped_bid_vol: int = 0
    skipped_velocity: int = 0
    tp_hits: int = 0
    sl_hits: int = 0
    force_exits: int = 0
    total_pnl: float = 0.0
    wins: int = 0
    losses: int = 0
    last_action: str = ""
    hourly_pnl: dict = field(default_factory=dict)
    last_hour_report: str = ""
    choppy_would_win: int = 0
    choppy_would_lose: int = 0
    noleader_would_win: int = 0
    noleader_would_lose: int = 0
    opp_would_win: int = 0
    opp_would_lose: int = 0
    depth_would_win: int = 0
    depth_would_lose: int = 0
    hour_would_win: int = 0
    hour_would_lose: int = 0
    bidvol_would_win: int = 0
    bidvol_would_lose: int = 0
    vel_would_win: int = 0
    vel_would_lose: int = 0
    redeems: int = 0
    usdc_redeemed: float = 0.0


@dataclass
class PerfectedPosition:
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
    exit_reason: str = ""
    filter_reason: str = ""
    ask_at_buy: float = 0.0
    btc_at_entry: float = 0.0
    btc_at_exit: float = 0.0
    depth_ratio_at_entry: float = 0.0
    hour_at_entry: int = -1
    velocity_conf: int = 0


@dataclass
class PerfectedWindowTracker:
    market: Market
    up_high: float = 0.0
    down_high: float = 0.0
    analyzing: bool = False
    bought: bool = False
    choppy: bool = False
    finalized: bool = False
    tick_history: list = field(default_factory=list)

    def add_tick(self, up_bid, dn_bid, up_depth, dn_depth, btc):
        self.tick_history.append((time.time(), up_bid, dn_bid, up_depth, dn_depth, btc))

    def avg_depth_ratio(self, leader_side):
        if len(self.tick_history) < 2:
            return 1.0
        ratios = []
        for _, ub, db, ud, dd, _ in self.tick_history:
            if leader_side == "Up":
                ld, od = ud, dd
            else:
                ld, od = dd, ud
            ratios.append(ld / max(od, 1))
        return sum(ratios) / len(ratios)

    def btc_swing(self):
        prices = [btc for _, _, _, _, _, btc in self.tick_history if btc > 0]
        if len(prices) < 2:
            return 0.0
        return max(prices) - min(prices)

    def avg_opp_bid(self, leader_side):
        if not self.tick_history:
            return 0.0
        opp = [db if leader_side == "Up" else ub
               for _, ub, db, _, _, _ in self.tick_history]
        return sum(opp) / len(opp) if opp else 0.0

    def bid_stability(self, leader_side):
        if len(self.tick_history) < 3:
            return 0.0
        bids = [t[1] if leader_side == "Up" else t[2] for t in self.tick_history]
        mean = sum(bids) / len(bids)
        variance = sum((b - mean) ** 2 for b in bids) / len(bids)
        return variance ** 0.5

    @property
    def tick_count(self):
        return len(self.tick_history)


class StrategyPerfected:

    def __init__(self, poly: PolymarketClient, trade_hours=None,
                 pnl_store=None, email_on_loss=False, bot_name="perfected",
                 **_ignored):
        self.poly = poly
        self.stats = PerfectedStats()
        self._positions: List[PerfectedPosition] = []
        self._closed: List[PerfectedPosition] = []
        self._phantoms: List[PerfectedPosition] = []
        self.pnl_store = pnl_store
        self._bot_name = bot_name
        self.trade_size = USDC_PER_TRADE
        self._trackers: Dict[str, PerfectedWindowTracker] = {}
        self._decided_cids: Set[str] = set()
        self._running = False
        self._last_hour_key = ""
        self._last_day = ""
        self._trade_hours = trade_hours
        self._data_logger = DataLogger(bot_name)
        self._velocity_scorer = VelocityScorer()
        self._session_tracker = SessionTracker()
        self._start_time = time.time()
        self._skipped_first_market = False
        self._last_redeem_check: float = 0

    def _is_trading_time(self) -> bool:
        if not self._trade_hours:
            return True
        sh, sm, eh, em = self._trade_hours
        try:
            from zoneinfo import ZoneInfo
        except ImportError:
            from backports.zoneinfo import ZoneInfo
        now_est = datetime.now(ZoneInfo("America/New_York"))
        cur = now_est.hour * 60 + now_est.minute
        start = sh * 60 + sm
        end = eh * 60 + em
        if start <= end:
            return start <= cur < end
        return cur >= start or cur < end

    def _get_edt_hour(self) -> int:
        try:
            from zoneinfo import ZoneInfo
        except ImportError:
            from backports.zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York")).hour

    async def run(self):
        self._running = True
        log.info(
            "PERFECTED BOT started | buy %.0fc-%.0fc | TP %.0fc | SL %.0fc | "
            "maxOpp<=%.0fc | maxBidVol<=%.2f | vel>=%d | "
            "skip hours=%s | force-exit %ds",
            BUY_THRESHOLD * 100, BUY_MAX_PRICE * 100,
            TP_PRICE * 100, SL_PRICE * 100,
            MAX_AVG_OPP_BID * 100, MAX_BID_VOLATILITY,
            MIN_VELOCITY_CONF, BLEED_HOURS, int(FORCE_EXIT_SECS),
        )
        while self._running:
            try:
                await self._tick()
            except Exception as exc:
                log.error("PERFECTED tick error: %s", exc, exc_info=True)
            await asyncio.sleep(1)

    def stop(self):
        self._running = False

    async def _tick(self):
        now = time.time()

        if not hasattr(self, "_last_disc") or now - self._last_disc > 30:
            await self._discover()
            self._last_disc = now

        trading_ok = self._is_trading_time()

        for cid, tracker in list(self._trackers.items()):
            mkt = tracker.market
            if not mkt.window_end:
                continue

            remaining = mkt.window_end - now

            if remaining <= -RESOLUTION_WAIT:
                if not tracker.finalized:
                    tracker.finalized = True
                    self._decided_cids.add(cid)
                self._trackers.pop(cid, None)
                continue

            if self._data_logger.should_log_full_tick(cid):
                try:
                    up_bid = await self.poly._get_best_bid(mkt.yes_token_id)
                    down_bid = await self.poly._get_best_bid(mkt.no_token_id)
                    btc = await self._data_logger.fetch_btc_price()
                    yes_book = await self.poly.get_book_depth(mkt.yes_token_id)
                    no_book = await self.poly.get_book_depth(mkt.no_token_id)

                    if remaining <= 0:
                        phase = "resolved"
                    elif tracker.bought:
                        phase = "holding"
                    elif remaining <= ANALYSIS_START:
                        phase = "analyzing"
                    else:
                        phase = "watching"

                    pos_side, pos_entry = "", 0.0
                    for pos in self._positions:
                        if pos.market.condition_id == cid:
                            pos_side = pos.side
                            pos_entry = pos.entry_price
                            break

                    self._data_logger.log_full_tick(
                        mkt.question, phase, pos_side, pos_entry,
                        up_bid or 0, down_bid or 0,
                        yes_book.get("ask", 0), no_book.get("ask", 0),
                        yes_book["depth"], no_book["depth"],
                        yes_book.get("ask_depth", 0), no_book.get("ask_depth", 0),
                        btc, remaining, market_id=cid,
                    )

                    if phase == "analyzing" and not tracker.bought:
                        tracker.add_tick(
                            up_bid or 0, down_bid or 0,
                            yes_book["depth"], no_book["depth"], btc)
                        self._velocity_scorer.feed_tick(
                            cid, up_bid or 0, down_bid or 0,
                            yes_book["depth"], no_book["depth"], btc, remaining)
                except Exception as exc:
                    log.debug("Full tick log error: %s", exc)

            if tracker.finalized:
                continue

            if remaining <= 0:
                if not tracker.bought:
                    tracker.finalized = True
                    self._decided_cids.add(cid)
                continue

            if remaining <= ANALYSIS_START and remaining > BUY_WINDOW_END:
                if not tracker.analyzing:
                    tracker.analyzing = True
                    log.info("PERF: Analyzing %s (%.0fs left)", mkt.question[:40], remaining)

                up_bid = await self.poly._get_best_bid(mkt.yes_token_id)
                down_bid = await self.poly._get_best_bid(mkt.no_token_id)

                if up_bid and up_bid > tracker.up_high:
                    tracker.up_high = up_bid
                if down_bid and down_bid > tracker.down_high:
                    tracker.down_high = down_bid

                if self._data_logger.should_log_analysis(cid):
                    btc = await self._data_logger.fetch_btc_price()
                    yes_book = await self.poly.get_book_depth(mkt.yes_token_id)
                    no_book = await self.poly.get_book_depth(mkt.no_token_id)
                    tracker.add_tick(
                        up_bid or 0, down_bid or 0,
                        yes_book["depth"], no_book["depth"], btc)
                    self._data_logger.log_analysis_tick(
                        mkt.question, up_bid or 0, down_bid or 0,
                        yes_book.get("ask", 0), no_book.get("ask", 0),
                        yes_book["depth"], no_book["depth"],
                        yes_book.get("ask_depth", 0), no_book.get("ask_depth", 0),
                        btc, remaining, market_id=cid,
                    )

                if (tracker.up_high >= SKIP_THRESHOLD and
                        tracker.down_high >= SKIP_THRESHOLD and
                        not tracker.choppy):
                    tracker.choppy = True
                    log.info("PERF CHOPPY: %s (Up=%.2f Down=%.2f)",
                             mkt.question[:35], tracker.up_high, tracker.down_high)

                if (remaining <= BUY_WINDOW_START and
                        not tracker.bought and
                        not tracker.choppy and
                        trading_ok):
                    await self._buy_decision(mkt, tracker, cid, remaining)

            elif remaining <= BUY_WINDOW_END and not tracker.bought and not tracker.finalized:
                tracker.finalized = True
                self._decided_cids.add(cid)
                self.stats.markets_analyzed += 1
                self._velocity_scorer.clear(cid)
                btc = await self._data_logger.fetch_btc_price()

                if tracker.choppy:
                    self.stats.skipped_choppy += 1
                    self.stats.last_action = f"SKIP CHOPPY (Up={tracker.up_high:.2f} Down={tracker.down_high:.2f})"
                    self._create_phantom(mkt, tracker, "choppy")
                    self._data_logger.log_skipped(
                        mkt.question, "choppy", tracker.up_high, tracker.down_high, btc)
                else:
                    leader = "Up" if tracker.up_high >= tracker.down_high else "Down"
                    leader_price = tracker.up_high if leader == "Up" else tracker.down_high
                    if BUY_THRESHOLD <= leader_price <= BUY_MAX_PRICE:
                        tracker.finalized = False
                        await self._buy_decision(mkt, tracker, cid, remaining)
                    else:
                        self.stats.skipped_no_leader += 1
                        self.stats.last_action = "SKIP NO LEADER"
                        self._create_phantom(mkt, tracker, "no_leader")
                        self._data_logger.log_skipped(
                            mkt.question, "no_leader", tracker.up_high, tracker.down_high, btc)

        await self._check_positions()
        await self._auto_redeem_check()
        self._hourly_report()

    async def _buy_decision(self, mkt, tracker, cid, remaining):
        """Data-proven filters only. No speculative depth/trend filters."""

        up_now = await self.poly._get_best_bid(mkt.yes_token_id) or 0
        down_now = await self.poly._get_best_bid(mkt.no_token_id) or 0

        buy_side = None
        buy_token = ""

        if up_now >= BUY_THRESHOLD and up_now <= BUY_MAX_PRICE and up_now >= down_now:
            buy_side = "Up"
            buy_token = mkt.yes_token_id
        elif down_now >= BUY_THRESHOLD and down_now <= BUY_MAX_PRICE and down_now >= up_now:
            buy_side = "Down"
            buy_token = mkt.no_token_id

        if not buy_side:
            return

        buy_price = up_now if buy_side == "Up" else down_now

        avg_dr = tracker.avg_depth_ratio(buy_side)
        avg_opp = tracker.avg_opp_bid(buy_side)
        bid_stab = tracker.bid_stability(buy_side)
        btc_sw = tracker.btc_swing()
        ticks = tracker.tick_count

        edt_hour = self._get_edt_hour()
        btc = await self._data_logger.fetch_btc_price()

        v_conf, v_class, v_det = self._velocity_scorer.score(cid, buy_side)
        sess_adj = self._session_tracker.confidence_adjustment()
        v_conf = max(0, min(100, v_conf + sess_adj))

        log.info(
            "PERF EVAL: %s @%.2f | avgOpp=%.0fc avgDR=%.1fx bidStab=%.3f | "
            "btcSw=$%.0f vel=%d[%s] | hour=%d | %d ticks | %.0fs left | %s",
            buy_side, buy_price, avg_opp * 100, avg_dr, bid_stab,
            btc_sw, v_conf, v_class, edt_hour, ticks,
            remaining, mkt.question[:35])

        def _skip(reason, filter_name):
            self.stats.last_action = reason
            log.info("  PERF SKIP: %s | %s", reason, mkt.question[:30])
            tracker.bought = True
            tracker.finalized = True
            self._decided_cids.add(cid)
            self.stats.markets_analyzed += 1
            self._create_phantom(mkt, tracker, filter_name)
            self._data_logger.log_skipped(
                mkt.question, filter_name, tracker.up_high, tracker.down_high, btc)

        # FILTER 1: Need enough data
        if ticks < 4:
            log.info("  PERF WAIT: only %d ticks | %s", ticks, mkt.question[:30])
            return

        # FILTER 2: Bleed hours
        if edt_hour in BLEED_HOURS:
            self.stats.skipped_hour += 1
            _skip(f"BLEED HOUR {edt_hour}", "bleed_hour")
            return

        # FILTER 3: Opp bid too HIGH (THE #1 loss predictor)
        # 40c+ = 25% WR, -$356 in 8hrs
        if avg_opp > MAX_AVG_OPP_BID:
            self.stats.skipped_opp_high += 1
            _skip(f"OPP TOO STRONG {avg_opp*100:.0f}c > {MAX_AVG_OPP_BID*100:.0f}c", "opp_strong")
            return

        # FILTER 4: Bid too volatile
        # Volatile (>0.15) = 54% WR. Bouncy (0.10-0.15) = 80%.
        if bid_stab > MAX_BID_VOLATILITY:
            self.stats.skipped_bid_vol += 1
            _skip(f"BID VOLATILE {bid_stab:.3f} > {MAX_BID_VOLATILITY}", "bid_volatile")
            return

        # FILTER 6: Velocity — only block manipulation
        if v_conf < MIN_VELOCITY_CONF:
            self.stats.skipped_velocity += 1
            _skip(f"LOW VELOCITY {v_conf} [{v_class}]", f"velocity_{v_class}")
            return

        # ALL FILTERS PASSED
        log.info("  PERF BUY SIGNAL: %s @ %.2f | opp=%.0fc dr=%.1fx stab=%.3f vel=%d | %s",
                 buy_side, buy_price, avg_opp * 100, avg_dr, bid_stab,
                 v_conf, mkt.question[:35])
        await self._execute_buy(mkt, tracker, buy_side, buy_token, remaining,
                                avg_dr, edt_hour, v_conf)

    async def _execute_buy(self, mkt, tracker, buy_side, buy_token, remaining,
                           depth_ratio=0, hour=-1, vel_conf=0):
        side_str = "YES" if buy_side == "Up" else "NO"

        await self.poly.get_market_prices(mkt)
        ask = mkt.yes_ask if buy_side == "Up" else mkt.no_ask
        if ask <= 0 or ask >= 1.0:
            bid = await self.poly._get_best_bid(buy_token)
            ask = bid if bid else 0
        if ask <= 0:
            return

        if ask > 0.94:
            log.info("  PERF SKIP: ASK %.0fc too high | %s", ask * 100, mkt.question[:30])
            self.stats.last_action = f"ASK TOO HIGH {ask*100:.0f}c"
            return

        result = await self.poly.buy(mkt, side_str, self.trade_size)

        if not result.filled and not cfg.dry_run:
            log.warning("PERF BUY FAILED | %s", mkt.question[:40])
            tracker.bought = True
            tracker.finalized = True
            self._decided_cids.add(mkt.condition_id)
            self.stats.markets_analyzed += 1
            self.stats.last_action = f"BUY FAILED | {mkt.question[:30]}"
            return

        entry = result.avg_entry if result.avg_entry > 0 else ask
        qty = result.qty if result.qty > 0 else math.floor((self.trade_size / ask) * 100) / 100
        btc_now = await self._data_logger.fetch_btc_price()

        pos = PerfectedPosition(
            market=mkt, side=buy_side, token_id=buy_token,
            entry_price=entry, qty=qty,
            spent=round(entry * qty, 2),
            entry_time=time.time(),
            ask_at_buy=ask,
            btc_at_entry=btc_now,
            depth_ratio_at_entry=depth_ratio,
            hour_at_entry=hour,
            velocity_conf=vel_conf,
        )
        self._positions.append(pos)
        tracker.bought = True
        tracker.finalized = True
        self._decided_cids.add(mkt.condition_id)
        self.stats.markets_analyzed += 1
        self.stats.trades += 1
        self.stats.last_action = (
            f"BUY {buy_side} @ ${entry:.3f} | dr={depth_ratio:.1f}x vel={vel_conf} | "
            f"{mkt.question[:25]}"
        )
        log.info(
            "[PERF] BUY %s %.1f @ $%.3f ($%.2f) | dr=%.1fx vel=%d | %.0fs left | %s",
            buy_side, qty, entry, pos.spent, depth_ratio, vel_conf,
            remaining, mkt.question[:45],
        )

    async def _check_positions(self):
        now = time.time()
        btc = await self._data_logger.fetch_btc_price()

        for pos in self._positions:
            if pos.status != "open":
                continue

            bid = await self.poly._get_best_bid(pos.token_id)
            if bid is None:
                if pos.market.window_end and now > pos.market.window_end + RESOLUTION_WAIT:
                    self._close_position(pos, 0.0, "resolved-loss")
                continue

            remaining = (pos.market.window_end - now) if pos.market.window_end else 0

            other_token = (pos.market.no_token_id if pos.side == "Up"
                           else pos.market.yes_token_id)
            other_bid = await self.poly._get_best_bid(other_token) or 0

            yes_bid = bid if pos.side == "Up" else other_bid
            no_bid = bid if pos.side == "Down" else other_bid
            yes_book = await self.poly.get_book_depth(pos.market.yes_token_id)
            no_book = await self.poly.get_book_depth(pos.market.no_token_id)

            self._velocity_scorer.feed_tick(
                pos.market.condition_id,
                yes_bid, no_bid,
                yes_book["depth"], no_book["depth"], btc, remaining)

            self._data_logger.log_position_tick(
                pos.market.question, pos.side, pos.entry_price,
                yes_bid, no_bid,
                yes_book.get("ask", 0), no_book.get("ask", 0),
                yes_book["depth"], no_book["depth"],
                yes_book.get("ask_depth", 0), no_book.get("ask_depth", 0),
                btc, remaining,
            )

            if pos.market.window_end and now > pos.market.window_end + RESOLUTION_WAIT:
                if bid > RESOLUTION_BID_WIN:
                    self._close_position(pos, 1.0, "resolved-win")
                else:
                    self._close_position(pos, 0.0, "resolved-loss")
                continue

            if pos.market.window_end and now > pos.market.window_end:
                continue

            if remaining <= FORCE_EXIT_SECS and remaining > 0:
                pos.btc_at_exit = btc
                log.info("  PERF FORCE EXIT: %s %.0fs left, bid=%.2f | %s",
                         pos.side, remaining, bid, pos.market.question[:40])
                reason = "force-exit-win" if bid >= pos.entry_price else "force-exit-loss"
                await self._sell_position(pos, bid, reason)
                continue

            if bid >= TP_PRICE:
                pos.btc_at_exit = btc
                await self._sell_position(pos, bid, "tp")
                continue

            if remaining > 30:
                opp_vel, opp_now = self._velocity_scorer.get_opposing_velocity(
                    pos.market.condition_id, pos.side, window_secs=12)
                if opp_vel > 3.5 and opp_now >= 0.30:
                    pos.btc_at_exit = btc
                    log.info("  PERF VEL EXIT: %s opp %.1fc/s at %.2f | %s",
                             pos.side, opp_vel, opp_now, pos.market.question[:40])
                    await self._sell_position(pos, bid, "velocity-exit")
                    continue

            if bid <= SL_PRICE:
                pos.btc_at_exit = btc
                await self._sell_position(pos, bid, "sl")
                continue

        for ph in list(self._phantoms):
            if ph.status != "phantom-open":
                continue

            bid = await self.poly._get_best_bid(ph.token_id)
            resolved = False
            won = False

            if bid is None:
                if ph.market.window_end and now > ph.market.window_end + RESOLUTION_WAIT:
                    ph.exit_price = 0.0
                    ph.pnl = -ph.entry_price * ph.qty
                    resolved, won = True, False
                else:
                    continue
            elif ph.market.window_end and now > ph.market.window_end + RESOLUTION_WAIT:
                ph.exit_price = 1.0 if bid > RESOLUTION_BID_WIN else 0.0
                ph.pnl = (ph.exit_price - ph.entry_price) * ph.qty
                resolved, won = True, ph.pnl >= 0
            elif bid >= TP_PRICE:
                ph.exit_price = bid
                ph.pnl = (bid - ph.entry_price) * ph.qty
                resolved, won = True, True
            elif bid <= SL_PRICE:
                ph.exit_price = bid
                ph.pnl = (bid - ph.entry_price) * ph.qty
                resolved, won = True, False

            if resolved:
                result_str = "WIN" if won else "LOSE"
                ph.status = f"phantom-{result_str.lower()}"
                label = ph.filter_reason.upper().replace("_", " ")
                log.info(
                    "  PHANTOM WOULD-%s (%s): %s %s $%.2f->$%.2f PnL $%+.2f",
                    result_str, label, ph.side, ph.market.question[:30],
                    ph.entry_price, ph.exit_price or 0, ph.pnl or 0,
                )
                fr = ph.filter_reason
                if fr == "choppy":
                    if won: self.stats.choppy_would_win += 1
                    else: self.stats.choppy_would_lose += 1
                elif fr == "no_leader":
                    if won: self.stats.noleader_would_win += 1
                    else: self.stats.noleader_would_lose += 1
                elif fr == "opp_strong":
                    if won: self.stats.opp_would_win += 1
                    else: self.stats.opp_would_lose += 1
                elif fr == "depth_high":
                    if won: self.stats.depth_would_win += 1
                    else: self.stats.depth_would_lose += 1
                elif fr == "bleed_hour":
                    if won: self.stats.hour_would_win += 1
                    else: self.stats.hour_would_lose += 1
                elif fr == "bid_volatile":
                    if won: self.stats.bidvol_would_win += 1
                    else: self.stats.bidvol_would_lose += 1
                elif "velocity" in fr:
                    if won: self.stats.vel_would_win += 1
                    else: self.stats.vel_would_lose += 1

                self._closed.append(ph)
                self._phantoms.remove(ph)
                try:
                    log_s3_trade(ph, bot_name=self._bot_name)
                except Exception as e:
                    log.warning("Failed to log phantom: %s", e)

    async def _sell_position(self, pos, bid, reason):
        if not cfg.dry_run:
            await self.poly.sell_position(pos)
        self._close_position(pos, bid, reason)

    def _close_position(self, pos, exit_price, reason):
        pos.exit_price = exit_price
        pos.pnl = (exit_price - pos.entry_price) * pos.qty
        pos.status = "closed"
        pos.exit_reason = reason

        is_win = pos.pnl >= 0
        if is_win:
            self.stats.wins += 1
            if "tp" in reason:
                self.stats.tp_hits += 1
        else:
            self.stats.losses += 1
            if reason == "sl":
                self.stats.sl_hits += 1
        if "force-exit" in reason:
            self.stats.force_exits += 1

        self.stats.total_pnl += pos.pnl
        self._session_tracker.record_outcome(is_win, False, pos.pnl, reason)

        try:
            from zoneinfo import ZoneInfo
        except ImportError:
            from backports.zoneinfo import ZoneInfo
        now_est = datetime.now(ZoneInfo("America/New_York"))
        hkey = str(now_est.hour)
        self.stats.hourly_pnl[hkey] = self.stats.hourly_pnl.get(hkey, 0) + pos.pnl

        if self.pnl_store:
            self.pnl_store.record_trade(pos.pnl, is_win)

        log.info(
            "[PERF] %s %s %.1f @ $%.3f -> $%.3f | PnL $%+.2f | dr=%.1fx vel=%d | %s | $%+.2f",
            reason.upper(), pos.side, pos.qty, pos.entry_price, exit_price,
            pos.pnl, pos.depth_ratio_at_entry, pos.velocity_conf,
            pos.market.question[:35], self.stats.total_pnl,
        )

        self._closed.append(pos)
        self._positions.remove(pos)

        try:
            log_s3_trade(pos, bot_name=self._bot_name)
        except Exception as e:
            log.warning("Failed to log trade: %s", e)

    def _create_phantom(self, mkt, tracker, skip_reason: str):
        leader = "Up" if tracker.up_high >= tracker.down_high else "Down"
        leader_token = mkt.yes_token_id if leader == "Up" else mkt.no_token_id
        leader_price = tracker.up_high if leader == "Up" else tracker.down_high
        if leader_price <= 0:
            leader_price = 0.50
        qty = int(self.trade_size / max(leader_price, 0.01))
        phantom = PerfectedPosition(
            market=mkt, side=leader, token_id=leader_token,
            entry_price=round(leader_price, 2),
            qty=qty, spent=0, entry_time=time.time(),
            status="phantom-open", filter_reason=skip_reason,
        )
        self._phantoms.append(phantom)
        log.info("  PHANTOM (%s): tracking %s %s @ $%.2f",
                 skip_reason, leader, mkt.question[:30], leader_price)

    async def _auto_redeem_check(self):
        if cfg.dry_run:
            return
        now = time.time()
        if now - self._last_redeem_check < 120:
            return
        self._last_redeem_check = now
        try:
            result = await self.poly.auto_redeem()
            if result["redeemed"] > 0:
                self.stats.redeems += result["redeemed"]
                self.stats.usdc_redeemed += result["usdc_recovered"]
                log.info("[PERF] Auto-redeemed %d -> $%.2f",
                         result["redeemed"], result["usdc_recovered"])
        except Exception as exc:
            log.warning("[PERF] Auto-redeem error: %s", exc)

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
                    if not self._skipped_first_market and (time.time() - self._start_time) < 30:
                        self._skipped_first_market = True
                        self._decided_cids.add(cid)
                        log.info("PERF: SKIP first market after restart: %s (%.0fs left)",
                                 mkt.question[:50], remaining)
                        continue
                    self._trackers[cid] = PerfectedWindowTracker(market=mkt)
                    log.info("PERF: Tracking %s (%.0fs left)", mkt.question[:50], remaining)
                elif remaining <= 0 and remaining > -30:
                    self._decided_cids.add(cid)

    @property
    def open_positions(self) -> List[PerfectedPosition]:
        return [p for p in self._positions if p.status == "open"]

    @property
    def closed_positions(self) -> List[PerfectedPosition]:
        return self._closed

    def _hourly_report(self):
        try:
            from zoneinfo import ZoneInfo
        except ImportError:
            from backports.zoneinfo import ZoneInfo
        now_est = datetime.now(ZoneInfo("America/New_York"))
        hour_key = now_est.strftime("%Y-%m-%d %H")
        day_key = now_est.strftime("%Y-%m-%d")

        if hour_key != self._last_hour_key and self._last_hour_key:
            total = self.stats.total_pnl
            wr = self.stats.wins / max(self.stats.wins + self.stats.losses, 1) * 100
            self.stats.last_hour_report = (
                f"Hourly: {self.stats.trades}t {self.stats.wins}W/{self.stats.losses}L "
                f"({wr:.0f}%) ${total:+.2f}"
            )
            log.info("[PERF] %s", self.stats.last_hour_report)
        self._last_hour_key = hour_key

        if day_key != self._last_day and self._last_day:
            log_daily_snapshot(self.stats, bot_name=self._bot_name)
        self._last_day = day_key
