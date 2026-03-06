"""
Strategy 3 Volume Research — identical to S3 but adds order-book depth
logging at buy decision time. Does NOT trade differently (dry run only) —
it logs what the book looks like so we can study whether volume predicts
whether a 70c+ move follows through to TP or reverses to SL.

Extra data logged per market:
  - bid_depth_total: total $ sitting on the bid side of the leading token
  - ask_depth_total: total $ sitting on the ask side
  - bid_depth_70plus: $ on bids at 70c or above (strong conviction)
  - spread: best_ask - best_bid
  - num_bid_levels / num_ask_levels
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

log = logging.getLogger("strategy3_vol")

BUY_THRESHOLD = 0.70
BUY_MAX_PRICE = 0.90
SKIP_THRESHOLD = 0.60
ANALYSIS_START = 240.0
BUY_WINDOW_START = 180.0
BUY_WINDOW_END = 60.0
TP_PRICE = 0.94
SL_PRICE = 0.28
USDC_PER_TRADE = 20.0

# Volume filters — skip trades that don't pass these
MIN_BID_70PLUS = 200.0       # must have >= $200 on bids at 70c+
MIN_DEPTH_RATIO = 4.0        # leader depth must be >= 4x the other side
MIN_BTC_MOVE = 40.0          # BTC must have moved >= $40 since window start


@dataclass
class S3Stats:
    markets_analyzed: int = 0
    trades: int = 0
    skipped_choppy: int = 0
    skipped_no_leader: int = 0
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
    # Speed tracking
    analysis_start_time: float = 0.0
    up_first_60: float = 0.0
    up_first_70: float = 0.0
    down_first_60: float = 0.0
    down_first_70: float = 0.0
    # BTC price at window start
    btc_start: float = 0.0
    # Spread tracking
    spreads: list = field(default_factory=list)


class Strategy3Vol:
    """S3 with volume/depth research logging — always dry run."""

    def __init__(self, poly: PolymarketClient, trade_hours=None,
                 pnl_store=None, email_on_loss=False):
        self.poly = poly
        self.stats = S3Stats()
        self._positions: List[S3Position] = []
        self._closed: List[S3Position] = []
        self._phantoms: List[S3Position] = []  # filtered trades we track for research
        self.pnl_store = pnl_store
        self._email_on_loss = False
        self.trade_size = USDC_PER_TRADE
        self._trackers: Dict[str, S3WindowTracker] = {}
        self._decided_cids: Set[str] = set()
        self._running = False
        self._last_hour_key = ""
        self._last_day = ""
        self._trade_hours = trade_hours
        self._prev_side = ""
        self._prev_outcome = ""

    def _is_trading_time(self) -> bool:
        return True

    async def run(self):
        self._running = True
        log.info(
            "S3-VOL RESEARCH started | buy>=%.0fc<=%.0fc | choppy>=%.0fc | TP>=%.0fc | SL<=%.0fc",
            BUY_THRESHOLD * 100, BUY_MAX_PRICE * 100, SKIP_THRESHOLD * 100,
            TP_PRICE * 100, SL_PRICE * 100,
        )
        while self._running:
            try:
                await self._tick()
            except Exception as exc:
                log.error("S3-VOL tick error: %s", exc, exc_info=True)
            await asyncio.sleep(1)

    def stop(self):
        self._running = False

    async def _get_btc_price(self) -> float:
        """Get current BTC price from Binance."""
        try:
            import aiohttp
            url = "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"
            async with self.poly._session.get(url, timeout=aiohttp.ClientTimeout(total=3)) as resp:
                data = await resp.json()
                return float(data.get("price", 0))
        except Exception:
            return 0.0

    async def _get_book_depth(self, token_id: str) -> dict:
        """Fetch full order book and compute depth metrics."""
        import aiohttp
        result = {
            "bid_depth_total": 0.0,
            "ask_depth_total": 0.0,
            "bid_depth_70plus": 0.0,
            "best_bid": 0.0,
            "best_ask": 0.0,
            "spread": 0.0,
            "num_bid_levels": 0,
            "num_ask_levels": 0,
        }
        try:
            url = f"{cfg.poly_clob_host}/book"
            params = {"token_id": token_id}
            async with self.poly._session.get(
                url, params=params, timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                book = await resp.json()

            bids = book.get("bids", [])
            asks = book.get("asks", [])

            result["num_bid_levels"] = len(bids)
            result["num_ask_levels"] = len(asks)

            for b in bids:
                price = float(b.get("price", 0))
                size = float(b.get("size", 0))
                dollar_val = price * size
                result["bid_depth_total"] += dollar_val
                if price >= 0.70:
                    result["bid_depth_70plus"] += dollar_val

            for a in asks:
                price = float(a.get("price", 0))
                size = float(a.get("size", 0))
                result["ask_depth_total"] += price * size

            if bids:
                result["best_bid"] = max(float(b.get("price", 0)) for b in bids)
            if asks:
                result["best_ask"] = min(float(a.get("price", 0)) for a in asks)
            if result["best_bid"] > 0 and result["best_ask"] > 0:
                result["spread"] = round(result["best_ask"] - result["best_bid"], 4)

            for k in ("bid_depth_total", "ask_depth_total", "bid_depth_70plus"):
                result[k] = round(result[k], 2)

        except Exception as exc:
            log.warning("Book depth fetch failed for %s: %s", token_id[:8], exc)
        return result

    async def _tick(self):
        now = time.time()

        if not hasattr(self, "_last_disc") or now - self._last_disc > 30:
            await self._discover()
            self._last_disc = now

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
                    tracker.analysis_start_time = now
                    # Capture BTC price at the start of analysis for the BTC move filter
                    _btc = await self._get_btc_price()
                    if _btc > 0:
                        tracker.btc_start = _btc
                    log.info("S3-VOL: Analyzing %s (%.0fs left) btc=$%.0f", mkt.question[:40], remaining, tracker.btc_start)

                up_bid = await self.poly._get_best_bid(mkt.yes_token_id)
                down_bid = await self.poly._get_best_bid(mkt.no_token_id)

                if up_bid and up_bid > tracker.up_high:
                    tracker.up_high = up_bid
                if down_bid and down_bid > tracker.down_high:
                    tracker.down_high = down_bid

                # Speed tracking
                if up_bid and up_bid >= 0.60 and tracker.up_first_60 == 0:
                    tracker.up_first_60 = now
                if up_bid and up_bid >= 0.70 and tracker.up_first_70 == 0:
                    tracker.up_first_70 = now
                if down_bid and down_bid >= 0.60 and tracker.down_first_60 == 0:
                    tracker.down_first_60 = now
                if down_bid and down_bid >= 0.70 and tracker.down_first_70 == 0:
                    tracker.down_first_70 = now

                if (tracker.up_high >= SKIP_THRESHOLD and
                        tracker.down_high >= SKIP_THRESHOLD and
                        not tracker.choppy):
                    tracker.choppy = True
                    log.info(
                        "S3-VOL CHOPPY: %s (Up=%.2f Down=%.2f)",
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
                    up_spd = (tracker.up_first_70 - tracker.analysis_start_time) if tracker.up_first_70 and tracker.analysis_start_time else 0
                    dn_spd = (tracker.down_first_70 - tracker.analysis_start_time) if tracker.down_first_70 and tracker.analysis_start_time else 0
                    log.info(
                        "S3-VOL SKIP CHOPPY: %s | Up_high=%.2f No_high=%.2f | "
                        "up_speed_70=%.0fs no_speed_70=%.0fs",
                        mkt.question[:40], tracker.up_high, tracker.down_high, up_spd, dn_spd,
                    )
                    await self._track_skipped_market(mkt, tracker, "choppy", remaining)
                else:
                    self.stats.skipped_no_leader += 1
                    self.stats.last_action = "SKIP NO LEADER (<1:00 left)"
                    log.info(
                        "S3-VOL SKIP NO LEADER: %s | Up_high=%.2f No_high=%.2f",
                        mkt.question[:40], tracker.up_high, tracker.down_high,
                    )
                    await self._track_skipped_market(mkt, tracker, "no_leader", remaining)

        await self._check_positions()
        self._hourly_report()

    async def _track_skipped_market(self, mkt, tracker, skip_reason, remaining):
        """Create phantom positions for skipped markets to analyze what would have happened."""
        try:
            up_vol = await self._get_book_depth(mkt.yes_token_id)
            dn_vol = await self._get_book_depth(mkt.no_token_id)
            btc_price = await self._get_btc_price()
            btc_move = abs(btc_price - tracker.btc_start) if btc_price > 0 and tracker.btc_start > 0 else 0

            up_spd70 = (tracker.up_first_70 - tracker.analysis_start_time) if tracker.up_first_70 and tracker.analysis_start_time else 0
            dn_spd70 = (tracker.down_first_70 - tracker.analysis_start_time) if tracker.down_first_70 and tracker.analysis_start_time else 0

            # Determine which side was leading at skip time
            leader = "Up" if tracker.up_high >= tracker.down_high else "Down"
            leader_token = mkt.yes_token_id if leader == "Up" else mkt.no_token_id
            leader_price = mkt.yes_ask if leader == "Up" else mkt.no_ask
            if leader_price <= 0 or leader_price >= 1.0:
                await self.poly.get_market_prices(mkt)
                leader_price = mkt.yes_ask if leader == "Up" else mkt.no_ask

            log.info(
                "  TRACKING SKIPPED (%s): %s | leader=%s up_high=%.2f dn_high=%.2f | "
                "up_depth=$%.0f dn_depth=$%.0f | btc=$%.0f move=$%.0f",
                skip_reason, mkt.question[:35], leader, tracker.up_high, tracker.down_high,
                up_vol["bid_depth_total"], dn_vol["bid_depth_total"], btc_price, btc_move,
            )

            phantom = S3Position(
                market=mkt, side=leader, token_id=leader_token,
                entry_price=round(leader_price, 2) if leader_price > 0 else tracker.up_high if leader == "Up" else tracker.down_high,
                qty=int(self.trade_size / max(leader_price, 0.01)),
                spent=0,
                entry_time=time.time(),
                status="phantom-open",
                exit_reason="",
                vol_snapshot={
                    "up_depth": up_vol, "down_depth": dn_vol,
                    "up_high": tracker.up_high, "down_high": tracker.down_high,
                    "up_speed_70": up_spd70, "down_speed_70": dn_spd70,
                    "btc_price": btc_price, "btc_move": btc_move,
                    "remaining": remaining,
                    "skip_reason": skip_reason,
                    "avg_spread": sum(tracker.spreads) / len(tracker.spreads) if tracker.spreads else 0,
                    "prev_side": self._prev_side, "prev_outcome": self._prev_outcome,
                    "filtered": True,
                    "filter_reasons": [f"skipped_{skip_reason}"],
                },
            )
            self._phantoms.append(phantom)
        except Exception as e:
            log.warning("Failed to track skipped market: %s", e)

    async def _execute_buy(self, mkt, tracker, buy_side, buy_token, remaining):
        """Simulated buy with full volume snapshot."""
        side_str = "YES" if buy_side == "Up" else "NO"

        await self.poly.get_market_prices(mkt)
        ask = mkt.yes_ask if buy_side == "Up" else mkt.no_ask
        if ask <= 0 or ask >= 1.0:
            bid = await self.poly._get_best_bid(buy_token)
            ask = bid if bid else 0
        if ask <= 0:
            return

        vol = await self._get_book_depth(buy_token)

        other_token = mkt.no_token_id if buy_side == "Up" else mkt.yes_token_id
        other_vol = await self._get_book_depth(other_token)

        btc_price = await self._get_btc_price()

        # Speed to 70c
        if buy_side == "Up" and tracker.up_first_70 and tracker.analysis_start_time:
            speed_60 = tracker.up_first_60 - tracker.analysis_start_time if tracker.up_first_60 else 0
            speed_70 = tracker.up_first_70 - tracker.analysis_start_time
        elif buy_side == "Down" and tracker.down_first_70 and tracker.analysis_start_time:
            speed_60 = tracker.down_first_60 - tracker.analysis_start_time if tracker.down_first_60 else 0
            speed_70 = tracker.down_first_70 - tracker.analysis_start_time
        else:
            speed_60 = 0
            speed_70 = 0

        depth_ratio = round(vol["bid_depth_total"] / other_vol["bid_depth_total"], 2) if other_vol["bid_depth_total"] > 0 else 0

        log.info(
            "═══ S3-VOL BUY SIGNAL ═══ %s %s @ $%.3f | %.0fs left | %s",
            buy_side, side_str, ask, remaining, mkt.question[:45],
        )
        log.info(
            "  LEADER BOOK: bid_total=$%.2f ask_total=$%.2f bid_70+=$%.2f | "
            "spread=%.4f | levels=%d/%d | best_bid=$%.2f best_ask=$%.2f",
            vol["bid_depth_total"], vol["ask_depth_total"], vol["bid_depth_70plus"],
            vol["spread"], vol["num_bid_levels"], vol["num_ask_levels"],
            vol["best_bid"], vol["best_ask"],
        )
        log.info(
            "  OTHER  BOOK: bid_total=$%.2f ask_total=$%.2f | "
            "spread=%.4f | levels=%d/%d | best_bid=$%.2f best_ask=$%.2f",
            other_vol["bid_depth_total"], other_vol["ask_depth_total"],
            other_vol["spread"], other_vol["num_bid_levels"], other_vol["num_ask_levels"],
            other_vol["best_bid"], other_vol["best_ask"],
        )
        btc_move = abs(btc_price - tracker.btc_start) if btc_price > 0 and tracker.btc_start > 0 else 0

        log.info(
            "  ANALYTICS: speed_to_60=%.0fs speed_to_70=%.0fs | depth_ratio=%.1fx | "
            "btc=$%.0f (move=$%.0f) | prev_window=%s(%s)",
            speed_60, speed_70, depth_ratio, btc_price, btc_move,
            self._prev_side or "none", self._prev_outcome or "none",
        )

        # ── Volume filters ──
        skip_reasons = []
        if vol["bid_depth_70plus"] < MIN_BID_70PLUS:
            skip_reasons.append(f"bid_70+=${vol['bid_depth_70plus']:.0f}<${MIN_BID_70PLUS:.0f}")
        if depth_ratio < MIN_DEPTH_RATIO:
            skip_reasons.append(f"depth_ratio={depth_ratio:.1f}x<{MIN_DEPTH_RATIO:.0f}x")
        if btc_move < MIN_BTC_MOVE:
            skip_reasons.append(f"btc_move=${btc_move:.0f}<${MIN_BTC_MOVE:.0f}")

        if skip_reasons:
            reason_str = " | ".join(skip_reasons)
            log.info(
                "  ✗ FILTERED OUT: %s | Would have bought %s @ $%.3f",
                reason_str, buy_side, ask,
            )
            self.stats.last_action = f"FILTERED {buy_side} @ ${ask:.2f} ({reason_str})"

            # Still track what would have happened (for research)
            tracker.bought = True
            tracker.finalized = True
            self._decided_cids.add(mkt.condition_id)
            self.stats.markets_analyzed += 1

            # Track what would have happened (phantom position)
            phantom = S3Position(
                market=mkt, side=buy_side, token_id=buy_token,
                entry_price=round(ask, 2), qty=int(self.trade_size / round(ask, 2)) if round(ask, 2) > 0 else 0,
                spent=0,
                entry_time=time.time(),
                status="phantom-open",
                exit_reason="",
                vol_snapshot={
                    "leader": vol, "other": other_vol, "remaining": remaining,
                    "speed_to_60": speed_60, "speed_to_70": speed_70,
                    "depth_ratio": depth_ratio, "btc_price": btc_price,
                    "btc_move": btc_move,
                    "prev_side": self._prev_side, "prev_outcome": self._prev_outcome,
                    "filtered": True, "filter_reasons": skip_reasons,
                },
            )
            self._phantoms.append(phantom)
            self.stats.filtered_out += 1
            return

        log.info("  ✓ PASSED ALL FILTERS — executing buy")

        ask_price = round(ask, 2)
        qty = int(self.trade_size / ask_price) if ask_price > 0 else 0
        if qty <= 0:
            return

        pos = S3Position(
            market=mkt, side=buy_side, token_id=buy_token,
            entry_price=ask_price, qty=float(qty),
            spent=round(ask_price * qty, 2),
            entry_time=time.time(),
            vol_snapshot={
                "leader": vol, "other": other_vol, "remaining": remaining,
                "speed_to_60": speed_60, "speed_to_70": speed_70,
                "depth_ratio": depth_ratio, "btc_price": btc_price,
                "btc_move": btc_move,
                "prev_side": self._prev_side, "prev_outcome": self._prev_outcome,
                "filtered": False,
            },
        )
        self._positions.append(pos)
        tracker.bought = True
        tracker.finalized = True
        self._decided_cids.add(mkt.condition_id)
        self.stats.markets_analyzed += 1
        self.stats.trades += 1
        self.stats.last_action = f"BUY {buy_side} @ ${ask_price:.3f} | depth=${vol['bid_depth_total']:.0f} | {mkt.question[:30]}"
        log.info(
            "[S3-VOL] BUY %s %.1f @ $%.3f ($%.2f) | depth=$%.0f | %.0fs left",
            buy_side, qty, ask_price, pos.spent, vol["bid_depth_total"], remaining,
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

        # Check phantom (filtered/skipped) positions — track what WOULD have happened
        for ph in list(self._phantoms):
            if ph.status != "phantom-open":
                continue

            reasons = ph.vol_snapshot.get("filter_reasons", []) if ph.vol_snapshot else []
            skip_type = ph.vol_snapshot.get("skip_reason", "") if ph.vol_snapshot else ""
            label = "CHOPPY" if skip_type == "choppy" else "NO-LEADER" if skip_type == "no_leader" else "FILTERED"

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
                log.info(
                    "  ✗ %s WOULD-%s: %s %s $%.2f→$%.2f PnL $%+.2f | %s",
                    label, result, ph.side, ph.market.question[:30],
                    ph.entry_price, ph.exit_price or 0, ph.pnl or 0,
                    " | ".join(reasons),
                )
                # Categorize stats
                if skip_type == "choppy":
                    if won:
                        self.stats.choppy_would_win += 1
                    else:
                        self.stats.choppy_would_lose += 1
                elif skip_type == "no_leader":
                    if won:
                        self.stats.noleader_would_win += 1
                    else:
                        self.stats.noleader_would_lose += 1
                else:
                    if won:
                        self.stats.filtered_would_win += 1
                    else:
                        self.stats.filtered_would_lose += 1
                self._closed.append(ph)
                self._phantoms.remove(ph)

    def _close_position(self, pos: S3Position, exit_price: float, reason: str):
        pos.exit_price = exit_price
        pos.pnl = (exit_price - pos.entry_price) * pos.qty
        pos.status = reason
        pos.exit_reason = reason
        is_win = pos.pnl >= 0
        if is_win:
            self.stats.wins += 1
        else:
            self.stats.losses += 1
        if "tp" in reason:
            self.stats.tp_hits += 1
        elif "sl" in reason:
            self.stats.sl_hits += 1
        self.stats.total_pnl += pos.pnl
        self._record_hourly_pnl(pos.pnl)
        self._closed.append(pos)

        vol_info = ""
        analytics_info = ""
        if pos.vol_snapshot:
            v = pos.vol_snapshot["leader"]
            vol_info = f" | entry_depth=${v['bid_depth_total']:.0f} bid70+=${v['bid_depth_70plus']:.0f} spread={v['spread']:.4f}"
            analytics_info = (
                f" | speed_70={pos.vol_snapshot.get('speed_to_70', 0):.0f}s"
                f" depth_ratio={pos.vol_snapshot.get('depth_ratio', 0):.1f}x"
                f" btc=${pos.vol_snapshot.get('btc_price', 0):.0f}"
                f" prev={pos.vol_snapshot.get('prev_side', '')}({pos.vol_snapshot.get('prev_outcome', '')})"
            )

        log.info(
            "═══ S3-VOL %s %s: $%.3f → $%.3f | PnL $%+.2f%s%s | %s",
            reason.upper(), pos.side, pos.entry_price, exit_price,
            pos.pnl, vol_info, analytics_info, pos.market.question[:40],
        )

        self._prev_side = pos.side
        self._prev_outcome = reason

        self.stats.last_action = f"{reason.upper()} {pos.side} ${pos.pnl:+.2f} depth=${pos.vol_snapshot['leader']['bid_depth_total']:.0f}" if pos.vol_snapshot else f"{reason.upper()} {pos.side} ${pos.pnl:+.2f}"
        self._persist_trade(pos.pnl, is_win)

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
                log.info("═══ S3-VOL NEW DAY — resetting hourly P&L ═══")
            self.stats.hourly_pnl = {}
            self._last_day = today

        if hour_key != self._last_hour_key and self._last_hour_key:
            prev_pnl = self.stats.hourly_pnl.get(self._last_hour_key, 0)
            log.info(
                "═══ S3-VOL HOURLY [%s] ═══  PnL: $%+.2f | Total: $%+.2f | W:%d L:%d | "
                "Filtered:%d (W:%d L:%d) | Choppy(W:%d L:%d) | NoLead(W:%d L:%d)",
                self._last_hour_key, prev_pnl, self.stats.total_pnl,
                self.stats.wins, self.stats.losses,
                self.stats.filtered_out, self.stats.filtered_would_win, self.stats.filtered_would_lose,
                self.stats.choppy_would_win, self.stats.choppy_would_lose,
                self.stats.noleader_would_win, self.stats.noleader_would_lose,
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
