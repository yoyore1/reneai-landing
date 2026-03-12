"""
Simulate tuned guard parameters across March 10+11 to find the balanced sweet spot.
Uses the test bot's REAL trades as ground truth, then simulates what would happen
if a guard skipped certain ones.
"""
import csv
import os
from datetime import datetime, timezone
from collections import defaultdict, deque

HISTORY = os.path.expanduser("~/reneai-landing/history")

# EDT = UTC-4, so midnight EDT = 04:00 UTC
START = datetime(2026, 3, 7, 4, 0, tzinfo=timezone.utc)    # Mar 7 00:00 EDT
END = datetime(2026, 3, 12, 3, 59, tzinfo=timezone.utc)    # Mar 11 23:59 EDT

def load_trades(bot_name):
    path = os.path.join(HISTORY, f"{bot_name}_trades.csv")
    trades = []
    with open(path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                ts = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if START <= ts <= END:
                    row["_ts"] = ts
                    row["_pnl"] = float(row.get("pnl", 0))
                    row["_entry"] = float(row.get("entry_price", 0))
                    row["_exit"] = float(row.get("exit_price", 0))
                    row["_is_real"] = not row.get("type", "").startswith("phantom")
                    row["_is_choppy"] = "choppy" in row.get("type", "")
                    row["_is_noleader"] = "no_leader" in row.get("type", "")
                    trades.append(row)
            except Exception:
                continue
    return sorted(trades, key=lambda t: t["_ts"])


class SimGuard:
    """Simulated manipulation guard with tunable parameters."""

    def __init__(self, win_streak_thresh=5, alt_thresh=4, choppy_thresh=0.30,
                 signals_required=2, cooldown=2, entry_gate=1.0,
                 extreme_alt=5):
        self.win_streak_thresh = win_streak_thresh
        self.alt_thresh = alt_thresh
        self.choppy_thresh = choppy_thresh
        self.signals_required = signals_required
        self.cooldown = cooldown
        self.entry_gate = entry_gate  # if entry >= this, NEVER skip
        self.extreme_alt = extreme_alt

        self._history = deque(maxlen=20)
        self._consec_wins = 0
        self._cooldown_remaining = 0

    def record(self, side, won, was_choppy=False, was_noleader=False):
        self._history.append({
            "side": side, "won": won,
            "choppy": was_choppy, "noleader": was_noleader
        })
        if won:
            self._consec_wins += 1
        else:
            self._consec_wins = 0

    def should_skip(self, entry_price):
        # Entry gate: high-confidence entries always trade
        if entry_price >= self.entry_gate:
            return False

        # Cooldown
        if self._cooldown_remaining > 0:
            self._cooldown_remaining -= 1
            return True

        # Calculate signals
        signals = 0
        recent = list(self._history)

        # Side alternation
        if len(recent) >= 6:
            last6 = recent[-6:]
            alts = sum(1 for i in range(1, len(last6)) if last6[i]["side"] != last6[i-1]["side"])
            if alts >= self.alt_thresh:
                signals += 1
            if alts >= self.extreme_alt:
                self._cooldown_remaining = self.cooldown
                return True

        # Win streak
        if self._consec_wins >= self.win_streak_thresh:
            signals += 1

        # Choppy rate
        if len(recent) >= 10:
            last10 = recent[-10:]
            choppy_count = sum(1 for r in last10 if r["choppy"])
            if choppy_count / 10 >= self.choppy_thresh:
                signals += 1

        if signals >= self.signals_required:
            self._cooldown_remaining = self.cooldown
            return True

        return False


def simulate(trades, guard_params, label=""):
    """Run simulation with given guard parameters. Returns daily PnL."""
    guard = SimGuard(**guard_params)

    real_trades = [t for t in trades if t["_is_real"]]
    choppy = [t for t in trades if t["_is_choppy"]]
    noleader = [t for t in trades if t["_is_noleader"]]

    # Feed choppy/noleader as phantom context first chronologically
    all_events = []
    for t in trades:
        all_events.append(t)
    all_events.sort(key=lambda t: t["_ts"])

    total_pnl = 0.0
    trades_taken = 0
    trades_skipped = 0
    wins = 0
    losses = 0
    skipped_would_win = 0
    skipped_would_lose = 0
    daily = defaultdict(lambda: {"pnl": 0.0, "trades": 0, "wins": 0, "losses": 0,
                                  "skipped": 0, "skip_w": 0, "skip_l": 0})

    for t in all_events:
        utc_ts = t["_ts"]
        edt_day = utc_ts.day
        if utc_ts.hour < 4:
            edt_day -= 1
        day = f"Mar{edt_day:02d}"

        if t["_is_choppy"]:
            guard.record(t.get("side", "Up"), t["_pnl"] >= 0, was_choppy=True)
            continue
        if t["_is_noleader"]:
            guard.record(t.get("side", "Up"), t["_pnl"] >= 0, was_noleader=True)
            continue

        if not t["_is_real"]:
            continue

        # This is a real trade the test bot took. Should our guard skip it?
        entry = t["_entry"]
        if guard.should_skip(entry):
            trades_skipped += 1
            daily[day]["skipped"] += 1
            if t["_pnl"] >= 0:
                skipped_would_win += 1
                daily[day]["skip_w"] += 1
            else:
                skipped_would_lose += 1
                daily[day]["skip_l"] += 1
            # Still record the result for the guard's history
            guard.record(t.get("side", "Up"), t["_pnl"] >= 0)
        else:
            total_pnl += t["_pnl"]
            trades_taken += 1
            daily[day]["pnl"] += t["_pnl"]
            daily[day]["trades"] += 1
            if t["_pnl"] >= 0:
                wins += 1
                daily[day]["wins"] += 1
            else:
                losses += 1
                daily[day]["losses"] += 1
            guard.record(t.get("side", "Up"), t["_pnl"] >= 0)

    wr = (wins / trades_taken * 100) if trades_taken else 0
    return {
        "label": label,
        "total_pnl": total_pnl,
        "trades": trades_taken,
        "skipped": trades_skipped,
        "wins": wins, "losses": losses, "wr": wr,
        "skip_w": skipped_would_win, "skip_l": skipped_would_lose,
        "daily": dict(daily),
    }


DAYS = ["Mar07", "Mar08", "Mar09", "Mar10", "Mar11"]

def print_result(r, compact=False):
    empty = {"pnl": 0, "trades": 0, "wins": 0, "losses": 0, "skipped": 0, "skip_w": 0, "skip_l": 0}
    day_data = {d: r["daily"].get(d, empty) for d in DAYS}

    if compact:
        parts = []
        for d in DAYS:
            dd = day_data[d]
            if dd["trades"] > 0:
                parts.append(f"{d[-2:]}:${dd['pnl']:>+.0f}")
            else:
                parts.append(f"{d[-2:]}:  --")
        day_str = " | ".join(parts)
        print(f"  {r['label']:<42s} | Tot:${r['total_pnl']:>+7.0f} | {day_str} | Sk:{r['skipped']}({r['skip_w']}W/{r['skip_l']}L)")
    else:
        parts = []
        for d in DAYS:
            dd = day_data[d]
            if dd["trades"] > 0:
                wr = (dd["wins"] / dd["trades"] * 100) if dd["trades"] else 0
                parts.append(f"{d}: ${dd['pnl']:>+7.2f} ({dd['trades']}T {wr:.0f}%)")
        day_str = " | ".join(parts)
        print(f"  {r['label']:<42s} | Total: ${r['total_pnl']:>+7.2f} | {day_str} | Skip:{r['skipped']}(W:{r['skip_w']} L:{r['skip_l']})")


if __name__ == "__main__":
    trades = load_trades("test")
    print("=" * 130)
    print("  BALANCED BOT SIMULATION — Testing guard parameters across March 10 (bad) + March 11 (good)")
    print("=" * 130)

    real_count = len([t for t in trades if t["_is_real"]])
    print(f"  Test bot baseline: {real_count} real trades over both days\n")

    # Baseline: no guard at all (= test bot)
    baseline = simulate(trades, {
        "win_streak_thresh": 999, "alt_thresh": 999, "choppy_thresh": 1.0,
        "signals_required": 3, "cooldown": 0, "entry_gate": 1.0
    }, "NO GUARD (test bot)")
    print_result(baseline)

    # Current guard settings
    current = simulate(trades, {
        "win_streak_thresh": 5, "alt_thresh": 4, "choppy_thresh": 0.30,
        "signals_required": 2, "cooldown": 2, "entry_gate": 1.0
    }, "CURRENT GUARD (research bot)")
    print_result(current)

    print(f"\n  --- ENTRY GATE TUNING (keep current signals, add entry gate) ---")
    for gate in [0.76, 0.78, 0.80, 0.82, 0.85]:
        r = simulate(trades, {
            "win_streak_thresh": 5, "alt_thresh": 4, "choppy_thresh": 0.30,
            "signals_required": 2, "cooldown": 2, "entry_gate": gate
        }, f"Current + entry gate >= {gate:.2f}")
        print_result(r)

    print(f"\n  --- COOLDOWN TUNING (keep current signals, vary cooldown) ---")
    for cd in [0, 1, 2, 3]:
        r = simulate(trades, {
            "win_streak_thresh": 5, "alt_thresh": 4, "choppy_thresh": 0.30,
            "signals_required": 2, "cooldown": cd, "entry_gate": 1.0
        }, f"Current + cooldown={cd}")
        print_result(r)

    print(f"\n  --- WIN STREAK THRESHOLD (vary streak, keep rest) ---")
    for ws in [4, 5, 6, 7, 8, 10]:
        r = simulate(trades, {
            "win_streak_thresh": ws, "alt_thresh": 4, "choppy_thresh": 0.30,
            "signals_required": 2, "cooldown": 2, "entry_gate": 1.0
        }, f"Win streak >= {ws}")
        print_result(r)

    print(f"\n  --- SIGNAL REQUIREMENT (require more signals to trigger) ---")
    for sig in [1, 2, 3]:
        r = simulate(trades, {
            "win_streak_thresh": 5, "alt_thresh": 4, "choppy_thresh": 0.30,
            "signals_required": sig, "cooldown": 2, "entry_gate": 1.0
        }, f"Require {sig}/3 signals")
        print_result(r)

    print(f"\n  --- COMBINED TUNING (best combos) ---")
    combos = [
        {"win_streak_thresh": 7, "alt_thresh": 4, "choppy_thresh": 0.30,
         "signals_required": 2, "cooldown": 1, "entry_gate": 0.80,
         "_label": "streak>=7 + cd=1 + gate>=0.80"},
        {"win_streak_thresh": 7, "alt_thresh": 4, "choppy_thresh": 0.30,
         "signals_required": 2, "cooldown": 1, "entry_gate": 0.78,
         "_label": "streak>=7 + cd=1 + gate>=0.78"},
        {"win_streak_thresh": 6, "alt_thresh": 4, "choppy_thresh": 0.30,
         "signals_required": 2, "cooldown": 1, "entry_gate": 0.80,
         "_label": "streak>=6 + cd=1 + gate>=0.80"},
        {"win_streak_thresh": 7, "alt_thresh": 5, "choppy_thresh": 0.30,
         "signals_required": 2, "cooldown": 1, "entry_gate": 0.78,
         "_label": "streak>=7 + alt>=5 + cd=1 + gate>=0.78"},
        {"win_streak_thresh": 8, "alt_thresh": 4, "choppy_thresh": 0.30,
         "signals_required": 2, "cooldown": 0, "entry_gate": 0.78,
         "_label": "streak>=8 + cd=0 + gate>=0.78"},
        {"win_streak_thresh": 7, "alt_thresh": 4, "choppy_thresh": 0.40,
         "signals_required": 2, "cooldown": 1, "entry_gate": 0.78,
         "_label": "streak>=7 + choppy>=40% + cd=1 + gate>=0.78"},
        {"win_streak_thresh": 6, "alt_thresh": 4, "choppy_thresh": 0.30,
         "signals_required": 2, "cooldown": 0, "entry_gate": 0.80,
         "_label": "streak>=6 + cd=0 + gate>=0.80"},
        {"win_streak_thresh": 7, "alt_thresh": 4, "choppy_thresh": 0.30,
         "signals_required": 2, "cooldown": 0, "entry_gate": 0.80,
         "_label": "streak>=7 + cd=0 + gate>=0.80"},
        {"win_streak_thresh": 7, "alt_thresh": 5, "choppy_thresh": 0.40,
         "signals_required": 2, "cooldown": 1, "entry_gate": 0.78,
         "_label": "streak>=7 + alt>=5 + choppy>=40% + cd=1 + gate>=0.78"},
        {"win_streak_thresh": 5, "alt_thresh": 4, "choppy_thresh": 0.30,
         "signals_required": 2, "cooldown": 1, "entry_gate": 0.78,
         "_label": "CURRENT + cd=1 + gate>=0.78"},
        {"win_streak_thresh": 5, "alt_thresh": 4, "choppy_thresh": 0.30,
         "signals_required": 2, "cooldown": 0, "entry_gate": 0.78,
         "_label": "CURRENT + cd=0 + gate>=0.78"},
        {"win_streak_thresh": 5, "alt_thresh": 5, "choppy_thresh": 0.30,
         "signals_required": 2, "cooldown": 1, "entry_gate": 0.80,
         "_label": "CURRENT + alt>=5 + cd=1 + gate>=0.80"},
    ]
    results = []
    for c in combos:
        label = c.pop("_label")
        r = simulate(trades, c, label)
        results.append(r)
        print_result(r)

    # Rankings
    all_results = [baseline, current] + results

    print(f"\n  {'='*140}")
    print(f"  TOP 5 BY TOTAL PNL (all 5 days)")
    print(f"  {'='*140}")
    all_results.sort(key=lambda r: r["total_pnl"], reverse=True)
    for r in all_results[:5]:
        print_result(r, compact=True)

    print(f"\n  {'='*140}")
    print(f"  TOP 5 BY BALANCE (highest worst-day PnL)")
    print(f"  {'='*140}")
    def min_day(r):
        empty = {"pnl": 0, "trades": 0}
        return min(r["daily"].get(d, empty)["pnl"] for d in DAYS if r["daily"].get(d, empty)["trades"] > 0) if any(r["daily"].get(d, empty)["trades"] > 0 for d in DAYS) else 0
    all_results.sort(key=min_day, reverse=True)
    for r in all_results[:5]:
        worst = min_day(r)
        print_result(r, compact=True)
        print(f"    ^ worst day = ${worst:+.2f}")

    print(f"\n  {'='*140}")
    print(f"  DAILY PNL TABLE (detailed)")
    print(f"  {'='*140}")
    # Sort by total again
    all_results.sort(key=lambda r: r["total_pnl"], reverse=True)
    empty = {"pnl": 0, "trades": 0, "wins": 0, "losses": 0}
    header = f"  {'Config':<42s} |"
    for d in DAYS:
        header += f" {d:>10} |"
    header += f" {'TOTAL':>10}"
    print(header)
    print(f"  {'-'*42}-+" + ("-"*12 + "+") * len(DAYS) + "-"*10)
    for r in all_results[:8]:
        line = f"  {r['label']:<42s} |"
        for d in DAYS:
            dd = r["daily"].get(d, empty)
            if dd["trades"] > 0:
                line += f"  ${dd['pnl']:>+7.2f} |"
            else:
                line += f"       -- |"
        line += f"  ${r['total_pnl']:>+7.2f}"
        print(line)
