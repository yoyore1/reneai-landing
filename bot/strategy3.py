"""
Strategy 3: Late Momentum — Buy the Leader

Rules:
  - Analyze each 5-min window from 4:00 to 1:00 remaining
  - Track the highest bid Up and Down reach during that analysis window
  - If BOTH sides hit 60c+ during analysis → SKIP (choppy)
  - Buy window: 3:00 to 1:00 remaining
    - As soon as one side's bid is 70c+ → BUY that side
    - Only one buy per window
  - If buy window ends (≤1:00 left) without buying → SKIP NO LEADER
  - Position management:
    - TP: sell when bid ≥ 94c
    - SL: sell when bid ≤ 28c
    - If neither hits before resolution → resolve at $1 or $0
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

log = logging.getLogger("strategy3")

BUY_THRESHOLD = 0.70
SKIP_THRESHOLD = 0.60
ANALYSIS_START = 240.0      # start tracking highs at 4:00 remaining
BUY_WINDOW_START = 180.0    # can buy from 3:00 remaining
BUY_WINDOW_END = 60.0       # stop buying at 1:00 remaining
TP_PRICE = 0.94
SL_PRICE = 0.28
USDC_PER_TRADE = 20.0


@dataclass
class S3Stats:
    markets_analyzed: int = 0
    trades: int = 0
    skipped_choppy: int = 0
    skipped_no_leader: int = 0
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
    side: str          # "Up" or "Down"
    token_id: str
    entry_price: float
    qty: float
    spent: float
    entry_time: float
    exit_price: Optional[float] = None
    pnl: Optional[float] = None
    status: str = "open"
    exit_reason: str = ""


@dataclass
class S3WindowTracker:
    market: Market
    up_high: float = 0.0
    down_high: float = 0.0
    analyzing: bool = False
    bought: bool = False
    choppy: bool = False
    finalized: bool = False


class Strategy3:

    def __init__(self, poly: PolymarketClient, trade_hours=None,
                 pnl_store=None, email_on_loss=False):
        """
        trade_hours: optional (start_hour, start_min, end_hour, end_min) in EST.
        pnl_store: PnLStore instance for persisting PnL data.
        email_on_loss: if True, sends email alerts on losing trades.
        """
        self.poly = poly
        self.stats = S3Stats()
        self._positions: List[S3Position] = []
        self._closed: List[S3Position] = []
        self.pnl_store = pnl_store
        self._email_on_loss = email_on_loss
        self.trade_size = USDC_PER_TRADE
        self._trackers: Dict[str, S3WindowTracker] = {}
        self._decided_cids: Set[str] = set()
        self._running = False
        self._last_hour_key = ""
        self._last_day = ""
        self._trade_hours = trade_hours
        self._cooldown_until: float = 0  # skip next window after a loss

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
            "S3 started | buy>=%.0fc | choppy>=%.0fc | TP>=%.0fc | SL<=%.0fc | "
            "analyze 4:00→1:00 | buy 3:00→1:00",
            BUY_THRESHOLD * 100, SKIP_THRESHOLD * 100,
            TP_PRICE * 100, SL_PRICE * 100,
        )
        if self._trade_hours:
            sh, sm, eh, em = self._trade_hours
            log.info("S3 trading hours: %02d:%02d → %02d:%02d EST", sh, sm, eh, em)
        while self._running:
            try:
                await self._tick()
            except Exception as exc:
                log.error("S3 tick error: %s", exc, exc_info=True)
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

            # Analysis window: 4:00 to 1:00 remaining — track highs
            if remaining <= ANALYSIS_START and remaining > BUY_WINDOW_END:
                if not tracker.analyzing:
                    tracker.analyzing = True
                    log.info("S3: Analyzing %s (%.0fs left)", mkt.question[:40], remaining)

                up_bid = await self.poly._get_best_bid(mkt.yes_token_id)
                down_bid = await self.poly._get_best_bid(mkt.no_token_id)

                if up_bid and up_bid > tracker.up_high:
                    tracker.up_high = up_bid
                if down_bid and down_bid > tracker.down_high:
                    tracker.down_high = down_bid

                # Check choppy
                if (tracker.up_high >= SKIP_THRESHOLD and
                        tracker.down_high >= SKIP_THRESHOLD and
                        not tracker.choppy):
                    tracker.choppy = True
                    log.info(
                        "S3 CHOPPY: %s (Up high=%.2f Down high=%.2f)",
                        mkt.question[:35], tracker.up_high, tracker.down_high,
                    )

                # Buy window: 3:00 to 1:00 remaining
                in_cooldown = now < self._cooldown_until
                if (remaining <= BUY_WINDOW_START and
                        not tracker.bought and
                        not tracker.choppy and
                        not in_cooldown and
                        trading_ok):
                    up_now = up_bid or 0
                    down_now = down_bid or 0

                    buy_side = None
                    buy_token = ""

                    if up_now >= BUY_THRESHOLD and up_now >= down_now:
                        buy_side = "Up"
                        buy_token = mkt.yes_token_id
                    elif down_now >= BUY_THRESHOLD and down_now >= up_now:
                        buy_side = "Down"
                        buy_token = mkt.no_token_id

                    if buy_side:
                        await self._execute_buy(mkt, tracker, buy_side, buy_token, remaining)

            # End of buy window — finalize if we didn't buy
            elif remaining <= BUY_WINDOW_END and not tracker.bought and not tracker.finalized:
                tracker.finalized = True
                self._decided_cids.add(cid)
                self.stats.markets_analyzed += 1

                if now < self._cooldown_until:
                    self.stats.last_action = "SKIP COOLDOWN (loss recovery)"
                    log.info("S3 SKIP COOLDOWN: %s", mkt.question[:40])
                elif tracker.choppy:
                    self.stats.skipped_choppy += 1
                    self.stats.last_action = (
                        f"SKIP CHOPPY (Up={tracker.up_high:.2f} Down={tracker.down_high:.2f})"
                    )
                    log.info("S3 SKIP CHOPPY: %s", mkt.question[:40])
                else:
                    self.stats.skipped_no_leader += 1
                    self.stats.last_action = "SKIP NO LEADER (<1:00 left)"
                    log.info("S3 SKIP NO LEADER: %s", mkt.question[:40])

        await self._check_positions()
        self._hourly_report()

    async def _execute_buy(self, mkt, tracker, buy_side, buy_token, remaining):
        """Place a buy order."""
        side_str = "YES" if buy_side == "Up" else "NO"

        await self.poly.get_market_prices(mkt)
        ask = mkt.yes_ask if buy_side == "Up" else mkt.no_ask
        if ask <= 0 or ask >= 1.0:
            bid = await self.poly._get_best_bid(buy_token)
            ask = bid if bid else 0
        if ask <= 0:
            return

        result = await self.poly.buy(mkt, side_str, self.trade_size)
        entry = result.avg_entry if result.avg_entry > 0 else ask
        qty = result.qty if result.qty > 0 else math.floor((self.trade_size / ask) * 100) / 100

        pos = S3Position(
            market=mkt, side=buy_side, token_id=buy_token,
            entry_price=entry, qty=qty,
            spent=round(entry * qty, 2),
            entry_time=time.time(),
        )
        self._positions.append(pos)
        tracker.bought = True
        tracker.finalized = True
        self._decided_cids.add(mkt.condition_id)
        self.stats.markets_analyzed += 1
        self.stats.trades += 1
        self.stats.last_action = f"BUY {buy_side} @ ${entry:.3f} | {mkt.question[:30]}"
        log.info(
            "[S3] BUY %s %.1f @ $%.3f ($%.2f) | %.0fs left | %s",
            buy_side, qty, entry, pos.spent, remaining, mkt.question[:45],
        )

    async def _check_positions(self):
        now = time.time()
        for pos in self._positions:
            if pos.status != "open":
                continue

            bid = await self.poly._get_best_bid(pos.token_id)
            if bid is None:
                # If window ended and we can't get a bid, resolve
                if pos.market.window_end and now > pos.market.window_end + 10:
                    self._close_position(pos, 0.0, "resolved-unknown")
                continue

            # TP: bid >= 94c
            if bid >= TP_PRICE:
                await self._sell_position(pos, bid, "tp")
                continue

            # SL: bid <= 28c
            if bid <= SL_PRICE:
                await self._sell_position(pos, bid, "sl")
                continue

            # Resolution: window ended
            if pos.market.window_end and now > pos.market.window_end:
                if bid > 0.5:
                    self._close_position(pos, 1.0, "resolved-win")
                else:
                    self._close_position(pos, 0.0, "resolved-loss")

    async def _sell_position(self, pos: S3Position, bid: float, reason: str):
        """Actively sell at current bid (TP or SL)."""
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
            success = await self.poly.sell(temp)
            if not success:
                log.warning("S3 sell failed for %s %s, will retry", reason.upper(), pos.side)
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
        self.stats.total_pnl += pos.pnl
        self._record_hourly_pnl(pos.pnl)
        self.stats.last_action = f"{reason.upper()} {pos.side} @ ${pos.exit_price:.3f} PnL ${pos.pnl:+.2f}"
        self._closed.append(pos)
        log.info(
            "[S3] %s %s @ $%.3f → $%.3f | PnL $%+.2f | %s",
            reason.upper(), pos.side, pos.entry_price, pos.exit_price,
            pos.pnl, pos.market.question[:40],
        )
        self._persist_trade(pos.pnl, is_win)
        if not is_win:
            self._cooldown_until = time.time() + 300
            log.info("S3 COOLDOWN: skipping next 5-min window after loss")
            if self._email_on_loss:
                self._send_loss_email(pos, reason)

    def _close_position(self, pos: S3Position, exit_price: float, reason: str):
        """Close a position at resolution (no active sell needed)."""
        pos.exit_price = exit_price
        pos.pnl = (exit_price - pos.entry_price) * pos.qty
        pos.status = reason
        pos.exit_reason = reason
        if pos.pnl >= 0:
            self.stats.wins += 1
        else:
            self.stats.losses += 1
        self.stats.total_pnl += pos.pnl
        self._record_hourly_pnl(pos.pnl)
        self.stats.last_action = f"RESOLVED {pos.side} ${pos.pnl:+.2f}"
        self._closed.append(pos)
        log.info(
            "[S3] RESOLVED %s: $%.2f → PnL $%+.2f | %s",
            pos.side, exit_price, pos.pnl, pos.market.question[:45],
        )
        is_win = pos.pnl >= 0
        self._persist_trade(pos.pnl, is_win)
        if not is_win:
            self._cooldown_until = time.time() + 300
            log.info("S3 COOLDOWN: skipping next 5-min window after loss")
            if self._email_on_loss:
                self._send_loss_email(pos, reason)

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
                log.info("═══ S3 NEW DAY — resetting hourly P&L ═══")
            self.stats.hourly_pnl = {}
            self._last_day = today

        if hour_key != self._last_hour_key and self._last_hour_key:
            prev_pnl = self.stats.hourly_pnl.get(self._last_hour_key, 0)
            log.info(
                "═══ S3 HOURLY [%s] ═══  PnL: $%+.2f | Total: $%+.2f | W:%d L:%d TP:%d SL:%d",
                self._last_hour_key, prev_pnl, self.stats.total_pnl,
                self.stats.wins, self.stats.losses,
                self.stats.tp_hits, self.stats.sl_hits,
            )

        if hour_key not in self.stats.hourly_pnl:
            self.stats.hourly_pnl[hour_key] = 0.0
        self._last_hour_key = hour_key

    def _persist_trade(self, pnl: float, is_win: bool):
        if self.pnl_store:
            self.pnl_store.record_trade(pnl, is_win)

    def _send_loss_email(self, pos: S3Position, reason: str):
        import threading
        threading.Thread(target=self._email_worker, args=(pos, reason), daemon=True).start()

    def _email_worker(self, pos: S3Position, reason: str):
        try:
            import smtplib
            from email.mime.text import MIMEText
            subject = f"S3 LOSS: {reason.upper()} {pos.side} ${pos.pnl:+.2f}"
            body = (
                f"Trade Loss Alert\n"
                f"{'='*40}\n"
                f"Side:    {pos.side}\n"
                f"Entry:   ${pos.entry_price:.3f}\n"
                f"Exit:    ${pos.exit_price:.3f}\n"
                f"Qty:     {pos.qty:.1f}\n"
                f"PnL:     ${pos.pnl:+.2f}\n"
                f"Reason:  {reason.upper()}\n"
                f"Market:  {pos.market.question}\n"
                f"{'='*40}\n"
                f"Total PnL: ${self.stats.total_pnl:+.2f}\n"
                f"W/L: {self.stats.wins}/{self.stats.losses}\n"
            )
            msg = MIMEText(body)
            msg["Subject"] = subject
            msg["From"] = cfg.email_from
            msg["To"] = cfg.email_to
            with smtplib.SMTP(cfg.smtp_host, cfg.smtp_port) as s:
                s.starttls()
                s.login(cfg.email_user, cfg.email_password)
                s.sendmail(cfg.email_from, cfg.email_to, msg.as_string())
            log.info("Loss email sent: %s", subject)
        except Exception as exc:
            log.warning("Failed to send loss email: %s", exc)

    @property
    def open_positions(self) -> List[S3Position]:
        return [p for p in self._positions if p.status == "open"]

    @property
    def closed_positions(self) -> List[S3Position]:
        return self._closed
