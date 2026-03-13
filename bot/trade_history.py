"""
Persistent trade history — appends every trade to a CSV file per bot.
Survives restarts. Stores all analytics for long-term research.
"""

import csv
import os
import logging
import threading
from datetime import datetime
from pathlib import Path

log = logging.getLogger("trade_history")

HISTORY_DIR = Path(__file__).resolve().parent.parent / "history"

RESEARCH_COLS = [
    "timestamp", "est_time", "market", "side", "type",
    "entry_price", "exit_price", "qty", "pnl", "exit_reason",
    "remaining_at_entry",
    "leader_bid_total", "leader_ask_total", "leader_bid_70plus",
    "leader_spread", "leader_best_bid", "leader_best_ask",
    "other_bid_total", "other_ask_total", "other_spread",
    "depth_ratio", "speed_to_60", "speed_to_70",
    "btc_price", "btc_move",
    "prev_side", "prev_outcome",
    "filtered", "filter_reasons",
    "skip_reason",
    "up_high", "down_high",
    "avg_spread",
]

SCALP_COLS = [
    "timestamp", "est_time", "market", "side", "type",
    "entry_price", "exit_price", "qty", "pnl", "exit_reason",
    "remaining_at_entry",
]

TEST_COLS = [
    "timestamp", "est_time", "market", "side", "type",
    "entry_price", "exit_price", "qty", "pnl", "exit_reason",
    "filter_reason",
    "ask_at_buy", "bid_at_sell_trigger",
    "btc_at_entry", "btc_at_exit",
    "other_side_high", "reversal_detected",
]

_locks = {}


def _get_lock(name: str) -> threading.Lock:
    if name not in _locks:
        _locks[name] = threading.Lock()
    return _locks[name]


def _ensure_csv(path: Path, columns: list):
    """Create CSV or upgrade header if new columns were added."""
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", newline="") as f:
            csv.writer(f).writerow(columns)
        return
    with open(path, "r") as f:
        existing = f.readline().strip().split(",")
    if len(existing) < len(columns):
        bak = path.with_suffix(".pre_upgrade.csv")
        if not bak.exists():
            import shutil
            shutil.copy2(path, bak)
            log.info("Backed up %s -> %s before header upgrade", path, bak)
        with open(path, "r") as f:
            all_lines = f.readlines()
        with open(path, "w", newline="") as f:
            f.write(",".join(columns) + "\n")
            for line in all_lines[1:]:
                f.write(line)


def log_research_trade(pos, bot_name="research"):
    """Log a research bot trade (real, filtered, or phantom)."""
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        from backports.zoneinfo import ZoneInfo

    path = HISTORY_DIR / f"{bot_name}_trades.csv"
    lock = _get_lock(bot_name)

    now = datetime.now(ZoneInfo("America/New_York"))
    snap = pos.vol_snapshot or {}
    leader = snap.get("leader", {})
    other = snap.get("other", {})

    is_filtered = snap.get("filtered", False)
    skip_reason = snap.get("skip_reason", "")
    is_phantom = "phantom" in (pos.status or "")

    if is_phantom and skip_reason:
        trade_type = f"phantom-{skip_reason}"
    elif is_phantom:
        trade_type = "phantom-filtered"
    else:
        trade_type = "real"

    row = {
        "timestamp": now.strftime("%Y-%m-%d %H:%M:%S"),
        "est_time": now.strftime("%I:%M:%S %p"),
        "market": getattr(pos.market, "question", ""),
        "side": pos.side,
        "type": trade_type,
        "entry_price": pos.entry_price,
        "exit_price": pos.exit_price or 0,
        "qty": pos.qty,
        "pnl": round(pos.pnl, 2) if pos.pnl else 0,
        "exit_reason": pos.exit_reason or pos.status or "",
        "remaining_at_entry": snap.get("remaining", 0),
        "leader_bid_total": leader.get("bid_depth_total", 0),
        "leader_ask_total": leader.get("ask_depth_total", 0),
        "leader_bid_70plus": leader.get("bid_depth_70plus", 0),
        "leader_spread": leader.get("spread", 0),
        "leader_best_bid": leader.get("best_bid", 0),
        "leader_best_ask": leader.get("best_ask", 0),
        "other_bid_total": other.get("bid_depth_total", 0),
        "other_ask_total": other.get("ask_depth_total", 0),
        "other_spread": other.get("spread", 0),
        "depth_ratio": snap.get("depth_ratio", 0),
        "speed_to_60": snap.get("speed_to_60", 0),
        "speed_to_70": snap.get("speed_to_70", 0),
        "btc_price": snap.get("btc_price", 0),
        "btc_move": snap.get("btc_move", 0),
        "prev_side": snap.get("prev_side", ""),
        "prev_outcome": snap.get("prev_outcome", ""),
        "filtered": is_filtered,
        "filter_reasons": "|".join(snap.get("filter_reasons", [])),
        "skip_reason": skip_reason,
        "up_high": snap.get("up_high", 0),
        "down_high": snap.get("down_high", 0),
        "avg_spread": snap.get("avg_spread", 0),
    }

    with lock:
        _ensure_csv(path, RESEARCH_COLS)
        with open(path, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=RESEARCH_COLS, extrasaction="ignore")
            writer.writerow(row)

    log.debug("Logged %s trade: %s %s $%.2f→$%.2f", trade_type, pos.side, row["market"][:30], pos.entry_price, row["exit_price"])


def log_scalp_trade(pos, bot_name="scalp"):
    """Log a scalp bot trade."""
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        from backports.zoneinfo import ZoneInfo

    path = HISTORY_DIR / f"{bot_name}_trades.csv"
    lock = _get_lock(bot_name)

    now = datetime.now(ZoneInfo("America/New_York"))

    row = {
        "timestamp": now.strftime("%Y-%m-%d %H:%M:%S"),
        "est_time": now.strftime("%I:%M:%S %p"),
        "market": getattr(pos.market, "question", ""),
        "side": pos.side,
        "type": "real",
        "entry_price": pos.entry_price,
        "exit_price": pos.exit_price or 0,
        "qty": pos.qty,
        "pnl": round(pos.pnl, 2) if pos.pnl else 0,
        "exit_reason": pos.exit_reason or pos.status or "",
        "remaining_at_entry": "",
    }

    with lock:
        _ensure_csv(path, SCALP_COLS)
        with open(path, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=SCALP_COLS, extrasaction="ignore")
            writer.writerow(row)


def log_s3_trade(pos, bot_name="test"):
    """Log an S3 bot trade (test or official), including phantoms."""
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        from backports.zoneinfo import ZoneInfo

    path = HISTORY_DIR / f"{bot_name}_trades.csv"
    lock = _get_lock(bot_name)

    now = datetime.now(ZoneInfo("America/New_York"))
    is_phantom = "phantom" in (pos.status or "")
    filter_reason = getattr(pos, "filter_reason", "")

    if is_phantom and filter_reason:
        trade_type = f"phantom-{filter_reason}"
    elif is_phantom:
        trade_type = "phantom"
    else:
        trade_type = "real"

    row = {
        "timestamp": now.strftime("%Y-%m-%d %H:%M:%S"),
        "est_time": now.strftime("%I:%M:%S %p"),
        "market": getattr(pos.market, "question", ""),
        "side": pos.side,
        "type": trade_type,
        "entry_price": pos.entry_price,
        "exit_price": pos.exit_price or 0,
        "qty": pos.qty,
        "pnl": round(pos.pnl, 2) if pos.pnl else 0,
        "exit_reason": pos.exit_reason or pos.status or "",
        "filter_reason": filter_reason,
        "ask_at_buy": getattr(pos, "ask_at_buy", 0),
        "bid_at_sell_trigger": getattr(pos, "bid_at_sell_trigger", 0),
        "btc_at_entry": getattr(pos, "btc_at_entry", 0),
        "btc_at_exit": getattr(pos, "btc_at_exit", 0),
        "other_side_high": getattr(pos, "other_side_high", 0),
        "reversal_detected": getattr(pos, "reversal_detected", False),
    }

    with lock:
        _ensure_csv(path, TEST_COLS)
        with open(path, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=TEST_COLS, extrasaction="ignore")
            writer.writerow(row)


def log_daily_snapshot(bot_name: str, stats_dict: dict):
    """Log a daily stats snapshot for long-term tracking."""
    try:
        from zoneinfo import ZoneInfo
    except ImportError:
        from backports.zoneinfo import ZoneInfo

    path = HISTORY_DIR / f"{bot_name}_daily.csv"
    lock = _get_lock(f"{bot_name}_daily")

    now = datetime.now(ZoneInfo("America/New_York"))
    row = {"date": now.strftime("%Y-%m-%d"), "timestamp": now.strftime("%Y-%m-%d %H:%M:%S")}
    row.update(stats_dict)

    cols = list(row.keys())

    with lock:
        _ensure_csv(path, cols)
        with open(path, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            writer.writerow(row)

    log.debug("Daily snapshot for %s: %s", bot_name, row)
