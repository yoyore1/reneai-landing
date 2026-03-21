"""
Market Health Monitor — Real-time market analysis before trade entry.

Watches tick data during the analysis window (300-60s before resolution)
and computes a composite health score for the current market. Works as
a second layer alongside the ManipulationGuard:

  Guard  → historical pattern detection (across markets)
  Health → real-time market state analysis (current market)

Eight signals:
  1. Depth Ratio:      our liquidity / opposing liquidity
  2. Opposing Bid Max: absolute level of the opposing bid
  3. Opposing Trend:   is opposing bid rising during the window?
  4. Bid Volatility:   standard deviation of our bid (stability)
  5. BTC Calm:         BTC price range during the window
  6. Depth Spike:      sudden jump in opposing depth (manipulator dumping liquidity)
  7. Late-Window Pressure: opposing bid behavior in the last portion of analysis
  8. Thin Market:      low absolute opposing depth (easy to manipulate)

Score range: roughly -12 to +5
  >= -1  → healthy, trade normally
  <= -2  → unhealthy, skip (or flip)

All thresholds are configurable. Persists decision history for analysis.
"""

import json
import logging
import math
import time
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional, Dict, List, Tuple

log = logging.getLogger("market_health")

PERSIST_DIR = Path(__file__).resolve().parent.parent / "history"


@dataclass
class TickSample:
    timestamp: float
    seconds_left: float
    our_bid: float
    opp_bid: float
    our_depth: float
    opp_depth: float
    btc_price: float


@dataclass
class HealthDecision:
    timestamp: float
    market: str
    side: str
    score: int
    depth_ratio: float
    opp_bid_max: float
    opp_trend: float
    bid_vol: float
    btc_range_pct: float
    depth_spike: float = 0.0
    late_pressure: float = 0.0
    action: str = "trade"
    result: str = ""
    pnl: float = 0.0


class MarketHealthMonitor:

    # --- Score component thresholds ---
    DEPTH_RATIO_GOOD = 2.0      # +2
    DEPTH_RATIO_OK = 1.0        # +1
    DEPTH_RATIO_BAD = 0.5       # -2

    OPP_BID_LOW = 0.25          # +1
    OPP_BID_HIGH = 0.30         # -1  (was 0.35 — tightened per tick analysis)
    OPP_BID_DANGER = 0.38       # -2  (was 0.45 — tightened per tick analysis)

    OPP_TREND_RISING = 0.03     # -1  (was 0.05 — more sensitive to rising opp)
    OPP_TREND_FALLING = -0.02   # +1

    BID_VOL_HIGH = 0.10         # -1

    BTC_CALM = 0.05             # +1
    BTC_VOLATILE = 0.10         # -1

    DEPTH_SPIKE_MULTIPLIER = 3.0  # opposing depth jumps 3x in one tick → -1
    DEPTH_SPIKE_SEVERE = 5.0      # opposing depth jumps 5x → -2

    LATE_WINDOW_CUTOFF = 120.0    # ticks with seconds_left <= this are "late"
    LATE_OPP_RISING = 0.05        # opposing bid rising in late window → -1
    LATE_OPP_SURGE = 0.10         # opposing bid surging in late window → -2

    OPP_DEPTH_THIN = 800         # avg opp depth below this → -2 (thin = easy to manipulate)
    OPP_DEPTH_LOW = 1200         # avg opp depth below this → -1

    SKIP_THRESHOLD = -2         # score <= this → skip

    MIN_TICKS = 3               # minimum ticks to compute score

    def __init__(self, bot_name: str = "",
                 skip_threshold: int = -2,
                 depth_ratio_good: float = 2.0,
                 depth_ratio_ok: float = 1.0,
                 depth_ratio_bad: float = 0.5,
                 opp_bid_low: float = 0.25,
                 opp_bid_high: float = 0.30,
                 opp_bid_danger: float = 0.38,
                 opp_trend_rising: float = 0.03,
                 opp_trend_falling: float = -0.02,
                 bid_vol_high: float = 0.10,
                 btc_calm: float = 0.05,
                 btc_volatile: float = 0.10,
                 depth_spike_multiplier: float = 3.0,
                 depth_spike_severe: float = 5.0,
                 late_window_cutoff: float = 120.0,
                 late_opp_rising: float = 0.05,
                 late_opp_surge: float = 0.10,
                 opp_depth_thin: float = 800,
                 opp_depth_low: float = 1200,
                 min_ticks: int = 3):

        self._bot_name = bot_name
        self.SKIP_THRESHOLD = skip_threshold
        self.DEPTH_RATIO_GOOD = depth_ratio_good
        self.DEPTH_RATIO_OK = depth_ratio_ok
        self.DEPTH_RATIO_BAD = depth_ratio_bad
        self.OPP_BID_LOW = opp_bid_low
        self.OPP_BID_HIGH = opp_bid_high
        self.OPP_BID_DANGER = opp_bid_danger
        self.OPP_TREND_RISING = opp_trend_rising
        self.OPP_TREND_FALLING = opp_trend_falling
        self.BID_VOL_HIGH = bid_vol_high
        self.BTC_CALM = btc_calm
        self.BTC_VOLATILE = btc_volatile
        self.DEPTH_SPIKE_MULTIPLIER = depth_spike_multiplier
        self.DEPTH_SPIKE_SEVERE = depth_spike_severe
        self.LATE_WINDOW_CUTOFF = late_window_cutoff
        self.LATE_OPP_RISING = late_opp_rising
        self.LATE_OPP_SURGE = late_opp_surge
        self.OPP_DEPTH_THIN = opp_depth_thin
        self.OPP_DEPTH_LOW = opp_depth_low
        self.MIN_TICKS = min_ticks

        self._ticks: Dict[str, List[TickSample]] = defaultdict(list)
        self._decisions: List[HealthDecision] = []
        self._persist_path = PERSIST_DIR / f"health_{bot_name}.json" if bot_name else None
        self._log_path = PERSIST_DIR / f"health_{bot_name}_log.csv" if bot_name else None

        self._total_skips = 0
        self._total_trades = 0
        self._skip_wins = 0    # skipped but would have won (false positive)
        self._skip_losses = 0  # skipped and would have lost (true catch)

        self._load()
        self._init_log()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def feed_tick(self, market_id: str, side: str,
                  yes_bid: float, no_bid: float,
                  yes_depth: float, no_depth: float,
                  btc_price: float, seconds_left: float):
        """
        Feed a tick during the analysis window.
        Call this every time a tick is collected for a market
        that is being analyzed (300s-60s before resolution).
        """
        if seconds_left < 60 or seconds_left > 300:
            return

        if side == "Up":
            our_bid, opp_bid = yes_bid, no_bid
            our_depth, opp_depth = yes_depth, no_depth
        else:
            our_bid, opp_bid = no_bid, yes_bid
            our_depth, opp_depth = no_depth, yes_depth

        self._ticks[market_id].append(TickSample(
            timestamp=time.time(),
            seconds_left=seconds_left,
            our_bid=our_bid,
            opp_bid=opp_bid,
            our_depth=our_depth,
            opp_depth=opp_depth,
            btc_price=btc_price,
        ))

    def evaluate(self, market_id: str, side: str,
                 market_name: str = "") -> Tuple[bool, int, str]:
        """
        Evaluate the health of a market based on accumulated ticks.
        Call this right before placing a buy order.

        Returns:
            (should_skip: bool, score: int, reason: str)
        """
        tick_list = self._ticks.get(market_id, [])

        if len(tick_list) < self.MIN_TICKS:
            log.debug("HEALTH: Not enough ticks for %s (%d < %d)",
                      market_name[:30], len(tick_list), self.MIN_TICKS)
            return False, 0, ""

        metrics = self._compute_metrics(tick_list)
        score = self._compute_score(metrics)

        action = "skip" if score <= self.SKIP_THRESHOLD else "trade"

        decision = HealthDecision(
            timestamp=time.time(),
            market=market_name,
            side=side,
            score=score,
            depth_ratio=metrics["depth_ratio"],
            opp_bid_max=metrics["opp_bid_last"],
            opp_trend=metrics["opp_trend"],
            bid_vol=metrics["bid_vol"],
            btc_range_pct=metrics["btc_range_pct"],
            depth_spike=metrics["depth_spike_max"],
            late_pressure=metrics["late_opp_trend"],
            action=action,
        )
        self._decisions.append(decision)

        if action == "skip":
            self._total_skips += 1
            reason = (
                f"health_monitor: score={score} "
                f"(depth={metrics['depth_ratio']:.2f} "
                f"opp_last={metrics['opp_bid_last']:.2f} "
                f"opp_max={metrics['opp_bid_max']:.2f} "
                f"opp_trend={metrics['opp_trend']:+.3f} "
                f"vol={metrics['bid_vol']:.3f} "
                f"btc={metrics['btc_range_pct']:.3f}% "
                f"spike={metrics['depth_spike_max']:.1f}x "
                f"late={metrics['late_opp_trend']:+.3f})"
            )
            log.warning("HEALTH SKIP: %s | %s", market_name[:40], reason)
            self._log_decision(decision)
            self._save()
            return True, score, reason
        else:
            self._total_trades += 1
            log.info(
                "HEALTH OK: %s | score=%d "
                "(depth=%.2f opp_last=%.2f trend=%+.3f spike=%.1fx late=%+.3f opp_d=%.0f)",
                market_name[:40], score, metrics["depth_ratio"],
                metrics["opp_bid_last"], metrics["opp_trend"],
                metrics["depth_spike_max"], metrics["late_opp_trend"],
                metrics["opp_depth_avg"])
            self._log_decision(decision)
            self._save()
            return False, score, ""

    def record_outcome(self, market_id: str, won: bool, pnl: float):
        """Record the actual outcome after a market resolves."""
        for d in reversed(self._decisions):
            if d.market and d.result == "":
                d.result = "win" if won else "loss"
                d.pnl = pnl
                if d.action == "skip":
                    if won:
                        self._skip_wins += 1
                    else:
                        self._skip_losses += 1
                break
        self._save()

    def clear_market(self, market_id: str):
        """Clean up tick data for a resolved market."""
        self._ticks.pop(market_id, None)

    @property
    def status_dict(self) -> dict:
        return {
            "total_trades": self._total_trades,
            "total_skips": self._total_skips,
            "skip_wins_avoided": self._skip_losses,
            "skip_false_positives": self._skip_wins,
            "precision": (
                f"{self._skip_losses / (self._skip_losses + self._skip_wins) * 100:.0f}%"
                if (self._skip_losses + self._skip_wins) > 0 else "n/a"
            ),
            "recent_decisions": [
                {
                    "market": d.market[:30] if d.market else "",
                    "score": d.score,
                    "action": d.action,
                    "result": d.result,
                    "pnl": d.pnl,
                }
                for d in self._decisions[-10:]
            ],
        }

    # ------------------------------------------------------------------
    # Score computation
    # ------------------------------------------------------------------

    def _compute_metrics(self, tick_list: List[TickSample]) -> dict:
        our_bids = [t.our_bid for t in tick_list if t.our_bid > 0]
        opp_bids = [t.opp_bid for t in tick_list if t.opp_bid > 0]
        our_depths = [t.our_depth for t in tick_list if t.our_depth > 0]
        opp_depths = [t.opp_depth for t in tick_list if t.opp_depth > 0]
        btcs = [t.btc_price for t in tick_list if t.btc_price > 0]

        our_depth_avg = sum(our_depths) / len(our_depths) if our_depths else 0
        opp_depth_avg = sum(opp_depths) / len(opp_depths) if opp_depths else 0

        # Signal 6: Depth spike — largest jump in opposing depth between consecutive ticks
        depth_spike_max = 0.0
        if len(opp_depths) >= 2:
            for i in range(1, len(opp_depths)):
                if opp_depths[i - 1] > 0:
                    ratio = opp_depths[i] / opp_depths[i - 1]
                    if ratio > depth_spike_max:
                        depth_spike_max = ratio

        # Signal 7: Late-window pressure — opposing bid trend in the last portion
        late_ticks = [t for t in tick_list if t.seconds_left <= self.LATE_WINDOW_CUTOFF]
        late_opp_bids = [t.opp_bid for t in late_ticks if t.opp_bid > 0]
        if len(late_opp_bids) >= 2:
            late_opp_trend = late_opp_bids[-1] - late_opp_bids[0]
        else:
            late_opp_trend = 0.0

        # Use the last opposing bid (near buy time) rather than max over the window.
        # A high opp bid early that falls by buy time is actually a good sign.
        opp_bid_last = opp_bids[-1] if opp_bids else 0
        opp_bid_max = max(opp_bids) if opp_bids else 0
        opp_trend = (opp_bids[-1] - opp_bids[0]) if len(opp_bids) >= 2 else 0

        return {
            "depth_ratio": our_depth_avg / opp_depth_avg if opp_depth_avg > 0 else 99.0,
            "opp_bid_last": opp_bid_last,
            "opp_bid_max": opp_bid_max,
            "opp_trend": opp_trend,
            "bid_vol": self._std(our_bids) if len(our_bids) > 1 else 0,
            "btc_range_pct": (
                (max(btcs) - min(btcs)) / min(btcs) * 100
                if len(btcs) >= 2 and min(btcs) > 0 else 0
            ),
            "depth_spike_max": depth_spike_max,
            "late_opp_trend": late_opp_trend,
            "opp_depth_avg": opp_depth_avg,
        }

    def _compute_score(self, m: dict) -> int:
        score = 0

        # 1. Depth ratio
        if m["depth_ratio"] >= self.DEPTH_RATIO_GOOD:
            score += 2
        elif m["depth_ratio"] >= self.DEPTH_RATIO_OK:
            score += 1
        elif m["depth_ratio"] < self.DEPTH_RATIO_BAD:
            score -= 2

        # 2. Opposing bid level — use LAST bid (near buy time), not max
        if m["opp_bid_last"] < self.OPP_BID_LOW:
            score += 1
        elif m["opp_bid_last"] > self.OPP_BID_DANGER:
            score -= 2
        elif m["opp_bid_last"] > self.OPP_BID_HIGH:
            score -= 1

        # 3. Opposing bid trend (full window)
        # Strong falling trend gets extra credit — market is resolving in our favor
        if m["opp_trend"] > self.OPP_TREND_RISING:
            score -= 1
        elif m["opp_trend"] < -0.15:
            score += 2
        elif m["opp_trend"] < self.OPP_TREND_FALLING:
            score += 1

        # 4. Our bid volatility
        if m["bid_vol"] > self.BID_VOL_HIGH:
            score -= 1

        # 5. BTC calm
        if m["btc_range_pct"] < self.BTC_CALM:
            score += 1
        elif m["btc_range_pct"] > self.BTC_VOLATILE:
            score -= 1

        # 6. Depth spike — sudden opposing liquidity dump
        if m["depth_spike_max"] >= self.DEPTH_SPIKE_SEVERE:
            score -= 2
        elif m["depth_spike_max"] >= self.DEPTH_SPIKE_MULTIPLIER:
            score -= 1

        # 7. Late-window pressure — opposing bid behavior near buy time
        if m["late_opp_trend"] >= self.LATE_OPP_SURGE:
            score -= 2
        elif m["late_opp_trend"] >= self.LATE_OPP_RISING:
            score -= 1

        # 8. Thin market — low absolute opposing depth = easy to manipulate
        if m["opp_depth_avg"] < self.OPP_DEPTH_THIN:
            score -= 2
        elif m["opp_depth_avg"] < self.OPP_DEPTH_LOW:
            score -= 1

        return score

    @staticmethod
    def _std(values: list) -> float:
        if len(values) < 2:
            return 0.0
        mean = sum(values) / len(values)
        return math.sqrt(sum((x - mean) ** 2 for x in values) / len(values))

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _save(self):
        if not self._persist_path:
            return
        try:
            PERSIST_DIR.mkdir(parents=True, exist_ok=True)
            data = {
                "total_trades": self._total_trades,
                "total_skips": self._total_skips,
                "skip_wins": self._skip_wins,
                "skip_losses": self._skip_losses,
                "decisions": [asdict(d) for d in self._decisions[-50:]],
            }
            tmp = self._persist_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2))
            tmp.replace(self._persist_path)
        except Exception as exc:
            log.warning("Health persist save failed: %s", exc)

    def _load(self):
        if not self._persist_path or not self._persist_path.exists():
            return
        try:
            data = json.loads(self._persist_path.read_text())
            self._total_trades = data.get("total_trades", 0)
            self._total_skips = data.get("total_skips", 0)
            self._skip_wins = data.get("skip_wins", 0)
            self._skip_losses = data.get("skip_losses", 0)
            for d in data.get("decisions", []):
                self._decisions.append(HealthDecision(**d))
            log.info("HEALTH: Restored %d decisions for '%s'",
                     len(self._decisions), self._bot_name)
        except Exception as exc:
            log.warning("Health persist load failed: %s", exc)

    def _init_log(self):
        if not self._log_path:
            return
        try:
            PERSIST_DIR.mkdir(parents=True, exist_ok=True)
            if not self._log_path.exists():
                self._log_path.write_text(
                    "timestamp,edt_time,market,side,score,"
                    "depth_ratio,opp_bid_max,opp_trend,bid_vol,btc_range_pct,"
                    "depth_spike,late_pressure,"
                    "action,result,pnl\n"
                )
        except Exception:
            pass

    def _log_decision(self, d: HealthDecision):
        if not self._log_path:
            return
        try:
            from datetime import datetime, timezone, timedelta
            edt = datetime.fromtimestamp(
                d.timestamp, tz=timezone(timedelta(hours=-4))
            ).strftime("%I:%M:%S %p")
            with open(self._log_path, "a") as f:
                f.write(
                    f"{d.timestamp:.0f},{edt},"
                    f"{d.market},{d.side},{d.score},"
                    f"{d.depth_ratio:.3f},{d.opp_bid_max:.3f},"
                    f"{d.opp_trend:+.4f},{d.bid_vol:.4f},{d.btc_range_pct:.4f},"
                    f"{d.depth_spike:.2f},{d.late_pressure:+.4f},"
                    f"{d.action},{d.result},{d.pnl:.2f}\n"
                )
        except Exception:
            pass
