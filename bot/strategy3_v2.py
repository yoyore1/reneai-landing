"""
Strategy 3 V2 — Research Bot (Changed March 8th)

Based on the raw S3 strategy (test bot) but with targeted improvements
informed by 5 days of data analysis:

Changes from test bot:
  1. Time-based hour skipping: skip hours 03, 09, 11, 14, 15, 18, 19 EST
     (consistently negative across all days, ~$170 in losses)
  2. Max buy price lowered from 90c to 85c (87c+ has 60% WR, too risky)
  3. Volatility guard: pauses when BTC range < $100 + low WR + high choppy rate
  4. Streak breaker: 3 consecutive losses = skip next market

What's NOT in here (removed from old research bot):
  - No volume filters (bid_70+, depth_ratio, btc_move) — they blocked 72%
    winning trades, costing ~$157 over 2 days
  - No order book depth fetching at buy time (not needed without filters)

Still tracks phantom positions for skipped hours so we can keep refining.
Always dry run.
"""

import asyncio
import logging
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, List, Dict, Set

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

from bot.config import cfg
from bot.polymarket import PolymarketClient, Market
from bot.trade_history import log_research_trade, log_daily_snapshot
from bot.vol_guard import VolatilityGuard

log = logging.getLogger("strategy3_v2")
EST = ZoneInfo("America/New_York")

BUY_THRESHOLD = 0.70
BUY_MAX_PRICE = 0.85
SKIP_THRESHOLD = 0.60
ANALYSIS_START = 240.0
BUY_WINDOW_START = 180.0
BUY_WINDOW_END = 60.0
TP_PRICE = 0.94
SL_PRICE = 0.28
USDC_PER_TRADE = 20.0

SKIP_HOURS = {3, 9, 11, 14, 15, 18, 19}


@dataclass
class S3Stats:
    markets_analyzed: int = 0
    trades: int = 0
    skipped_choppy: int = 0
    skipped_no_leader: int = 0
    skipped_hour: int = 0
    filtered_out: int = 0
    filtered_would_win: int = 0
    filtered_would_lose: int = 0
    choppy_would_win: int = 0
    choppy_would_lose: int = 0
    noleader_would_win: int = 0
    noleader_would_lose: int = 0
    tp_hits: int = 0
    sl_hits: int = 0
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
    exit_reason: str = ""
    vol_snapshot: Optional[dict] = None


@dataclass
class S3WindowTracker:
    market: Market
    up_high: float = 0.0
    down_high: float = 0.0
    analyzing: bool = False
    bought: bool = False
    choppy: bool = False
    finalized: bool = False


class Strategy3V2:
    """S3 V2: time-based skips + vol guard + streak breaker. Always dry run."""

    def __init__(self, poly: PolymarketClient, trade_hours=None,
                 pnl_store=None, email_on_loss=False):
        self.poly = poly
        self.stats = S3Stats()
        self._positions: List[S3Position] = []
        self._closed: List[S3Position] = []
        self._phantoms: List[S3Position] = []
        self.pnl_store = pnl_store
        self._email_on_loss = False
        self.trade_size = USDC_PER_TRADE
        self._trackers: Dict[str, S3WindowTracker] = {}
        self._decided_cids: Set[str] = set()
        self._running = False
        self._last_hour_key = ""
        self._last_day = ""
        self._trade_hours = trade_hours
        self.vol_guard = VolatilityGuard()
        self._consec_losses = 0
        self._skip_next = False

    def _is_trading_time(self) -> bool:
        return True

    def _is_skip_hour(self) -> bool:
        now_est = datetime.now(EST)
        return now_est.hour in SKIP_HOURS

    async def run(self):
        self._running = True
        skip_str = ",".join(f"{h:02d}" for h in sorted(SKIP_HOURS))
        log.info(
            "S3-V2 RESEARCH started | buy>=%.0fc<=%.0fc | choppy>=%.0fc | TP>=%.0fc | SL<=%.0fc | skip_hours=[%s]",
            BUY_THRESHOLD * 100, BUY_MAX_PRICE * 100, SKIP_THRESHOLD * 100,
            TP_PRICE * 100, SL_PRICE * 100, skip_str,
        )
        while self._running:
            try:
                await self._tick()
            except Exception as exc:
                log.error("S3-V2 tick error: %s", exc, exc_info=True)
            await asyncio.sleep(1)

    def stop(self):
        self._running = False

    async def _tick(self):
        now = time.time()

        if not hasattr(self, "_last_disc") or now - self._last_disc > 30:
            await self._discover()
            self._last_disc = now

        await self.vol_guard.check_btc()

        for cid, tracker in list(self._trackers.items()):
            if tracker.finalized:
                continue

            mkt = tracker.market
            if not mkt.window_end:
                continue

            remaining = mkt.window_end - now

            if remaining <= 0:
                if not tracker.bought and not tracker.finalized:
                    tracker.finalized = True
                    self._decided_cids.add(cid)
                self._trackers.pop(cid, None)
                continue

            if remaining <= ANALYSIS_START and remaining > BUY_WINDOW_END:
                if not tracker.analyzing:
                    tracker.analyzing = True
                    log.info("S3-V2: Analyzing %s (%.0fs left)", mkt.question[:40], remaining)

                up_bid = await self.poly._get_best_bid(mkt.yes_token_id)
                down_bid = await self.poly._get_best_bid(mkt.no_token_id)

                if up_bid and up_bid > tracker.up_high:
                    tracker.up_high = up_bid
                if down_bid and down_bid > tracker.down_high:
                    tracker.down_high = down_bid

                if (tracker.up_high >= SKIP_THRESHOLD and
                        tracker.down_high >= SKIP_THRESHOLD and
                        not tracker.choppy):
                    tracker.choppy = True
                    log.info(
                        "S3-V2 CHOPPY: %s (Up=%.2f Down=%.2f)",
                        mkt.question[:35], tracker.up_high, tracker.down_high,
                    )

                if (remaining <= BUY_WINDOW_START and
                        not tracker.bought and
                        not tracker.choppy):
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
                        ask_price = up_now if buy_side == "Up" else down_now
                        skip_reason = None

                        if self._skip_next:
                            skip_reason = "streak_breaker"
                            self._skip_next = False
                            log.info("  STREAK BREAKER: skipping after 3 losses | %s @ %.2f", buy_side, ask_price)
                        elif self.vol_guard.is_paused:
                            skip_reason = f"vol_guard:{self.vol_guard.reason}"
                            log.info("  VOL GUARD BLOCKED: %s @ %.2f | %s", buy_side, ask_price, self.vol_guard.reason)
                        elif self._is_skip_hour():
                            now_est = datetime.now(EST)
                            skip_reason = f"skip_hour:{now_est.hour:02d}"
                            self.stats.skipped_hour += 1
                            log.info("  HOUR SKIP: %s @ %.2f | hour %02d EST is in skip list", buy_side, ask_price, now_est.hour)

                        if skip_reason:
                            tracker.bought = True
                            tracker.finalized = True
                            self._decided_cids.add(cid)
                            self.stats.markets_analyzed += 1
                            bt = mkt.yes_token_id if buy_side == "Up" else mkt.no_token_id
                            phantom = S3Position(
                                market=mkt, side=buy_side, token_id=bt,
                                entry_price=round(ask_price, 2),
                                qty=int(self.trade_size / max(ask_price, 0.01)),
                                spent=0, entry_time=time.time(),
                                status="phantom-open", exit_reason="",
                                vol_snapshot={
                                    "remaining": remaining,
                                    "filtered": True,
                                    "filter_reasons": [skip_reason],
                                    "skip_reason": skip_reason.split(":")[0] if ":" in skip_reason else skip_reason,
                                },
                            )
                            self._phantoms.append(phantom)
                            self.stats.filtered_out += 1
                            self.stats.last_action = f"SKIPPED {buy_side} ({skip_reason})"
                        else:
                            await self._execute_buy(mkt, tracker, buy_side, buy_token, remaining)

            elif remaining <= BUY_WINDOW_END and not tracker.bought and not tracker.finalized:
                tracker.finalized = True
                self._decided_cids.add(cid)
                self.stats.markets_analyzed += 1

                if tracker.choppy:
                    self.stats.skipped_choppy += 1
                    self.stats.last_action = (
                        f"SKIP CHOPPY (Up={tracker.up_high:.2f} Down={tracker.down_high:.2f})"
                    )
                    log.info("S3-V2 SKIP CHOPPY: %s", mkt.question[:40])
                    self.vol_guard.record_market(True)
                    self._create_skip_phantom(mkt, tracker, "choppy", remaining)
                else:
                    self.stats.skipped_no_leader += 1
                    self.stats.last_action = "SKIP NO LEADER (<1:00 left)"
                    log.info("S3-V2 SKIP NO LEADER: %s", mkt.question[:40])
                    self.vol_guard.record_market(False)
                    self._create_skip_phantom(mkt, tracker, "no_leader", remaining)

        await self._check_positions()
        self._hourly_report()

    def _create_skip_phantom(self, mkt, tracker, skip_reason, remaining):
        leader = "Up" if tracker.up_high >= tracker.down_high else "Down"
        leader_token = mkt.yes_token_id if leader == "Up" else mkt.no_token_id
        leader_price = tracker.up_high if leader == "Up" else tracker.down_high
        if leader_price <= 0:
            return

        phantom = S3Position(
            market=mkt, side=leader, token_id=leader_token,
            entry_price=round(leader_price, 2),
            qty=int(self.trade_size / max(leader_price, 0.01)),
            spent=0, entry_time=time.time(),
            status="phantom-open", exit_reason="",
            vol_snapshot={
                "remaining": remaining,
                "filtered": True,
                "filter_reasons": [f"skipped_{skip_reason}"],
                "skip_reason": skip_reason,
            },
        )
        self._phantoms.append(phantom)

    async def _execute_buy(self, mkt, tracker, buy_side, buy_token, remaining):
        side_str = "YES" if buy_side == "Up" else "NO"

        await self.poly.get_market_prices(mkt)
        ask = mkt.yes_ask if buy_side == "Up" else mkt.no_ask
        if ask <= 0 or ask >= 1.0:
            bid = await self.poly._get_best_bid(buy_token)
            ask = bid if bid else 0
        if ask <= 0:
            return

        self.vol_guard.record_market(False)

        ask_price = round(ask, 2)
        qty = int(self.trade_size / ask_price) if ask_price > 0 else 0
        if qty <= 0:
            return

        pos = S3Position(
            market=mkt, side=buy_side, token_id=buy_token,
            entry_price=ask_price, qty=float(qty),
            spent=round(ask_price * qty, 2),
            entry_time=time.time(),
            vol_snapshot={"remaining": remaining, "filtered": False},
        )
        self._positions.append(pos)
        tracker.bought = True
        tracker.finalized = True
        self._decided_cids.add(mkt.condition_id)
        self.stats.markets_analyzed += 1
        self.stats.trades += 1
        self.stats.last_action = f"BUY {buy_side} @ ${ask_price:.3f} | {mkt.question[:30]}"
        log.info(
            "[S3-V2] BUY %s %.1f @ $%.3f ($%.2f) | %.0fs left | %s",
            buy_side, qty, ask_price, pos.spent, remaining, mkt.question[:45],
        )

    async def _check_positions(self):
        now = time.time()
        for pos in self._positions:
            if pos.status != "open":
                continue

            bid = await self.poly._get_best_bid(pos.token_id)
            if bid is None:
                if pos.market.window_end and now > pos.market.window_end + 10:
                    self._close_position(pos, 0.0, "resolved-unknown")
                continue

            if pos.market.window_end and now > pos.market.window_end - 5:
                if bid > 0.5:
                    self._close_position(pos, 1.0, "resolved-win")
                else:
                    self._close_position(pos, 0.0, "resolved-loss")
                continue

            if bid >= TP_PRICE:
                self._close_position(pos, bid, "tp")
                continue

            if bid <= SL_PRICE:
                self._close_position(pos, bid, "sl")
                continue

        for ph in list(self._phantoms):
            if ph.status != "phantom-open":
                continue

            bid = await self.poly._get_best_bid(ph.token_id)
            resolved = False
            won = False

            if bid is None:
                if ph.market.window_end and now > ph.market.window_end + 10:
                    ph.exit_price = 0.0
                    ph.pnl = -ph.entry_price * ph.qty
                    resolved = True
                    won = False
                else:
                    continue
            elif ph.market.window_end and now > ph.market.window_end - 5:
                ph.exit_price = 1.0 if bid > 0.5 else 0.0
                ph.pnl = (ph.exit_price - ph.entry_price) * ph.qty
                resolved = True
                won = ph.pnl >= 0
            elif bid >= TP_PRICE:
                ph.exit_price = bid
                ph.pnl = (bid - ph.entry_price) * ph.qty
                resolved = True
                won = True
            elif bid <= SL_PRICE:
                ph.exit_price = bid
                ph.pnl = (bid - ph.entry_price) * ph.qty
                resolved = True
                won = False

            if resolved:
                result = "WIN" if won else "LOSE"
                ph.status = f"phantom-{result.lower()}"
                reasons = ph.vol_snapshot.get("filter_reasons", []) if ph.vol_snapshot else []
                skip_type = ph.vol_snapshot.get("skip_reason", "") if ph.vol_snapshot else ""
                label = skip_type.upper() if skip_type else "FILTERED"

                log.info(
                    "  PHANTOM %s WOULD-%s: %s %s $%.2f->$%.2f PnL $%+.2f | %s",
                    label, result, ph.side, ph.market.question[:30],
                    ph.entry_price, ph.exit_price or 0, ph.pnl or 0,
                    " | ".join(reasons),
                )

                if skip_type == "choppy":
                    if won: self.stats.choppy_would_win += 1
                    else: self.stats.choppy_would_lose += 1
                elif skip_type == "no_leader":
                    if won: self.stats.noleader_would_win += 1
                    else: self.stats.noleader_would_lose += 1
                else:
                    if won: self.stats.filtered_would_win += 1
                    else: self.stats.filtered_would_lose += 1

                self._closed.append(ph)
                self._phantoms.remove(ph)
                try:
                    log_research_trade(ph, bot_name="research_v2")
                except Exception as e:
                    log.warning("Failed to log phantom: %s", e)

    def _close_position(self, pos: S3Position, exit_price: float, reason: str):
        pos.exit_price = exit_price
        pos.pnl = (exit_price - pos.entry_price) * pos.qty
        pos.status = reason
        pos.exit_reason = reason
        is_win = pos.pnl >= 0
        self.vol_guard.record_trade(is_win)

        if is_win:
            self.stats.wins += 1
            self._consec_losses = 0
        else:
            self.stats.losses += 1
            self._consec_losses += 1
            if self._consec_losses >= 3:
                self._skip_next = True
                log.info("  STREAK BREAKER ARMED: 3 consecutive losses, will skip next market")

        if "tp" in reason:
            self.stats.tp_hits += 1
        elif "sl" in reason:
            self.stats.sl_hits += 1
        self.stats.total_pnl += pos.pnl
        self._record_hourly_pnl(pos.pnl)
        self._closed.append(pos)

        log.info(
            "[S3-V2] %s %s @ $%.3f -> $%.3f | PnL $%+.2f | %s",
            reason.upper(), pos.side, pos.entry_price, exit_price,
            pos.pnl, pos.market.question[:40],
        )

        self.stats.last_action = f"{reason.upper()} {pos.side} ${pos.pnl:+.2f}"
        self._persist_trade(pos.pnl, is_win)

        try:
            log_research_trade(pos, bot_name="research_v2")
        except Exception as e:
            log.warning("Failed to log trade: %s", e)

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

    def _record_hourly_pnl(self, pnl: float):
        hour_key = datetime.now(timezone.utc).strftime("%H:00")
        self.stats.hourly_pnl[hour_key] = self.stats.hourly_pnl.get(hour_key, 0) + pnl

    def _hourly_report(self):
        now = datetime.now(timezone.utc)
        hour_key = now.strftime("%H:00")
        today = now.strftime("%Y-%m-%d")

        if self._last_day != today:
            if self._last_day:
                log.info("=== S3-V2 NEW DAY — resetting hourly P&L ===")
                try:
                    log_daily_snapshot("research_v2", {
                        "trades": self.stats.trades, "wins": self.stats.wins,
                        "losses": self.stats.losses, "pnl": round(self.stats.total_pnl, 2),
                        "tp_hits": self.stats.tp_hits, "sl_hits": self.stats.sl_hits,
                        "filtered_out": self.stats.filtered_out,
                        "filtered_would_win": self.stats.filtered_would_win,
                        "filtered_would_lose": self.stats.filtered_would_lose,
                        "choppy_would_win": self.stats.choppy_would_win,
                        "choppy_would_lose": self.stats.choppy_would_lose,
                        "noleader_would_win": self.stats.noleader_would_win,
                        "noleader_would_lose": self.stats.noleader_would_lose,
                        "skipped_hour": self.stats.skipped_hour,
                        "hourly_pnl": str(self.stats.hourly_pnl),
                    })
                except Exception as e:
                    log.warning("Failed to log daily snapshot: %s", e)
            self.stats.hourly_pnl = {}
            self._last_day = today

        if hour_key != self._last_hour_key and self._last_hour_key:
            prev_pnl = self.stats.hourly_pnl.get(self._last_hour_key, 0)
            log.info(
                "=== S3-V2 HOURLY [%s] === PnL: $%+.2f | Total: $%+.2f | W:%d L:%d | "
                "Skipped(hour:%d vg:%d streak:%d)",
                self._last_hour_key, prev_pnl, self.stats.total_pnl,
                self.stats.wins, self.stats.losses,
                self.stats.skipped_hour, self.stats.filtered_out - self.stats.skipped_hour,
                self._consec_losses,
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
