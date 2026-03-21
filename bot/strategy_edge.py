"""
Strategy Edge — Scalp bot with 1:1 risk/reward

Simulated at +$190/day over 6 days (Mar 13-19).
Uses +/-12c TP/SL from entry instead of holding to expiry.

Filters (minimal):
  - Skip choppy (both sides hit 65c)
  - Skip no leader (avg bid diff < 2c)
  - Skip opp > 40c
Entry:
  - Buy leader during 3min-1min window
Exit:
  - TP: bid >= entry + 12c → sell immediately
  - SL: bid <= entry - 12c → sell immediately
  - Timeout: 30s before expiry → sell at market
"""

import asyncio
import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, List, Dict, Set

from bot.config import cfg
from bot.polymarket import PolymarketClient, Market
from bot.trade_history import log_s3_trade, log_daily_snapshot
from bot.data_logger import DataLogger

log = logging.getLogger("strategy_edge")

ANALYSIS_START = 240.0
BUY_WINDOW_START = 180.0
BUY_WINDOW_END = 55.0
CHOPPY_THRESHOLD = 0.65
LEADER_MIN_DIFF = 0.02
MAX_OPP_BID = 0.40

SCALP_SIZE = 0.12
USDC_PER_TRADE = 30.0
TIMEOUT_SECS = 30.0

RESOLUTION_WAIT = 30
RESOLUTION_BID_WIN = 0.90
HOLD_POLL_INTERVAL = 1.5


@dataclass
class EdgeStats:
    markets_analyzed: int = 0
    trades: int = 0
    skipped_choppy: int = 0
    skipped_no_leader: int = 0
    skipped_opp_high: int = 0
    tp_hits: int = 0
    sl_hits: int = 0
    time_stops: int = 0
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
    force_exits: int = 0
    redeems: int = 0
    usdc_redeemed: float = 0.0


@dataclass
class EdgePosition:
    market: Market
    side: str
    token_id: str
    entry_price: float
    qty: float
    spent: float
    entry_time: float
    tp_price: float = 0.0
    sl_price: float = 0.0
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
class EdgeTracker:
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

    def avg_opp_bid(self, leader_side):
        if not self.tick_history:
            return 0.0
        opp = [t[2] if leader_side == "Up" else t[1] for t in self.tick_history]
        return sum(opp) / len(opp) if opp else 0.0

    def avg_depth_ratio(self, leader_side):
        if len(self.tick_history) < 2:
            return 0.0
        ratios = []
        for _, ub, db, ud, dd, _ in self.tick_history:
            ld, od = (ud, dd) if leader_side == "Up" else (dd, ud)
            ratios.append(ld / max(od, 1))
        return sum(ratios) / len(ratios)

    @property
    def tick_count(self):
        return len(self.tick_history)


class StrategyEdge:

    def __init__(self, poly: PolymarketClient, trade_hours=None,
                 pnl_store=None, email_on_loss=False, bot_name="edge",
                 **_ignored):
        self.poly = poly
        self.stats = EdgeStats()
        self._positions: List[EdgePosition] = []
        self._closed: List[EdgePosition] = []
        self._phantoms: List[EdgePosition] = []
        self.pnl_store = pnl_store
        self._bot_name = bot_name
        self.trade_size = USDC_PER_TRADE
        self._trackers: Dict[str, EdgeTracker] = {}
        self._decided_cids: Set[str] = set()
        self._running = False
        self._last_hour_key = ""
        self._last_day = ""
        self._trade_hours = trade_hours
        self._data_logger = DataLogger(bot_name)
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
            "EDGE BOT started | scalp +/-%.0fc | $%.0f/trade | "
            "choppy>=%.0fc | oppMax=%.0fc | timeout=%ds",
            SCALP_SIZE * 100, USDC_PER_TRADE,
            CHOPPY_THRESHOLD * 100, MAX_OPP_BID * 100, int(TIMEOUT_SECS),
        )
        while self._running:
            try:
                await self._tick()
            except Exception as exc:
                log.error("EDGE tick error: %s", exc, exc_info=True)
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

            # Log full ticks
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
                except Exception as exc:
                    log.debug("Full tick log error: %s", exc)

            if tracker.finalized:
                continue

            if remaining <= 0:
                if not tracker.bought:
                    tracker.finalized = True
                    self._decided_cids.add(cid)
                continue

            # Analysis phase: track highs and check choppy
            if remaining <= ANALYSIS_START and remaining > BUY_WINDOW_END:
                if not tracker.analyzing:
                    tracker.analyzing = True
                    log.info("EDGE: Analyzing %s (%.0fs left)", mkt.question[:40], remaining)

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

                if (tracker.up_high >= CHOPPY_THRESHOLD and
                        tracker.down_high >= CHOPPY_THRESHOLD and
                        not tracker.choppy):
                    tracker.choppy = True
                    log.info("EDGE CHOPPY: %s (Up=%.2f Down=%.2f)",
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

                btc = await self._data_logger.fetch_btc_price()
                if tracker.choppy:
                    self.stats.skipped_choppy += 1
                    self.stats.last_action = f"SKIP CHOPPY (Up={tracker.up_high:.2f} Down={tracker.down_high:.2f})"
                    self._create_phantom(mkt, tracker, "choppy")
                    self._data_logger.log_skipped(
                        mkt.question, "choppy", tracker.up_high, tracker.down_high, btc)
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
        up_now = await self.poly._get_best_bid(mkt.yes_token_id) or 0
        down_now = await self.poly._get_best_bid(mkt.no_token_id) or 0

        if abs(up_now - down_now) < LEADER_MIN_DIFF:
            return

        if up_now > down_now:
            buy_side = "Up"
            buy_token = mkt.yes_token_id
            buy_price = up_now
        else:
            buy_side = "Down"
            buy_token = mkt.no_token_id
            buy_price = down_now

        if tracker.tick_count < 3:
            return

        avg_opp = tracker.avg_opp_bid(buy_side)
        avg_dr = tracker.avg_depth_ratio(buy_side)
        btc = await self._data_logger.fetch_btc_price()
        est_hour = self._get_est_hour()

        log.info(
            "EDGE EVAL: %s @%.2f | opp=%.0fc depth=%.1fx | h%d | %.0fs | %s",
            buy_side, buy_price, avg_opp * 100, avg_dr, est_hour,
            remaining, mkt.question[:35])

        # Filter: opp bid too high
        if avg_opp > MAX_OPP_BID:
            self.stats.skipped_opp_high += 1
            self.stats.last_action = f"SKIP OPP {avg_opp*100:.0f}c > {MAX_OPP_BID*100:.0f}c"
            log.info("  EDGE SKIP: opp %.0fc > %.0fc | %s",
                     avg_opp * 100, MAX_OPP_BID * 100, mkt.question[:30])
            tracker.bought = True
            tracker.finalized = True
            self._decided_cids.add(cid)
            self.stats.markets_analyzed += 1
            self._create_phantom(mkt, tracker, "opp_strong")
            self._data_logger.log_skipped(
                mkt.question, "opp_strong", tracker.up_high, tracker.down_high, btc)
            return

        log.info("  EDGE BUY: %s @ %.2f | opp=%.0fc dr=%.1fx | %s",
                 buy_side, buy_price, avg_opp * 100, avg_dr, mkt.question[:35])
        await self._execute_buy(mkt, tracker, buy_side, buy_token, remaining,
                                avg_dr, est_hour)

    async def _execute_buy(self, mkt, tracker, buy_side, buy_token, remaining,
                           depth_ratio=0, hour=-1):
        side_str = "YES" if buy_side == "Up" else "NO"

        await self.poly.get_market_prices(mkt)
        ask = mkt.yes_ask if buy_side == "Up" else mkt.no_ask
        if ask <= 0 or ask >= 1.0:
            bid = await self.poly._get_best_bid(buy_token)
            ask = bid if bid else 0
        if ask <= 0:
            return

        if ask > 0.94:
            log.info("  EDGE SKIP: ASK %.2fc too high", ask * 100)
            return

        result = await self.poly.buy(mkt, side_str, self.trade_size)

        if not result.filled and not cfg.dry_run:
            log.warning("EDGE BUY FAILED | %s", mkt.question[:40])
            tracker.bought = True
            tracker.finalized = True
            self._decided_cids.add(mkt.condition_id)
            self.stats.markets_analyzed += 1
            return

        entry = result.avg_entry if result.avg_entry > 0 else ask
        qty = result.qty if result.qty > 0 else math.floor((self.trade_size / ask) * 100) / 100
        btc_now = await self._data_logger.fetch_btc_price()

        tp_price = min(entry + SCALP_SIZE, 0.99)
        sl_price = max(entry - SCALP_SIZE, 0.01)

        pos = EdgePosition(
            market=mkt, side=buy_side, token_id=buy_token,
            entry_price=entry, qty=qty,
            spent=round(entry * qty, 2),
            entry_time=time.time(),
            tp_price=tp_price,
            sl_price=sl_price,
            ask_at_buy=ask,
            btc_at_entry=btc_now,
            depth_ratio_at_entry=depth_ratio,
            hour_at_entry=hour,
        )
        self._positions.append(pos)
        tracker.bought = True
        tracker.finalized = True
        self._decided_cids.add(mkt.condition_id)
        self.stats.markets_analyzed += 1
        self.stats.trades += 1
        self.stats.last_action = (
            f"BUY {buy_side} @ ${entry:.3f} | "
            f"TP ${tp_price:.2f} SL ${sl_price:.2f} | {mkt.question[:25]}"
        )
        log.info(
            "[EDGE] BUY %s %.1f @ $%.3f ($%.2f) | TP $%.2f SL $%.2f | %.0fs left | %s",
            buy_side, qty, entry, pos.spent, tp_price, sl_price,
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

            self._data_logger.log_position_tick(
                pos.market.question, pos.side, pos.entry_price,
                yes_bid, no_bid,
                yes_book.get("ask", 0), no_book.get("ask", 0),
                yes_book["depth"], no_book["depth"],
                yes_book.get("ask_depth", 0), no_book.get("ask_depth", 0),
                btc, remaining,
            )

            # Post-resolution fallback
            if pos.market.window_end and now > pos.market.window_end + RESOLUTION_WAIT:
                if bid > RESOLUTION_BID_WIN:
                    self._close_position(pos, 1.0, "resolved-win")
                else:
                    self._close_position(pos, 0.0, "resolved-loss")
                continue

            if pos.market.window_end and now > pos.market.window_end:
                continue

            # TP: bid >= entry + scalp
            if bid >= pos.tp_price:
                pos.btc_at_exit = btc
                log.info("  EDGE TP HIT: %s bid=%.2f >= tp=%.2f | %s",
                         pos.side, bid, pos.tp_price, pos.market.question[:40])
                await self._sell_position(pos, bid, "tp")
                continue

            # SL: bid <= entry - scalp
            if bid <= pos.sl_price:
                pos.btc_at_exit = btc
                log.info("  EDGE SL HIT: %s bid=%.2f <= sl=%.2f | %s",
                         pos.side, bid, pos.sl_price, pos.market.question[:40])
                await self._sell_position(pos, bid, "sl")
                continue

            # Timeout before expiry
            if remaining <= TIMEOUT_SECS and remaining > 0:
                pos.btc_at_exit = btc
                reason = "timeout-win" if bid >= pos.entry_price else "timeout-loss"
                log.info("  EDGE TIMEOUT: %s bid=%.2f entry=%.2f | %s | %s",
                         pos.side, bid, pos.entry_price, reason, pos.market.question[:40])
                await self._sell_position(pos, bid, reason)
                continue

        # Phantom tracking
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
            elif bid >= ph.entry_price + SCALP_SIZE:
                ph.exit_price = ph.entry_price + SCALP_SIZE
                ph.pnl = SCALP_SIZE * ph.qty
                resolved, won = True, True
            elif bid <= ph.entry_price - SCALP_SIZE:
                ph.exit_price = ph.entry_price - SCALP_SIZE
                ph.pnl = -SCALP_SIZE * ph.qty
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
        else:
            self.stats.losses += 1

        if "tp" == reason:
            self.stats.tp_hits += 1
        elif "sl" == reason:
            self.stats.sl_hits += 1
        elif "timeout" in reason:
            self.stats.time_stops += 1
            self.stats.force_exits += 1

        self.stats.total_pnl += pos.pnl

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
            "[EDGE] %s %s %.1f @ $%.3f -> $%.3f | PnL $%+.2f | session $%+.2f | %s",
            reason.upper(), pos.side, pos.qty, pos.entry_price, exit_price,
            pos.pnl, self.stats.total_pnl, pos.market.question[:35],
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
        phantom = EdgePosition(
            market=mkt, side=leader, token_id=leader_token,
            entry_price=round(leader_price, 2),
            qty=qty, spent=0, entry_time=time.time(),
            tp_price=round(leader_price + SCALP_SIZE, 2),
            sl_price=round(max(leader_price - SCALP_SIZE, 0.01), 2),
            status="phantom-open", filter_reason=skip_reason,
        )
        self._phantoms.append(phantom)
        log.info("  PHANTOM (%s): tracking %s %s @ $%.2f (TP $%.2f SL $%.2f)",
                 skip_reason, leader, mkt.question[:30], leader_price,
                 phantom.tp_price, phantom.sl_price)

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
                log.info("[EDGE] Auto-redeemed %d -> $%.2f",
                         result["redeemed"], result["usdc_recovered"])
        except Exception as exc:
            log.warning("[EDGE] Auto-redeem error: %s", exc)

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
                        log.info("EDGE: SKIP first market after restart: %s (%.0fs left)",
                                 mkt.question[:50], remaining)
                        continue
                    self._trackers[cid] = EdgeTracker(market=mkt)
                    log.info("EDGE: Tracking %s (%.0fs left)", mkt.question[:50], remaining)
                elif remaining <= 0 and remaining > -30:
                    self._decided_cids.add(cid)

    @property
    def open_positions(self) -> List[EdgePosition]:
        return [p for p in self._positions if p.status == "open"]

    @property
    def closed_positions(self) -> List[EdgePosition]:
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
            log.info("[EDGE] %s", self.stats.last_hour_report)
        self._last_hour_key = hour_key

        if day_key != self._last_day and self._last_day:
            log_daily_snapshot(self._bot_name, vars(self.stats))
        self._last_day = day_key
