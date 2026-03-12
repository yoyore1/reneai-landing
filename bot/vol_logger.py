"""
Volume/Liquidity + Analytics Logger

Tracks per 5-min window:
  1. Order book depth (bid/ask totals, 70c+ depth, spread, levels)
  2. Speed to 60c/70c — timestamps when each side first crosses thresholds
  3. BTC actual price from Binance (to compare against Polymarket odds)
  4. Consecutive window correlation — did previous window go same direction?
  5. Choppy outcome tracking — what happened in markets we'd skip?
  6. Spread behavior over time within a window
  7. Samples every 10s during analysis window for granular data

Writes two CSVs:
  - volume_log.csv:   per-sample book snapshots (every 10s)
  - trades_log.csv:   per-window outcome summary with all analytics

Usage: python3 -m bot.vol_logger
"""

import asyncio
import csv
import json as _json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, Dict

import aiohttp

from bot.config import cfg

log = logging.getLogger("vol_logger")

GAMMA_API = "https://gamma-api.polymarket.com"
BINANCE_API = "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT"

SAMPLE_INTERVAL = 10  # sample every 10 seconds for granular data
BOOK_CSV = "volume_log.csv"
TRADES_CSV = "trades_log.csv"


@dataclass
class WindowTracker:
    """Track analytics for one 5-minute window."""
    condition_id: str
    question: str
    yes_token: str
    no_token: str
    window_end: float
    start_time: float = 0.0

    # Speed tracking
    yes_first_60: float = 0.0   # timestamp when YES first hit 60c
    yes_first_70: float = 0.0   # timestamp when YES first hit 70c
    no_first_60: float = 0.0
    no_first_70: float = 0.0
    analysis_start_time: float = 0.0  # when we started watching (4:00 remaining)

    # Price tracking
    yes_high: float = 0.0
    no_high: float = 0.0
    yes_prices: list = field(default_factory=list)  # [(timestamp, bid)] samples
    no_prices: list = field(default_factory=list)
    spreads: list = field(default_factory=list)  # [(timestamp, yes_spread, no_spread)]

    # BTC price
    btc_start: float = 0.0
    btc_at_buy: float = 0.0
    btc_at_end: float = 0.0

    # Book depth at buy signal
    leader_depth_at_buy: float = 0.0
    leader_depth_70_at_buy: float = 0.0
    other_depth_at_buy: float = 0.0
    spread_at_buy: float = 0.0
    levels_at_buy: int = 0

    # Outcome
    bought: bool = False
    buy_side: str = ""
    entry_price: float = 0.0
    entry_remaining: float = 0.0
    choppy: bool = False
    no_leader: bool = False
    outcome: str = ""       # "tp", "sl", "resolved-win", "resolved-loss"
    exit_price: float = 0.0
    pnl: float = 0.0
    finalized: bool = False


async def get_btc_price(session) -> float:
    """Get current BTC price from Binance."""
    try:
        async with session.get(BINANCE_API, timeout=aiohttp.ClientTimeout(total=3)) as resp:
            data = await resp.json()
            return float(data.get("price", 0))
    except Exception:
        return 0.0


async def get_active_markets(session):
    """Find active BTC 5-min markets."""
    now = time.time()
    current_slot = (int(now) // 300) * 300
    markets = []
    seen = set()
    for offset in range(0, 3):
        epoch = current_slot + offset * 300
        slug = f"btc-updown-5m-{epoch}"
        url = f"{GAMMA_API}/events?slug={slug}"
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                events = await resp.json()
            if not events:
                continue
            from datetime import datetime as _dt
            for m in events[0].get("markets", []):
                cid = m.get("conditionId", "")
                if cid in seen:
                    continue
                raw_tokens = m.get("clobTokenIds", "[]")
                tokens = _json.loads(raw_tokens) if isinstance(raw_tokens, str) else raw_tokens
                if len(tokens) < 2:
                    continue
                end_str = m.get("endDate", "")
                if not end_str:
                    continue
                end_dt = _dt.fromisoformat(end_str.replace("Z", "+00:00"))
                end_ts = end_dt.timestamp()
                remaining = end_ts - now
                if 0 < remaining <= 300:
                    markets.append({
                        "condition_id": cid,
                        "question": m.get("question", ""),
                        "yes_token": tokens[0],
                        "no_token": tokens[1],
                        "window_end": end_ts,
                        "remaining": remaining,
                    })
                    seen.add(cid)
        except Exception:
            continue
    return markets


async def get_book_depth(session, token_id):
    """Fetch full order book and compute depth metrics."""
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
        async with session.get(url, params=params, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            book = await resp.json()
        bids = book.get("bids", [])
        asks = book.get("asks", [])
        result["num_bid_levels"] = len(bids)
        result["num_ask_levels"] = len(asks)
        for b in bids:
            p = float(b.get("price", 0))
            s = float(b.get("size", 0))
            result["bid_depth_total"] += p * s
            if p >= 0.70:
                result["bid_depth_70plus"] += p * s
        for a in asks:
            p = float(a.get("price", 0))
            s = float(a.get("size", 0))
            result["ask_depth_total"] += p * s
        if bids:
            result["best_bid"] = max(float(b.get("price", 0)) for b in bids)
        if asks:
            result["best_ask"] = min(float(a.get("price", 0)) for a in asks)
        if result["best_bid"] > 0 and result["best_ask"] > 0:
            result["spread"] = round(result["best_ask"] - result["best_bid"], 4)
        for k in ("bid_depth_total", "ask_depth_total", "bid_depth_70plus"):
            result[k] = round(result[k], 2)
    except Exception as exc:
        log.warning("Book depth fetch failed: %s", exc)
    return result


async def run():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    )

    # Book snapshot CSV
    book_header_needed = not os.path.exists(BOOK_CSV)
    book_file = open(BOOK_CSV, "a", newline="")
    book_writer = csv.writer(book_file)
    if book_header_needed:
        book_writer.writerow([
            "timestamp_utc", "est_time", "market", "remaining_secs",
            "btc_price",
            "yes_bid_total", "yes_ask_total", "yes_bid_70plus",
            "yes_best_bid", "yes_best_ask", "yes_spread",
            "yes_bid_levels", "yes_ask_levels",
            "no_bid_total", "no_ask_total", "no_bid_70plus",
            "no_best_bid", "no_best_ask", "no_spread",
            "no_bid_levels", "no_ask_levels",
        ])
        book_file.flush()

    # Trades outcome CSV
    trades_header_needed = not os.path.exists(TRADES_CSV)
    trades_file = open(TRADES_CSV, "a", newline="")
    trades_writer = csv.writer(trades_file)
    if trades_header_needed:
        trades_writer.writerow([
            "timestamp_utc", "est_time", "market",
            # Outcome
            "decision", "buy_side", "entry_price", "exit_price", "pnl", "outcome",
            "entry_remaining_secs",
            # Speed
            "secs_to_60c", "secs_to_70c",
            # Volume at buy
            "leader_bid_depth", "leader_bid_70plus", "other_bid_depth",
            "depth_ratio", "spread_at_buy", "bid_levels_at_buy",
            # BTC
            "btc_start", "btc_at_buy", "btc_at_end", "btc_move_dollars",
            # Spread behavior
            "avg_spread", "max_spread", "spread_widened_before_exit",
            # Consecutive
            "prev_window_side", "prev_window_outcome",
            # Choppy
            "was_choppy", "yes_high", "no_high",
        ])
        trades_file.flush()

    log.info("Analytics logger started — sampling every %ds", SAMPLE_INTERVAL)
    log.info("  Book snapshots → %s", BOOK_CSV)
    log.info("  Trade outcomes → %s", TRADES_CSV)

    trackers: Dict[str, WindowTracker] = {}
    decided: set = set()
    prev_side = ""
    prev_outcome = ""

    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        from backports.zoneinfo import ZoneInfo

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                now = time.time()
                est_now = datetime.now(ZoneInfo("America/New_York"))
                est_str = est_now.strftime("%H:%M:%S")
                ts_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

                # Discover markets
                mkts = await get_active_markets(session)
                btc_price = await get_btc_price(session)

                for m in mkts:
                    cid = m["condition_id"]
                    if cid in decided:
                        continue
                    if cid not in trackers:
                        t = WindowTracker(
                            condition_id=cid,
                            question=m["question"],
                            yes_token=m["yes_token"],
                            no_token=m["no_token"],
                            window_end=m["window_end"],
                            start_time=now,
                            analysis_start_time=now,
                            btc_start=btc_price,
                        )
                        trackers[cid] = t
                        log.info("Tracking: %s (%.0fs left)", m["question"][:50], m["remaining"])

                # Process each active tracker
                for cid, t in list(trackers.items()):
                    if t.finalized:
                        continue

                    remaining = t.window_end - now

                    if remaining <= 0:
                        # Window ended — finalize
                        if not t.bought and not t.finalized:
                            t.finalized = True
                            t.btc_at_end = btc_price
                            if t.choppy:
                                t.outcome = "skipped-choppy"
                            else:
                                t.outcome = "skipped-no-leader"
                                t.no_leader = True

                            # Track what WOULD have happened in skipped markets
                            yes_bid = await get_book_depth(session, t.yes_token)
                            no_bid_d = await get_book_depth(session, t.no_token)
                            final_yes = yes_bid["best_bid"]
                            final_no = no_bid_d["best_bid"]
                            would_win = ""
                            if t.yes_high >= 0.70:
                                would_win = "yes-would-tp" if final_yes >= 0.94 else ("yes-would-sl" if final_yes <= 0.28 else "yes-would-hold")
                            elif t.no_high >= 0.70:
                                would_win = "no-would-tp" if final_no >= 0.94 else ("no-would-sl" if final_no <= 0.28 else "no-would-hold")

                            log.info(
                                "═══ SKIPPED %s: %s | yes_high=%.2f no_high=%.2f | choppy=%s | hypothetical=%s",
                                t.outcome.upper(), t.question[:40],
                                t.yes_high, t.no_high, t.choppy, would_win or "n/a",
                            )

                            _write_trade(trades_writer, trades_file, ts_str, est_str, t,
                                         prev_side, prev_outcome, would_win)
                            decided.add(cid)

                        trackers.pop(cid, None)
                        continue

                    if remaining > 240:
                        continue

                    # Sample book depth
                    yes_depth = await get_book_depth(session, t.yes_token)
                    no_depth = await get_book_depth(session, t.no_token)
                    yes_bid = yes_depth["best_bid"]
                    no_bid = no_depth["best_bid"]

                    # Track prices
                    t.yes_prices.append((now, yes_bid))
                    t.no_prices.append((now, no_bid))
                    t.spreads.append((now, yes_depth["spread"], no_depth["spread"]))

                    if yes_bid > t.yes_high:
                        t.yes_high = yes_bid
                    if no_bid > t.no_high:
                        t.no_high = no_bid

                    # Speed tracking — when did each side first cross 60c / 70c?
                    if yes_bid >= 0.60 and t.yes_first_60 == 0:
                        t.yes_first_60 = now
                    if yes_bid >= 0.70 and t.yes_first_70 == 0:
                        t.yes_first_70 = now
                    if no_bid >= 0.60 and t.no_first_60 == 0:
                        t.no_first_60 = now
                    if no_bid >= 0.70 and t.no_first_70 == 0:
                        t.no_first_70 = now

                    # Choppy check
                    if t.yes_high >= 0.60 and t.no_high >= 0.60 and not t.choppy:
                        t.choppy = True
                        log.info("CHOPPY: %s (Yes=%.2f No=%.2f)", t.question[:35], t.yes_high, t.no_high)

                    # Buy signal detection (same logic as S3)
                    if (remaining <= 180 and remaining > 60 and
                            not t.bought and not t.choppy):
                        buy_side = ""
                        if yes_bid >= 0.70 and yes_bid <= 0.90 and yes_bid >= no_bid:
                            buy_side = "Up"
                        elif no_bid >= 0.70 and no_bid <= 0.90 and no_bid >= yes_bid:
                            buy_side = "Down"

                        if buy_side:
                            t.bought = True
                            t.buy_side = buy_side
                            t.entry_price = yes_bid if buy_side == "Up" else no_bid
                            t.entry_remaining = remaining
                            t.btc_at_buy = btc_price

                            leader_d = yes_depth if buy_side == "Up" else no_depth
                            other_d = no_depth if buy_side == "Up" else yes_depth
                            t.leader_depth_at_buy = leader_d["bid_depth_total"]
                            t.leader_depth_70_at_buy = leader_d["bid_depth_70plus"]
                            t.other_depth_at_buy = other_d["bid_depth_total"]
                            t.spread_at_buy = leader_d["spread"]
                            t.levels_at_buy = leader_d["num_bid_levels"]

                            secs_to_70 = (t.yes_first_70 - t.analysis_start_time) if buy_side == "Up" and t.yes_first_70 else \
                                         (t.no_first_70 - t.analysis_start_time) if buy_side == "Down" and t.no_first_70 else 0

                            log.info(
                                "═══ BUY SIGNAL ═══ %s @ $%.2f | %.0fs left | depth=$%.0f/$%.0f | "
                                "speed_to_70=%.0fs | btc=$%.0f | %s",
                                buy_side, t.entry_price, remaining,
                                t.leader_depth_at_buy, t.other_depth_at_buy,
                                secs_to_70, btc_price, t.question[:40],
                            )

                    # Check outcome for bought positions
                    if t.bought and not t.finalized:
                        check_bid = yes_bid if t.buy_side == "Up" else no_bid

                        if remaining <= 5:
                            if check_bid > 0.5:
                                t.outcome = "resolved-win"
                                t.exit_price = 1.0
                            else:
                                t.outcome = "resolved-loss"
                                t.exit_price = 0.0
                            t.pnl = (t.exit_price - t.entry_price) * (20.0 / t.entry_price)
                            t.finalized = True
                        elif check_bid >= 0.94:
                            t.outcome = "tp"
                            t.exit_price = check_bid
                            t.pnl = (check_bid - t.entry_price) * (20.0 / t.entry_price)
                            t.finalized = True
                        elif check_bid <= 0.28:
                            t.outcome = "sl"
                            t.exit_price = check_bid
                            t.pnl = (check_bid - t.entry_price) * (20.0 / t.entry_price)
                            t.finalized = True

                        if t.finalized:
                            t.btc_at_end = btc_price

                            # Spread behavior analysis
                            spreads_vals = [s[1] if t.buy_side == "Up" else s[2] for s in t.spreads]
                            avg_spread = sum(spreads_vals) / len(spreads_vals) if spreads_vals else 0
                            max_spread = max(spreads_vals) if spreads_vals else 0
                            last_spreads = spreads_vals[-3:] if len(spreads_vals) >= 3 else spreads_vals
                            first_spreads = spreads_vals[:3] if len(spreads_vals) >= 3 else spreads_vals
                            spread_widened = (sum(last_spreads)/len(last_spreads)) > (sum(first_spreads)/len(first_spreads)) * 1.5 if first_spreads and last_spreads else False

                            log.info(
                                "═══ RESULT %s %s: $%.2f → $%.2f | PnL $%+.2f | "
                                "depth=$%.0f | speed=%.0fs | btc_move=$%.0f | "
                                "avg_spread=%.4f widened=%s",
                                t.outcome.upper(), t.buy_side, t.entry_price, t.exit_price,
                                t.pnl, t.leader_depth_at_buy,
                                (t.yes_first_70 or t.no_first_70 or 0) - t.analysis_start_time if (t.yes_first_70 or t.no_first_70) else 0,
                                abs(t.btc_at_end - t.btc_start),
                                avg_spread, spread_widened,
                            )

                            _write_trade(trades_writer, trades_file, ts_str, est_str, t,
                                         prev_side, prev_outcome, "")
                            prev_side = t.buy_side
                            prev_outcome = t.outcome
                            decided.add(cid)

                    # Write book snapshot
                    book_writer.writerow([
                        ts_str, est_str, t.question[:60], int(remaining),
                        round(btc_price, 2),
                        yes_depth["bid_depth_total"], yes_depth["ask_depth_total"], yes_depth["bid_depth_70plus"],
                        yes_depth["best_bid"], yes_depth["best_ask"], yes_depth["spread"],
                        yes_depth["num_bid_levels"], yes_depth["num_ask_levels"],
                        no_depth["bid_depth_total"], no_depth["ask_depth_total"], no_depth["bid_depth_70plus"],
                        no_depth["best_bid"], no_depth["best_ask"], no_depth["spread"],
                        no_depth["num_bid_levels"], no_depth["num_ask_levels"],
                    ])
                    book_file.flush()

            except Exception as exc:
                log.warning("Logger error: %s", exc, exc_info=True)

            await asyncio.sleep(SAMPLE_INTERVAL)


def _write_trade(writer, f, ts, est, t: WindowTracker, prev_side, prev_outcome, hypothetical):
    """Write one row to trades_log.csv."""
    decision = "buy" if t.bought else ("skip-choppy" if t.choppy else "skip-no-leader")

    # Speed to 70c for the buy side
    if t.buy_side == "Up" and t.yes_first_70:
        secs_to_60 = t.yes_first_60 - t.analysis_start_time if t.yes_first_60 else 0
        secs_to_70 = t.yes_first_70 - t.analysis_start_time if t.yes_first_70 else 0
    elif t.buy_side == "Down" and t.no_first_70:
        secs_to_60 = t.no_first_60 - t.analysis_start_time if t.no_first_60 else 0
        secs_to_70 = t.no_first_70 - t.analysis_start_time if t.no_first_70 else 0
    else:
        secs_to_60 = 0
        secs_to_70 = 0

    depth_ratio = round(t.leader_depth_at_buy / t.other_depth_at_buy, 2) if t.other_depth_at_buy > 0 else 0

    spreads_vals = [s[1] if t.buy_side == "Up" else s[2] for s in t.spreads] if t.spreads else [0]
    avg_spread = round(sum(spreads_vals) / len(spreads_vals), 4) if spreads_vals else 0
    max_spread = round(max(spreads_vals), 4) if spreads_vals else 0
    last_spreads = spreads_vals[-3:] if len(spreads_vals) >= 3 else spreads_vals
    first_spreads = spreads_vals[:3] if len(spreads_vals) >= 3 else spreads_vals
    spread_widened = (sum(last_spreads)/len(last_spreads)) > (sum(first_spreads)/len(first_spreads)) * 1.5 if first_spreads and last_spreads and sum(first_spreads) > 0 else False

    btc_move = round(abs(t.btc_at_end - t.btc_start), 2) if t.btc_at_end and t.btc_start else 0

    outcome = t.outcome or hypothetical or ""

    writer.writerow([
        ts, est, t.question[:60],
        decision, t.buy_side, round(t.entry_price, 4), round(t.exit_price, 4),
        round(t.pnl, 2), outcome, round(t.entry_remaining, 0),
        round(secs_to_60, 1), round(secs_to_70, 1),
        round(t.leader_depth_at_buy, 0), round(t.leader_depth_70_at_buy, 0),
        round(t.other_depth_at_buy, 0),
        depth_ratio, round(t.spread_at_buy, 4), t.levels_at_buy,
        round(t.btc_start, 2), round(t.btc_at_buy, 2), round(t.btc_at_end, 2), btc_move,
        avg_spread, max_spread, spread_widened,
        prev_side, prev_outcome,
        t.choppy, round(t.yes_high, 2), round(t.no_high, 2),
    ])
    f.flush()


if __name__ == "__main__":
    asyncio.run(run())
