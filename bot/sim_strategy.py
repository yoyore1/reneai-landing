"""
Strategy Simulation — Compare current vs proposed strategy changes
across March 7-11 EDT using test bot trade history.

Tests multiple configurations varying:
  - Buy threshold (70c vs 73c vs 75c)
  - Stop loss (28c vs 40c vs 45c vs 50c)
  - Max buy price (85c vs 90c)
  - TP (94c stays)
  
Uses test_trades.csv (all real + phantom trades) as the data source.
"""

import csv
import sys
from datetime import datetime, timezone, timedelta
from collections import defaultdict

EDT = timezone(timedelta(hours=-4))
START = datetime(2026, 3, 7, 4, 0, tzinfo=timezone.utc)   # Mar 7 00:00 EDT
END   = datetime(2026, 3, 12, 4, 0, tzinfo=timezone.utc)   # Mar 12 00:00 EDT

DAY_BOUNDARIES = [
    ("Mar 7",  datetime(2026, 3, 7, 4, 0, tzinfo=timezone.utc),  datetime(2026, 3, 8, 4, 0, tzinfo=timezone.utc)),
    ("Mar 8",  datetime(2026, 3, 8, 4, 0, tzinfo=timezone.utc),  datetime(2026, 3, 9, 4, 0, tzinfo=timezone.utc)),
    ("Mar 9",  datetime(2026, 3, 9, 4, 0, tzinfo=timezone.utc),  datetime(2026, 3, 10, 4, 0, tzinfo=timezone.utc)),
    ("Mar 10", datetime(2026, 3, 10, 4, 0, tzinfo=timezone.utc), datetime(2026, 3, 11, 4, 0, tzinfo=timezone.utc)),
    ("Mar 11", datetime(2026, 3, 11, 4, 0, tzinfo=timezone.utc), datetime(2026, 3, 12, 4, 0, tzinfo=timezone.utc)),
]


def load_trades(path):
    trades = []
    with open(path, "r") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                ts = datetime.strptime(row["timestamp"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            except:
                continue
            if ts < START or ts >= END:
                continue
            entry = float(row["entry_price"])
            exit_p = float(row["exit_price"])
            qty = float(row["qty"])
            pnl = float(row["pnl"])
            exit_reason = row.get("exit_reason", "")
            trade_type = row.get("type", "real")
            trades.append({
                "ts": ts,
                "entry": entry,
                "exit": exit_p,
                "qty": qty,
                "pnl": pnl,
                "reason": exit_reason,
                "type": trade_type,
                "side": row.get("side", ""),
                "market": row.get("market", ""),
            })
    return trades


def simulate(trades, buy_min, buy_max, sl, tp, label):
    """
    Re-simulate trades with different thresholds.
    For each trade, we know entry_price and the actual market outcome.
    We re-evaluate whether we'd take the trade and what the exit would be.
    """
    daily_pnl = defaultdict(float)
    daily_trades = defaultdict(int)
    daily_wins = defaultdict(int)
    daily_losses = defaultdict(int)
    total_pnl = 0.0
    total_trades = 0
    total_wins = 0
    total_losses = 0
    skipped_entry_low = 0
    skipped_entry_high = 0
    sl_saved = 0       # trades where tighter SL saved money
    sl_saved_amount = 0.0
    tp_unchanged = 0
    early_sl_wrong = 0  # trades where tighter SL stopped out but would've won

    for t in trades:
        if t["type"] not in ("real", "phantom-win", "phantom-lose"):
            if "phantom" in t["type"]:
                pass
            else:
                continue

        entry = t["entry"]
        actual_exit = t["exit"]
        actual_pnl = t["pnl"]
        qty = t["qty"]

        is_real_or_phantom = t["type"] in ("real",) or "phantom" in t["type"]
        if not is_real_or_phantom:
            continue

        day_label = None
        for dlabel, dstart, dend in DAY_BOUNDARIES:
            if dstart <= t["ts"] < dend:
                day_label = dlabel
                break
        if not day_label:
            continue

        if entry < buy_min:
            skipped_entry_low += 1
            continue
        if entry > buy_max:
            skipped_entry_high += 1
            continue

        new_qty = int(20.0 / entry) if entry > 0 else 0
        if new_qty <= 0:
            continue

        won_market = actual_exit >= 0.9 or t["reason"] in ("tp", "resolved-win", "phantom-win")
        lost_market = actual_exit <= 0.15 or t["reason"] in ("sl", "resolved-loss", "phantom-lose", "resolved-unknown")

        if "phantom" in t["type"]:
            won_market = "win" in t["type"]
            lost_market = "lose" in t["type"] or "loss" in t["type"]

        if won_market:
            sim_exit = min(tp, 1.0)
            sim_pnl = (sim_exit - entry) * new_qty
            total_wins += 1
            daily_wins[day_label] += 1
        elif lost_market:
            sim_exit = sl
            sim_pnl = (sl - entry) * new_qty

            if sl > 0.28 and actual_exit <= 0.28:
                sl_saved += 1
                old_loss = (0.28 - entry) * new_qty
                new_loss = (sl - entry) * new_qty
                sl_saved_amount += (new_loss - old_loss)

            total_losses += 1
            daily_losses[day_label] += 1
        else:
            sim_exit = actual_exit
            sim_pnl = (sim_exit - entry) * new_qty
            if sim_pnl >= 0:
                total_wins += 1
                daily_wins[day_label] += 1
            else:
                total_losses += 1
                daily_losses[day_label] += 1

        total_pnl += sim_pnl
        total_trades += 1
        daily_pnl[day_label] += sim_pnl
        daily_trades[day_label] += 1

    win_rate = (total_wins / total_trades * 100) if total_trades > 0 else 0

    print(f"\n{'='*70}")
    print(f"  {label}")
    print(f"  Buy: {buy_min:.0%}-{buy_max:.0%} | SL: {sl:.0%} | TP: {tp:.0%}")
    print(f"{'='*70}")
    print(f"  Total trades: {total_trades}  |  W: {total_wins}  L: {total_losses}  |  Win rate: {win_rate:.1f}%")
    print(f"  Total PnL: ${total_pnl:+.2f}")
    if skipped_entry_low:
        print(f"  Skipped (entry too low <{buy_min:.0%}): {skipped_entry_low}")
    if skipped_entry_high:
        print(f"  Skipped (entry too high >{buy_max:.0%}): {skipped_entry_high}")
    if sl_saved:
        print(f"  SL saved money on {sl_saved} trades (saved ${abs(sl_saved_amount):.2f} vs 28c SL)")
    print()

    print(f"  {'Day':<8} {'Trades':>7} {'Wins':>6} {'Losses':>7} {'WR%':>6} {'PnL':>10}")
    print(f"  {'-'*48}")
    for dlabel, _, _ in DAY_BOUNDARIES:
        dp = daily_pnl.get(dlabel, 0)
        dt = daily_trades.get(dlabel, 0)
        dw = daily_wins.get(dlabel, 0)
        dl = daily_losses.get(dlabel, 0)
        wr = (dw / dt * 100) if dt > 0 else 0
        marker = " <<<" if dp < -5 else (" ***" if dp > 30 else "")
        print(f"  {dlabel:<8} {dt:>7} {dw:>6} {dl:>7} {wr:>5.1f}% ${dp:>+8.2f}{marker}")

    print()
    return {"label": label, "pnl": total_pnl, "trades": total_trades,
            "wins": total_wins, "losses": total_losses, "win_rate": win_rate,
            "daily": dict(daily_pnl)}


def main():
    print("Loading test bot trade history...")
    trades = load_trades("test_trades.csv")
    print(f"  Loaded {len(trades)} trades in Mar 7-11 EDT window")

    real = [t for t in trades if t["type"] == "real"]
    phantoms = [t for t in trades if "phantom" in t["type"]]
    print(f"  Real trades: {len(real)}, Phantoms: {len(phantoms)}")

    all_trades = trades

    configs = [
        (0.70, 0.90, 0.28, 0.94, "A) BASELINE SL=28c"),
        (0.70, 0.90, 0.35, 0.94, "B) SL=35c"),
        (0.70, 0.90, 0.38, 0.94, "C) SL=38c"),
        (0.70, 0.90, 0.40, 0.94, "D) SL=40c"),
        (0.70, 0.90, 0.42, 0.94, "E) SL=42c"),
        (0.70, 0.90, 0.44, 0.94, "F) SL=44c"),
        (0.70, 0.90, 0.45, 0.94, "G) SL=45c"),
        (0.70, 0.90, 0.46, 0.94, "H) SL=46c"),
        (0.70, 0.90, 0.48, 0.94, "I) SL=48c"),
        (0.70, 0.90, 0.50, 0.94, "J) SL=50c"),
        (0.70, 0.90, 0.52, 0.94, "K) SL=52c"),
        (0.70, 0.90, 0.55, 0.94, "L) SL=55c"),
        (0.70, 0.90, 0.58, 0.94, "M) SL=58c"),
        (0.70, 0.90, 0.60, 0.94, "N) SL=60c"),
    ]

    results = []
    for buy_min, buy_max, sl, tp, label in configs:
        r = simulate(all_trades, buy_min, buy_max, sl, tp, label)
        results.append(r)

    print("\n" + "="*70)
    print("  SUMMARY COMPARISON")
    print("="*70)
    print(f"  {'Config':<55} {'Trades':>7} {'WR%':>6} {'Total PnL':>10}")
    print(f"  {'-'*80}")
    for r in results:
        marker = " <-- BEST" if r["pnl"] == max(x["pnl"] for x in results) else ""
        print(f"  {r['label'][:55]:<55} {r['trades']:>7} {r['win_rate']:>5.1f}% ${r['pnl']:>+8.2f}{marker}")

    print("\n" + "="*70)
    print("  DAILY PnL COMPARISON (key configs)")
    print("="*70)
    key_configs = ["A)", "D)", "G)", "J)", "N)"]
    key_results = [r for r in results if any(r["label"].startswith(k) for k in key_configs)]

    header = f"  {'Day':<8}"
    for r in key_results:
        short = r["label"].split(")")[0] + ")"
        header += f" {short:>10}"
    print(header)
    print(f"  {'-'*50}")

    for dlabel, _, _ in DAY_BOUNDARIES:
        row = f"  {dlabel:<8}"
        for r in key_results:
            dp = r["daily"].get(dlabel, 0)
            row += f" ${dp:>+8.2f}"
        print(row)

    row = f"  {'TOTAL':<8}"
    for r in key_results:
        row += f" ${r['pnl']:>+8.2f}"
    print(f"  {'-'*50}")
    print(row)

    # Risk analysis
    print("\n" + "="*70)
    print("  RISK ANALYSIS")
    print("="*70)
    for r in key_results:
        short = r["label"].split(")")[0] + ")"
        worst_day = min(r["daily"].values()) if r["daily"] else 0
        best_day = max(r["daily"].values()) if r["daily"] else 0
        avg_day = r["pnl"] / len(DAY_BOUNDARIES) if DAY_BOUNDARIES else 0
        neg_days = sum(1 for v in r["daily"].values() if v < 0)
        print(f"  {short}: Worst day ${worst_day:+.2f} | Best day ${best_day:+.2f} | "
              f"Avg/day ${avg_day:+.2f} | Negative days: {neg_days}/5")


if __name__ == "__main__":
    main()
