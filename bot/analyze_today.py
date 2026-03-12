"""
Analyze all bots from 3 PM EDT today (March 11) to now.
"""

import csv
from datetime import datetime, timezone, timedelta
from collections import defaultdict

EDT = timezone(timedelta(hours=-4))
# 3 PM EDT = 7 PM UTC on March 11
START = datetime(2026, 3, 11, 19, 0, tzinfo=timezone.utc)


def load(path, label):
    trades = []
    with open(path) as f:
        for row in csv.DictReader(f):
            try:
                ts = datetime.strptime(row["timestamp"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            except:
                continue
            if ts < START:
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
                "bot": label,
            })
    return trades


def analyze_bot(trades, label):
    real = [t for t in trades if t["type"] == "real"]
    phantoms = [t for t in trades if "phantom" in t["type"]]

    wins = [t for t in real if t["pnl"] >= 0]
    losses = [t for t in real if t["pnl"] < 0]
    pnl = sum(t["pnl"] for t in real)
    wr = len(wins) / len(real) * 100 if real else 0

    tp = [t for t in real if t["reason"] == "tp"]
    sl = [t for t in real if t["reason"] == "sl"]
    res_w = [t for t in real if t["reason"] == "resolved-win"]
    res_l = [t for t in real if t["reason"] == "resolved-loss"]

    avg_win = sum(t["pnl"] for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t["pnl"] for t in losses) / len(losses) if losses else 0

    choppy_ph = [t for t in phantoms if "choppy" in t.get("reason", "")]
    noleader_ph = [t for t in phantoms if "no_leader" in t.get("reason", "") or "noleader" in t.get("reason", "")]
    manip_ph = [t for t in phantoms if "manip" in t.get("reason", "")]

    # Hourly breakdown
    hourly = defaultdict(lambda: {"pnl": 0, "trades": 0, "wins": 0, "losses": 0})
    for t in real:
        h = t["edt"].hour
        hourly[h]["pnl"] += t["pnl"]
        hourly[h]["trades"] += 1
        if t["pnl"] >= 0:
            hourly[h]["wins"] += 1
        else:
            hourly[h]["losses"] += 1

    # Loss streaks
    max_lstreak = 0
    cur = 0
    for t in real:
        if t["pnl"] < 0:
            cur += 1
            max_lstreak = max(max_lstreak, cur)
        else:
            cur = 0

    # Win streaks
    max_wstreak = 0
    cur = 0
    for t in real:
        if t["pnl"] >= 0:
            cur += 1
            max_wstreak = max(max_wstreak, cur)
        else:
            cur = 0

    # Trade by trade log
    trade_log = []
    for t in real:
        edt = t["edt"].strftime("%I:%M %p")
        marker = "W" if t["pnl"] >= 0 else "L"
        trade_log.append(f"    {edt} {t['side']:>4} {t['entry']:.2f}->{t['exit']:.2f} ${t['pnl']:>+6.2f} [{t['reason']}] {marker}")

    return {
        "label": label,
        "total": len(trades),
        "real": len(real),
        "phantoms": len(phantoms),
        "wins": len(wins),
        "losses": len(losses),
        "pnl": pnl,
        "wr": wr,
        "tp": len(tp),
        "sl": len(sl),
        "res_w": len(res_w),
        "res_l": len(res_l),
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "choppy_ph": len(choppy_ph),
        "noleader_ph": len(noleader_ph),
        "manip_ph": len(manip_ph),
        "hourly": dict(hourly),
        "max_lstreak": max_lstreak,
        "max_wstreak": max_wstreak,
        "trade_log": trade_log,
    }


def main():
    bots = [
        ("test_trades.csv", "Test Bot (SL=28c)"),
        ("research_trades.csv", "Research (MG original)"),
        ("mg2_trades.csv", "Tuned Guard V2"),
        ("tight_sl_trades.csv", "Tight SL (45c)"),
    ]

    results = []
    for path, label in bots:
        try:
            trades = load(path, label)
            r = analyze_bot(trades, label)
            results.append(r)
        except Exception as e:
            print(f"  Could not load {path}: {e}")

    print(f"\n{'=' * 80}")
    print(f"  ANALYSIS: March 11, 3:00 PM EDT -> Now")
    print(f"{'=' * 80}")

    # Summary table
    print(f"\n  {'Bot':<25} {'Real':>5} {'W':>4} {'L':>4} {'WR%':>6} {'PnL':>9} {'AvgW':>7} {'AvgL':>8}")
    print(f"  {'-'*72}")
    for r in results:
        print(f"  {r['label']:<25} {r['real']:>5} {r['wins']:>4} {r['losses']:>4} "
              f"{r['wr']:>5.1f}% ${r['pnl']:>+7.2f} ${r['avg_win']:>+5.2f} ${r['avg_loss']:>+6.2f}")

    # Exit reasons
    print(f"\n  {'Bot':<25} {'TP':>5} {'SL':>5} {'ResW':>5} {'ResL':>5} {'Phantoms':>9}")
    print(f"  {'-'*60}")
    for r in results:
        print(f"  {r['label']:<25} {r['tp']:>5} {r['sl']:>5} {r['res_w']:>5} {r['res_l']:>5} "
              f"{r['phantoms']:>9}")

    # Phantom breakdown
    print(f"\n  {'Bot':<25} {'Choppy':>7} {'NoLeader':>9} {'ManipGuard':>11}")
    print(f"  {'-'*55}")
    for r in results:
        print(f"  {r['label']:<25} {r['choppy_ph']:>7} {r['noleader_ph']:>9} {r['manip_ph']:>11}")

    # Streaks
    print(f"\n  {'Bot':<25} {'MaxWinStrk':>11} {'MaxLossStrk':>12}")
    print(f"  {'-'*50}")
    for r in results:
        print(f"  {r['label']:<25} {r['max_wstreak']:>11} {r['max_lstreak']:>12}")

    # Hourly breakdown
    all_hours = set()
    for r in results:
        all_hours.update(r["hourly"].keys())
    sorted_hours = sorted(all_hours)

    if sorted_hours:
        print(f"\n  HOURLY PNL (EDT)")
        print(f"  {'-'*72}")
        header = f"  {'Hour':<7}"
        for r in results:
            short = r["label"][:12]
            header += f" {short:>12}"
        print(header)
        print(f"  {'-'*72}")
        for h in sorted_hours:
            row = f"  {h:>2}:00  "
            for r in results:
                d = r["hourly"].get(h)
                if d:
                    row += f" ${d['pnl']:>+7.2f}({d['trades']:>2})"
                else:
                    row += f" {'--':>12}"
            print(row)

    # Trade logs
    for r in results:
        if r["trade_log"]:
            print(f"\n  {'=' * 60}")
            print(f"  {r['label']} — Trade by Trade")
            print(f"  {'=' * 60}")
            for line in r["trade_log"]:
                print(line)

    # Direct comparison
    if len(results) >= 2:
        print(f"\n{'=' * 80}")
        print(f"  HEAD-TO-HEAD: Same markets, different outcomes")
        print(f"{'=' * 80}")
        test = results[0]
        for r in results[1:]:
            diff = r["pnl"] - test["pnl"]
            print(f"  {r['label']} vs Test Bot: ${diff:>+.2f} "
                  f"({'better' if diff > 0 else 'worse'})")


if __name__ == "__main__":
    main()
