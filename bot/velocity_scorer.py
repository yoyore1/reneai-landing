"""
Tick Velocity Scorer — analyzes how bids evolve to classify markets.

Genuine leaders build gradually (1-2c/tick over 30-60s).
Manipulation pumps spike suddenly (10-20c in 1-2 ticks).
This module scores confidence from 0-100 based on velocity, spike size,
consistency, depth changes, opposing side behavior, and BTC movement.
"""

import logging
import time

log = logging.getLogger("velocity")


class VelocityScorer:

    def __init__(self):
        self._ticks = {}   # cid -> [(time, up_bid, down_bid, up_depth, down_depth, btc)]

    def feed_tick(self, cid, up_bid, down_bid, up_depth, down_depth, btc, remaining):
        if cid not in self._ticks:
            self._ticks[cid] = []
        self._ticks[cid].append((time.time(), up_bid, down_bid, up_depth, down_depth, btc))
        if len(self._ticks[cid]) > 300:
            self._ticks[cid] = self._ticks[cid][-200:]

    def score(self, cid, leader_side):
        """Return (confidence 0-100, classification, details_dict)."""
        ticks = self._ticks.get(cid, [])
        if len(ticks) < 4:
            return 50, "insufficient", {}

        if leader_side == "Up":
            leader_bids = [t[1] for t in ticks]
            opp_bids = [t[2] for t in ticks]
            leader_depths = [t[3] for t in ticks]
            opp_depths = [t[4] for t in ticks]
        else:
            leader_bids = [t[2] for t in ticks]
            opp_bids = [t[1] for t in ticks]
            leader_depths = [t[4] for t in ticks]
            opp_depths = [t[3] for t in ticks]

        times = [t[0] for t in ticks]
        btc_prices = [t[5] for t in ticks if t[5] > 0]

        time_span = times[-1] - times[0]
        if time_span < 2:
            return 50, "too_short", {}

        # --- 1. Velocity: cents per second ---
        leader_change = leader_bids[-1] - leader_bids[0]
        velocity = abs(leader_change / time_span) * 100

        # --- 2. Max spike: largest single tick-to-tick jump ---
        tick_changes = [abs(leader_bids[i] - leader_bids[i-1]) * 100
                        for i in range(1, len(leader_bids))]
        max_spike = max(tick_changes) if tick_changes else 0

        # --- 3. Consistency: std dev of tick changes ---
        if len(tick_changes) > 1:
            mean_tc = sum(tick_changes) / len(tick_changes)
            variance = sum((tc - mean_tc)**2 for tc in tick_changes) / len(tick_changes)
            consistency = variance ** 0.5
        else:
            consistency = 0

        # --- 4. Depth trend: is leader depth growing or shrinking? ---
        n_avg = min(3, len(leader_depths))
        depth_start = sum(leader_depths[:n_avg]) / n_avg
        depth_end = sum(leader_depths[-n_avg:]) / n_avg
        depth_change = depth_end - depth_start

        # --- 5. Current depth ---
        cur_leader_depth = leader_depths[-1]
        cur_opp_depth = opp_depths[-1]
        total_depth = cur_leader_depth + cur_opp_depth

        # --- 6. Opposing side behavior ---
        opp_max = max(opp_bids)
        opp_now = opp_bids[-1]
        opp_change = opp_bids[-1] - opp_bids[0]
        opp_velocity = abs(opp_change / time_span) * 100

        # --- 7. BTC movement ---
        btc_range = 0
        if len(btc_prices) >= 2:
            btc_range = (max(btc_prices) - min(btc_prices)) / min(btc_prices) * 100

        # --- 8. Build direction: is leader still building or fading? ---
        mid = len(leader_bids) // 2
        first_half_avg = sum(leader_bids[:mid]) / max(mid, 1)
        second_half_avg = sum(leader_bids[mid:]) / max(len(leader_bids) - mid, 1)
        still_building = second_half_avg > first_half_avg

        # ============= SCORING =============
        confidence = 50

        # Velocity
        if velocity < 1.5:
            confidence += 18
        elif velocity < 3:
            confidence += 5
        elif velocity < 6:
            confidence -= 12
        else:
            confidence -= 25

        # Max spike
        if max_spike > 15:
            confidence -= 20
        elif max_spike > 10:
            confidence -= 12
        elif max_spike > 6:
            confidence -= 5
        elif max_spike < 3:
            confidence += 8

        # Consistency
        if consistency < 2:
            confidence += 8
        elif consistency > 6:
            confidence -= 15
        elif consistency > 4:
            confidence -= 8

        # Total depth
        if total_depth > 4000:
            confidence += 10
        elif total_depth > 2500:
            confidence += 5
        elif total_depth < 800:
            confidence -= 18
        elif total_depth < 1500:
            confidence -= 8

        # Depth trend
        if depth_change < -800:
            confidence -= 12
        elif depth_change < -400:
            confidence -= 5
        elif depth_change > 500:
            confidence += 5

        # Opposing side
        if opp_max > 0.35:
            confidence -= 18
        elif opp_max > 0.28:
            confidence -= 8
        elif opp_now < 0.18:
            confidence += 5

        # Opposing velocity
        if opp_velocity > 4:
            confidence -= 10

        # BTC
        if btc_range > 0.20:
            confidence -= 12
        elif btc_range > 0.10:
            confidence -= 5
        elif btc_range < 0.04:
            confidence += 5

        # Still building vs fading
        if still_building:
            confidence += 5
        else:
            confidence -= 8

        confidence = max(0, min(100, confidence))

        if confidence >= 70:
            classification = "genuine"
        elif confidence >= 45:
            classification = "uncertain"
        elif confidence >= 25:
            classification = "suspicious"
        else:
            classification = "manipulation"

        details = {
            "velocity": round(velocity, 2),
            "max_spike": round(max_spike, 1),
            "consistency": round(consistency, 2),
            "depth_change": round(depth_change, 0),
            "leader_depth": round(cur_leader_depth, 0),
            "opp_depth": round(cur_opp_depth, 0),
            "total_depth": round(total_depth, 0),
            "opp_max": round(opp_max, 3),
            "opp_velocity": round(opp_velocity, 2),
            "btc_range": round(btc_range, 4),
            "still_building": still_building,
            "ticks": len(ticks),
            "time_span": round(time_span, 1),
        }

        return confidence, classification, details

    def clear(self, cid):
        self._ticks.pop(cid, None)

    def get_opposing_velocity(self, cid, leader_side, window_secs=12):
        """Get opposing side's velocity over last N seconds (for exit decisions)."""
        ticks = self._ticks.get(cid, [])
        if len(ticks) < 2:
            return 0, 0

        now = time.time()
        recent = [(t, u, d) for t, u, d, *_ in ticks if now - t < window_secs]
        if len(recent) < 2:
            return 0, 0

        if leader_side == "Up":
            opp_bids = [d for _, _, d in recent]
        else:
            opp_bids = [u for _, u, _ in recent]

        dt = recent[-1][0] - recent[0][0]
        if dt < 1:
            return 0, opp_bids[-1]

        vel = (opp_bids[-1] - opp_bids[0]) / dt * 100
        return round(vel, 2), opp_bids[-1]
