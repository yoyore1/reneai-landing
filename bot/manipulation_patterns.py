#!/usr/bin/env python3
"""Analyze patterns that precede bad streaks — what signals manipulation?"""
import csv, os, json
from datetime import datetime
from collections import deque

HISTORY = "history"

# Load all test bot trades (real + phantom) in order
trades = []
for fname in ["test_trades.csv"]:
    path = os.path.join(HISTORY, fname)
    if not os.path.exists(path):
        continue
    with open(path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            trades.append(row)

print(f"Total trades loaded: {len(trades)}")

# =====================================================
# PATTERN 1: Side alternation before losses
# If Up/Down keeps flipping, market is choppy/manipulated
# =====================================================
print("\n" + "=" * 70)
print("  PATTERN 1: Side Alternation (reversal frequency)")
print("=" * 70)

# For each trade, calculate how many of the last 5 trades alternated sides
real_trades = [t for t in trades if t.get("type", "") == "real"]
print(f"  Real trades: {len(real_trades)}")

# Track rolling win rate and alternation
window = 6
streaks = []
for i in range(window, len(real_trades)):
    recent = real_trades[i-window:i]
    current = real_trades[i]

    # Count alternations in the window
    alternations = 0
    for j in range(1, len(recent)):
        if recent[j]["side"] != recent[j-1]["side"]:
            alternations += 1

    # Win rate of window
    wins = sum(1 for t in recent if float(t.get("pnl", 0)) > 0)
    wr = wins / window * 100

    # Current trade result
    cur_pnl = float(current.get("pnl", 0))
    cur_won = cur_pnl > 0

    streaks.append({
        "time": current.get("est_time", ""),
        "timestamp": current.get("timestamp", ""),
        "alternations": alternations,
        "window_wr": wr,
        "current_won": cur_won,
        "current_pnl": cur_pnl,
        "side": current.get("side", ""),
    })

# Bucket by alternation count
print(f"\n  How does alternation in the last {window} trades predict the NEXT trade?")
print(f"  (More alternation = more side-flipping = more manipulation)")
print()
for alt_count in range(window):
    subset = [s for s in streaks if s["alternations"] == alt_count]
    if not subset:
        continue
    wins = sum(1 for s in subset if s["current_won"])
    total = len(subset)
    wr = wins / total * 100
    avg_pnl = sum(s["current_pnl"] for s in subset) / total
    print(f"  {alt_count} alternations in last {window}: {total} trades, WR={wr:.1f}%, avg PnL=${avg_pnl:+.2f}")

# =====================================================
# PATTERN 2: Rolling win rate before losses
# =====================================================
print("\n" + "=" * 70)
print("  PATTERN 2: Rolling Win Rate — Does a dropping WR predict more losses?")
print("=" * 70)

for wr_bucket in [(0, 33), (33, 50), (50, 67), (67, 83), (83, 101)]:
    lo, hi = wr_bucket
    subset = [s for s in streaks if lo <= s["window_wr"] < hi]
    if not subset:
        continue
    wins = sum(1 for s in subset if s["current_won"])
    total = len(subset)
    wr = wins / total * 100
    avg_pnl = sum(s["current_pnl"] for s in subset) / total
    print(f"  Last {window} WR {lo}-{hi}%: next trade WR={wr:.1f}% ({wins}/{total}), avg PnL=${avg_pnl:+.2f}")

# =====================================================
# PATTERN 3: Entry price vs outcome
# =====================================================
print("\n" + "=" * 70)
print("  PATTERN 3: Entry Price vs Outcome")
print("=" * 70)
print("  Higher entry = more confident market, but also more to lose")
print()

for price_range in [(0.50, 0.65), (0.65, 0.72), (0.72, 0.80), (0.80, 0.90), (0.90, 1.0)]:
    lo, hi = price_range
    subset = [t for t in real_trades if lo <= float(t.get("entry_price", 0)) < hi]
    if not subset:
        continue
    wins = sum(1 for t in subset if float(t.get("pnl", 0)) > 0)
    total = len(subset)
    wr = wins / total * 100
    avg_pnl = sum(float(t.get("pnl", 0)) for t in subset) / total
    total_pnl = sum(float(t.get("pnl", 0)) for t in subset)
    avg_entry = sum(float(t.get("entry_price", 0)) for t in subset) / total

    # losses in this range
    losses = [t for t in subset if float(t.get("pnl", 0)) < 0]
    avg_loss = sum(float(t.get("pnl", 0)) for t in losses) / len(losses) if losses else 0

    print(f"  Entry ${lo:.2f}-${hi:.2f}: {total} trades, WR={wr:.1f}%, avg PnL=${avg_pnl:+.2f}, total=${total_pnl:+.2f}")
    print(f"    Avg entry=${avg_entry:.3f}, avg loss=${avg_loss:+.2f}")

# =====================================================
# PATTERN 4: Choppy frequency before losses
# =====================================================
print("\n" + "=" * 70)
print("  PATTERN 4: Choppy/Skip frequency before bad streaks")
print("=" * 70)

all_trades = trades  # includes phantoms
print(f"  Total trades+phantoms: {len(all_trades)}")

# Find sequences of 10 trades, count how many are choppy phantoms
for i in range(10, len(all_trades)):
    recent_10 = all_trades[i-10:i]
    choppy_count = sum(1 for t in recent_10 if "choppy" in t.get("type", ""))
    noleader_count = sum(1 for t in recent_10 if "no_leader" in t.get("type", ""))
    skip_pct = (choppy_count + noleader_count) / 10 * 100

    # Next real trade
    current = all_trades[i]
    if current.get("type") != "real":
        continue

    cur_pnl = float(current.get("pnl", 0))
    cur_won = cur_pnl > 0

    # Store for bucketing
    all_trades[i]["_skip_pct"] = skip_pct
    all_trades[i]["_choppy_count"] = choppy_count

# Bucket by skip percentage
print(f"\n  How does choppy/skip rate in last 10 markets predict next trade?")
buckets = {}
for t in all_trades:
    if "_skip_pct" not in t or t.get("type") != "real":
        continue
    sp = t["_skip_pct"]
    bucket = int(sp // 20) * 20  # 0%, 20%, 40%, 60%, 80%
    if bucket not in buckets:
        buckets[bucket] = {"wins": 0, "total": 0, "pnl": 0}
    pnl = float(t.get("pnl", 0))
    buckets[bucket]["total"] += 1
    buckets[bucket]["pnl"] += pnl
    if pnl > 0:
        buckets[bucket]["wins"] += 1

for b in sorted(buckets.keys()):
    d = buckets[b]
    wr = d["wins"] / d["total"] * 100 if d["total"] > 0 else 0
    avg = d["pnl"] / d["total"] if d["total"] > 0 else 0
    print(f"  Skip rate {b}-{b+20}% in last 10: {d['total']} trades, WR={wr:.1f}%, avg PnL=${avg:+.2f}")

# =====================================================
# PATTERN 5: Consecutive loss streaks — what preceded them?
# =====================================================
print("\n" + "=" * 70)
print("  PATTERN 5: What happens right before loss streaks?")
print("=" * 70)

# Find all 3+ loss streaks
streak_start = None
streak_len = 0
loss_streaks = []
for i, t in enumerate(real_trades):
    pnl = float(t.get("pnl", 0))
    if pnl < 0:
        if streak_start is None:
            streak_start = i
        streak_len += 1
    else:
        if streak_len >= 2:
            loss_streaks.append((streak_start, streak_len))
        streak_start = None
        streak_len = 0
if streak_len >= 2:
    loss_streaks.append((streak_start, streak_len))

print(f"  Found {len(loss_streaks)} loss streaks of 2+ trades")
for start, length in loss_streaks:
    streak_trades = real_trades[start:start+length]
    total_loss = sum(float(t.get("pnl", 0)) for t in streak_trades)

    # Look at 5 trades before the streak
    pre = real_trades[max(0,start-5):start]
    pre_wr = sum(1 for t in pre if float(t.get("pnl",0)) > 0) / len(pre) * 100 if pre else 0
    pre_sides = [t.get("side","?") for t in pre]
    pre_alts = sum(1 for j in range(1, len(pre_sides)) if pre_sides[j] != pre_sides[j-1])

    streak_sides = [t.get("side","?") for t in streak_trades]
    streak_entries = [float(t.get("entry_price",0)) for t in streak_trades]
    streak_exits = [t.get("exit_reason","?") for t in streak_trades]

    print(f"\n  Streak at {streak_trades[0].get('timestamp','?')}:")
    print(f"    Length: {length} losses, total: ${total_loss:+.2f}")
    print(f"    Sides: {streak_sides}, Entries: {[f'${e:.2f}' for e in streak_entries]}")
    print(f"    Exit reasons: {streak_exits}")
    print(f"    Pre-streak (5 trades): WR={pre_wr:.0f}%, sides={pre_sides}, alternations={pre_alts}")

# =====================================================
# PATTERN 6: Exit reason analysis — resolution vs SL
# =====================================================
print("\n" + "=" * 70)
print("  PATTERN 6: How do losses happen?")
print("=" * 70)

losses = [t for t in real_trades if float(t.get("pnl", 0)) < 0]
exit_reasons = {}
for t in losses:
    reason = t.get("exit_reason", "unknown")
    if reason not in exit_reasons:
        exit_reasons[reason] = {"count": 0, "pnl": 0}
    exit_reasons[reason]["count"] += 1
    exit_reasons[reason]["pnl"] += float(t.get("pnl", 0))

print(f"  Total losses: {len(losses)}")
for reason, d in sorted(exit_reasons.items(), key=lambda x: x[1]["pnl"]):
    avg = d["pnl"] / d["count"]
    print(f"    {reason}: {d['count']} trades, total=${d['pnl']:+.2f}, avg=${avg:+.2f}")

# Liquidation (resolved-loss/unknown) vs SL
liq = sum(1 for t in losses if "resolved" in t.get("exit_reason", ""))
sl = sum(1 for t in losses if t.get("exit_reason", "") == "sl")
liq_pnl = sum(float(t.get("pnl",0)) for t in losses if "resolved" in t.get("exit_reason",""))
sl_pnl = sum(float(t.get("pnl",0)) for t in losses if t.get("exit_reason","") == "sl")
print(f"\n  Liquidations (resolved): {liq} trades, ${liq_pnl:+.2f}")
print(f"  Stop-loss exits: {sl} trades, ${sl_pnl:+.2f}")
if liq > 0 and sl > 0:
    print(f"  Avg liquidation loss: ${liq_pnl/liq:+.2f} vs avg SL loss: ${sl_pnl/sl:+.2f}")
    print(f"  SL saves ${abs(liq_pnl/liq) - abs(sl_pnl/sl):.2f} per trade on average")
