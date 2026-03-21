"""
Prediction Engine — forecasts short-term price movement for scalp trades.

Uses current market signals (velocity, depth, resistance, opposing side,
BTC, resolution proximity) to predict expected move and set dynamic
TP / SL / time limit per trade.
"""

import logging

log = logging.getLogger("prediction")


class PredictionEngine:

    def predict_scalp(self, entry_price, opp_bid, leader_depth, opp_depth,
                      leader_ask_depth, velocity_details, remaining):
        """
        Predict expected price movement for a leader-side scalp.

        Returns dict with keys:
            target   – dynamic TP price
            sl       – dynamic SL price
            time_limit – seconds before force-exit
            confidence – 0-100
            predicted_move – raw expected move in dollars
            reasons  – list of contributing signal descriptions
        """
        velocity = velocity_details.get("velocity", 0)
        opp_velocity = velocity_details.get("opp_velocity", 0)
        opp_max = velocity_details.get("opp_max", 0)
        btc_range = velocity_details.get("btc_range", 0)
        still_building = velocity_details.get("still_building", True)
        total_depth = velocity_details.get("total_depth", 0)
        max_spike = velocity_details.get("max_spike", 0)

        room = 1.0 - entry_price
        reasons = []

        # 1. Velocity continuation: extrapolate 20 s at 40 % efficiency
        vel_move = velocity * 20 * 0.004
        if vel_move > 0.01:
            reasons.append(f"vel +{vel_move * 100:.1f}c")

        # 2. Resolution proximity — prices converge as time runs out
        if remaining < 90:
            convergence = room * max(0, (90 - remaining)) / 90 * 0.5
            if convergence > 0.02:
                reasons.append(f"conv +{convergence * 100:.1f}c")
        else:
            convergence = 0

        # 3. Opposing side direction
        if opp_velocity < -1:
            opp_factor = 0.03
            reasons.append("opp dropping")
        elif opp_velocity > 1.5:
            opp_factor = -0.04
            reasons.append("opp rising")
        else:
            opp_factor = 0

        # 4. Depth support multiplier
        if total_depth > 4000:
            depth_mult = 1.15
        elif total_depth < 1000:
            depth_mult = 0.65
            reasons.append("thin mkt")
        elif total_depth < 1800:
            depth_mult = 0.85
        else:
            depth_mult = 1.0

        # 5. Ask-side resistance cap
        if leader_ask_depth > 5000:
            resistance_cap = 0.06
            reasons.append("heavy resist")
        elif leader_ask_depth > 2500:
            resistance_cap = 0.10
        elif leader_ask_depth > 800:
            resistance_cap = 0.15
        else:
            resistance_cap = 0.25

        # 6. Building vs fading
        building_bonus = 0.02 if still_building else -0.01

        # Combine
        raw_move = (vel_move + convergence + opp_factor + building_bonus) * depth_mult
        raw_move = min(raw_move, resistance_cap)
        raw_move = min(raw_move, room * 0.7)
        raw_move = max(0, raw_move)

        # Target: 55 % of predicted move, clamped [4c, 15c]
        target_move = raw_move * 0.55
        if target_move < 0.03:
            target_move = 0.06          # fallback minimum
        target_move = max(0.04, min(0.15, target_move))

        target_price = round(entry_price + target_move, 3)

        # SL: 1.4x downside of the target move, minimum 6c
        sl_move = max(0.06, min(0.12, target_move * 1.4))
        sl_price = round(entry_price - sl_move, 3)

        # Time limit based on market speed
        if velocity > 3:
            time_limit = 20
        elif velocity > 1.5:
            time_limit = 28
        elif convergence > 0.03:
            time_limit = 40
        else:
            time_limit = 32

        # Confidence scoring
        confidence = 50
        if velocity > 1 and still_building:
            confidence += 15
        if opp_velocity < -0.5:
            confidence += 10
        if total_depth > 3000:
            confidence += 8
        if leader_ask_depth < 1000:
            confidence += 8
        if remaining < 60:
            confidence += 10
        if btc_range < 0.05:
            confidence += 5
        if velocity < 0:
            confidence -= 20
        if opp_velocity > 2:
            confidence -= 15
        if opp_max > 0.32:
            confidence -= 12
        if max_spike > 12:
            confidence -= 10
        confidence = max(0, min(100, confidence))

        log.info(
            "PREDICT SCALP: entry=%.2f move=%.3f tgt=%.3f sl=%.3f "
            "time=%ds conf=%d | %s",
            entry_price, raw_move, target_price, sl_price,
            time_limit, confidence, " | ".join(reasons) if reasons else "-",
        )

        return {
            "target": target_price,
            "sl": sl_price,
            "time_limit": time_limit,
            "confidence": confidence,
            "predicted_move": round(raw_move, 4),
            "reasons": reasons,
        }

    def predict_flip_scalp(self, entry_price, strong_bid, leader_depth, opp_depth,
                           opp_ask_depth, velocity_details, remaining):
        """
        Predict expected move for a flip scalp (buying weak side during manipulation).
        *entry_price* is the weak side's bid we are buying.
        *strong_bid* is the manipulated leader's current bid.
        """
        velocity = velocity_details.get("velocity", 0)
        opp_max = velocity_details.get("opp_max", 0)
        total_depth = velocity_details.get("total_depth", 0)
        max_spike = velocity_details.get("max_spike", 0)

        room = 1.0 - entry_price

        # Bigger leader spike → stronger expected reversal
        if max_spike > 15:
            predicted_reversal = 0.22
        elif max_spike > 10:
            predicted_reversal = 0.16
        elif velocity > 4:
            predicted_reversal = 0.12
        elif velocity > 2:
            predicted_reversal = 0.09
        else:
            predicted_reversal = 0.06

        predicted_reversal = min(predicted_reversal, room * 0.5)

        target_move = predicted_reversal * 0.50
        target_move = max(0.05, min(0.15, target_move))

        target_price = round(entry_price + target_move, 3)
        sl_price = round(entry_price - 0.06, 3)
        time_limit = 45

        confidence = 40
        if max_spike > 12:
            confidence += 15
        if velocity > 5:
            confidence += 10
        if opp_max > 0.35:
            confidence += 8
        if total_depth < 1000:
            confidence += 5
        confidence = max(0, min(100, confidence))

        log.info(
            "PREDICT FLIP: entry=%.2f rev=%.3f tgt=%.3f sl=%.3f "
            "time=%ds conf=%d",
            entry_price, predicted_reversal, target_price, sl_price,
            time_limit, confidence,
        )

        return {
            "target": target_price,
            "sl": sl_price,
            "time_limit": time_limit,
            "confidence": confidence,
            "predicted_move": round(predicted_reversal, 4),
        }

    @staticmethod
    def should_trade(prediction):
        """Return True if the prediction justifies opening a position."""
        return prediction["confidence"] >= 35 and prediction["predicted_move"] >= 0.04
