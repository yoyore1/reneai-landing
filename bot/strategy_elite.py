"""
Strategy Elite v3 — Full-window averaged data (no single-snapshot filters)

v2 used single-snapshot API calls for depth/BTC at eval time. This caused
depth filters to block 6/6 winners while letting the one loser through.

v3 fix: accumulate ALL tick data (depth, BTC, bids) across the full analysis
window and use averaged/aggregated values for every filter decision. Fresh
API calls only used for current bid price (what we're actually paying).
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

log = logging.getLogger("strategy_elite")

# -- Entry thresholds (tick-data calibrated) --
BUY_THRESHOLD = 0.70          # min leader bid to consider
BUY_MAX_PRICE = 0.85          # max entry (82c=79%WR +$94, 85c+ bleeds)
SKIP_THRESHOLD = 0.63         # choppy = both sides above this

# -- Analysis / buy windows --
ANALYSIS_START = 240.0        # 4:00 left
BUY_WINDOW_START = 180.0      # buy from 3:00 left
BUY_WINDOW_END = 55.0         # stop buying at 0:55

# -- Exit levels --
TP_PRICE = 0.94               # TP at 94c (proven level)
SL_PRICE = 0.35               # SL at 35c
FORCE_EXIT_SECS = 25.0        # exit 25s before resolution (avoids -$19.59 avg resolved loss)

# -- Position sizing --
USDC_PER_TRADE = 25.0         # fixed, never changes

# -- Depth filters (NOW uses window-averaged depth, not single snapshot) --
MIN_AVG_DEPTH_RATIO = 0.5     # avg across window — 0.5x is very loose, catches only truly dead books
ELITE_DEPTH_RATIO = 3.0       # 3x+ avg = bypass all other filters
DOWN_MIN_AVG_DEPTH = 0.8      # Down side needs slightly higher avg depth

# -- Hour restrictions --
BLEED_HOURS = {4, 9, 10, 21}  # 4AM=-$69, 9AM=-$25, 10AM=-$36, 9PM from 13-day data

# -- BTC volatility (window-averaged) --
MAX_BTC_SWING = 120.0         # if BTC swung > $120 during window, too volatile

# -- Depth trend --
MIN_DEPTH_TREND = -500.0      # if leader depth dropped > $500 during window, support eroding

# -- Opposing bid (window-averaged) --
MIN_AVG_OPP_BID = 0.08        # avg opp bid < 8c = extreme market, reversal risk

RESOLUTION_WAIT = 30
RESOLUTION_BID_WIN = 0.90


@dataclass
class EliteStats:
    markets_analyzed: int = 0
    trades: int = 0
    skipped_choppy: int = 0
    skipped_no_leader: int = 0
    skipped_depth: int = 0
    skipped_hour: int = 0
    skipped_btc_vol: int = 0
    skipped_down_weak: int = 0
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
    depth_would_win: int = 0
    depth_would_lose: int = 0
    hour_would_win: int = 0
    hour_would_lose: int = 0
    btcvol_would_win: int = 0
    btcvol_would_lose: int = 0
    redeems: int = 0
    usdc_redeemed: float = 0.0


@dataclass
class ElitePosition:
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
    bid70_at_entry: float = 0.0
    hour_at_entry: int = -1
    velocity_conf: int = 0


@dataclass
class EliteWindowTracker:
    market: Market
    up_high: float = 0.0
    down_high: float = 0.0
    analyzing: bool = False
    bought: bool = False
    choppy: bool = False
    finalized: bool = False
    last_depth_ratio: float = 0.0
    last_bid70: float = 0.0
    last_btc_move: float = 0.0
    tick_history: list = field(default_factory=list)

    def add_tick(self, up_bid, dn_bid, up_depth, dn_depth, btc):
        self.tick_history.append((time.time(), up_bid, dn_bid, up_depth, dn_depth, btc))

    def avg_depth_ratio(self, leader_side):
        if len(self.tick_history) < 2:
            return 0.0
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

    def depth_trend(self, leader_side):
        if len(self.tick_history) < 4:
            return 0.0
        n = len(self.tick_history)
        q = max(n // 4, 1)
        if leader_side == "Up":
            first = sum(t[3] for t in self.tick_history[:q]) / q
            last = sum(t[3] for t in self.tick_history[-q:]) / q
        else:
            first = sum(t[4] for t in self.tick_history[:q]) / q
            last = sum(t[4] for t in self.tick_history[-q:]) / q
        return last - first

    def bid_stability(self, leader_side):
        if len(self.tick_history) < 3:
            return 0.0
        bids = [t[1] if leader_side == "Up" else t[2] for t in self.tick_history]
        mean = sum(bids) / len(bids)
        variance = sum((b - mean) ** 2 for b in bids) / len(bids)
        return variance ** 0.5

    def avg_leader_depth(self, leader_side):
        if not self.tick_history:
            return 0.0
        depths = [t[3] if leader_side == "Up" else t[4] for t in self.tick_history]
        return sum(depths) / len(depths)

    def avg_total_depth(self):
        if not self.tick_history:
            return 0.0
        return sum(t[3] + t[4] for t in self.tick_history) / len(self.tick_history)

    @property
    def tick_count(self):
        return len(self.tick_history)


class StrategyElite:

    def __init__(self, poly: PolymarketClient, trade_hours=None,
                 pnl_store=None, email_on_loss=False, bot_name="elite",
                 **_ignored):
        self.poly = poly
        self.stats = EliteStats()
        self._positions: List[ElitePosition] = []
        self._closed: List[ElitePosition] = []
        self._phantoms: List[ElitePosition] = []
        self.pnl_store = pnl_store
        self._bot_name = bot_name
        self.trade_size = USDC_PER_TRADE
        self._trackers: Dict[str, EliteWindowTracker] = {}
        self._decided_cids: Set[str] = set()
        self._running = False
        self._last_hour_key = ""
        self._last_day = ""
        self._trade_hours = trade_hours
        self._btc_prices: list = []
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

    def _get_est_hour(self) -> int:
        try:
            from zoneinfo import ZoneInfo
        except ImportError:
            from backports.zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York")).hour

    async def run(self):
        self._running = True
        log.info(
            "ELITE BOT v3 started | buy %.0fc-%.0fc | TP %.0fc | SL %.0fc | "
            "avgDepth>=%.1fx | btcSwing<=$%.0f | avgOpp>=%.0fc | "
            "skip hours=%s | force-exit %ds before res",
            BUY_THRESHOLD * 100, BUY_MAX_PRICE * 100,
            TP_PRICE * 100, SL_PRICE * 100,
            MIN_AVG_DEPTH_RATIO, MAX_BTC_SWING, MIN_AVG_OPP_BID * 100,
            BLEED_HOURS, int(FORCE_EXIT_SECS),
        )
        while self._running:
            try:
                await self._tick()
            except Exception as exc:
                log.error("ELITE tick error: %s", exc, exc_info=True)
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

                    if phase == "analyzing":
                        self._btc_prices.append((time.time(), btc))
                        self._btc_prices = [(t, p) for t, p in self._btc_prices if time.time() - t < 600]

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
                    log.info("ELITE: Analyzing %s (%.0fs left)", mkt.question[:40], remaining)

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
                    log.info("ELITE CHOPPY: %s (Up=%.2f Down=%.2f)",
                             mkt.question[:35], tracker.up_high, tracker.down_high)

                if (remaining <= BUY_WINDOW_START and
                        not tracker.bought and
                        not tracker.choppy and
                        trading_ok):
                    await self._elite_buy_decision(mkt, tracker, cid, remaining)

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
                    if leader_price >= BUY_THRESHOLD and leader_price <= BUY_MAX_PRICE:
                        leader_token = mkt.yes_token_id if leader == "Up" else mkt.no_token_id
                        log.info("ELITE NO-LEADER BUY: %s %s @ ~%.2f | %s",
                                 leader, mkt.question[:35], leader_price, mkt.question[:35])
                        tracker.finalized = False
                        await self._elite_buy_decision(mkt, tracker, cid, remaining)
                    else:
                        self.stats.skipped_no_leader += 1
                        self.stats.last_action = "SKIP NO LEADER (price out of range)"
                        self._create_phantom(mkt, tracker, "no_leader")
                        self._data_logger.log_skipped(
                            mkt.question, "no_leader", tracker.up_high, tracker.down_high, btc)

        await self._check_positions()
        await self._auto_redeem_check()
        self._hourly_report()

    async def _elite_buy_decision(self, mkt, tracker, cid, remaining):
        """Core filtering engine — uses FULL WINDOW averaged data, not snapshots."""

        # Current bids (fresh) — only for determining side and actual buy price
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
        opp_token = mkt.no_token_id if buy_side == "Up" else mkt.yes_token_id

        # ===== ALL FILTER DATA FROM WINDOW-AVERAGED TRACKER =====
        avg_dr = tracker.avg_depth_ratio(buy_side)
        btc_swing = tracker.btc_swing()
        avg_opp = tracker.avg_opp_bid(buy_side)
        d_trend = tracker.depth_trend(buy_side)
        bid_stab = tracker.bid_stability(buy_side)
        avg_ldepth = tracker.avg_leader_depth(buy_side)
        avg_tdepth = tracker.avg_total_depth()
        ticks = tracker.tick_count

        tracker.last_depth_ratio = avg_dr
        tracker.last_btc_move = btc_swing

        est_hour = self._get_est_hour()
        btc = await self._data_logger.fetch_btc_price()

        # Velocity scoring (already uses accumulated ticks)
        v_conf, v_class, v_det = self._velocity_scorer.score(cid, buy_side)
        sess_adj = self._session_tracker.confidence_adjustment()
        v_conf = max(0, min(100, v_conf + sess_adj))

        log.info(
            "ELITE EVAL: %s @%.2f | avgDepth=%.1fx (%d ticks) avgOpp=%.0fc | "
            "btcSwing=$%.0f dTrend=%+.0f bidStab=%.3f | vel=%d[%s] | "
            "hour=%d | %.0fs left | %s",
            buy_side, buy_price, avg_dr, ticks, avg_opp * 100,
            btc_swing, d_trend, bid_stab, v_conf, v_class,
            est_hour, remaining, mkt.question[:35])

        # =====================================================
        # FILTER 1: Not enough data — need at least 4 ticks
        # =====================================================
        if ticks < 4:
            log.info("  ELITE WAIT: only %d ticks, need 4+ | %s", ticks, mkt.question[:30])
            return

        # =====================================================
        # FILTER 2: Hour restriction (skip bleed hours)
        # =====================================================
        if est_hour in BLEED_HOURS and avg_dr < ELITE_DEPTH_RATIO:
            self.stats.skipped_hour += 1
            self.stats.last_action = f"SKIP BLEED HOUR {est_hour} (avgDepth={avg_dr:.1f}x)"
            log.info("  ELITE SKIP: bleed hour %d, avgDepth %.1fx | %s",
                     est_hour, avg_dr, mkt.question[:30])
            tracker.bought = True
            tracker.finalized = True
            self._decided_cids.add(cid)
            self.stats.markets_analyzed += 1
            self._create_phantom(mkt, tracker, "bleed_hour")
            self._data_logger.log_skipped(
                mkt.question, "bleed_hour", tracker.up_high, tracker.down_high, btc)
            return

        # =====================================================
        # FILTER 3: Avg depth ratio minimum (window-averaged)
        # =====================================================
        if avg_dr < MIN_AVG_DEPTH_RATIO and avg_dr < ELITE_DEPTH_RATIO:
            self.stats.skipped_depth += 1
            self.stats.last_action = f"SKIP LOW AVG DEPTH {avg_dr:.2f}x < {MIN_AVG_DEPTH_RATIO}x"
            log.info("  ELITE SKIP: avgDepth %.2fx < %.1fx (%d ticks) | %s",
                     avg_dr, MIN_AVG_DEPTH_RATIO, ticks, mkt.question[:30])
            tracker.bought = True
            tracker.finalized = True
            self._decided_cids.add(cid)
            self.stats.markets_analyzed += 1
            self._create_phantom(mkt, tracker, "low_depth")
            self._data_logger.log_skipped(
                mkt.question, "low_depth", tracker.up_high, tracker.down_high, btc)
            return

        # =====================================================
        # FILTER 4: Down side needs higher avg depth
        # =====================================================
        if buy_side == "Down" and avg_dr < DOWN_MIN_AVG_DEPTH and avg_dr < ELITE_DEPTH_RATIO:
            self.stats.skipped_down_weak += 1
            self.stats.last_action = f"SKIP DOWN WEAK AVG {avg_dr:.2f}x < {DOWN_MIN_AVG_DEPTH}x"
            log.info("  ELITE SKIP: Down avgDepth %.2fx < %.1fx | %s",
                     avg_dr, DOWN_MIN_AVG_DEPTH, mkt.question[:30])
            tracker.bought = True
            tracker.finalized = True
            self._decided_cids.add(cid)
            self.stats.markets_analyzed += 1
            self._create_phantom(mkt, tracker, "down_weak")
            self._data_logger.log_skipped(
                mkt.question, "down_weak", tracker.up_high, tracker.down_high, btc)
            return

        # =====================================================
        # FILTER 5: BTC swing too high during window
        # =====================================================
        if btc_swing > MAX_BTC_SWING and avg_dr < ELITE_DEPTH_RATIO:
            self.stats.skipped_btc_vol += 1
            self.stats.last_action = f"SKIP BTC VOLATILE ${btc_swing:.0f} > ${MAX_BTC_SWING:.0f}"
            log.info("  ELITE SKIP: btcSwing $%.0f > $%.0f | %s",
                     btc_swing, MAX_BTC_SWING, mkt.question[:30])
            tracker.bought = True
            tracker.finalized = True
            self._decided_cids.add(cid)
            self.stats.markets_analyzed += 1
            self._create_phantom(mkt, tracker, "btc_volatile")
            self._data_logger.log_skipped(
                mkt.question, "btc_volatile", tracker.up_high, tracker.down_high, btc)
            return

        # =====================================================
        # FILTER 6: Depth trend — leader support eroding
        # =====================================================
        if d_trend < MIN_DEPTH_TREND and avg_dr < ELITE_DEPTH_RATIO:
            self.stats.skipped_depth += 1
            self.stats.last_action = f"SKIP DEPTH ERODING {d_trend:+.0f}"
            log.info("  ELITE SKIP: depth trend %+.0f (support eroding) | %s",
                     d_trend, mkt.question[:30])
            tracker.bought = True
            tracker.finalized = True
            self._decided_cids.add(cid)
            self.stats.markets_analyzed += 1
            self._create_phantom(mkt, tracker, "depth_eroding")
            self._data_logger.log_skipped(
                mkt.question, "depth_eroding", tracker.up_high, tracker.down_high, btc)
            return

        # =====================================================
        # FILTER 7: Velocity confidence
        # =====================================================
        if v_conf < 50 and avg_dr < ELITE_DEPTH_RATIO:
            self.stats.last_action = f"SKIP LOW VELOCITY conf={v_conf} [{v_class}]"
            log.info("  ELITE SKIP: velocity %d [%s] too low | %s",
                     v_conf, v_class, mkt.question[:30])
            tracker.bought = True
            tracker.finalized = True
            self._decided_cids.add(cid)
            self.stats.markets_analyzed += 1
            self._create_phantom(mkt, tracker, f"velocity_{v_class}")
            self._data_logger.log_skipped(
                mkt.question, f"velocity_{v_class}", tracker.up_high, tracker.down_high, btc)
            return

        # =====================================================
        # FILTER 8: Avg opposing bid too low (reversal risk)
        # =====================================================
        if avg_opp < MIN_AVG_OPP_BID and avg_dr < ELITE_DEPTH_RATIO:
            self.stats.last_action = f"SKIP AVG OPP DEAD {avg_opp*100:.0f}c"
            log.info("  ELITE SKIP: avgOpp %.0fc < %.0fc — reversal risk | %s",
                     avg_opp * 100, MIN_AVG_OPP_BID * 100, mkt.question[:30])
            tracker.bought = True
            tracker.finalized = True
            self._decided_cids.add(cid)
            self.stats.markets_analyzed += 1
            self._create_phantom(mkt, tracker, "opp_dead")
            self._data_logger.log_skipped(
                mkt.question, "opp_dead", tracker.up_high, tracker.down_high, btc)
            return

        # =====================================================
        # ALL FILTERS PASSED — EXECUTE BUY
        # =====================================================
        log.info("  ELITE BUY SIGNAL: %s @ %.2f | avgDepth=%.1fx btcSwing=$%.0f vel=%d | %s",
                 buy_side, buy_price, avg_dr, btc_swing, v_conf, mkt.question[:35])
        await self._execute_buy(mkt, tracker, buy_side, buy_token, remaining,
                                avg_dr, 0, est_hour, v_conf)

    async def _execute_buy(self, mkt, tracker, buy_side, buy_token, remaining,
                           depth_ratio=0, bid70=0, hour=-1, vel_conf=0):
        side_str = "YES" if buy_side == "Up" else "NO"

        await self.poly.get_market_prices(mkt)
        ask = mkt.yes_ask if buy_side == "Up" else mkt.no_ask
        if ask <= 0 or ask >= 1.0:
            bid = await self.poly._get_best_bid(buy_token)
            ask = bid if bid else 0
        if ask <= 0:
            return

        if ask > 0.94:
            log.info("  ELITE SKIP: ASK %.2fc too high | %s", ask * 100, mkt.question[:30])
            self.stats.last_action = f"ASK TOO HIGH {ask*100:.0f}c"
            return

        result = await self.poly.buy(mkt, side_str, self.trade_size)

        if not result.filled and not cfg.dry_run:
            log.warning("ELITE BUY FAILED — order not filled | %s", mkt.question[:40])
            tracker.bought = True
            tracker.finalized = True
            self._decided_cids.add(mkt.condition_id)
            self.stats.markets_analyzed += 1
            self.stats.last_action = f"BUY FAILED | {mkt.question[:30]}"
            return

        entry = result.avg_entry if result.avg_entry > 0 else ask
        qty = result.qty if result.qty > 0 else math.floor((self.trade_size / ask) * 100) / 100
        btc_now = await self._data_logger.fetch_btc_price()

        pos = ElitePosition(
            market=mkt, side=buy_side, token_id=buy_token,
            entry_price=entry, qty=qty,
            spent=round(entry * qty, 2),
            entry_time=time.time(),
            ask_at_buy=ask,
            btc_at_entry=btc_now,
            depth_ratio_at_entry=depth_ratio,
            bid70_at_entry=bid70,
            hour_at_entry=hour,
            velocity_conf=vel_conf,
        )
        self._positions.append(pos)
        tracker.bought = True
        tracker.finalized = True
        self._decided_cids.add(mkt.condition_id)
        self.stats.markets_analyzed += 1
        self.stats.trades += 1
        self.stats.last_action = f"BUY {buy_side} @ ${entry:.3f} | depth={depth_ratio:.1f}x vel={vel_conf} | {mkt.question[:25]}"
        log.info(
            "[ELITE] BUY %s %.1f @ $%.3f ($%.2f) | depth=%.1fx bid70=$%.0f vel=%d | %.0fs left | %s",
            buy_side, qty, entry, pos.spent, depth_ratio, bid70, vel_conf,
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

            # Log position tick
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

            # Post-resolution check
            if pos.market.window_end and now > pos.market.window_end + RESOLUTION_WAIT:
                if bid > RESOLUTION_BID_WIN:
                    self._close_position(pos, 1.0, "resolved-win")
                else:
                    self._close_position(pos, 0.0, "resolved-loss")
                continue

            if pos.market.window_end and now > pos.market.window_end:
                continue

            # =====================================================
            # FORCE EXIT before resolution (data: resolved-loss = -$19.59 avg)
            # =====================================================
            if remaining <= FORCE_EXIT_SECS and remaining > 0:
                pos.btc_at_exit = btc
                log.info("  ELITE FORCE EXIT: %s %.0fs before resolution, bid=%.2f | %s",
                         pos.side, remaining, bid, pos.market.question[:40])
                if bid >= pos.entry_price:
                    await self._sell_position(pos, bid, "force-exit-win")
                else:
                    await self._sell_position(pos, bid, "force-exit-loss")
                continue

            # =====================================================
            # TAKE PROFIT
            # =====================================================
            if bid >= TP_PRICE:
                pos.btc_at_exit = btc
                await self._sell_position(pos, bid, "tp")
                continue

            # =====================================================
            # VELOCITY-BASED REVERSAL EXIT
            # If opposing side is surging fast and strong
            # =====================================================
            if remaining > 30:
                opp_vel, opp_now = self._velocity_scorer.get_opposing_velocity(
                    pos.market.condition_id, pos.side, window_secs=12)
                if opp_vel > 3.5 and opp_now >= 0.30:
                    pos.btc_at_exit = btc
                    log.info("  ELITE VEL EXIT: %s opp %.1fc/s at %.2f, our=%.2f | %s",
                             pos.side, opp_vel, opp_now, bid, pos.market.question[:40])
                    await self._sell_position(pos, bid, "velocity-exit")
                    continue

            # =====================================================
            # STOP LOSS (tighter at 0.35 instead of 0.28)
            # =====================================================
            if bid <= SL_PRICE:
                pos.btc_at_exit = btc
                await self._sell_position(pos, bid, "sl")
                continue

        # Check phantoms
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
                result = "WIN" if won else "LOSE"
                ph.status = f"phantom-{result.lower()}"
                label = ph.filter_reason.upper().replace("_", " ")
                log.info(
                    "  PHANTOM WOULD-%s (%s): %s %s $%.2f->$%.2f PnL $%+.2f",
                    result, label, ph.side, ph.market.question[:30],
                    ph.entry_price, ph.exit_price or 0, ph.pnl or 0,
                )
                # Track phantom accuracy per filter
                fr = ph.filter_reason
                if fr == "choppy":
                    if won: self.stats.choppy_would_win += 1
                    else: self.stats.choppy_would_lose += 1
                elif fr == "no_leader":
                    if won: self.stats.noleader_would_win += 1
                    else: self.stats.noleader_would_lose += 1
                elif fr in ("low_depth", "thin_bid70", "down_weak", "depth_eroding"):
                    if won: self.stats.depth_would_win += 1
                    else: self.stats.depth_would_lose += 1
                elif fr == "bleed_hour":
                    if won: self.stats.hour_would_win += 1
                    else: self.stats.hour_would_lose += 1
                elif fr == "btc_volatile":
                    if won: self.stats.btcvol_would_win += 1
                    else: self.stats.btcvol_would_lose += 1

                self._closed.append(ph)
                self._phantoms.remove(ph)
                try:
                    log_s3_trade(ph, bot_name=self._bot_name)
                except Exception as e:
                    log.warning("Failed to log phantom: %s", e)

    async def _sell_position(self, pos, bid, reason):
        sold = False
        if not cfg.dry_run:
            sold = await self.poly.sell_position(pos)
        else:
            sold = True

        exit_price = bid
        pnl = (exit_price - pos.entry_price) * pos.qty
        self._close_position(pos, exit_price, reason)

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

        # Track to session
        self._session_tracker.record_outcome(is_win, False, pos.pnl, reason)

        # Hourly PnL
        try:
            from zoneinfo import ZoneInfo
        except ImportError:
            from backports.zoneinfo import ZoneInfo
        now_est = datetime.now(ZoneInfo("America/New_York"))
        hkey = str(now_est.hour)
        self.stats.hourly_pnl[hkey] = self.stats.hourly_pnl.get(hkey, 0) + pos.pnl

        # PnL store
        if self.pnl_store:
            self.pnl_store.record_trade(pos.pnl, is_win)

        log.info(
            "[ELITE] %s %s %.1f @ $%.3f -> $%.3f | PnL $%+.2f | depth=%.1fx vel=%d | %s | session $%+.2f",
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
        phantom = ElitePosition(
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
                log.info("[ELITE] Auto-redeemed %d -> $%.2f",
                         result["redeemed"], result["usdc_recovered"])
        except Exception as exc:
            log.warning("[ELITE] Auto-redeem error: %s", exc)

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
                        log.info("ELITE: SKIP first market after restart: %s (%.0fs left)",
                                 mkt.question[:50], remaining)
                        continue
                    self._trackers[cid] = EliteWindowTracker(market=mkt)
                    log.info("ELITE: Tracking %s (%.0fs left)", mkt.question[:50], remaining)
                elif remaining <= 0 and remaining > -30:
                    self._decided_cids.add(cid)

    @property
    def open_positions(self) -> List[ElitePosition]:
        return [p for p in self._positions if p.status == "open"]

    @property
    def closed_positions(self) -> List[ElitePosition]:
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
            log.info("[ELITE] %s", self.stats.last_hour_report)
        self._last_hour_key = hour_key

        if day_key != self._last_day and self._last_day:
            log_daily_snapshot(self.stats, bot_name=self._bot_name)
        self._last_day = day_key
