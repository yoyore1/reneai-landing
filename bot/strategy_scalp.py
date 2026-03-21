"""
Predictive Scalp Strategy — Quick In, Quick Out with Intelligence

Core idea: use tick velocity, depth, and market signals to *predict*
how far the price will move, then set dynamic TP / SL / time limit
per trade.  Flip-scalps manipulation when detected.

Entry flow:
  1. Analyze window (4:00 → 1:00): feed VelocityScorer every tick
  2. Buy window (3:00 → 1:00): score velocity → classify
     • Manipulation / very suspicious → FLIP scalp (buy weak side)
     • Genuine / uncertain-but-ok      → run PredictionEngine
       – Confident enough → BUY with dynamic TP / SL / time
       – Not confident    → SKIP (track phantom)
  3. Hold: exit on TP / SL / time limit / velocity surge

Exit checks (first to trigger wins):
  - Dynamic TP   (set by prediction at entry)
  - Dynamic SL   (set by prediction at entry)
  - Time limit   (set by prediction at entry, typically 20-40 s)
  - Velocity exit (opposing side surging > 3 c/s while > 28 c)
  - Resolution    (market ends before any of the above)
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
from bot.prediction_engine import PredictionEngine
from bot.session_tracker import SessionTracker

log = logging.getLogger("scalp_predict")

BUY_THRESHOLD = 0.70
BUY_MAX_PRICE = 0.86
MAX_ENTRY_PRICE = 0.90
SKIP_THRESHOLD = 0.60
ANALYSIS_START = 240.0
BUY_WINDOW_START = 240.0
BUY_WINDOW_END = 55.0
FALLBACK_TP = 0.94
FALLBACK_SL = 0.28
USDC_PER_TRADE = 20.0

RESOLUTION_WAIT = 30
RESOLUTION_BID_WIN = 0.90

MIN_VELOCITY_CONFIDENCE = 30


@dataclass
class ScalpStats:
    markets_analyzed: int = 0
    trades: int = 0
    flip_trades: int = 0
    flip_wins: int = 0
    skipped_choppy: int = 0
    skipped_no_leader: int = 0
    skipped_manip: int = 0
    skipped_prediction: int = 0
    tp_hits: int = 0
    sl_hits: int = 0
    time_stops: int = 0
    velocity_exits: int = 0
    total_pnl: float = 0.0
    wins: int = 0
    losses: int = 0
    last_action: str = ""
    hourly_pnl: dict = field(default_factory=dict)
    choppy_would_win: int = 0
    choppy_would_lose: int = 0
    noleader_would_win: int = 0
    noleader_would_lose: int = 0
    manip_would_win: int = 0
    manip_would_lose: int = 0
    pred_would_win: int = 0
    pred_would_lose: int = 0
    reversals_detected: int = 0
    redeems: int = 0
    usdc_redeemed: float = 0.0


@dataclass
class ScalpPosition:
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
    # per-position scalp targets
    scalp_target: Optional[float] = None
    scalp_sl: Optional[float] = None
    scalp_time_limit: float = 0
    is_flip: bool = False
    prediction_confidence: int = 0
    predicted_move: float = 0
    # tracking
    reversal_detected: bool = False
    other_side_high: float = 0.0
    bid_at_sell_trigger: float = 0.0
    btc_at_entry: float = 0.0
    btc_at_exit: float = 0.0
    ask_at_buy: float = 0.0


@dataclass
class ScalpWindowTracker:
    market: Market
    up_high: float = 0.0
    down_high: float = 0.0
    analyzing: bool = False
    bought: bool = False
    choppy: bool = False
    finalized: bool = False
    skip_cooldown: float = 0
    phantom_created: bool = False


class StrategyScalp:

    def __init__(self, poly: PolymarketClient, trade_hours=None,
                 pnl_store=None, email_on_loss=False, bot_name="scalp_predict",
                 flip_size: float = 20.0,
                 guard_config: dict = None):
        self.poly = poly
        self.stats = ScalpStats()
        self._positions: List[ScalpPosition] = []
        self._closed: List[ScalpPosition] = []
        self._phantoms: List[ScalpPosition] = []
        self.pnl_store = pnl_store
        self._email_on_loss = email_on_loss
        self._bot_name = bot_name
        self.trade_size = USDC_PER_TRADE
        self._flip_size = flip_size
        self._trackers: Dict[str, ScalpWindowTracker] = {}
        self._decided_cids: Set[str] = set()
        self._running = False
        self._last_hour_key = ""
        self._last_day = ""
        self._trade_hours = trade_hours

        # intelligence modules
        self._velocity_scorer = VelocityScorer()
        self._prediction_engine = PredictionEngine()
        self._session_tracker = SessionTracker(window=10)
        self._data_logger = DataLogger(bot_name)

        self._last_redeem_check: float = 0
        self._live_analysis: dict = {}

        log.info(
            "Predictive Scalp started | buy >=%.0fc <=%.0fc | "
            "analyze 4:00→1:00 | buy 3:00→0:55 | flip_size=$%.0f | bot=%s",
            BUY_THRESHOLD * 100, BUY_MAX_PRICE * 100,
            self._flip_size, bot_name,
        )

    # ------------------------------------------------------------------
    # time filter
    # ------------------------------------------------------------------
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

    # ------------------------------------------------------------------
    # main loop
    # ------------------------------------------------------------------
    async def run(self):
        self._running = True
        while self._running:
            try:
                await self._tick()
            except Exception as exc:
                log.error("Scalp tick error: %s", exc, exc_info=True)
            await asyncio.sleep(1)

    def stop(self):
        self._running = False

    # ------------------------------------------------------------------
    # tick
    # ------------------------------------------------------------------
    async def _tick(self):
        now = time.time()

        if not hasattr(self, "_last_disc") or now - self._last_disc > 30:
            await self._discover()
            self._last_disc = now

        trading_ok = self._is_trading_time()

        # clear stale live analysis if that market is no longer active
        if self._live_analysis:
            _la_mkt = self._live_analysis.get("market", "")
            _still = any(
                t.market.question[:60] == _la_mkt and not t.finalized
                for t in self._trackers.values()
            )
            if not _still:
                self._live_analysis = {}

        for cid, tracker in list(self._trackers.items()):
            mkt = tracker.market
            if not mkt.window_end:
                continue

            remaining = mkt.window_end - now

            # --- expired market cleanup ---
            if remaining <= -RESOLUTION_WAIT:
                if not tracker.finalized:
                    tracker.finalized = True
                    self._decided_cids.add(cid)
                    self._velocity_scorer.clear(cid)
                self._trackers.pop(cid, None)
                continue

            # --- full tick logging (all phases) ---
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

                    # feed velocity scorer during full-tick logging
                    self._velocity_scorer.feed_tick(
                        cid, up_bid or 0, down_bid or 0,
                        yes_book["depth"], no_book["depth"],
                        btc, remaining,
                    )
                except Exception as exc:
                    log.debug("Full tick log error: %s", exc)

            if tracker.finalized:
                continue

            if remaining <= 0:
                if not tracker.bought:
                    tracker.finalized = True
                    self._decided_cids.add(cid)
                    self._velocity_scorer.clear(cid)
                continue

            # =========================================================
            # ANALYSIS + BUY WINDOW  (4:00 → 0:55 remaining)
            # =========================================================
            if remaining <= ANALYSIS_START and remaining > BUY_WINDOW_END:
                if not tracker.analyzing:
                    tracker.analyzing = True
                    log.info("SCALP: Analyzing %s (%.0fs left)",
                             mkt.question[:40], remaining)

                up_bid = await self.poly._get_best_bid(mkt.yes_token_id)
                down_bid = await self.poly._get_best_bid(mkt.no_token_id)

                if up_bid and up_bid > tracker.up_high:
                    tracker.up_high = up_bid
                if down_bid and down_bid > tracker.down_high:
                    tracker.down_high = down_bid

                # feed velocity scorer on analysis ticks too
                if self._data_logger.should_log_analysis(cid):
                    btc = await self._data_logger.fetch_btc_price()
                    yes_book = await self.poly.get_book_depth(mkt.yes_token_id)
                    no_book = await self.poly.get_book_depth(mkt.no_token_id)
                    self._velocity_scorer.feed_tick(
                        cid, up_bid or 0, down_bid or 0,
                        yes_book["depth"], no_book["depth"],
                        btc, remaining,
                    )
                    self._data_logger.log_analysis_tick(
                        mkt.question, up_bid or 0, down_bid or 0,
                        yes_book.get("ask", 0), no_book.get("ask", 0),
                        yes_book["depth"], no_book["depth"],
                        yes_book.get("ask_depth", 0), no_book.get("ask_depth", 0),
                        btc, remaining, market_id=cid,
                    )

                    # update live analysis for dashboard
                    _ub = up_bid or 0
                    _db = down_bid or 0
                    if _ub >= 0.50 or _db >= 0.50:
                        _leader = "Up" if _ub >= _db else "Down"
                        _lbid = _ub if _leader == "Up" else _db
                        _obid = _db if _leader == "Up" else _ub
                        _vc, _vcl, _vd = self._velocity_scorer.score(cid, _leader)
                        _ss, _, _ = self._session_tracker.get_session_state()
                        _sa = self._session_tracker.confidence_adjustment()
                        self._live_analysis = {
                            "market": mkt.question[:60],
                            "remaining": round(remaining),
                            "phase": "analyzing",
                            "leader": _leader,
                            "leader_bid": round(_lbid, 2),
                            "opp_bid": round(_obid, 2),
                            "velocity_confidence": _vc,
                            "velocity_class": _vcl,
                            "velocity_details": _vd,
                            "session_state": _ss,
                            "session_adjustment": _sa,
                            "adjusted_confidence": _vc + _sa,
                            "total_depth": round(yes_book["depth"] + no_book["depth"]),
                            "decision": "analyzing",
                        }

                # choppy detection
                if (tracker.up_high >= SKIP_THRESHOLD
                        and tracker.down_high >= SKIP_THRESHOLD
                        and not tracker.choppy):
                    tracker.choppy = True
                    log.info("SCALP CHOPPY: %s (Up=%.2f Down=%.2f)",
                             mkt.question[:35], tracker.up_high, tracker.down_high)

                # =====================================================
                # BUY DECISION  (3:00 → 0:55 remaining)
                # =====================================================
                if (remaining <= BUY_WINDOW_START
                        and not tracker.bought
                        and not tracker.choppy
                        and trading_ok):
                    if tracker.skip_cooldown and (now - tracker.skip_cooldown) < 10:
                        pass  # prediction skip cooldown, re-evaluate in a few seconds
                    else:
                        await self._buy_decision(mkt, tracker, remaining)

            # --- missed the buy window ---
            elif (remaining <= BUY_WINDOW_END
                  and not tracker.bought
                  and not tracker.finalized):
                tracker.finalized = True
                self._decided_cids.add(cid)
                self._velocity_scorer.clear(cid)
                self.stats.markets_analyzed += 1
                btc = await self._data_logger.fetch_btc_price()

                if tracker.choppy:
                    self.stats.skipped_choppy += 1
                    self.stats.last_action = (
                        f"SKIP CHOPPY (Up={tracker.up_high:.2f} "
                        f"Down={tracker.down_high:.2f})")
                    log.info("SCALP SKIP CHOPPY: %s", mkt.question[:40])
                    self._create_skip_phantom(mkt, tracker, "choppy")
                    self._data_logger.log_skipped(
                        mkt.question, "choppy",
                        tracker.up_high, tracker.down_high, btc)
                else:
                    self.stats.skipped_no_leader += 1
                    self.stats.last_action = "SKIP NO LEADER"
                    log.info("SCALP SKIP NO LEADER: %s", mkt.question[:40])
                    self._create_skip_phantom(mkt, tracker, "no_leader")
                    self._data_logger.log_skipped(
                        mkt.question, "no_leader",
                        tracker.up_high, tracker.down_high, btc)

        # clear live analysis if no market is actively being analyzed
        any_active = any(
            t.analyzing and not t.finalized and not t.bought
            for t in self._trackers.values()
        )
        if not any_active and self._live_analysis.get("phase") != "holding":
            self._live_analysis = {}

        await self._check_positions()
        await self._auto_redeem_check()
        self._hourly_report()

    # ------------------------------------------------------------------
    # buy decision
    # ------------------------------------------------------------------
    async def _buy_decision(self, mkt, tracker, remaining):
        cid = mkt.condition_id
        up_now = await self.poly._get_best_bid(mkt.yes_token_id) or 0
        down_now = await self.poly._get_best_bid(mkt.no_token_id) or 0

        buy_side = None
        buy_token = ""

        if (up_now >= BUY_THRESHOLD and up_now <= BUY_MAX_PRICE
                and up_now >= down_now):
            buy_side = "Up"
            buy_token = mkt.yes_token_id
        elif (down_now >= BUY_THRESHOLD and down_now <= BUY_MAX_PRICE
              and down_now >= up_now):
            buy_side = "Down"
            buy_token = mkt.no_token_id

        if not buy_side:
            return

        buy_price = up_now if buy_side == "Up" else down_now
        opp_bid = down_now if buy_side == "Up" else up_now

        # --- velocity scoring ---
        v_conf, v_class, v_details = self._velocity_scorer.score(cid, buy_side)
        sess_state, _, _ = self._session_tracker.get_session_state()
        sess_adj = self._session_tracker.confidence_adjustment()
        adj_conf = v_conf + sess_adj

        log.info(
            "SCALP DECISION: %s %.2f | vel_conf=%d(%s) sess=%+d adj=%d | %s",
            buy_side, buy_price, v_conf, v_class, sess_adj, adj_conf,
            mkt.question[:40],
        )

        self._live_analysis = {
            "market": mkt.question[:60],
            "remaining": round(remaining),
            "phase": "buy_window",
            "leader": buy_side,
            "leader_bid": round(buy_price, 2),
            "opp_bid": round(opp_bid, 2),
            "velocity_confidence": v_conf,
            "velocity_class": v_class,
            "velocity_details": v_details,
            "session_state": sess_state,
            "session_adjustment": sess_adj,
            "adjusted_confidence": adj_conf,
            "decision": "evaluating",
        }

        # fetch depth for prediction
        yes_book = await self.poly.get_book_depth(mkt.yes_token_id)
        no_book = await self.poly.get_book_depth(mkt.no_token_id)
        btc = await self._data_logger.fetch_btc_price()

        if buy_side == "Up":
            leader_depth = yes_book["depth"]
            opp_depth = no_book["depth"]
            leader_ask_depth = yes_book.get("ask_depth", 0)
        else:
            leader_depth = no_book["depth"]
            opp_depth = yes_book["depth"]
            leader_ask_depth = no_book.get("ask_depth", 0)

        # --- MANIPULATION → FLIP SCALP ---
        if v_class == "manipulation" or adj_conf < MIN_VELOCITY_CONFIDENCE:
            flip_side = "Down" if buy_side == "Up" else "Up"
            flip_token = mkt.no_token_id if buy_side == "Up" else mkt.yes_token_id
            flip_bid = opp_bid

            flip_pred = self._prediction_engine.predict_flip_scalp(
                flip_bid, buy_price,
                opp_depth, leader_depth,
                leader_ask_depth, v_details, remaining,
            )

            if PredictionEngine.should_trade(flip_pred):
                self._live_analysis["decision"] = "flip"
                self._live_analysis["prediction"] = flip_pred
                log.info(
                    "SCALP FLIP: %s %.2f → tgt=%.3f sl=%.3f time=%ds conf=%d | %s",
                    flip_side, flip_bid,
                    flip_pred["target"], flip_pred["sl"],
                    flip_pred["time_limit"], flip_pred["confidence"],
                    mkt.question[:40],
                )
                await self._execute_buy(
                    mkt, tracker, flip_side, flip_token, remaining,
                    flip_pred, size_override=self._flip_size, is_flip=True,
                )
            else:
                self._live_analysis["decision"] = "skip_manip"
                self.stats.skipped_manip += 1
                self.stats.last_action = f"MANIP SKIP (flip conf too low) | {mkt.question[:30]}"
                self._create_manip_phantom(mkt, tracker, buy_side, buy_token)
                self._data_logger.log_skipped(
                    mkt.question, "manip_guard",
                    tracker.up_high, tracker.down_high, btc)
            return

        # --- PREDICTION → NORMAL SCALP ---
        pred = self._prediction_engine.predict_scalp(
            buy_price, opp_bid,
            leader_depth, opp_depth, leader_ask_depth,
            v_details, remaining,
        )

        if PredictionEngine.should_trade(pred):
            self._live_analysis["decision"] = "buy"
            self._live_analysis["prediction"] = pred
            log.info(
                "SCALP BUY: %s %.2f → tgt=%.3f sl=%.3f time=%ds conf=%d | %s",
                buy_side, buy_price,
                pred["target"], pred["sl"],
                pred["time_limit"], pred["confidence"],
                mkt.question[:40],
            )
            await self._execute_buy(
                mkt, tracker, buy_side, buy_token, remaining, pred,
            )
        else:
            self._live_analysis["decision"] = "skip_pred"
            self._live_analysis["prediction"] = pred
            self.stats.skipped_prediction += 1
            self.stats.last_action = (
                f"PRED SKIP (conf={pred['confidence']} "
                f"move={pred['predicted_move']*100:.1f}c) — will retry | {mkt.question[:25]}")
            log.info("SCALP PRED SKIP (cooldown 10s): conf=%d move=%.3f | %s",
                     pred["confidence"], pred["predicted_move"],
                     mkt.question[:40])
            tracker.skip_cooldown = time.time()
            if not tracker.phantom_created:
                tracker.phantom_created = True
                self._create_skip_phantom(mkt, tracker, "prediction_low")
            self._data_logger.log_skipped(
                mkt.question, "prediction_low",
                tracker.up_high, tracker.down_high, btc)

    # ------------------------------------------------------------------
    # execute buy
    # ------------------------------------------------------------------
    async def _execute_buy(self, mkt, tracker, buy_side, buy_token, remaining,
                           prediction, size_override=None, is_flip=False):
        side_str = "YES" if buy_side == "Up" else "NO"
        trade_sz = size_override if size_override else self.trade_size

        await self.poly.get_market_prices(mkt)
        ask = mkt.yes_ask if buy_side == "Up" else mkt.no_ask
        if ask <= 0 or ask >= 1.0:
            bid = await self.poly._get_best_bid(buy_token)
            ask = bid if bid else 0
        if ask <= 0:
            return

        if ask > MAX_ENTRY_PRICE:
            log.info("[SCALP] ASK %.2fc > MAX_ENTRY %.0fc — skip | %s",
                     ask * 100, MAX_ENTRY_PRICE * 100, mkt.question[:40])
            self.stats.last_action = f"ASK TOO HIGH {ask*100:.0f}c | {mkt.question[:25]}"
            return

        result = await self.poly.buy(mkt, side_str, trade_sz)

        if not result.filled and not cfg.dry_run:
            log.warning("[SCALP] BUY FAILED — not filled | %s", mkt.question[:40])
            tracker.bought = True
            tracker.finalized = True
            self._decided_cids.add(mkt.condition_id)
            self.stats.markets_analyzed += 1
            self.stats.last_action = f"BUY FAILED | {mkt.question[:30]}"
            return

        entry = result.avg_entry if result.avg_entry > 0 else ask
        qty = (result.qty if result.qty > 0
               else math.floor((trade_sz / ask) * 100) / 100)
        btc_now = await self._data_logger.fetch_btc_price()

        pos = ScalpPosition(
            market=mkt, side=buy_side, token_id=buy_token,
            entry_price=entry, qty=qty,
            spent=round(entry * qty, 2),
            entry_time=time.time(),
            ask_at_buy=ask,
            btc_at_entry=btc_now,
            scalp_target=prediction["target"],
            scalp_sl=prediction["sl"],
            scalp_time_limit=prediction["time_limit"],
            is_flip=is_flip,
            prediction_confidence=prediction["confidence"],
            predicted_move=prediction["predicted_move"],
        )
        self._positions.append(pos)
        tracker.bought = True
        tracker.finalized = True
        self._decided_cids.add(mkt.condition_id)
        self.stats.markets_analyzed += 1
        self.stats.trades += 1
        if is_flip:
            self.stats.flip_trades += 1

        flip_tag = " [FLIP]" if is_flip else ""
        self.stats.last_action = (
            f"{'FLIP ' if is_flip else ''}BUY {buy_side} @ ${entry:.3f} "
            f"tgt=${prediction['target']:.3f} sl=${prediction['sl']:.3f} "
            f"time={prediction['time_limit']}s | {mkt.question[:25]}")
        log.info(
            "[SCALP] BUY%s %s %.1f @ $%.3f ($%.2f) | "
            "tgt=$%.3f sl=$%.3f time=%ds conf=%d | %.0fs left | %s",
            flip_tag, buy_side, qty, entry, pos.spent,
            prediction["target"], prediction["sl"],
            prediction["time_limit"], prediction["confidence"],
            remaining, mkt.question[:40],
        )

    # ------------------------------------------------------------------
    # position monitoring
    # ------------------------------------------------------------------
    async def _check_positions(self):
        now = time.time()
        btc = await self._data_logger.fetch_btc_price()

        for pos in self._positions:
            if pos.status != "open":
                continue

            bid = await self.poly._get_best_bid(pos.token_id)
            if bid is None:
                if (pos.market.window_end
                        and now > pos.market.window_end + RESOLUTION_WAIT):
                    self._close_position(pos, 0.0, "resolved-loss")
                continue

            remaining = ((pos.market.window_end - now)
                         if pos.market.window_end else 0)
            elapsed = now - pos.entry_time

            other_token = (pos.market.no_token_id if pos.side == "Up"
                           else pos.market.yes_token_id)
            other_bid = await self.poly._get_best_bid(other_token) or 0

            if other_bid > pos.other_side_high:
                pos.other_side_high = other_bid

            if not pos.reversal_detected and other_bid >= 0.60:
                pos.reversal_detected = True

            # fetch depth + feed velocity scorer
            yes_book = await self.poly.get_book_depth(pos.market.yes_token_id)
            no_book = await self.poly.get_book_depth(pos.market.no_token_id)

            up_b = bid if pos.side == "Up" else other_bid
            dn_b = bid if pos.side == "Down" else other_bid
            self._velocity_scorer.feed_tick(
                pos.market.condition_id, up_b, dn_b,
                yes_book["depth"], no_book["depth"], btc, remaining,
            )

            yes_bid = bid if pos.side == "Up" else other_bid
            no_bid = bid if pos.side == "Down" else other_bid
            self._data_logger.log_position_tick(
                pos.market.question, pos.side, pos.entry_price,
                yes_bid, no_bid,
                yes_book.get("ask", 0), no_book.get("ask", 0),
                yes_book["depth"], no_book["depth"],
                yes_book.get("ask_depth", 0), no_book.get("ask_depth", 0),
                btc, remaining,
            )

            # --- resolution ---
            if (pos.market.window_end
                    and now > pos.market.window_end + RESOLUTION_WAIT):
                if bid > RESOLUTION_BID_WIN:
                    self._close_position(pos, 1.0, "resolved-win")
                else:
                    self._close_position(pos, 0.0, "resolved-loss")
                continue

            if pos.market.window_end and now > pos.market.window_end:
                continue

            # ============== SCALP EXIT CHECKS ==============

            # 1. TIME LIMIT — force exit
            if pos.scalp_time_limit and elapsed >= pos.scalp_time_limit:
                pos.bid_at_sell_trigger = bid
                pnl_est = (bid - pos.entry_price) * pos.qty
                self.stats.time_stops += 1
                flip_tag = " [FLIP]" if pos.is_flip else ""
                log.info(
                    "  SCALP TIMEOUT%s: %s after %.0fs, bid=%.2f, "
                    "est_pnl=$%.2f | %s",
                    flip_tag, pos.side, elapsed, bid, pnl_est,
                    pos.market.question[:40],
                )
                await self._sell_position(pos, bid, "scalp-timeout")
                continue

            # 2. DYNAMIC TP
            tp = pos.scalp_target if pos.scalp_target else FALLBACK_TP
            if bid >= tp:
                pos.bid_at_sell_trigger = bid
                await self._sell_position(pos, bid, "tp")
                continue

            # 3. DYNAMIC SL
            sl = pos.scalp_sl if pos.scalp_sl else FALLBACK_SL
            if bid <= sl:
                pos.bid_at_sell_trigger = bid
                await self._sell_position(pos, bid, "sl")
                continue

            # 4. VELOCITY EXIT — opposing side surging (skip for flips)
            if not pos.is_flip:
                opp_vel, opp_now = self._velocity_scorer.get_opposing_velocity(
                    pos.market.condition_id, pos.side)
                if opp_vel > 3.0 and opp_now > 0.28:
                    pos.bid_at_sell_trigger = bid
                    self.stats.velocity_exits += 1
                    log.info(
                        "  VELOCITY EXIT: %s opp_vel=%.1fc/s opp=%.2f | %s",
                        pos.side, opp_vel, opp_now,
                        pos.market.question[:40],
                    )
                    await self._sell_position(pos, bid, "velocity-exit")
                    continue

        # --- phantom tracking ---
        await self._check_phantoms()

    async def _check_phantoms(self):
        now = time.time()
        for ph in list(self._phantoms):
            if ph.status != "phantom-open":
                continue

            bid = await self.poly._get_best_bid(ph.token_id)
            resolved = False
            won = False

            if bid is None:
                if (ph.market.window_end
                        and now > ph.market.window_end + RESOLUTION_WAIT):
                    ph.exit_price = 0.0
                    ph.pnl = -ph.entry_price * ph.qty
                    resolved, won = True, False
                else:
                    continue
            elif (ph.market.window_end
                  and now > ph.market.window_end + RESOLUTION_WAIT):
                ph.exit_price = 1.0 if bid > RESOLUTION_BID_WIN else 0.0
                ph.pnl = (ph.exit_price - ph.entry_price) * ph.qty
                resolved, won = True, ph.pnl >= 0
            elif bid >= FALLBACK_TP:
                ph.exit_price = bid
                ph.pnl = (bid - ph.entry_price) * ph.qty
                resolved, won = True, True
            elif bid <= FALLBACK_SL:
                ph.exit_price = bid
                ph.pnl = (bid - ph.entry_price) * ph.qty
                resolved, won = True, False

            if resolved:
                result = "WIN" if won else "LOSE"
                ph.status = f"phantom-{result.lower()}"
                label = ph.filter_reason.upper().replace("_", " ")
                log.info(
                    "  PHANTOM WOULD-%s (%s): %s %s $%.2f→$%.2f PnL $%+.2f",
                    result, label, ph.side, ph.market.question[:30],
                    ph.entry_price, ph.exit_price or 0, ph.pnl or 0,
                )

                if ph.filter_reason == "choppy":
                    if won:
                        self.stats.choppy_would_win += 1
                    else:
                        self.stats.choppy_would_lose += 1
                elif ph.filter_reason == "no_leader":
                    if won:
                        self.stats.noleader_would_win += 1
                    else:
                        self.stats.noleader_would_lose += 1
                elif ph.filter_reason == "manip_guard":
                    if won:
                        self.stats.manip_would_win += 1
                    else:
                        self.stats.manip_would_lose += 1
                elif ph.filter_reason == "prediction_low":
                    if won:
                        self.stats.pred_would_win += 1
                    else:
                        self.stats.pred_would_lose += 1

                self._closed.append(ph)
                self._phantoms.remove(ph)
                try:
                    log_s3_trade(ph, bot_name=self._bot_name)
                except Exception as e:
                    log.warning("Failed to log phantom: %s", e)

    # ------------------------------------------------------------------
    # sell / close
    # ------------------------------------------------------------------
    async def _sell_position(self, pos: ScalpPosition, bid: float, reason: str):
        pos.btc_at_exit = await self._data_logger.fetch_btc_price()
        if cfg.dry_run:
            pos.exit_price = bid
            pos.pnl = (bid - pos.entry_price) * pos.qty
        else:
            from bot.polymarket import Position
            temp = Position(
                market=pos.market,
                side="YES" if pos.side == "Up" else "NO",
                token_id=pos.token_id,
                qty=pos.qty,
                avg_entry=pos.entry_price,
                entry_time=pos.entry_time,
            )
            success = await self.poly.sell(temp, reason=reason)
            if not success:
                log.warning("SCALP sell failed for %s %s, will retry",
                            reason.upper(), pos.side)
                return
            pos.exit_price = temp.exit_price or bid
            pos.pnl = (temp.pnl if temp.pnl is not None
                       else (pos.exit_price - pos.entry_price) * pos.qty)

        pos.status = reason
        pos.exit_reason = reason
        is_win = pos.pnl >= 0
        if is_win:
            self.stats.wins += 1
            if pos.is_flip:
                self.stats.flip_wins += 1
        else:
            self.stats.losses += 1
        if reason == "tp":
            self.stats.tp_hits += 1
        elif reason == "sl":
            self.stats.sl_hits += 1
        if pos.reversal_detected:
            self.stats.reversals_detected += 1
        self.stats.total_pnl += pos.pnl
        self._record_hourly_pnl(pos.pnl)

        flip_tag = " [FLIP]" if pos.is_flip else ""
        rev_tag = " [REV]" if pos.reversal_detected else ""
        self.stats.last_action = (
            f"{reason.upper()}{flip_tag} {pos.side} @ "
            f"${pos.exit_price:.3f} PnL ${pos.pnl:+.2f}{rev_tag}")
        self._closed.append(pos)
        log.info(
            "[SCALP] %s%s %s @ $%.3f → $%.3f | PnL $%+.2f%s | "
            "pred_conf=%d elapsed=%.0fs | %s",
            reason.upper(), flip_tag, pos.side,
            pos.entry_price, pos.exit_price,
            pos.pnl, rev_tag,
            pos.prediction_confidence,
            time.time() - pos.entry_time,
            pos.market.question[:40],
        )

        self._persist_trade(pos.pnl, is_win)
        was_reversal = pos.reversal_detected or reason == "velocity-exit"
        self._session_tracker.record_outcome(
            was_reversal=was_reversal,
            was_manipulation_skip=False,
            pnl=pos.pnl,
            detail=reason,
        )

        self._velocity_scorer.clear(pos.market.condition_id)
        try:
            log_s3_trade(pos, bot_name=self._bot_name)
        except Exception as e:
            log.warning("Failed to log trade: %s", e)

    def _close_position(self, pos: ScalpPosition, exit_price: float,
                        reason: str):
        pos.exit_price = exit_price
        pos.pnl = (exit_price - pos.entry_price) * pos.qty
        pos.status = reason
        pos.exit_reason = reason
        if pos.btc_at_exit == 0:
            pos.btc_at_exit = self._data_logger._btc_price

        btc_swing = self._data_logger.finalize_market(pos.market.condition_id)
        resolution = (pos.side if exit_price > 0.5
                      else ("Down" if pos.side == "Up" else "Up"))
        self._data_logger.log_resolution(
            pos.market.question, resolution,
            pos.btc_at_entry, pos.btc_at_exit, btc_swing,
        )

        is_win = pos.pnl >= 0
        if is_win:
            self.stats.wins += 1
            if pos.is_flip:
                self.stats.flip_wins += 1
        else:
            self.stats.losses += 1
        if pos.reversal_detected:
            self.stats.reversals_detected += 1
        self.stats.total_pnl += pos.pnl
        self._record_hourly_pnl(pos.pnl)

        flip_tag = " [FLIP]" if pos.is_flip else ""
        rev_tag = " [REV]" if pos.reversal_detected else ""
        self.stats.last_action = (
            f"RESOLVED{flip_tag} {pos.side} ${pos.pnl:+.2f}{rev_tag}")
        self._closed.append(pos)
        log.info(
            "[SCALP] RESOLVED%s %s: $%.2f → PnL $%+.2f%s | %s",
            flip_tag, pos.side, exit_price,
            pos.pnl, rev_tag, pos.market.question[:40],
        )

        self._persist_trade(pos.pnl, is_win)
        was_reversal = pos.reversal_detected
        self._session_tracker.record_outcome(
            was_reversal=was_reversal,
            was_manipulation_skip=False,
            pnl=pos.pnl,
            detail=reason,
        )

        self._velocity_scorer.clear(pos.market.condition_id)
        try:
            log_s3_trade(pos, bot_name=self._bot_name)
        except Exception as e:
            log.warning("Failed to log trade: %s", e)

    # ------------------------------------------------------------------
    # phantom helpers
    # ------------------------------------------------------------------
    def _create_manip_phantom(self, mkt, tracker, buy_side, buy_token):
        tracker.bought = True
        tracker.finalized = True
        self._decided_cids.add(mkt.condition_id)
        self.stats.markets_analyzed += 1

        entry_price = tracker.up_high if buy_side == "Up" else tracker.down_high
        if entry_price <= 0:
            entry_price = 0.50
        qty = int(self.trade_size / max(entry_price, 0.01))

        phantom = ScalpPosition(
            market=mkt, side=buy_side, token_id=buy_token,
            entry_price=round(entry_price, 2),
            qty=qty, spent=0, entry_time=time.time(),
            status="phantom-open", filter_reason="manip_guard",
        )
        self._phantoms.append(phantom)
        log.warning("  MANIP SKIP (phantom): %s @ $%.2f | %s",
                     buy_side, entry_price, mkt.question[:30])

    def _create_skip_phantom(self, mkt, tracker, skip_reason: str):
        leader = "Up" if tracker.up_high >= tracker.down_high else "Down"
        leader_token = (mkt.yes_token_id if leader == "Up"
                        else mkt.no_token_id)
        leader_price = (tracker.up_high if leader == "Up"
                        else tracker.down_high)
        if leader_price <= 0:
            leader_price = 0.50
        qty = int(self.trade_size / max(leader_price, 0.01))

        phantom = ScalpPosition(
            market=mkt, side=leader, token_id=leader_token,
            entry_price=round(leader_price, 2),
            qty=qty, spent=0, entry_time=time.time(),
            status="phantom-open", filter_reason=skip_reason,
        )
        self._phantoms.append(phantom)
        log.info("  PHANTOM (%s): tracking %s @ $%.2f | %s",
                 skip_reason, leader, leader_price, mkt.question[:30])

    # ------------------------------------------------------------------
    # market discovery
    # ------------------------------------------------------------------
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
                    self._trackers[cid] = ScalpWindowTracker(market=mkt)
                elif remaining <= 0 and remaining > -30:
                    self._decided_cids.add(cid)
                    btc = await self._data_logger.fetch_btc_price()
                    self._data_logger.log_skipped(
                        mkt.question, "missed_expired", 0, 0, btc)

    # ------------------------------------------------------------------
    # auto-redeem (live mode)
    # ------------------------------------------------------------------
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
                log.info(
                    "[SCALP] Auto-redeemed %d → $%.2f (total: $%.2f)",
                    result["redeemed"], result["usdc_recovered"],
                    self.stats.usdc_redeemed,
                )
        except Exception as exc:
            log.warning("[SCALP] Auto-redeem error: %s", exc)

    # ------------------------------------------------------------------
    # hourly reporting
    # ------------------------------------------------------------------
    def _record_hourly_pnl(self, pnl: float):
        hour_key = datetime.now(timezone.utc).strftime("%H:00")
        self.stats.hourly_pnl[hour_key] = (
            self.stats.hourly_pnl.get(hour_key, 0) + pnl)

    def _hourly_report(self):
        now = datetime.now(timezone.utc)
        hour_key = now.strftime("%H:00")
        today = now.strftime("%Y-%m-%d")

        if self._last_day != today:
            if self._last_day:
                log.info("=== SCALP NEW DAY — resetting hourly P&L ===")
                try:
                    log_daily_snapshot(self._bot_name, {
                        "trades": self.stats.trades,
                        "wins": self.stats.wins,
                        "losses": self.stats.losses,
                        "pnl": round(self.stats.total_pnl, 2),
                        "tp_hits": self.stats.tp_hits,
                        "sl_hits": self.stats.sl_hits,
                        "time_stops": self.stats.time_stops,
                        "flip_trades": self.stats.flip_trades,
                        "flip_wins": self.stats.flip_wins,
                        "velocity_exits": self.stats.velocity_exits,
                        "skipped_prediction": self.stats.skipped_prediction,
                        "hourly_pnl": str(self.stats.hourly_pnl),
                    })
                except Exception as e:
                    log.warning("Failed to log daily snapshot: %s", e)
            self.stats.hourly_pnl = {}
            self._last_day = today

        if hour_key != self._last_hour_key and self._last_hour_key:
            prev_pnl = self.stats.hourly_pnl.get(self._last_hour_key, 0)
            sess_state, _, _ = self._session_tracker.get_session_state()
            log.info(
                "=== SCALP HOURLY [%s] ===  PnL: $%+.2f | Total: $%+.2f | "
                "W:%d L:%d | TP:%d SL:%d TO:%d VE:%d | "
                "Flips: %d (W:%d) | Session: %s",
                self._last_hour_key, prev_pnl, self.stats.total_pnl,
                self.stats.wins, self.stats.losses,
                self.stats.tp_hits, self.stats.sl_hits,
                self.stats.time_stops, self.stats.velocity_exits,
                self.stats.flip_trades, self.stats.flip_wins,
                sess_state,
            )

        if hour_key not in self.stats.hourly_pnl:
            self.stats.hourly_pnl[hour_key] = 0.0
        self._last_hour_key = hour_key

    # ------------------------------------------------------------------
    # persistence
    # ------------------------------------------------------------------
    def _persist_trade(self, pnl: float, is_win: bool):
        if self.pnl_store:
            self.pnl_store.record_trade(pnl, is_win)

    # ------------------------------------------------------------------
    # dashboard-compatible properties
    # ------------------------------------------------------------------
    @property
    def open_positions(self) -> List[ScalpPosition]:
        return [p for p in self._positions if p.status == "open"]

    @property
    def closed_positions(self) -> List[ScalpPosition]:
        return self._closed
