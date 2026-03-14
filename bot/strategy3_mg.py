"""
Strategy 3 + Manipulation Guard (MG)

Same core logic as test bot (S3 Late Momentum) with:
  - No skip-no-leader (buys the leader even if <70c)
  - Manipulation Guard: dynamically detects manipulation via
    side alternation, hot streaks, and choppy rate — pauses
    trading and tracks phantoms when triggered
  - Full phantom tracking for choppy, no-leader, and manip-guard skips
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
from bot.manip_guard import ManipulationGuard
from bot.data_logger import DataLogger
from bot.wallet_tracker import WalletTracker

log = logging.getLogger("strategy3_mg")

BUY_THRESHOLD = 0.70
BUY_MAX_PRICE = 0.90
SKIP_THRESHOLD = 0.60
ANALYSIS_START = 240.0
BUY_WINDOW_START = 180.0
BUY_WINDOW_END = 60.0
TP_PRICE = 0.94
SL_PRICE = 0.28
USDC_PER_TRADE = 20.0


@dataclass
class S3Stats:
    markets_analyzed: int = 0
    trades: int = 0
    skipped_choppy: int = 0
    skipped_no_leader: int = 0
    skipped_manip: int = 0
    tp_hits: int = 0
    sl_hits: int = 0
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
    manip_would_win: int = 0
    manip_would_lose: int = 0
    reversals_detected: int = 0
    redeems: int = 0
    usdc_redeemed: float = 0.0


REVERSAL_THRESHOLD = 0.60
RESOLUTION_WAIT = 30       # seconds after market end before checking resolution
RESOLUTION_BID_WIN = 0.90  # bid above this after resolution = our side won

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
    exit_reason: str = ""
    filter_reason: str = ""
    reversal_detected: bool = False
    ask_at_buy: float = 0.0
    btc_at_entry: float = 0.0
    btc_at_exit: float = 0.0
    other_side_high: float = 0.0
    bid_at_sell_trigger: float = 0.0


@dataclass
class PostExitTracker:
    """Monitors a market after the bot has exited (TP/SL) until resolution."""
    market: Market
    side: str
    token_id: str
    entry_price: float
    exit_price: float
    exit_reason: str
    qty: float
    exit_time: float
    our_side_max: float = 0.0
    our_side_min: float = 1.0
    other_side_max: float = 0.0
    recovered_above_entry: bool = False
    seconds_left_at_exit: float = 0.0


@dataclass
class S3WindowTracker:
    market: Market
    up_high: float = 0.0
    down_high: float = 0.0
    analyzing: bool = False
    bought: bool = False
    choppy: bool = False
    finalized: bool = False


class Strategy3MG:

    def __init__(self, poly: PolymarketClient, trade_hours=None,
                 pnl_store=None, email_on_loss=False, bot_name="research",
                 guard_config: dict = None, entry_gate: float = 1.0,
                 sl_price=None, skip_no_leader: bool = False,
                 reversal_exit_threshold: float = None):
        self.poly = poly
        self.stats = S3Stats()
        self._positions: List[S3Position] = []
        self._closed: List[S3Position] = []
        self._phantoms: List[S3Position] = []
        self.pnl_store = pnl_store
        self._email_on_loss = email_on_loss
        self._bot_name = bot_name
        self._sl_price = sl_price if sl_price is not None else SL_PRICE
        self._reversal_exit = reversal_exit_threshold
        self.trade_size = USDC_PER_TRADE
        self._trackers: Dict[str, S3WindowTracker] = {}
        self._post_exit: List[PostExitTracker] = []
        self._decided_cids: Set[str] = set()
        self._running = False
        self._last_hour_key = ""
        self._last_day = ""
        self._trade_hours = trade_hours
        self._entry_gate = entry_gate
        self._skip_no_leader = skip_no_leader
        gc = dict(guard_config or {})
        gc.setdefault("bot_name", bot_name)
        self.manip_guard = ManipulationGuard(**gc)
        self._last_redeem_check: float = 0
        self._data_logger = DataLogger(bot_name)
        self._wallet_tracker = WalletTracker(bot_name)
        if self._reversal_exit:
            log.info("S3+MG: Mid-trade reversal exit ENABLED at opposing bid >= %.2f",
                     self._reversal_exit)

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

    async def run(self):
        self._running = True
        log.info(
            "S3+MG started | buy>=%.0fc | choppy>=%.0fc | TP>=%.0fc | SL<=%.0fc | "
            "analyze 4:00-1:00 | buy 3:00-1:00 | NO skip-no-leader | MANIP GUARD ON",
            BUY_THRESHOLD * 100, SKIP_THRESHOLD * 100,
            TP_PRICE * 100, self._sl_price * 100,
        )
        while self._running:
            try:
                await self._tick()
            except Exception as exc:
                log.error("S3+MG tick error: %s", exc, exc_info=True)
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
                    log.info("S3+MG: Analyzing %s (%.0fs left)", mkt.question[:40], remaining)

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
                    self._data_logger.log_analysis_tick(
                        mkt.question, up_bid or 0, down_bid or 0,
                        yes_book.get("ask", 0), no_book.get("ask", 0),
                        yes_book["depth"], no_book["depth"],
                        yes_book.get("ask_depth", 0), no_book.get("ask_depth", 0),
                        btc, remaining,
                        market_id=cid,
                    )

                if (tracker.up_high >= SKIP_THRESHOLD and
                        tracker.down_high >= SKIP_THRESHOLD and
                        not tracker.choppy):
                    tracker.choppy = True
                    log.info(
                        "S3+MG CHOPPY: %s (Up=%.2f Down=%.2f)",
                        mkt.question[:35], tracker.up_high, tracker.down_high,
                    )

                if (remaining <= BUY_WINDOW_START and
                        not tracker.bought and
                        not tracker.choppy and
                        trading_ok):
                    up_now = up_bid or 0
                    down_now = down_bid or 0

                    buy_side = None
                    buy_token = ""

                    if up_now >= BUY_THRESHOLD and up_now <= BUY_MAX_PRICE and up_now >= down_now:
                        buy_side = "Up"
                        buy_token = mkt.yes_token_id
                    elif down_now >= BUY_THRESHOLD and down_now <= BUY_MAX_PRICE and down_now >= up_now:
                        buy_side = "Down"
                        buy_token = mkt.no_token_id

                    if buy_side:
                        buy_price = up_now if buy_side == "Up" else down_now
                        skip, reason = self.manip_guard.should_skip(
                            entry_price=buy_price, entry_gate=self._entry_gate)
                        if skip:
                            self._create_manip_phantom(mkt, tracker, buy_side, buy_token, reason)
                            btc_now = await self._data_logger.fetch_btc_price()
                            self._data_logger.log_skipped(
                                mkt.question, "manip_guard", tracker.up_high,
                                tracker.down_high, btc_now)
                        else:
                            await self._execute_buy(mkt, tracker, buy_side, buy_token, remaining)

            elif remaining <= BUY_WINDOW_END and not tracker.bought and not tracker.finalized:
                tracker.finalized = True
                self._decided_cids.add(cid)
                self.stats.markets_analyzed += 1
                btc = await self._data_logger.fetch_btc_price()

                if tracker.choppy:
                    self.stats.skipped_choppy += 1
                    self.stats.last_action = (
                        f"SKIP CHOPPY (Up={tracker.up_high:.2f} Down={tracker.down_high:.2f})"
                    )
                    log.info("S3+MG SKIP CHOPPY: %s", mkt.question[:40])
                    self._create_skip_phantom(mkt, tracker, "choppy")
                    self._data_logger.log_skipped(
                        mkt.question, "choppy", tracker.up_high, tracker.down_high, btc)
                else:
                    leader = "Up" if tracker.up_high >= tracker.down_high else "Down"
                    leader_token = mkt.yes_token_id if leader == "Up" else mkt.no_token_id
                    leader_price = tracker.up_high if leader == "Up" else tracker.down_high
                    if self._skip_no_leader:
                        self.stats.skipped_no_leader += 1
                        self.stats.last_action = (
                            f"SKIP NO LEADER ({leader} best={leader_price:.2f})")
                        log.info("S3+MG SKIP NO LEADER: %s (best=%.2f)",
                                 mkt.question[:40], leader_price)
                        self._create_skip_phantom(mkt, tracker, "no_leader")
                        self._data_logger.log_skipped(
                            mkt.question, "no_leader", tracker.up_high, tracker.down_high, btc)
                    elif leader_price > 0 and leader_price <= BUY_MAX_PRICE:
                        skip, reason = self.manip_guard.should_skip(
                            entry_price=leader_price, entry_gate=self._entry_gate)
                        if skip:
                            self._create_manip_phantom(mkt, tracker, leader, leader_token, reason)
                            self._data_logger.log_skipped(
                                mkt.question, "manip_guard", tracker.up_high, tracker.down_high, btc)
                        else:
                            log.info(
                                "S3+MG NO-LEADER BUY: %s %s @ ~%.2f",
                                leader, mkt.question[:35], leader_price,
                            )
                            tracker.finalized = False
                            await self._execute_buy(mkt, tracker, leader, leader_token, remaining)
                    else:
                        self.stats.skipped_no_leader += 1
                        self.stats.last_action = "SKIP NO LEADER (price out of range)"
                        log.info("S3+MG SKIP NO LEADER (price out of range): %s", mkt.question[:40])
                        self._create_skip_phantom(mkt, tracker, "no_leader")
                        self._data_logger.log_skipped(
                            mkt.question, "no_leader", tracker.up_high, tracker.down_high, btc)

        await self._check_positions()
        await self._auto_redeem_check()
        self._hourly_report()

    async def _auto_redeem_check(self):
        """Sweep redeemable tokens every 2 minutes (live mode only)."""
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
                    "[S3+MG] Auto-redeemed %d positions → $%.2f USDC (session total: $%.2f)",
                    result["redeemed"], result["usdc_recovered"],
                    self.stats.usdc_redeemed,
                )
        except Exception as exc:
            log.warning("[S3+MG] Auto-redeem error: %s", exc)

    def _create_manip_phantom(self, mkt, tracker, buy_side, buy_token, reason):
        """Skip a trade due to manipulation guard — track as phantom."""
        tracker.bought = True
        tracker.finalized = True
        self._decided_cids.add(mkt.condition_id)
        self.stats.markets_analyzed += 1
        self.stats.skipped_manip += 1
        self.stats.last_action = f"MANIP GUARD SKIP: {reason}"

        entry_price = tracker.up_high if buy_side == "Up" else tracker.down_high
        if entry_price <= 0:
            entry_price = 0.50
        qty = int(self.trade_size / max(entry_price, 0.01))

        phantom = S3Position(
            market=mkt, side=buy_side, token_id=buy_token,
            entry_price=round(entry_price, 2),
            qty=qty, spent=0, entry_time=time.time(),
            status="phantom-open", exit_reason="",
            filter_reason="manip_guard",
        )
        self._phantoms.append(phantom)
        log.warning(
            "  MANIP GUARD SKIP: would buy %s @ $%.2f | %s | %s",
            buy_side, entry_price, mkt.question[:30], reason,
        )

    def _create_skip_phantom(self, mkt, tracker, skip_reason: str):
        leader = "Up" if tracker.up_high >= tracker.down_high else "Down"
        leader_token = mkt.yes_token_id if leader == "Up" else mkt.no_token_id
        leader_price = tracker.up_high if leader == "Up" else tracker.down_high
        if leader_price <= 0:
            leader_price = 0.50
        qty = int(self.trade_size / max(leader_price, 0.01))
        phantom = S3Position(
            market=mkt, side=leader, token_id=leader_token,
            entry_price=round(leader_price, 2),
            qty=qty, spent=0, entry_time=time.time(),
            status="phantom-open", exit_reason="",
            filter_reason=skip_reason,
        )
        self._phantoms.append(phantom)
        log.info(
            "  PHANTOM (%s): tracking %s %s @ $%.2f",
            skip_reason, leader, mkt.question[:30], leader_price,
        )

    async def _execute_buy(self, mkt, tracker, buy_side, buy_token, remaining):
        side_str = "YES" if buy_side == "Up" else "NO"

        await self.poly.get_market_prices(mkt)
        ask = mkt.yes_ask if buy_side == "Up" else mkt.no_ask
        if ask <= 0 or ask >= 1.0:
            bid = await self.poly._get_best_bid(buy_token)
            ask = bid if bid else 0
        if ask <= 0:
            return

        result = await self.poly.buy(mkt, side_str, self.trade_size)

        if not result.filled and not cfg.dry_run:
            log.warning("[S3+MG] BUY FAILED — order not filled, skipping %s", mkt.question[:40])
            tracker.bought = True
            tracker.finalized = True
            self._decided_cids.add(mkt.condition_id)
            self.stats.markets_analyzed += 1
            self.stats.last_action = f"BUY FAILED (not filled) | {mkt.question[:30]}"
            return

        entry = result.avg_entry if result.avg_entry > 0 else ask
        qty = result.qty if result.qty > 0 else math.floor((self.trade_size / ask) * 100) / 100
        btc_now = await self._data_logger.fetch_btc_price()

        pos = S3Position(
            market=mkt, side=buy_side, token_id=buy_token,
            entry_price=entry, qty=qty,
            spent=round(entry * qty, 2),
            entry_time=time.time(),
            ask_at_buy=ask,
            btc_at_entry=btc_now,
        )
        self._positions.append(pos)
        tracker.bought = True
        tracker.finalized = True
        self._decided_cids.add(mkt.condition_id)
        self.stats.markets_analyzed += 1
        self.stats.trades += 1
        self.stats.last_action = f"BUY {buy_side} @ ${entry:.3f} | {mkt.question[:30]}"
        log.info(
            "[S3+MG] BUY %s %.1f @ $%.3f ($%.2f) | %.0fs left | %s",
            buy_side, qty, entry, pos.spent, remaining, mkt.question[:45],
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

            if not pos.reversal_detected and other_bid >= REVERSAL_THRESHOLD:
                pos.reversal_detected = True
                log.info("  REVERSAL DETECTED: %s bought %s @ %.2f, other side now %.2f | %s",
                         pos.side, pos.side, pos.entry_price, other_bid,
                         pos.market.question[:40])

            if other_bid > pos.other_side_high:
                pos.other_side_high = other_bid

            yes_bid = bid if pos.side == "Up" else other_bid
            no_bid = bid if pos.side == "Down" else other_bid
            yes_book = await self.poly.get_book_depth(pos.market.yes_token_id)
            no_book = await self.poly.get_book_depth(pos.market.no_token_id)

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

            if bid >= TP_PRICE:
                pos.bid_at_sell_trigger = bid
                await self._sell_position(pos, bid, "tp")
                continue

            if self._reversal_exit and other_bid >= self._reversal_exit and remaining > 30:
                pos.bid_at_sell_trigger = bid
                pos.reversal_detected = True
                log.info("  MID-TRADE REVERSAL EXIT: %s @ %.2f, opp=%.2f, our=%.2f | %s",
                         pos.side, pos.entry_price, other_bid, bid,
                         pos.market.question[:40])
                await self._sell_position(pos, bid, "reversal-exit")
                continue

            if bid <= self._sl_price:
                pos.bid_at_sell_trigger = bid
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
            elif bid <= self._sl_price:
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
                if ph.filter_reason == "choppy":
                    if won:
                        self.stats.choppy_would_win += 1
                    else:
                        self.stats.choppy_would_lose += 1
                    self.manip_guard.record_market(ph.side, won, ph.pnl or 0, was_choppy=True)
                elif ph.filter_reason == "no_leader":
                    if won:
                        self.stats.noleader_would_win += 1
                    else:
                        self.stats.noleader_would_lose += 1
                    self.manip_guard.record_market(ph.side, won, ph.pnl or 0, was_noleader=True)
                elif ph.filter_reason == "manip_guard":
                    if won:
                        self.stats.manip_would_win += 1
                    else:
                        self.stats.manip_would_lose += 1
                    self.manip_guard.record_phantom(won)
                    self.manip_guard.record_market(ph.side, won, ph.pnl or 0)

                self._closed.append(ph)
                self._phantoms.remove(ph)
                try:
                    log_s3_trade(ph, bot_name=self._bot_name)
                except Exception as e:
                    log.warning("Failed to log phantom to history: %s", e)

                ph_resolution = ph.side if won else ("Down" if ph.side == "Up" else "Up")
                asyncio.ensure_future(self._wallet_tracker.fetch_and_log_trades(
                    condition_id=ph.market.condition_id,
                    market_name=ph.market.question,
                    window_end=ph.market.window_end or 0,
                    resolution=ph_resolution,
                    bot_side=ph.side,
                ))

        for pet in list(self._post_exit):
            bid = await self.poly._get_best_bid(pet.token_id)
            resolved = False
            resolution = ""

            if bid is None:
                if pet.market.window_end and now > pet.market.window_end + RESOLUTION_WAIT:
                    resolved = True
                    resolution = "Down" if pet.side == "Up" else "Up"
                else:
                    continue
            else:
                if bid > pet.our_side_max:
                    pet.our_side_max = bid
                if bid < pet.our_side_min:
                    pet.our_side_min = bid
                if not pet.recovered_above_entry and bid >= pet.entry_price:
                    pet.recovered_above_entry = True

                other_token = (pet.market.no_token_id if pet.side == "Up"
                               else pet.market.yes_token_id)
                other_bid = await self.poly._get_best_bid(other_token) or 0
                if other_bid > pet.other_side_max:
                    pet.other_side_max = other_bid

                if pet.market.window_end and now > pet.market.window_end + RESOLUTION_WAIT:
                    resolved = True
                    resolution = pet.side if bid > RESOLUTION_BID_WIN else ("Down" if pet.side == "Up" else "Up")

            if resolved:
                res_price = 1.0 if resolution == pet.side else 0.0
                held_pnl = (res_price - pet.entry_price) * pet.qty
                self._data_logger.log_post_exit(
                    pet.market.question, pet.side, pet.exit_reason,
                    pet.entry_price, pet.exit_price, pet.exit_price,
                    resolution, held_pnl,
                    pet.our_side_max, pet.our_side_min,
                    pet.other_side_max,
                    pet.recovered_above_entry,
                    pet.seconds_left_at_exit,
                )
                held_tag = "SAME" if (held_pnl >= 0) == (pet.exit_reason == "tp") else "DIFF"
                log.info(
                    "  POST-EXIT RESOLVED: %s %s | exit=%s@$%.2f | resolved=%s | held=$%+.2f (%s) | max=$%.2f min=$%.2f",
                    pet.side, pet.market.question[:30], pet.exit_reason,
                    pet.exit_price, resolution, held_pnl, held_tag,
                    pet.our_side_max, pet.our_side_min,
                )
                self._post_exit.remove(pet)

    async def _sell_position(self, pos: S3Position, bid: float, reason: str):
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
                log.warning("S3+MG sell failed for %s %s, will retry", reason.upper(), pos.side)
                return
            pos.exit_price = temp.exit_price or bid
            pos.pnl = temp.pnl if temp.pnl is not None else (pos.exit_price - pos.entry_price) * pos.qty

        pos.status = reason
        pos.exit_reason = reason
        is_win = pos.pnl >= 0
        if is_win:
            self.stats.wins += 1
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
        rev_tag = " [REVERSAL]" if pos.reversal_detected else ""
        self.stats.last_action = f"{reason.upper()} {pos.side} @ ${pos.exit_price:.3f} PnL ${pos.pnl:+.2f}{rev_tag}"
        self._closed.append(pos)
        log.info(
            "[S3+MG] %s %s @ $%.3f -> $%.3f | PnL $%+.2f%s | %s",
            reason.upper(), pos.side, pos.entry_price, pos.exit_price,
            pos.pnl, rev_tag, pos.market.question[:40],
        )
        self._persist_trade(pos.pnl, is_win)
        self.manip_guard.record_market(pos.side, is_win, pos.pnl,
                                       was_reversal=pos.reversal_detected)
        try:
            log_s3_trade(pos, bot_name=self._bot_name)
        except Exception as e:
            log.warning("Failed to log trade to history: %s", e)

        remaining = (pos.market.window_end - time.time()) if pos.market.window_end else 0
        if remaining > 5:
            pet = PostExitTracker(
                market=pos.market, side=pos.side, token_id=pos.token_id,
                entry_price=pos.entry_price, exit_price=pos.exit_price or bid,
                exit_reason=reason, qty=pos.qty, exit_time=time.time(),
                our_side_max=pos.exit_price or bid,
                our_side_min=pos.exit_price or bid,
                other_side_max=pos.other_side_high,
                seconds_left_at_exit=remaining,
            )
            self._post_exit.append(pet)
            log.info("  POST-EXIT TRACKING: %s %s | %.0fs remaining | %s",
                     reason.upper(), pos.side, remaining, pos.market.question[:35])

    def _close_position(self, pos: S3Position, exit_price: float, reason: str):
        pos.exit_price = exit_price
        pos.pnl = (exit_price - pos.entry_price) * pos.qty
        pos.status = reason
        pos.exit_reason = reason
        if pos.btc_at_exit == 0:
            pos.btc_at_exit = self._data_logger._btc_price
        resolution = pos.side if exit_price > 0.5 else ("Down" if pos.side == "Up" else "Up")
        btc_swing = self._data_logger.finalize_market(pos.market.condition_id)
        self._data_logger.log_resolution(
            pos.market.question, resolution,
            pos.btc_at_entry, pos.btc_at_exit, btc_swing,
        )
        is_win = pos.pnl >= 0
        if is_win:
            self.stats.wins += 1
        else:
            self.stats.losses += 1
        if pos.reversal_detected:
            self.stats.reversals_detected += 1
        self.stats.total_pnl += pos.pnl
        self._record_hourly_pnl(pos.pnl)
        rev_tag = " [REVERSAL]" if pos.reversal_detected else ""
        self.stats.last_action = f"RESOLVED {pos.side} ${pos.pnl:+.2f}{rev_tag}"
        self._closed.append(pos)
        log.info(
            "[S3+MG] RESOLVED %s: $%.2f -> PnL $%+.2f%s | %s",
            pos.side, exit_price, pos.pnl, rev_tag, pos.market.question[:45],
        )
        self._persist_trade(pos.pnl, is_win)
        self.manip_guard.record_market(pos.side, is_win, pos.pnl,
                                       was_reversal=pos.reversal_detected)
        try:
            log_s3_trade(pos, bot_name=self._bot_name)
        except Exception as e:
            log.warning("Failed to log trade to history: %s", e)

        asyncio.ensure_future(self._wallet_tracker.fetch_and_log_trades(
            condition_id=pos.market.condition_id,
            market_name=pos.market.question,
            window_end=pos.market.window_end or 0,
            resolution=resolution,
            bot_side=pos.side,
        ))

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
                elif remaining <= 0 and remaining > -30:
                    self._decided_cids.add(cid)
                    btc = await self._data_logger.fetch_btc_price()
                    self._data_logger.log_skipped(
                        mkt.question, "missed_expired", 0, 0, btc)

    def _record_hourly_pnl(self, pnl: float):
        hour_key = datetime.now(timezone.utc).strftime("%H:00")
        self.stats.hourly_pnl[hour_key] = self.stats.hourly_pnl.get(hour_key, 0) + pnl

    def _hourly_report(self):
        now = datetime.now(timezone.utc)
        hour_key = now.strftime("%H:00")
        today = now.strftime("%Y-%m-%d")

        if self._last_day != today:
            if self._last_day:
                log.info("=== S3+MG NEW DAY — resetting hourly P&L ===")
                try:
                    mg = self.manip_guard.status_dict
                    log_daily_snapshot(self._bot_name, {
                        "trades": self.stats.trades, "wins": self.stats.wins,
                        "losses": self.stats.losses, "pnl": round(self.stats.total_pnl, 2),
                        "tp_hits": self.stats.tp_hits, "sl_hits": self.stats.sl_hits,
                        "skipped_choppy": self.stats.skipped_choppy,
                        "skipped_manip": self.stats.skipped_manip,
                        "choppy_would_win": self.stats.choppy_would_win,
                        "choppy_would_lose": self.stats.choppy_would_lose,
                        "noleader_would_win": self.stats.noleader_would_win,
                        "noleader_would_lose": self.stats.noleader_would_lose,
                        "manip_would_win": self.stats.manip_would_win,
                        "manip_would_lose": self.stats.manip_would_lose,
                        "reversals_detected": self.stats.reversals_detected,
                        "manip_total_pauses": mg["total_pauses"],
                        "hourly_pnl": str(self.stats.hourly_pnl),
                    })
                except Exception as e:
                    log.warning("Failed to log daily snapshot: %s", e)
            self.stats.hourly_pnl = {}
            self._last_day = today

        if hour_key != self._last_hour_key and self._last_hour_key:
            prev_pnl = self.stats.hourly_pnl.get(self._last_hour_key, 0)
            mg = self.manip_guard.status_dict
            log.info(
                "=== S3+MG HOURLY [%s] ===  PnL: $%+.2f | Total: $%+.2f | W:%d L:%d | "
                "ManipSkips:%d (W:%d L:%d) | Reversals:%d | RevRate:%.0f%% | Guard: %s",
                self._last_hour_key, prev_pnl, self.stats.total_pnl,
                self.stats.wins, self.stats.losses,
                self.stats.skipped_manip, self.stats.manip_would_win, self.stats.manip_would_lose,
                self.stats.reversals_detected, mg["reversal_rate"] * 100,
                "PAUSED" if mg["paused"] else "ACTIVE",
            )

        if hour_key not in self.stats.hourly_pnl:
            self.stats.hourly_pnl[hour_key] = 0.0
        self._last_hour_key = hour_key

    def _persist_trade(self, pnl: float, is_win: bool):
        if self.pnl_store:
            self.pnl_store.record_trade(pnl, is_win)

    @property
    def open_positions(self) -> List[S3Position]:
        return [p for p in self._positions if p.status == "open"]

    @property
    def closed_positions(self) -> List[S3Position]:
        return self._closed
