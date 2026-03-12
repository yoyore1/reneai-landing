"""
Analyze no-leader phantom trades from the research bot (MG).
These are markets where no side hit 70c, so the MG bot bought the leader anyway.
The test bot would have skipped these entirely.
"""

import csv
from datetime import datetime, timezone, timedelta
from collections import defaultdict

EDT = timezone(timedelta(hours=-4))
START = datetime(2026, 3, 6, 4, 0, tzinfo=timezone.utc)

def main():
    trades = []
    with open("research_trades.csv") as f:
        for row in csv.DictReader(f):
            try:
                ts = datetime.strptime(row["timestamp"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            except:
                continue
            if ts < START:
                continue
            reason = row.get("exit_reason", "")
            ttype = row.get("type", "real")
            if "no_leader" not in reason and "no_leader" not in ttype and "noleader" not in reason:
                continue
            entry = float(row["entry_price"])
            exit_p = float(row["exit_price"])
            pnl = float(row["pnl"])
            won = pnl >= 0
            trades.append({
                "ts": ts,
                "edt": ts.astimezone(EDT),
                "entry": entry,
                "exit": exit_p,
                "pnl": pnl,
                "won": won,
                "side": row.get("side", ""),
                "market": row.get("market", ""),
            })

    print(f"No-Leader Trades (research bot phantoms): {len(trades)}")
    print(f"Period: {trades[0]['edt'].strftime('%b %d')} to {trades[-1]['edt'].strftime('%b %d')}\n")

    wins = [t for t in trades if t["won"]]
    losses = [t for t in trades if not t["won"]]
    total_pnl = sum(t["pnl"] for t in trades)
    wr = len(wins) / len(trades) * 100 if trades else 0

    print(f"  Wins: {len(wins)}  |  Losses: {len(losses)}  |  Win Rate: {wr:.1f}%")
    print(f"  Total PnL: ${total_pnl:+.2f}")
    if wins:
        print(f"  Avg Win: ${sum(t['pnl'] for t in wins)/len(wins):+.2f}")
    if losses:
        print(f"  Avg Loss: ${sum(t['pnl'] for t in losses)/len(losses):+.2f}")

    # By entry price bracket
    print(f"\n  ENTRY PRICE BREAKDOWN:")
    print(f"  {'Bracket':<12} {'Count':>6} {'Wins':>6} {'Losses':>7} {'WR%':>6} {'PnL':>10}")
    print(f"  {'-'*50}")
    brackets = [
        (0, 0.50, "<50c"),
        (0.50, 0.60, "50-59c"),
        (0.60, 0.70, "60-69c"),
        (0.70, 0.80, "70-79c"),
        (0.80, 0.90, "80-89c"),
        (0.90, 1.01, "90c+"),
    ]
    for lo, hi, label in brackets:
        bt = [t for t in trades if lo <= t["entry"] < hi]
        if not bt:
            continue
        bw = [t for t in bt if t["won"]]
        bl = [t for t in bt if not t["won"]]
        bpnl = sum(t["pnl"] for t in bt)
        bwr = len(bw) / len(bt) * 100
        print(f"  {label:<12} {len(bt):>6} {len(bw):>6} {len(bl):>7} {bwr:>5.1f}% ${bpnl:>+8.2f}")

    # By day
    print(f"\n  DAILY BREAKDOWN:")
    print(f"  {'Day':<12} {'Count':>6} {'Wins':>6} {'Losses':>7} {'WR%':>6} {'PnL':>10}")
    print(f"  {'-'*50}")
    daily = defaultdict(list)
    for t in trades:
        day = t["edt"].strftime("%b %d")
        daily[day].append(t)
    for day in sorted(daily.keys()):
        dt = daily[day]
        dw = [t for t in dt if t["won"]]
        dl = [t for t in dt if not t["won"]]
        dpnl = sum(t["pnl"] for t in dt)
        dwr = len(dw) / len(dt) * 100
        print(f"  {day:<12} {len(dt):>6} {len(dw):>6} {len(dl):>7} {dwr:>5.1f}% ${dpnl:>+8.2f}")

    # Realistic trades only (entry < 0.90, filtering out the 0.99 "already decided" ones)
    real_entries = [t for t in trades if t["entry"] < 0.90]
    print(f"\n  REALISTIC NO-LEADER TRADES (entry < 90c):")
    print(f"  These are the ones that are actually coin-flip/risky:")
    rw = [t for t in real_entries if t["won"]]
    rl = [t for t in real_entries if not t["won"]]
    rpnl = sum(t["pnl"] for t in real_entries)
    rwr = len(rw) / len(real_entries) * 100 if real_entries else 0
    print(f"  Count: {len(real_entries)}  |  Wins: {len(rw)}  |  Losses: {len(rl)}  |  WR: {rwr:.1f}%")
    print(f"  Total PnL: ${rpnl:+.2f}")
    if rw:
        print(f"  Avg Win: ${sum(t['pnl'] for t in rw)/len(rw):+.2f}")
    if rl:
        print(f"  Avg Loss: ${sum(t['pnl'] for t in rl)/len(rl):+.2f}")

    # The actual losses — list them
    print(f"\n  ALL NO-LEADER LOSSES (entry < 90c):")
    for t in rl:
        edt = t["edt"].strftime("%b %d %I:%M %p")
        print(f"    {edt}  {t['side']:>4} {t['entry']:.2f}->{t['exit']:.2f}  ${t['pnl']:>+7.2f}  {t['market'][:45]}")


if __name__ == "__main__":
    main()
