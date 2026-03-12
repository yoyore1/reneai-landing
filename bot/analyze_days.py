"""
Analyze March 7-11 — Are the days similar or different?

Looks at: win rate, entry prices, loss severity, choppy rate, 
hourly patterns, streak patterns, and market behavior.
"""

import csv
from datetime import datetime, timezone, timedelta
from collections import defaultdict

EDT = timezone(timedelta(hours=-4))

DAY_BOUNDARIES = [
    ("Mar 7",  datetime(2026, 3, 7, 4, 0, tzinfo=timezone.utc),  datetime(2026, 3, 8, 4, 0, tzinfo=timezone.utc)),
    ("Mar 8",  datetime(2026, 3, 8, 4, 0, tzinfo=timezone.utc),  datetime(2026, 3, 9, 4, 0, tzinfo=timezone.utc)),
    ("Mar 9",  datetime(2026, 3, 9, 4, 0, tzinfo=timezone.utc),  datetime(2026, 3, 10, 4, 0, tzinfo=timezone.utc)),
    ("Mar 10", datetime(2026, 3, 10, 4, 0, tzinfo=timezone.utc), datetime(2026, 3, 11, 4, 0, tzinfo=timezone.utc)),
    ("Mar 11", datetime(2026, 3, 11, 4, 0, tzinfo=timezone.utc), datetime(2026, 3, 12, 4, 0, tzinfo=timezone.utc)),
]


def load(path):
    trades = []
    with open(path) as f:
        for row in csv.DictReader(f):
            try:
                ts = datetime.strptime(row["timestamp"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            except:
                continue
            trades.append({
                "ts": ts,
                "edt": ts.astimezone(EDT),
                "entry": float(row["entry_price"]),
                "exit": float(row["exit_price"]),
                "qty": float(row["qty"]),
                "pnl": float(row["pnl"]),
                "reason": row.get("exit_reason", ""),
                "type": row.get("type", "real"),
                "side": row.get("side", ""),
                "market": row.get("market", ""),
            })
    return trades


def get_day(ts):
    for label, start, end in DAY_BOUNDARIES:
        if start <= ts < end:
            return label
    return None


def analyze():
    trades = load("test_trades.csv")
    print(f"Loaded {len(trades)} total trades\n")

    # Split by day
    days = defaultdict(list)
    for t in trades:
        d = get_day(t["ts"])
        if d:
            days[d].append(t)

    # ============================================================
    # 1. BASIC STATS PER DAY
    # ============================================================
    print("=" * 80)
    print("  1. BASIC STATS PER DAY")
    print("=" * 80)
    print(f"  {'Day':<8} {'Total':>6} {'Real':>6} {'Phantom':>8} {'Choppy':>7} {'NoLdr':>6}")
    print(f"  {'-'*45}")
    for label, _, _ in DAY_BOUNDARIES:
        dt = days.get(label, [])
        real = [t for t in dt if t["type"] == "real"]
        phantom = [t for t in dt if "phantom" in t["type"]]
        choppy = [t for t in dt if "choppy" in t.get("reason", "") or
                  (t["type"] != "real" and "choppy" in str(t.get("market", "")))]
        # Count by filter reason from type
        choppy_ph = [t for t in phantom if any(k in t["reason"] for k in ["choppy"])]
        noleader_ph = [t for t in phantom if any(k in t["reason"] for k in ["no_leader", "noleader"])]
        print(f"  {label:<8} {len(dt):>6} {len(real):>6} {len(phantom):>8} {len(choppy_ph):>7} {len(noleader_ph):>6}")

    # ============================================================
    # 2. WIN RATE & PNL (real trades only)
    # ============================================================
    print(f"\n{'=' * 80}")
    print("  2. WIN RATE & PNL (real trades only)")
    print("=" * 80)
    print(f"  {'Day':<8} {'Trades':>7} {'Wins':>6} {'Losses':>7} {'WR%':>6} {'PnL':>10} {'AvgWin':>8} {'AvgLoss':>9}")
    print(f"  {'-'*65}")
    for label, _, _ in DAY_BOUNDARIES:
        real = [t for t in days.get(label, []) if t["type"] == "real"]
        if not real:
            continue
        wins = [t for t in real if t["pnl"] >= 0]
        losses = [t for t in real if t["pnl"] < 0]
        pnl = sum(t["pnl"] for t in real)
        wr = len(wins) / len(real) * 100 if real else 0
        avg_win = sum(t["pnl"] for t in wins) / len(wins) if wins else 0
        avg_loss = sum(t["pnl"] for t in losses) / len(losses) if losses else 0
        print(f"  {label:<8} {len(real):>7} {len(wins):>6} {len(losses):>7} {wr:>5.1f}% ${pnl:>+8.2f} ${avg_win:>+6.2f} ${avg_loss:>+7.2f}")

    # ============================================================
    # 3. ENTRY PRICE DISTRIBUTION
    # ============================================================
    print(f"\n{'=' * 80}")
    print("  3. ENTRY PRICE DISTRIBUTION (real trades)")
    print("=" * 80)
    brackets = [(0.70, 0.73, "70-72c"), (0.73, 0.76, "73-75c"), (0.76, 0.80, "76-79c"),
                (0.80, 0.85, "80-84c"), (0.85, 0.91, "85-90c")]
    
    header = f"  {'Day':<8}"
    for _, _, lbl in brackets:
        header += f" {lbl:>8}"
    header += f" {'AvgEntry':>9}"
    print(header)
    print(f"  {'-'*60}")
    
    for label, _, _ in DAY_BOUNDARIES:
        real = [t for t in days.get(label, []) if t["type"] == "real"]
        if not real:
            continue
        row = f"  {label:<8}"
        for lo, hi, _ in brackets:
            cnt = len([t for t in real if lo <= t["entry"] < hi])
            pct = cnt / len(real) * 100 if real else 0
            row += f" {pct:>6.0f}%  "
        avg_entry = sum(t["entry"] for t in real) / len(real)
        row += f"  {avg_entry:.3f}"
        print(row)

    # ============================================================
    # 4. EXIT REASON BREAKDOWN
    # ============================================================
    print(f"\n{'=' * 80}")
    print("  4. EXIT REASON BREAKDOWN (real trades)")
    print("=" * 80)
    reasons = ["tp", "sl", "resolved-win", "resolved-loss"]
    header = f"  {'Day':<8}"
    for r in reasons:
        header += f" {r:>14}"
    print(header)
    print(f"  {'-'*68}")
    for label, _, _ in DAY_BOUNDARIES:
        real = [t for t in days.get(label, []) if t["type"] == "real"]
        if not real:
            continue
        row = f"  {label:<8}"
        for r in reasons:
            cnt = len([t for t in real if t["reason"] == r])
            pct = cnt / len(real) * 100 if real else 0
            row += f" {cnt:>4} ({pct:>4.0f}%)  "
        print(row)

    # ============================================================
    # 5. SIDE DISTRIBUTION (Up vs Down wins)
    # ============================================================
    print(f"\n{'=' * 80}")
    print("  5. SIDE DISTRIBUTION (real trades)")
    print("=" * 80)
    print(f"  {'Day':<8} {'Up':>5} {'UpWR%':>7} {'Down':>6} {'DnWR%':>7} {'Dominant':>9} {'Alternations':>13}")
    print(f"  {'-'*60}")
    for label, _, _ in DAY_BOUNDARIES:
        real = [t for t in days.get(label, []) if t["type"] == "real"]
        if not real:
            continue
        up = [t for t in real if t["side"] == "Up"]
        dn = [t for t in real if t["side"] == "Down"]
        up_wr = len([t for t in up if t["pnl"] >= 0]) / len(up) * 100 if up else 0
        dn_wr = len([t for t in dn if t["pnl"] >= 0]) / len(dn) * 100 if dn else 0
        dominant = "Up" if len(up) > len(dn) else ("Down" if len(dn) > len(up) else "Even")
        
        alts = 0
        for i in range(1, len(real)):
            if real[i]["side"] != real[i-1]["side"]:
                alts += 1
        alt_rate = alts / (len(real) - 1) * 100 if len(real) > 1 else 0
        
        print(f"  {label:<8} {len(up):>5} {up_wr:>5.1f}%  {len(dn):>5} {dn_wr:>5.1f}%  {dominant:>8}  {alts:>4} ({alt_rate:.0f}%)")

    # ============================================================
    # 6. HOURLY PNL HEATMAP (EDT)
    # ============================================================
    print(f"\n{'=' * 80}")
    print("  6. HOURLY PNL HEATMAP (EDT, real trades)")
    print("=" * 80)
    
    hours_seen = set()
    hourly_data = {}
    for label, _, _ in DAY_BOUNDARIES:
        real = [t for t in days.get(label, []) if t["type"] == "real"]
        for t in real:
            h = t["edt"].hour
            hours_seen.add(h)
            key = (label, h)
            if key not in hourly_data:
                hourly_data[key] = {"pnl": 0, "trades": 0, "wins": 0}
            hourly_data[key]["pnl"] += t["pnl"]
            hourly_data[key]["trades"] += 1
            if t["pnl"] >= 0:
                hourly_data[key]["wins"] += 1
    
    sorted_hours = sorted(hours_seen)
    header = f"  {'Hour':<6}"
    for label, _, _ in DAY_BOUNDARIES:
        header += f" {label:>8}"
    header += f" {'AVG':>8}"
    print(header)
    print(f"  {'-'*55}")
    
    for h in sorted_hours:
        row = f"  {h:>2}:00 "
        vals = []
        for label, _, _ in DAY_BOUNDARIES:
            key = (label, h)
            d = hourly_data.get(key)
            if d:
                row += f" ${d['pnl']:>+6.1f} "
                vals.append(d["pnl"])
            else:
                row += f" {'--':>7} "
        avg = sum(vals) / len(vals) if vals else 0
        marker = " BAD" if avg < -3 else (" GOOD" if avg > 5 else "")
        row += f" ${avg:>+6.1f}{marker}"
        print(row)

    # ============================================================
    # 7. LOSS STREAKS
    # ============================================================
    print(f"\n{'=' * 80}")
    print("  7. LOSS STREAKS (real trades)")
    print("=" * 80)
    for label, _, _ in DAY_BOUNDARIES:
        real = [t for t in days.get(label, []) if t["type"] == "real"]
        if not real:
            continue
        max_streak = 0
        cur_streak = 0
        streaks = []
        for t in real:
            if t["pnl"] < 0:
                cur_streak += 1
            else:
                if cur_streak > 0:
                    streaks.append(cur_streak)
                cur_streak = 0
        if cur_streak > 0:
            streaks.append(cur_streak)
        max_streak = max(streaks) if streaks else 0
        avg_streak = sum(streaks) / len(streaks) if streaks else 0
        streaks_3plus = len([s for s in streaks if s >= 3])
        print(f"  {label}: Max streak={max_streak}, Avg streak={avg_streak:.1f}, "
              f"Streaks>=3: {streaks_3plus}, Total loss runs: {len(streaks)}")

    # ============================================================
    # 8. WIN STREAKS
    # ============================================================
    print(f"\n{'=' * 80}")
    print("  8. WIN STREAKS (real trades)")
    print("=" * 80)
    for label, _, _ in DAY_BOUNDARIES:
        real = [t for t in days.get(label, []) if t["type"] == "real"]
        if not real:
            continue
        streaks = []
        cur = 0
        for t in real:
            if t["pnl"] >= 0:
                cur += 1
            else:
                if cur > 0:
                    streaks.append(cur)
                cur = 0
        if cur > 0:
            streaks.append(cur)
        max_s = max(streaks) if streaks else 0
        avg_s = sum(streaks) / len(streaks) if streaks else 0
        s5plus = len([s for s in streaks if s >= 5])
        print(f"  {label}: Max streak={max_s}, Avg streak={avg_s:.1f}, "
              f"Streaks>=5: {s5plus}, Total win runs: {len(streaks)}")

    # ============================================================
    # 9. CHOPPY MARKET ANALYSIS
    # ============================================================
    print(f"\n{'=' * 80}")
    print("  9. CHOPPY MARKET OUTCOMES (phantoms)")
    print("=" * 80)
    print(f"  {'Day':<8} {'Choppy':>7} {'WouldWin':>9} {'WouldLose':>10} {'WouldWR%':>9} {'WouldPnL':>10}")
    print(f"  {'-'*58}")
    for label, _, _ in DAY_BOUNDARIES:
        dt = days.get(label, [])
        choppy = [t for t in dt if "phantom" in t["type"] and "choppy" in t.get("reason", "")]
        if not choppy:
            # Try by exit_reason
            choppy = [t for t in dt if "phantom" in t["type"]]
            choppy = [t for t in choppy if "choppy" in str(t.get("reason", ""))]
        cw = [t for t in choppy if t["pnl"] >= 0]
        cl = [t for t in choppy if t["pnl"] < 0]
        cpnl = sum(t["pnl"] for t in choppy)
        cwr = len(cw) / len(choppy) * 100 if choppy else 0
        print(f"  {label:<8} {len(choppy):>7} {len(cw):>9} {len(cl):>10} {cwr:>7.1f}%  ${cpnl:>+8.2f}")

    # ============================================================
    # 10. SIMILARITY SCORE
    # ============================================================
    print(f"\n{'=' * 80}")
    print("  10. DAY SIMILARITY ANALYSIS")
    print("=" * 80)
    
    day_profiles = {}
    for label, _, _ in DAY_BOUNDARIES:
        real = [t for t in days.get(label, []) if t["type"] == "real"]
        if not real:
            continue
        wins = [t for t in real if t["pnl"] >= 0]
        losses = [t for t in real if t["pnl"] < 0]
        up = [t for t in real if t["side"] == "Up"]
        alts = sum(1 for i in range(1, len(real)) if real[i]["side"] != real[i-1]["side"])
        
        day_profiles[label] = {
            "trades": len(real),
            "wr": len(wins) / len(real) * 100,
            "pnl": sum(t["pnl"] for t in real),
            "avg_entry": sum(t["entry"] for t in real) / len(real),
            "avg_loss": sum(t["pnl"] for t in losses) / len(losses) if losses else 0,
            "avg_win": sum(t["pnl"] for t in wins) / len(wins) if wins else 0,
            "up_pct": len(up) / len(real) * 100,
            "alt_rate": alts / (len(real) - 1) * 100 if len(real) > 1 else 0,
            "tp_pct": len([t for t in real if t["reason"] == "tp"]) / len(real) * 100,
            "sl_pct": len([t for t in real if t["reason"] == "sl"]) / len(real) * 100,
        }
    
    metrics = ["wr", "avg_entry", "avg_loss", "avg_win", "up_pct", "alt_rate", "tp_pct", "sl_pct"]
    metric_labels = {
        "wr": "Win Rate %", "avg_entry": "Avg Entry", "avg_loss": "Avg Loss $",
        "avg_win": "Avg Win $", "up_pct": "Up Side %", "alt_rate": "Alternation %",
        "tp_pct": "TP Exit %", "sl_pct": "SL Exit %",
    }
    
    print(f"\n  {'Metric':<16}", end="")
    for label in day_profiles:
        print(f" {label:>8}", end="")
    print(f" {'StdDev':>8}  {'Verdict':>10}")
    print(f"  {'-'*75}")
    
    for m in metrics:
        vals = [day_profiles[d][m] for d in day_profiles]
        avg = sum(vals) / len(vals) if vals else 0
        std = (sum((v - avg) ** 2 for v in vals) / len(vals)) ** 0.5 if vals else 0
        cv = std / avg * 100 if avg != 0 else 0
        
        row = f"  {metric_labels[m]:<16}"
        for d in day_profiles:
            v = day_profiles[d][m]
            if "Entry" in metric_labels[m]:
                row += f" {v:>7.3f} "
            elif "$" in metric_labels[m]:
                row += f" ${v:>+6.2f} "
            else:
                row += f" {v:>6.1f}% "
        
        verdict = "STABLE" if cv < 15 else ("VARIABLE" if cv < 30 else "VOLATILE")
        row += f" {std:>7.1f}   {verdict}"
        print(row)
    
    print(f"\n  OVERALL: ", end="")
    stable_count = 0
    for m in metrics:
        vals = [day_profiles[d][m] for d in day_profiles]
        avg = sum(vals) / len(vals) if vals else 0
        std = (sum((v - avg) ** 2 for v in vals) / len(vals)) ** 0.5 if vals else 0
        cv = std / avg * 100 if avg != 0 else 0
        if cv < 15:
            stable_count += 1
    
    pnl_vals = [day_profiles[d]["pnl"] for d in day_profiles]
    pnl_std = (sum((v - sum(pnl_vals)/len(pnl_vals)) ** 2 for v in pnl_vals) / len(pnl_vals)) ** 0.5
    
    print(f"{stable_count}/{len(metrics)} metrics are stable across days")
    print(f"  PnL range: ${min(pnl_vals):+.2f} to ${max(pnl_vals):+.2f} (std: ${pnl_std:.2f})")
    
    if pnl_std > 50:
        print("  --> Days are SIGNIFICANTLY DIFFERENT in profitability")
    elif pnl_std > 20:
        print("  --> Days are MODERATELY DIFFERENT")
    else:
        print("  --> Days are RELATIVELY SIMILAR")


if __name__ == "__main__":
    analyze()
