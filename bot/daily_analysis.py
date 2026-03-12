#!/usr/bin/env python3
"""Full daily analysis — March 9th (today) vs previous days."""
import json, csv, os
from datetime import datetime
from collections import defaultdict

DOW = {0: "Mon", 1: "Tue", 2: "Wed", 3: "Thu", 4: "Fri", 5: "Sat", 6: "Sun"}
def dow(d): return DOW[datetime.strptime(d, "%Y-%m-%d").weekday()]

HISTORY = "history"
TODAY = "2026-03-09"

# ========== LOAD PNL CALENDARS ==========
bots_pnl = {}
for name, fname in [("test", "pnl_test.json"), ("official", "pnl_official.json"),
                     ("research", "pnl_research.json"), ("research_v2", "pnl_research_v2.json")]:
    if os.path.exists(fname):
        with open(fname) as f:
            bots_pnl[name] = json.load(f)

# ========== SECTION 1: Day Summary All Bots ==========
print("=" * 80)
print("  DAILY SUMMARY — ALL BOTS")
print("=" * 80)

for name, data in bots_pnl.items():
    print(f"\n  --- {name.upper()} ---")
    days = sorted(data.keys())
    for d in days:
        dd = data[d]
        total = dd.get("total", 0)
        trades = dd.get("trades", 0)
        wins = dd.get("wins", 0)
        losses = dd.get("losses", 0)
        wr = wins / trades * 100 if trades > 0 else 0
        marker = " <-- TODAY" if d == TODAY else ""
        print(f"    {d} ({dow(d)}): ${total:>+8.2f} | {trades:>3} trades | W:{wins} L:{losses} | WR:{wr:.0f}%{marker}")

    # Totals
    grand = sum(data[d].get("total", 0) for d in days)
    total_trades = sum(data[d].get("trades", 0) for d in days)
    total_wins = sum(data[d].get("wins", 0) for d in days)
    total_losses = sum(data[d].get("losses", 0) for d in days)
    total_wr = total_wins / total_trades * 100 if total_trades > 0 else 0
    print(f"    {'TOTAL':>10}: ${grand:>+8.2f} | {total_trades:>3} trades | W:{total_wins} L:{total_losses} | WR:{total_wr:.0f}%")

# ========== SECTION 2: Today's Hourly Breakdown ==========
print(f"\n{'='*80}")
print(f"  HOURLY BREAKDOWN — {TODAY} ({dow(TODAY)})")
print(f"{'='*80}")

for name, data in bots_pnl.items():
    if TODAY not in data:
        continue
    today_data = data[TODAY]
    hours = today_data.get("hours", {})
    if not hours:
        continue
    print(f"\n  --- {name.upper()} ---")
    total = today_data.get("total", 0)
    print(f"    Day total: ${total:+.2f}")
    for h in sorted(hours.keys(), key=lambda x: int(x)):
        pnl = hours[h]
        marker = ""
        if pnl < -20: marker = "  !! BAD"
        elif pnl < -10: marker = "  ! rough"
        elif pnl > 20: marker = "  ** GREAT"
        elif pnl > 10: marker = "  * good"
        print(f"    {int(h):02d}:00 ET: ${pnl:>+8.2f}{marker}")

# ========== SECTION 3: Today's Trade Details ==========
print(f"\n{'='*80}")
print(f"  TRADE DETAILS — {TODAY}")
print(f"{'='*80}")

for name in ["test", "official", "research"]:
    csv_path = os.path.join(HISTORY, f"{name}_trades.csv")
    if not os.path.exists(csv_path):
        continue
    with open(csv_path) as f:
        reader = csv.DictReader(f)
        all_rows = list(reader)

    today_rows = [r for r in all_rows if r.get("timestamp", "").startswith(TODAY)]
    if not today_rows:
        continue

    real = [r for r in today_rows if r.get("type", "") == "real"]
    phantoms = [r for r in today_rows if "phantom" in r.get("type", "")]

    real_wins = sum(1 for r in real if float(r.get("pnl", 0)) > 0)
    real_losses = sum(1 for r in real if float(r.get("pnl", 0)) < 0)
    real_pnl = sum(float(r.get("pnl", 0)) for r in real)

    print(f"\n  --- {name.upper()} ---")
    print(f"    Real trades: {len(real)} (W:{real_wins} L:{real_losses}) PnL: ${real_pnl:+.2f}")
    print(f"    Phantoms: {len(phantoms)}")

    # Phantom breakdown
    phantom_types = defaultdict(lambda: {"count": 0, "wins": 0, "losses": 0, "pnl": 0})
    for p in phantoms:
        ptype = p.get("type", "unknown")
        reason = p.get("filter_reason", ptype)
        pnl = float(p.get("pnl", 0))
        phantom_types[reason]["count"] += 1
        phantom_types[reason]["pnl"] += pnl
        if pnl > 0:
            phantom_types[reason]["wins"] += 1
        elif pnl < 0:
            phantom_types[reason]["losses"] += 1

    for reason, d in phantom_types.items():
        wr = d["wins"] / (d["wins"] + d["losses"]) * 100 if (d["wins"] + d["losses"]) > 0 else 0
        print(f"      {reason}: {d['count']} skipped | Would-Win:{d['wins']} Would-Lose:{d['losses']} WR:{wr:.0f}% | Phantom PnL: ${d['pnl']:+.2f}")

    # Exit reason breakdown
    exit_reasons = defaultdict(lambda: {"count": 0, "pnl": 0})
    for r in real:
        reason = r.get("exit_reason", "unknown")
        pnl = float(r.get("pnl", 0))
        exit_reasons[reason]["count"] += 1
        exit_reasons[reason]["pnl"] += pnl

    print(f"    Exit reasons:")
    for reason, d in sorted(exit_reasons.items(), key=lambda x: x[1]["pnl"]):
        avg = d["pnl"] / d["count"] if d["count"] > 0 else 0
        print(f"      {reason}: {d['count']} trades, ${d['pnl']:+.2f} (avg ${avg:+.2f})")

    # Biggest wins and losses
    sorted_by_pnl = sorted(real, key=lambda x: float(x.get("pnl", 0)))
    if sorted_by_pnl:
        print(f"    Worst trades:")
        for t in sorted_by_pnl[:5]:
            print(f"      {t.get('est_time','')} | {t.get('side','')} @ ${float(t.get('entry_price',0)):.2f} -> ${float(t.get('exit_price',0)):.2f} | ${float(t.get('pnl',0)):+.2f} | {t.get('exit_reason','')}")
        print(f"    Best trades:")
        for t in sorted_by_pnl[-3:]:
            print(f"      {t.get('est_time','')} | {t.get('side','')} @ ${float(t.get('entry_price',0)):.2f} -> ${float(t.get('exit_price',0)):.2f} | ${float(t.get('pnl',0)):+.2f} | {t.get('exit_reason','')}")

# ========== SECTION 4: Loss Streaks Today ==========
print(f"\n{'='*80}")
print(f"  LOSS STREAKS — {TODAY}")
print(f"{'='*80}")

for name in ["test", "official"]:
    csv_path = os.path.join(HISTORY, f"{name}_trades.csv")
    if not os.path.exists(csv_path):
        continue
    with open(csv_path) as f:
        reader = csv.DictReader(f)
        all_rows = list(reader)

    today_real = [r for r in all_rows if r.get("timestamp", "").startswith(TODAY) and r.get("type", "") == "real"]
    if not today_real:
        continue

    streak = 0
    streak_start = None
    streaks = []
    for i, t in enumerate(today_real):
        pnl = float(t.get("pnl", 0))
        if pnl < 0:
            if streak == 0:
                streak_start = i
            streak += 1
        else:
            if streak >= 2:
                streaks.append((streak_start, streak))
            streak = 0
    if streak >= 2:
        streaks.append((streak_start, streak))

    print(f"\n  --- {name.upper()} ---")
    if not streaks:
        print(f"    No loss streaks of 2+ today")
    for start, length in streaks:
        trades = today_real[start:start+length]
        total_loss = sum(float(t.get("pnl", 0)) for t in trades)
        times = f"{trades[0].get('est_time','')} - {trades[-1].get('est_time','')}"
        sides = [t.get("side", "?") for t in trades]
        exits = [t.get("exit_reason", "?") for t in trades]
        print(f"    Streak: {length} losses | {times} | ${total_loss:+.2f}")
        print(f"      Sides: {sides} | Exits: {exits}")

# ========== SECTION 5: Day Comparison ==========
print(f"\n{'='*80}")
print(f"  DAY-BY-DAY COMPARISON (Test Bot)")
print(f"{'='*80}")

if "test" in bots_pnl:
    data = bots_pnl["test"]
    days = sorted(data.keys())

    print(f"\n  {'Day':<12} {'DOW':<5} {'PnL':>9} {'Trades':>7} {'WR':>5} {'TP':>4} {'SL':>4} {'Best Hour':>12} {'Worst Hour':>12}")
    print(f"  {'-'*75}")

    for d in days:
        dd = data[d]
        total = dd.get("total", 0)
        trades = dd.get("trades", 0)
        wins = dd.get("wins", 0)
        losses = dd.get("losses", 0)
        wr = wins / trades * 100 if trades > 0 else 0
        hours = dd.get("hours", {})

        best_h = max(hours, key=lambda x: hours[x]) if hours else "?"
        worst_h = min(hours, key=lambda x: hours[x]) if hours else "?"
        best_v = hours.get(best_h, 0)
        worst_v = hours.get(worst_h, 0)

        marker = " <--" if d == TODAY else ""
        print(f"  {d:<12} {dow(d):<5} ${total:>+7.2f} {trades:>6}  {wr:>4.0f}%  {dd.get('tp_hits','-'):>3}  {dd.get('sl_hits','-'):>3}  {best_h}h(${best_v:+.0f})  {worst_h}h(${worst_v:+.0f}){marker}")

# ========== SECTION 6: Weekday vs Weekend ==========
print(f"\n{'='*80}")
print(f"  WEEKDAY vs WEEKEND (Test Bot)")
print(f"{'='*80}")

if "test" in bots_pnl:
    data = bots_pnl["test"]
    weekday = {d: data[d] for d in data if datetime.strptime(d, "%Y-%m-%d").weekday() < 5}
    weekend = {d: data[d] for d in data if datetime.strptime(d, "%Y-%m-%d").weekday() >= 5}

    for label, subset in [("Weekday", weekday), ("Weekend", weekend)]:
        if not subset:
            continue
        total = sum(subset[d].get("total", 0) for d in subset)
        trades = sum(subset[d].get("trades", 0) for d in subset)
        wins = sum(subset[d].get("wins", 0) for d in subset)
        losses = sum(subset[d].get("losses", 0) for d in subset)
        wr = wins / trades * 100 if trades > 0 else 0
        avg = total / len(subset)
        print(f"  {label}: {len(subset)} days | ${total:+.2f} total | ${avg:+.2f}/day avg | {trades} trades | WR:{wr:.0f}%")

# ========== SECTION 7: Official Bot Status ==========
print(f"\n{'='*80}")
print(f"  OFFICIAL BOT STATUS")
print(f"{'='*80}")

if "official" in bots_pnl:
    data = bots_pnl["official"]
    if TODAY in data:
        dd = data[TODAY]
        print(f"  Today: ${dd.get('total', 0):+.2f} | {dd.get('trades', 0)} trades | W:{dd.get('wins', 0)} L:{dd.get('losses', 0)}")
    grand = sum(data[d].get("total", 0) for d in data)
    total_trades = sum(data[d].get("trades", 0) for d in data)
    print(f"  All-time: ${grand:+.2f} | {total_trades} total trades")

# ========== SECTION 8: Manipulation Guard Performance ==========
print(f"\n{'='*80}")
print(f"  MANIPULATION GUARD — Research Bot (new, started late today)")
print(f"{'='*80}")

csv_path = os.path.join(HISTORY, "research_trades.csv")
if os.path.exists(csv_path):
    with open(csv_path) as f:
        reader = csv.DictReader(f)
        all_rows = list(reader)

    today_rows = [r for r in all_rows if r.get("timestamp", "").startswith(TODAY)]
    mg_phantoms = [r for r in today_rows if "manip_guard" in r.get("filter_reason", "")]
    
    if mg_phantoms:
        mg_wins = sum(1 for r in mg_phantoms if float(r.get("pnl", 0)) > 0)
        mg_losses = sum(1 for r in mg_phantoms if float(r.get("pnl", 0)) < 0)
        mg_pnl = sum(float(r.get("pnl", 0)) for r in mg_phantoms)
        print(f"  Manip Guard skips today: {len(mg_phantoms)}")
        print(f"    Would-Win: {mg_wins} | Would-Lose: {mg_losses}")
        print(f"    Phantom PnL: ${mg_pnl:+.2f}")
        print(f"    {'GUARD SAVED MONEY' if mg_pnl < 0 else 'GUARD COST MONEY'}: ${abs(mg_pnl):.2f}")
    else:
        print(f"  No manip guard skips recorded today (just started)")
        
    # Show research bot real trades too
    research_real = [r for r in today_rows if r.get("type", "") == "real"]
    if research_real:
        rr_pnl = sum(float(r.get("pnl", 0)) for r in research_real)
        rr_wins = sum(1 for r in research_real if float(r.get("pnl", 0)) > 0)
        rr_losses = sum(1 for r in research_real if float(r.get("pnl", 0)) < 0)
        print(f"  Research real trades today: {len(research_real)} (W:{rr_wins} L:{rr_losses}) PnL: ${rr_pnl:+.2f}")
else:
    print(f"  No research trade data yet")
