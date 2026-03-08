"""
Full deep analysis — pulls directly from PnL JSON files (EST) and trade CSVs.
All hours are EST. Days of week are correct (Mar 4 = Wednesday).
"""
import json, csv, os, glob
from collections import defaultdict

DOW_MAP = {
    "2026-03-04": "Wed", "2026-03-05": "Thu", "2026-03-06": "Fri",
    "2026-03-07": "Sat", "2026-03-08": "Sun", "2026-03-09": "Mon",
    "2026-03-10": "Tue",
}

def is_weekend(date_str):
    return DOW_MAP.get(date_str, "") in ("Sat", "Sun")

def load_json(path):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {}

def load_csv(path):
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return list(csv.DictReader(f))

def sf(v, d=0.0):
    try: return float(v)
    except: return d

# ==================== LOAD DATA ====================
pnl = {}
for bot in ["test", "official", "research", "scalp"]:
    pnl[bot] = load_json(f"pnl_{bot}.json")

hist_dir = "history"
research_trades = load_csv(f"{hist_dir}/research_trades.csv") if os.path.isdir(hist_dir) else []
test_trades = load_csv(f"{hist_dir}/test_trades.csv") if os.path.isdir(hist_dir) else []
scalp_trades = load_csv(f"{hist_dir}/scalp_trades.csv") if os.path.isdir(hist_dir) else []
vol_log = load_csv("volume_log.csv")

print("=" * 80)
print("COMPREHENSIVE ANALYSIS — ALL DATA FROM PNL JSON (EST)")
print("=" * 80)

# ==================== SECTION 1: DAY-BY-DAY ALL BOTS ====================
print("\n" + "=" * 80)
print("1. DAY-BY-DAY COMPARISON (ALL BOTS)")
print("=" * 80)

all_dates = sorted(set(
    list(pnl["test"].keys()) + list(pnl["research"].keys()) +
    list(pnl["scalp"].keys()) + list(pnl["official"].keys())
))

print(f"\n  {'Date':<12} {'DOW':<4} {'WkEnd':<6} | {'Test PnL':>10} {'T-WR':>6} {'T-Trd':>6} | {'Res PnL':>10} {'R-WR':>6} {'R-Trd':>6} | {'Scalp PnL':>10} {'S-WR':>6} {'S-Trd':>6}")
print("  " + "-" * 110)

totals = {b: {"pnl": 0, "trades": 0, "wins": 0, "losses": 0} for b in ["test", "research", "scalp"]}
weekday_totals = {b: {"pnl": 0, "trades": 0, "wins": 0} for b in ["test", "research", "scalp"]}
weekend_totals = {b: {"pnl": 0, "trades": 0, "wins": 0} for b in ["test", "research", "scalp"]}

for date in all_dates:
    dow = DOW_MAP.get(date, "?")
    wkend = "YES" if is_weekend(date) else "no"

    row = f"  {date:<12} {dow:<4} {wkend:<6} |"

    for bot in ["test", "research", "scalp"]:
        d = pnl[bot].get(date, {})
        p = d.get("total", 0)
        t = d.get("trades", 0)
        w = d.get("wins", 0)
        l = d.get("losses", 0)
        wr = w / t * 100 if t > 0 else 0

        totals[bot]["pnl"] += p
        totals[bot]["trades"] += t
        totals[bot]["wins"] += w
        totals[bot]["losses"] += l

        bucket = weekend_totals if is_weekend(date) else weekday_totals
        bucket[bot]["pnl"] += p
        bucket[bot]["trades"] += t
        bucket[bot]["wins"] += w

        row += f" ${p:>+8.2f} {wr:>5.0f}% {t:>5} |"

    print(row)

print("  " + "-" * 110)
row = f"  {'TOTAL':<12} {'':4} {'':6} |"
for bot in ["test", "research", "scalp"]:
    t = totals[bot]
    wr = t["wins"] / t["trades"] * 100 if t["trades"] > 0 else 0
    row += f" ${t['pnl']:>+8.2f} {wr:>5.0f}% {t['trades']:>5} |"
print(row)

# ==================== SECTION 2: WEEKDAY vs WEEKEND ====================
print("\n" + "=" * 80)
print("2. WEEKDAY vs WEEKEND BREAKDOWN")
print("=" * 80)

for label, bucket in [("WEEKDAY (Wed-Fri)", weekday_totals), ("WEEKEND (Sat-Sun)", weekend_totals)]:
    print(f"\n  {label}:")
    for bot in ["test", "research", "scalp"]:
        b = bucket[bot]
        wr = b["wins"] / b["trades"] * 100 if b["trades"] > 0 else 0
        avg = b["pnl"] / b["trades"] if b["trades"] > 0 else 0
        print(f"    {bot:>10}: PnL ${b['pnl']:>+8.2f} | {b['trades']:>4} trades | WR {wr:.0f}% | Avg/trade ${avg:+.2f}")

# ==================== SECTION 3: HOURLY ANALYSIS (TEST BOT) ====================
print("\n" + "=" * 80)
print("3. TEST BOT — HOURLY ANALYSIS ACROSS ALL DAYS (EST)")
print("=" * 80)

# Aggregate hourly data from PnL JSON
hourly_all = defaultdict(lambda: {"pnl": 0, "days_positive": 0, "days_negative": 0, "day_pnls": []})
hourly_by_day = defaultdict(dict)

for date, day_data in pnl["test"].items():
    hours = day_data.get("hours", {})
    for h, val in hours.items():
        hr = int(h)
        hourly_all[hr]["pnl"] += val
        hourly_all[hr]["day_pnls"].append((date, val))
        if val >= 0:
            hourly_all[hr]["days_positive"] += 1
        else:
            hourly_all[hr]["days_negative"] += 1
        hourly_by_day[hr][date] = val

print(f"\n  {'Hour':>6} {'Total PnL':>10} {'Days+':>6} {'Days-':>6} {'Consistency':>12} | Per-Day Breakdown")
print("  " + "-" * 90)

for hr in range(24):
    if hr not in hourly_all:
        continue
    d = hourly_all[hr]
    pos = d["days_positive"]
    neg = d["days_negative"]
    total_days = pos + neg
    consistency = pos / total_days * 100 if total_days > 0 else 0
    tag = "STRONG" if consistency >= 75 and d["pnl"] > 10 else "WEAK" if consistency <= 40 else "AVOID" if d["pnl"] < -20 and consistency < 50 else "OK" if d["pnl"] > 0 else "RISKY"

    day_str = ""
    for date in sorted(hourly_by_day[hr].keys()):
        dow = DOW_MAP.get(date, "?")
        val = hourly_by_day[hr][date]
        day_str += f" {dow}:${val:+.0f}"

    print(f"  {hr:02d}:00  ${d['pnl']:>+8.2f} {pos:>5} {neg:>6} {tag:>12} |{day_str}")

# ==================== SECTION 4: HOURLY WEEKDAY vs WEEKEND (TEST) ====================
print("\n" + "=" * 80)
print("4. TEST BOT — HOURLY: WEEKDAY vs WEEKEND (EST)")
print("=" * 80)

hourly_wd = defaultdict(lambda: {"pnl": 0, "count": 0})
hourly_we = defaultdict(lambda: {"pnl": 0, "count": 0})

for date, day_data in pnl["test"].items():
    hours = day_data.get("hours", {})
    bucket = hourly_we if is_weekend(date) else hourly_wd
    for h, val in hours.items():
        hr = int(h)
        bucket[hr]["pnl"] += val
        bucket[hr]["count"] += 1

print(f"\n  {'Hour':>6} | {'Weekday PnL':>12} {'#Days':>6} | {'Weekend PnL':>12} {'#Days':>6} | {'Diff':>8}")
print("  " + "-" * 70)

for hr in range(24):
    wd = hourly_wd.get(hr, {"pnl": 0, "count": 0})
    we = hourly_we.get(hr, {"pnl": 0, "count": 0})
    if wd["count"] == 0 and we["count"] == 0:
        continue
    diff = we["pnl"] - wd["pnl"]
    marker = " <<< WORSE ON WEEKEND" if diff < -10 else " >>> BETTER ON WEEKEND" if diff > 10 else ""
    print(f"  {hr:02d}:00  | ${wd['pnl']:>+10.2f} {wd['count']:>5} | ${we['pnl']:>+10.2f} {we['count']:>5} | ${diff:>+6.0f}{marker}")

wd_total = sum(v["pnl"] for v in hourly_wd.values())
we_total = sum(v["pnl"] for v in hourly_we.values())
print(f"\n  TOTALS: Weekday ${wd_total:+.2f} | Weekend ${we_total:+.2f}")

# ==================== SECTION 5: RESEARCH BOT HOURLY ====================
print("\n" + "=" * 80)
print("5. RESEARCH BOT — HOURLY ANALYSIS (EST)")
print("=" * 80)

res_hourly = defaultdict(lambda: {"pnl": 0, "day_pnls": []})
for date, day_data in pnl["research"].items():
    hours = day_data.get("hours", {})
    for h, val in hours.items():
        hr = int(h)
        res_hourly[hr]["pnl"] += val
        res_hourly[hr]["day_pnls"].append((date, val))

print(f"\n  {'Hour':>6} {'Total PnL':>10} | Per-Day Breakdown")
print("  " + "-" * 60)

for hr in range(24):
    if hr not in res_hourly:
        continue
    d = res_hourly[hr]
    day_str = ""
    for date, val in sorted(d["day_pnls"]):
        dow = DOW_MAP.get(date, "?")
        day_str += f" {dow}:${val:+.0f}"
    print(f"  {hr:02d}:00  ${d['pnl']:>+8.2f} |{day_str}")

# ==================== SECTION 6: RESEARCH BOT FILTER ANALYSIS ====================
print("\n" + "=" * 80)
print("6. RESEARCH BOT — FILTER EFFECTIVENESS")
print("=" * 80)

# From the daily snapshots CSV which has filter data
res_daily = load_csv(f"{hist_dir}/research_daily.csv") if os.path.isdir(hist_dir) else []
for row in res_daily:
    date = row.get("date", "?")
    dow = DOW_MAP.get(date, "?")
    trades = int(row.get("trades", 0))
    wins = int(row.get("wins", 0))
    losses = int(row.get("losses", 0))
    p = sf(row.get("pnl", 0))
    filt = int(row.get("filtered_out", 0)) if row.get("filtered_out") else 0
    fw = int(row.get("filtered_would_win", 0)) if row.get("filtered_would_win") else 0
    fl = int(row.get("filtered_would_lose", 0)) if row.get("filtered_would_lose") else 0
    cw = int(row.get("choppy_would_win", 0)) if row.get("choppy_would_win") else 0
    cl = int(row.get("choppy_would_lose", 0)) if row.get("choppy_would_lose") else 0
    nw = int(row.get("noleader_would_win", 0)) if row.get("noleader_would_win") else 0
    nl = int(row.get("noleader_would_lose", 0)) if row.get("noleader_would_lose") else 0

    wr = wins / trades * 100 if trades > 0 else 0
    print(f"\n  {date} ({dow}):")
    print(f"    Real trades: {trades} | W:{wins} L:{losses} | WR:{wr:.0f}% | PnL: ${p:.2f}")
    if filt > 0:
        print(f"    Volume Filters blocked: {filt} trades -> Would-Win:{fw} Would-Lose:{fl}")
        if fw + fl > 0:
            blocked_wr = fw / (fw + fl) * 100
            print(f"      Blocked trade WR: {blocked_wr:.0f}% (if these are >70% we're blocking good trades)")
    if cw + cl > 0:
        print(f"    Choppy skips: {cw+cl} -> Would-Win:{cw} Would-Lose:{cl} (WR: {cw/(cw+cl)*100:.0f}%)")
    if nw + nl > 0:
        print(f"    No-Leader skips: {nw+nl} -> Would-Win:{nw} Would-Lose:{nl} (WR: {nw/(nw+nl)*100:.0f}%)")

# ==================== SECTION 7: ENTRY PRICE ANALYSIS ====================
print("\n" + "=" * 80)
print("7. TEST BOT — ENTRY PRICE ANALYSIS")
print("=" * 80)

price_buckets = defaultdict(lambda: {"count": 0, "wins": 0, "pnl": 0.0, "weekday": 0, "wd_wins": 0, "weekend": 0, "we_wins": 0})
for row in test_trades:
    price = sf(row.get("entry_price", 0))
    p = sf(row.get("pnl", 0))
    mkt = row.get("market", "")
    if price <= 0:
        continue
    bucket = f"{int(price*100)}c"
    price_buckets[bucket]["count"] += 1
    price_buckets[bucket]["pnl"] += p
    if p > 0:
        price_buckets[bucket]["wins"] += 1

print(f"\n  {'Price':>8} {'Trades':>8} {'Wins':>6} {'WR':>6} {'PnL':>10}")
for bucket in sorted(price_buckets.keys()):
    d = price_buckets[bucket]
    wr = d["wins"] / d["count"] * 100 if d["count"] > 0 else 0
    marker = " <<<" if wr < 70 and d["count"] >= 5 else " ***" if wr >= 85 and d["count"] >= 5 else ""
    print(f"  {bucket:>8} {d['count']:>8} {d['wins']:>6} {wr:>5.0f}% ${d['pnl']:>+8.2f}{marker}")

# ==================== SECTION 8: BTC VOLATILITY CORRELATION ====================
print("\n" + "=" * 80)
print("8. BTC VOLATILITY vs BOT PERFORMANCE")
print("=" * 80)

btc_by_day = defaultdict(list)
for row in vol_log:
    ts = row.get("timestamp", "")
    btc = sf(row.get("btc_price", 0))
    if ts and btc > 0:
        day = ts[:10]
        btc_by_day[day].append(btc)

print(f"\n  {'Date':<12} {'DOW':<4} {'WkEnd':<6} {'BTC Range':>10} {'BTC Low':>10} {'BTC High':>10} {'Test PnL':>10}")
print("  " + "-" * 75)

for date in sorted(btc_by_day.keys()):
    prices = btc_by_day[date]
    if not prices:
        continue
    hi = max(prices)
    lo = min(prices)
    rng = hi - lo
    dow = DOW_MAP.get(date, "?")
    wkend = "YES" if is_weekend(date) else "no"
    test_pnl = pnl["test"].get(date, {}).get("total", 0)
    print(f"  {date:<12} {dow:<4} {wkend:<6} ${rng:>8.0f} ${lo:>8.0f} ${hi:>8.0f} ${test_pnl:>+8.2f}")

# ==================== SECTION 9: WORST LOSING STREAKS ====================
print("\n" + "=" * 80)
print("9. TEST BOT — CONSECUTIVE LOSS ANALYSIS")
print("=" * 80)

streaks = []
current_streak = 0
streak_pnl = 0
streak_start_hour = ""

for date in sorted(pnl["test"].keys()):
    hours = pnl["test"][date].get("hours", {})
    for h in sorted(hours.keys(), key=int):
        val = hours[h]
        if val < 0:
            if current_streak == 0:
                streak_start_hour = f"{date} {h}:00"
            current_streak += 1
            streak_pnl += val
        else:
            if current_streak >= 2:
                streaks.append((current_streak, streak_pnl, streak_start_hour))
            current_streak = 0
            streak_pnl = 0

if current_streak >= 2:
    streaks.append((current_streak, streak_pnl, streak_start_hour))

streaks.sort(key=lambda x: x[1])
print(f"\n  Worst losing streaks (consecutive negative hours):")
for length, loss, start in streaks[:10]:
    print(f"    {length} hours starting {start} -> ${loss:+.2f}")

# ==================== SECTION 10: RESEARCH vs TEST SIDE BY SIDE ====================
print("\n" + "=" * 80)
print("10. RESEARCH vs TEST — HOUR-BY-HOUR COMPARISON")
print("=" * 80)

print(f"\n  {'Hour':>6} | {'Test PnL':>10} | {'Res PnL':>10} | {'Diff':>8} | Notes")
print("  " + "-" * 65)

for hr in range(24):
    t_pnl = hourly_all.get(hr, {}).get("pnl", 0)
    r_pnl = res_hourly.get(hr, {}).get("pnl", 0)
    if t_pnl == 0 and r_pnl == 0:
        continue
    diff = r_pnl - t_pnl
    note = ""
    if t_pnl < -15 and r_pnl > 0:
        note = "RESEARCH SAVED"
    elif t_pnl > 15 and r_pnl < t_pnl * 0.3:
        note = "RESEARCH MISSED PROFIT"
    elif t_pnl < 0 and r_pnl < 0:
        note = "BOTH BAD"
    elif t_pnl > 0 and r_pnl > 0:
        note = "BOTH GOOD"
    print(f"  {hr:02d}:00  | ${t_pnl:>+8.2f} | ${r_pnl:>+8.2f} | ${diff:>+6.0f} | {note}")

t_total = sum(v.get("pnl", 0) for v in hourly_all.values())
r_total = sum(v.get("pnl", 0) for v in res_hourly.values())
print(f"\n  TOTALS: Test ${t_total:+.2f} | Research ${r_total:+.2f} | Diff ${r_total-t_total:+.2f}")

# ==================== SECTION 11: WHICH HOURS SHOULD WE SKIP? ====================
print("\n" + "=" * 80)
print("11. HOUR RECOMMENDATION TABLE (TEST BOT)")
print("=" * 80)

print(f"\n  Based on all {len(pnl['test'])} days of data:\n")
print(f"  {'Hour':>6} {'Total PnL':>10} {'Avg/Day':>8} {'Win Days':>9} {'Lose Days':>10} {'Verdict':>12}")
print("  " + "-" * 65)

total_saved = 0
for hr in range(24):
    if hr not in hourly_all:
        continue
    d = hourly_all[hr]
    total_days = d["days_positive"] + d["days_negative"]
    avg = d["pnl"] / total_days if total_days > 0 else 0
    win_pct = d["days_positive"] / total_days * 100 if total_days > 0 else 0

    if d["pnl"] < -15 and win_pct < 50:
        verdict = "SKIP"
        total_saved += abs(d["pnl"])
    elif d["pnl"] < -5 and win_pct < 45:
        verdict = "SKIP"
        total_saved += abs(d["pnl"])
    elif d["pnl"] < 0 and win_pct < 40:
        verdict = "SKIP"
        total_saved += abs(d["pnl"])
    elif d["pnl"] > 20 and win_pct >= 60:
        verdict = "KEEP"
    elif d["pnl"] > 0:
        verdict = "KEEP"
    else:
        verdict = "WATCH"

    print(f"  {hr:02d}:00  ${d['pnl']:>+8.2f} ${avg:>+6.2f} {d['days_positive']:>8} {d['days_negative']:>10} {verdict:>12}")

print(f"\n  If we skipped all 'SKIP' hours, estimated savings: ~${total_saved:.0f}")

# ==================== SECTION 12: SUMMARY ====================
print("\n" + "=" * 80)
print("12. EXECUTIVE SUMMARY")
print("=" * 80)

print(f"""
  TEST BOT OVERALL:
    Total PnL: ${totals['test']['pnl']:+.2f}
    Total Trades: {totals['test']['trades']}
    Overall WR: {totals['test']['wins']/totals['test']['trades']*100:.1f}%
    Weekday PnL: ${weekday_totals['test']['pnl']:+.2f}
    Weekend PnL: ${weekend_totals['test']['pnl']:+.2f}

  RESEARCH BOT OVERALL:
    Total PnL: ${totals['research']['pnl']:+.2f}
    Total Trades: {totals['research']['trades']}
    Overall WR: {totals['research']['wins']/totals['research']['trades']*100:.1f}%

  SCALP BOT (RETIRED):
    Total PnL: ${totals['scalp']['pnl']:+.2f}

  KEY PATTERNS:
    - Weekday avg daily PnL (test): ${weekday_totals['test']['pnl']/3:+.2f}
    - Weekend avg daily PnL (test): ${weekend_totals['test']['pnl']/2:+.2f}
    - Weekend trades about the same volume but lower WR and more SL hits
""")

print("=" * 80)
print("ANALYSIS COMPLETE")
print("=" * 80)
