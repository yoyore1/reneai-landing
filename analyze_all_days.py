import json, csv, os
from collections import defaultdict
from datetime import datetime

DATA = "/tmp/analysis_mar7"

def load_csv(name):
    path = f"{DATA}/{name}"
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return list(csv.DictReader(f))

def load_json(name):
    path = f"{DATA}/{name}"
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return json.load(f)

def safe_float(v, default=0.0):
    try:
        return float(v)
    except (ValueError, TypeError):
        return default

def get_date_from_question(q):
    """Extract date from market question like 'Bitcoin Up or Down - March 7, 12:40AM-12'"""
    if not q:
        return "unknown"
    try:
        if "March" in q:
            parts = q.split("March ")
            if len(parts) > 1:
                day = parts[1].split(",")[0].strip()
                return f"Mar-{int(day)}"
    except:
        pass
    return "unknown"

def get_hour_from_question(q):
    """Extract hour from market question"""
    if not q:
        return -1
    try:
        if "," in q:
            time_part = q.split(",")[1].strip()
            hour_str = time_part.split(":")[0]
            hour = int(hour_str)
            if "PM" in time_part.upper() and hour != 12:
                hour += 12
            elif "AM" in time_part.upper() and hour == 12:
                hour = 0
            return hour
    except:
        pass
    return -1

def get_day_of_week(date_str):
    """Mar-4 = Tuesday, Mar-5 = Wednesday, Mar-6 = Thursday, Mar-7 = Friday"""
    days = {"Mar-4": "Tue", "Mar-5": "Wed", "Mar-6": "Thu", "Mar-7": "Fri"}
    return days.get(date_str, "?")

print("=" * 70)
print("COMPREHENSIVE MULTI-DAY ANALYSIS")
print("=" * 70)

# ===== LOAD ALL DATA =====
test_trades = load_csv("test_trades.csv")
research_trades = load_csv("research_trades.csv")
scalp_trades = load_csv("scalp_trades.csv")
vol_log = load_csv("volume_log.csv")
trades_log = load_csv("trades_log.csv")
daily_csvs = {
    "test": load_csv("test_daily.csv"),
    "research": load_csv("research_daily.csv"),
    "scalp": load_csv("scalp_daily.csv"),
    "official": load_csv("official_daily.csv"),
}

# Load PnL calendars
pnl_data = {}
for bot in ["test", "official", "research", "scalp"]:
    d = load_json(f"pnl_{bot}.json")
    pnl_data[bot] = d

# Load current states
states = {}
for bot in ["test", "official", "research", "scalp"]:
    states[bot] = load_json(f"state_{bot}.json")

# ===== PNL CALENDARS =====
print("\n" + "=" * 70)
print("PNL CALENDARS (All Days)")
print("=" * 70)

for bot in ["test", "official", "research", "scalp"]:
    cal = pnl_data[bot].get("calendar", {})
    if cal:
        print(f"\n  {bot.upper()} Bot Calendar:")
        for date in sorted(cal.keys()):
            dow = ""
            try:
                dt = datetime.strptime(date, "%Y-%m-%d")
                dow = dt.strftime("%a")
            except:
                pass
            print(f"    {date} ({dow}): ${cal[date]:.2f}")
        total = sum(cal.values())
        print(f"    TOTAL: ${total:.2f}")

# ===== CURRENT SESSION STATS =====
print("\n" + "=" * 70)
print("CURRENT SESSION STATS (from live state)")
print("=" * 70)

for bot in ["test", "official", "research", "scalp"]:
    s = states.get(bot, {})
    if not s:
        continue
    print(f"\n  {bot.upper()} Bot:")
    for k in ["trades", "wins", "losses", "total_pnl", "tp_hits", "sl_hits", "skipped_choppy"]:
        if k in s:
            print(f"    {k}: {s[k]}")
    hp = s.get("hourly_pnl", {})
    if hp:
        print(f"    Hourly PnL: {hp}")

# ===== TEST BOT: DAY-BY-DAY FROM TRADE CSV =====
print("\n" + "=" * 70)
print("TEST BOT - DAY-BY-DAY BREAKDOWN (from trade history CSV)")
print("=" * 70)

test_by_day = defaultdict(lambda: {"trades": 0, "wins": 0, "losses": 0, "pnl": 0.0, "tp": 0, "sl": 0, "hours": defaultdict(float)})
for row in test_trades:
    date = get_date_from_question(row.get("market", ""))
    pnl = safe_float(row.get("pnl", 0))
    hour = get_hour_from_question(row.get("market", ""))
    exit_r = row.get("exit_reason", "")

    test_by_day[date]["trades"] += 1
    test_by_day[date]["pnl"] += pnl
    if pnl > 0:
        test_by_day[date]["wins"] += 1
    else:
        test_by_day[date]["losses"] += 1
    if "tp" in exit_r.lower():
        test_by_day[date]["tp"] += 1
    elif "sl" in exit_r.lower():
        test_by_day[date]["sl"] += 1
    if hour >= 0:
        test_by_day[date]["hours"][hour] += pnl

for date in sorted(test_by_day.keys()):
    d = test_by_day[date]
    wr = d["wins"] / d["trades"] * 100 if d["trades"] else 0
    dow = get_day_of_week(date)
    print(f"\n  {date} ({dow}):")
    print(f"    Trades: {d['trades']} | W: {d['wins']} L: {d['losses']} | WR: {wr:.0f}%")
    print(f"    PnL: ${d['pnl']:.2f} | TP: {d['tp']} SL: {d['sl']}")
    print(f"    R:R = {d['wins']}:{d['losses']} = 1:{d['losses']/d['wins']:.1f}" if d["wins"] else "    R:R = N/A")
    if d["hours"]:
        print(f"    Hourly PnL (EST):")
        for h in sorted(d["hours"].keys()):
            bar = "+" * int(abs(d["hours"][h]) / 2) if d["hours"][h] > 0 else "-" * int(abs(d["hours"][h]) / 2)
            print(f"      {h:02d}:00  ${d['hours'][h]:+.2f}  {bar}")

# ===== RESEARCH BOT: DAY-BY-DAY =====
print("\n" + "=" * 70)
print("RESEARCH BOT - DAY-BY-DAY BREAKDOWN")
print("=" * 70)

research_by_day = defaultdict(lambda: {"real": 0, "real_w": 0, "real_l": 0, "real_pnl": 0.0,
    "filtered": 0, "filt_w": 0, "filt_l": 0,
    "choppy": 0, "chop_w": 0, "chop_l": 0,
    "noleader": 0, "nl_w": 0, "nl_l": 0,
    "volguard": 0, "vg_w": 0, "vg_l": 0,
    "hours": defaultdict(float)})

for row in research_trades:
    date = get_date_from_question(row.get("market", ""))
    pnl = safe_float(row.get("pnl", 0))
    hour = get_hour_from_question(row.get("market", ""))
    ttype = row.get("trade_type", "real")
    won = pnl > 0

    rd = research_by_day[date]
    if ttype == "real":
        rd["real"] += 1
        rd["real_pnl"] += pnl
        if won: rd["real_w"] += 1
        else: rd["real_l"] += 1
        if hour >= 0:
            rd["hours"][hour] += pnl
    elif ttype == "filtered_phantom":
        rd["filtered"] += 1
        if won: rd["filt_w"] += 1
        else: rd["filt_l"] += 1
    elif ttype == "choppy_phantom":
        rd["choppy"] += 1
        if won: rd["chop_w"] += 1
        else: rd["chop_l"] += 1
    elif ttype == "noleader_phantom":
        rd["noleader"] += 1
        if won: rd["nl_w"] += 1
        else: rd["nl_l"] += 1
    elif ttype == "volguard_phantom":
        rd["volguard"] += 1
        if won: rd["vg_w"] += 1
        else: rd["vg_l"] += 1

for date in sorted(research_by_day.keys()):
    d = research_by_day[date]
    wr = d["real_w"] / d["real"] * 100 if d["real"] else 0
    dow = get_day_of_week(date)
    print(f"\n  {date} ({dow}):")
    print(f"    REAL trades: {d['real']} | W: {d['real_w']} L: {d['real_l']} | WR: {wr:.0f}% | PnL: ${d['real_pnl']:.2f}")
    if d["filtered"]:
        print(f"    Filtered (skipped): {d['filtered']} | Would-Win: {d['filt_w']} Would-Lose: {d['filt_l']}")
    if d["choppy"]:
        print(f"    Choppy (skipped):   {d['choppy']} | Would-Win: {d['chop_w']} Would-Lose: {d['chop_l']}")
    if d["noleader"]:
        print(f"    No-Leader (skipped):{d['noleader']} | Would-Win: {d['nl_w']} Would-Lose: {d['nl_l']}")
    if d["volguard"]:
        print(f"    VolGuard (skipped): {d['volguard']} | Would-Win: {d['vg_w']} Would-Lose: {d['vg_l']}")
    if d["hours"]:
        print(f"    Hourly PnL (EST):")
        for h in sorted(d["hours"].keys()):
            print(f"      {h:02d}:00  ${d['hours'][h]:+.2f}")

# ===== SCALP BOT: DAY-BY-DAY =====
print("\n" + "=" * 70)
print("SCALP BOT - DAY-BY-DAY BREAKDOWN")
print("=" * 70)

scalp_by_day = defaultdict(lambda: {"trades": 0, "wins": 0, "losses": 0, "pnl": 0.0, "tp": 0, "sl": 0, "time_stop": 0, "hours": defaultdict(float)})
for row in scalp_trades:
    date = get_date_from_question(row.get("market", ""))
    pnl = safe_float(row.get("pnl", 0))
    hour = get_hour_from_question(row.get("market", ""))
    exit_r = row.get("exit_reason", "")

    scalp_by_day[date]["trades"] += 1
    scalp_by_day[date]["pnl"] += pnl
    if pnl > 0:
        scalp_by_day[date]["wins"] += 1
    else:
        scalp_by_day[date]["losses"] += 1
    if "tp" in exit_r.lower():
        scalp_by_day[date]["tp"] += 1
    elif "sl" in exit_r.lower():
        scalp_by_day[date]["sl"] += 1
    elif "time" in exit_r.lower():
        scalp_by_day[date]["time_stop"] += 1
    if hour >= 0:
        scalp_by_day[date]["hours"][hour] += pnl

for date in sorted(scalp_by_day.keys()):
    d = scalp_by_day[date]
    wr = d["wins"] / d["trades"] * 100 if d["trades"] else 0
    dow = get_day_of_week(date)
    print(f"\n  {date} ({dow}):")
    print(f"    Trades: {d['trades']} | W: {d['wins']} L: {d['losses']} | WR: {wr:.0f}%")
    print(f"    PnL: ${d['pnl']:.2f} | TP: {d['tp']} SL: {d['sl']} Time-Stop: {d['time_stop']}")

# ===== CROSS-DAY COMPARISON TABLE =====
print("\n" + "=" * 70)
print("CROSS-DAY COMPARISON TABLE")
print("=" * 70)

all_dates = sorted(set(list(test_by_day.keys()) + list(research_by_day.keys()) + list(scalp_by_day.keys())))
all_dates = [d for d in all_dates if d != "unknown"]

print(f"\n  {'Day':<12} {'DOW':<5} {'Test PnL':>10} {'Test WR':>8} {'Res PnL':>10} {'Res WR':>8} {'Scalp PnL':>10} {'Scalp WR':>8}")
print(f"  {'-'*12} {'-'*5} {'-'*10} {'-'*8} {'-'*10} {'-'*8} {'-'*10} {'-'*8}")

totals = {"test": 0, "research": 0, "scalp": 0}
for date in all_dates:
    dow = get_day_of_week(date)
    t = test_by_day.get(date, {"pnl": 0, "wins": 0, "trades": 0})
    r = research_by_day.get(date, {"real_pnl": 0, "real_w": 0, "real": 0})
    s = scalp_by_day.get(date, {"pnl": 0, "wins": 0, "trades": 0})

    t_wr = t["wins"] / t["trades"] * 100 if t.get("trades") else 0
    r_wr = r["real_w"] / r["real"] * 100 if r.get("real") else 0
    s_wr = s["wins"] / s["trades"] * 100 if s.get("trades") else 0

    t_pnl = t.get("pnl", 0) or t.get("real_pnl", 0)
    r_pnl = r.get("real_pnl", 0)
    s_pnl = s.get("pnl", 0)

    totals["test"] += t_pnl
    totals["research"] += r_pnl
    totals["scalp"] += s_pnl

    print(f"  {date:<12} {dow:<5} ${t_pnl:>+8.2f} {t_wr:>7.0f}% ${r_pnl:>+8.2f} {r_wr:>7.0f}% ${s_pnl:>+8.2f} {s_wr:>7.0f}%")

print(f"  {'TOTAL':<12} {'':5} ${totals['test']:>+8.2f} {'':>8} ${totals['research']:>+8.2f} {'':>8} ${totals['scalp']:>+8.2f} {'':>8}")

# ===== HOURLY PATTERNS ACROSS ALL DAYS =====
print("\n" + "=" * 70)
print("TEST BOT - HOURLY PATTERNS (AGGREGATED ACROSS ALL DAYS)")
print("=" * 70)

hourly_agg = defaultdict(lambda: {"pnl": 0, "trades": 0, "wins": 0})
for row in test_trades:
    hour = get_hour_from_question(row.get("market", ""))
    pnl = safe_float(row.get("pnl", 0))
    if hour >= 0:
        hourly_agg[hour]["pnl"] += pnl
        hourly_agg[hour]["trades"] += 1
        if pnl > 0:
            hourly_agg[hour]["wins"] += 1

print(f"\n  {'Hour':>6} {'Trades':>8} {'Wins':>6} {'WR':>6} {'PnL':>10} {'Avg PnL':>10}")
for h in sorted(hourly_agg.keys()):
    d = hourly_agg[h]
    wr = d["wins"] / d["trades"] * 100 if d["trades"] else 0
    avg = d["pnl"] / d["trades"] if d["trades"] else 0
    bar = "+" * int(max(0, d["pnl"]) / 3) if d["pnl"] > 0 else "-" * int(abs(min(0, d["pnl"])) / 3)
    print(f"  {h:02d}:00  {d['trades']:>6} {d['wins']:>6} {wr:>5.0f}% ${d['pnl']:>+8.2f} ${avg:>+8.2f}  {bar}")

# ===== HOURLY PATTERNS PER DAY (TEST BOT) =====
print("\n" + "=" * 70)
print("TEST BOT - HOURLY WIN RATE BY DAY")
print("=" * 70)

hourly_by_day = defaultdict(lambda: defaultdict(lambda: {"trades": 0, "wins": 0, "pnl": 0.0}))
for row in test_trades:
    date = get_date_from_question(row.get("market", ""))
    hour = get_hour_from_question(row.get("market", ""))
    pnl = safe_float(row.get("pnl", 0))
    if hour >= 0 and date != "unknown":
        hourly_by_day[date][hour]["trades"] += 1
        hourly_by_day[date][hour]["pnl"] += pnl
        if pnl > 0:
            hourly_by_day[date][hour]["wins"] += 1

hours_seen = sorted(set(h for d in hourly_by_day.values() for h in d.keys()))
header = f"  {'Hour':>6}"
for date in sorted(hourly_by_day.keys()):
    if date == "unknown":
        continue
    dow = get_day_of_week(date)
    header += f"  {date}({dow})"
print(header)

for h in hours_seen:
    line = f"  {h:02d}:00"
    for date in sorted(hourly_by_day.keys()):
        if date == "unknown":
            continue
        d = hourly_by_day[date].get(h, {"trades": 0, "wins": 0, "pnl": 0})
        if d["trades"] > 0:
            wr = d["wins"] / d["trades"] * 100
            line += f"  {d['wins']}/{d['trades']}={wr:.0f}%${d['pnl']:+.0f}"
        else:
            line += f"  {'---':>14}"
    print(line)

# ===== ENTRY PRICE ANALYSIS =====
print("\n" + "=" * 70)
print("TEST BOT - ENTRY PRICE ANALYSIS (ALL DAYS)")
print("=" * 70)

price_buckets = defaultdict(lambda: {"trades": 0, "wins": 0, "pnl": 0.0})
for row in test_trades:
    price = safe_float(row.get("entry_price", 0))
    if price > 0:
        bucket = f"{int(price*100)}c"
        price_buckets[bucket]["trades"] += 1
        price_buckets[bucket]["pnl"] += safe_float(row.get("pnl", 0))
        if safe_float(row.get("pnl", 0)) > 0:
            price_buckets[bucket]["wins"] += 1

print(f"\n  {'Price':>8} {'Trades':>8} {'Wins':>6} {'WR':>6} {'PnL':>10}")
for bucket in sorted(price_buckets.keys()):
    d = price_buckets[bucket]
    wr = d["wins"] / d["trades"] * 100 if d["trades"] else 0
    print(f"  {bucket:>8} {d['trades']:>8} {d['wins']:>6} {wr:>5.0f}% ${d['pnl']:>+8.2f}")

# ===== BTC VOLATILITY CORRELATION =====
print("\n" + "=" * 70)
print("BTC VOLATILITY vs BOT PERFORMANCE (from volume_log)")
print("=" * 70)

btc_by_day = defaultdict(list)
for row in vol_log:
    ts = row.get("timestamp", "")
    btc = safe_float(row.get("btc_price", 0))
    if ts and btc > 0:
        try:
            day_str = ts[:10]
            dt = datetime.strptime(day_str, "%Y-%m-%d")
            day_key = f"Mar-{dt.day}"
            btc_by_day[day_key].append(btc)
        except:
            pass

for date in sorted(btc_by_day.keys()):
    prices = btc_by_day[date]
    if prices:
        hi = max(prices)
        lo = min(prices)
        rng = hi - lo
        avg = sum(prices) / len(prices)
        t_pnl = test_by_day.get(date, {}).get("pnl", 0)
        print(f"  {date} ({get_day_of_week(date)}): BTC range ${rng:.0f} (${lo:.0f}-${hi:.0f}) avg=${avg:.0f} | Test PnL: ${t_pnl:+.2f}")

# ===== VOLUME/LIQUIDITY METRICS BY DAY =====
print("\n" + "=" * 70)
print("VOLUME/LIQUIDITY BY DAY (from trades_log)")
print("=" * 70)

vol_by_day = defaultdict(lambda: {"bid_depth": [], "depth_ratio": [], "btc_move": [], "spread": [], "wins": 0, "losses": 0})
for row in trades_log:
    date = get_date_from_question(row.get("market", ""))
    bd = safe_float(row.get("leader_bid_depth", 0))
    dr = safe_float(row.get("depth_ratio", 0))
    bm = safe_float(row.get("btc_move_dollars", 0))
    sp = safe_float(row.get("spread", 0))
    result = row.get("result", "")

    if date != "unknown":
        if bd > 0: vol_by_day[date]["bid_depth"].append(bd)
        if dr > 0: vol_by_day[date]["depth_ratio"].append(dr)
        if bm > 0: vol_by_day[date]["btc_move"].append(bm)
        if sp > 0: vol_by_day[date]["spread"].append(sp)
        if result == "win": vol_by_day[date]["wins"] += 1
        elif result == "loss": vol_by_day[date]["losses"] += 1

for date in sorted(vol_by_day.keys()):
    d = vol_by_day[date]
    avg_bd = sum(d["bid_depth"]) / len(d["bid_depth"]) if d["bid_depth"] else 0
    avg_dr = sum(d["depth_ratio"]) / len(d["depth_ratio"]) if d["depth_ratio"] else 0
    avg_bm = sum(d["btc_move"]) / len(d["btc_move"]) if d["btc_move"] else 0
    avg_sp = sum(d["spread"]) / len(d["spread"]) if d["spread"] else 0
    dow = get_day_of_week(date)
    print(f"\n  {date} ({dow}):")
    print(f"    Avg Bid Depth: ${avg_bd:.0f} | Avg Depth Ratio: {avg_dr:.1f}x | Avg BTC Move: ${avg_bm:.0f} | Avg Spread: {avg_sp:.4f}")
    print(f"    W/L: {d['wins']}/{d['losses']}")

# ===== FILTER EFFECTIVENESS (RESEARCH BOT) =====
print("\n" + "=" * 70)
print("RESEARCH BOT - FILTER EFFECTIVENESS ACROSS ALL DAYS")
print("=" * 70)

filter_stats = {"real_total": 0, "real_wins": 0, "real_pnl": 0,
    "filtered_total": 0, "filtered_would_win": 0, "filtered_would_lose": 0,
    "choppy_total": 0, "choppy_would_win": 0, "choppy_would_lose": 0,
    "noleader_total": 0, "noleader_would_win": 0, "noleader_would_lose": 0,
    "volguard_total": 0, "volguard_would_win": 0, "volguard_would_lose": 0}

for row in research_trades:
    pnl = safe_float(row.get("pnl", 0))
    ttype = row.get("trade_type", "real")
    won = pnl > 0

    if ttype == "real":
        filter_stats["real_total"] += 1
        filter_stats["real_pnl"] += pnl
        if won: filter_stats["real_wins"] += 1
    elif ttype == "filtered_phantom":
        filter_stats["filtered_total"] += 1
        if won: filter_stats["filtered_would_win"] += 1
        else: filter_stats["filtered_would_lose"] += 1
    elif ttype == "choppy_phantom":
        filter_stats["choppy_total"] += 1
        if won: filter_stats["choppy_would_win"] += 1
        else: filter_stats["choppy_would_lose"] += 1
    elif ttype == "noleader_phantom":
        filter_stats["noleader_total"] += 1
        if won: filter_stats["noleader_would_win"] += 1
        else: filter_stats["noleader_would_lose"] += 1
    elif ttype == "volguard_phantom":
        filter_stats["volguard_total"] += 1
        if won: filter_stats["volguard_would_win"] += 1
        else: filter_stats["volguard_would_lose"] += 1

f = filter_stats
real_wr = f["real_wins"] / f["real_total"] * 100 if f["real_total"] else 0
print(f"\n  REAL TRADES: {f['real_total']} | Wins: {f['real_wins']} | WR: {real_wr:.0f}% | PnL: ${f['real_pnl']:.2f}")

if f["filtered_total"]:
    saved = f["filtered_would_lose"]
    missed = f["filtered_would_win"]
    print(f"  VOLUME FILTERS blocked: {f['filtered_total']} trades")
    print(f"    Would-Win (missed profits): {missed}")
    print(f"    Would-Lose (saved losses):  {saved}")
    net_saved = saved * 12 - missed * 5  # rough estimate: avg loss ~$12, avg win ~$5
    print(f"    Estimated net saved: ~${net_saved:.0f} (assuming avg loss $12, avg win $5)")

if f["choppy_total"]:
    print(f"  CHOPPY SKIP blocked: {f['choppy_total']} trades")
    print(f"    Would-Win: {f['choppy_would_win']} | Would-Lose: {f['choppy_would_lose']}")

if f["noleader_total"]:
    print(f"  NO-LEADER SKIP blocked: {f['noleader_total']} trades")
    print(f"    Would-Win: {f['noleader_would_win']} | Would-Lose: {f['noleader_would_lose']}")

if f["volguard_total"]:
    print(f"  VOL GUARD blocked: {f['volguard_total']} trades")
    print(f"    Would-Win: {f['volguard_would_win']} | Would-Lose: {f['volguard_would_lose']}")

# ===== SCALP BOT DEEP DIVE =====
print("\n" + "=" * 70)
print("SCALP BOT - DEEP DIVE (WHY IT WENT NEGATIVE)")
print("=" * 70)

scalp_exit_reasons = defaultdict(lambda: {"count": 0, "pnl": 0.0, "wins": 0})
scalp_entry_prices = defaultdict(lambda: {"count": 0, "pnl": 0.0, "wins": 0})

for row in scalp_trades:
    exit_r = row.get("exit_reason", "unknown")
    pnl = safe_float(row.get("pnl", 0))
    price = safe_float(row.get("entry_price", 0))

    scalp_exit_reasons[exit_r]["count"] += 1
    scalp_exit_reasons[exit_r]["pnl"] += pnl
    if pnl > 0: scalp_exit_reasons[exit_r]["wins"] += 1

    if price > 0:
        bucket = f"{int(price*100)}c"
        scalp_entry_prices[bucket]["count"] += 1
        scalp_entry_prices[bucket]["pnl"] += pnl
        if pnl > 0: scalp_entry_prices[bucket]["wins"] += 1

print(f"\n  Exit Reasons:")
for reason, d in sorted(scalp_exit_reasons.items()):
    wr = d["wins"] / d["count"] * 100 if d["count"] else 0
    print(f"    {reason:<20} Count: {d['count']:>4} | WR: {wr:.0f}% | PnL: ${d['pnl']:+.2f}")

print(f"\n  Entry Prices:")
for bucket in sorted(scalp_entry_prices.keys()):
    d = scalp_entry_prices[bucket]
    wr = d["wins"] / d["count"] * 100 if d["count"] else 0
    print(f"    {bucket:<8} Count: {d['count']:>4} | WR: {wr:.0f}% | PnL: ${d['pnl']:+.2f}")

# ===== KEY FINDINGS =====
print("\n" + "=" * 70)
print("KEY FINDINGS & PATTERNS")
print("=" * 70)

# Find best/worst hours for test bot
best_hour = max(hourly_agg.items(), key=lambda x: x[1]["pnl"]) if hourly_agg else None
worst_hour = min(hourly_agg.items(), key=lambda x: x[1]["pnl"]) if hourly_agg else None

if best_hour:
    print(f"\n  Test Bot Best Hour:  {best_hour[0]:02d}:00 EST -> ${best_hour[1]['pnl']:+.2f} ({best_hour[1]['trades']} trades, {best_hour[1]['wins']}/{best_hour[1]['trades']} wins)")
if worst_hour:
    print(f"  Test Bot Worst Hour: {worst_hour[0]:02d}:00 EST -> ${worst_hour[1]['pnl']:+.2f} ({worst_hour[1]['trades']} trades, {worst_hour[1]['wins']}/{worst_hour[1]['trades']} wins)")

# Best/worst day
if test_by_day:
    valid_days = {k: v for k, v in test_by_day.items() if k != "unknown"}
    if valid_days:
        best_day = max(valid_days.items(), key=lambda x: x[1]["pnl"])
        worst_day = min(valid_days.items(), key=lambda x: x[1]["pnl"])
        print(f"\n  Test Bot Best Day:  {best_day[0]} ({get_day_of_week(best_day[0])}) -> ${best_day[1]['pnl']:+.2f} (WR: {best_day[1]['wins']/best_day[1]['trades']*100:.0f}%)")
        print(f"  Test Bot Worst Day: {worst_day[0]} ({get_day_of_week(worst_day[0])}) -> ${worst_day[1]['pnl']:+.2f} (WR: {worst_day[1]['wins']/worst_day[1]['trades']*100:.0f}%)")

# Test bot total
total_test = sum(d.get("pnl", 0) for k, d in test_by_day.items() if k != "unknown")
total_test_trades = sum(d.get("trades", 0) for k, d in test_by_day.items() if k != "unknown")
total_test_wins = sum(d.get("wins", 0) for k, d in test_by_day.items() if k != "unknown")
overall_wr = total_test_wins / total_test_trades * 100 if total_test_trades else 0
print(f"\n  Test Bot Overall: ${total_test:+.2f} over {total_test_trades} trades (WR: {overall_wr:.0f}%)")

# Research vs Test comparison
total_research = sum(d.get("real_pnl", 0) for k, d in research_by_day.items() if k != "unknown")
total_research_trades = sum(d.get("real", 0) for k, d in research_by_day.items() if k != "unknown")
print(f"  Research Bot Overall: ${total_research:+.2f} over {total_research_trades} trades")
print(f"  Difference: Test is ${total_test - total_research:+.2f} ahead")

total_scalp = sum(d.get("pnl", 0) for k, d in scalp_by_day.items() if k != "unknown")
total_scalp_trades = sum(d.get("trades", 0) for k, d in scalp_by_day.items() if k != "unknown")
print(f"  Scalp Bot Overall: ${total_scalp:+.2f} over {total_scalp_trades} trades")

print("\n" + "=" * 70)
print("ANALYSIS COMPLETE")
print("=" * 70)
