"""
Session Tracker — detects manipulation sessions across sequential markets.

Manipulation comes in clusters. When a manipulator is active, they don't hit
just one market — they hit 3, 5, 8 in a row. This module tracks recent
outcomes and detects when we're in an active manipulation session.

Session states:
  - clean: recent markets were normal, trade aggressively
  - cautious: some manipulation signals, trade carefully
  - manipulation_session: active manipulation, only flip or skip
"""

import logging
import time

log = logging.getLogger("session")


class SessionTracker:

    def __init__(self, window=10):
        self._window = window
        self._history = []  # [(timestamp, was_bad, pnl, detail)]

    def record_outcome(self, was_reversal, was_manipulation_skip, pnl, detail=""):
        """Record a market outcome (called after each position close or phantom resolve)."""
        was_bad = was_reversal or was_manipulation_skip
        self._history.append((time.time(), was_bad, pnl, detail))
        if len(self._history) > self._window * 3:
            self._history = self._history[-self._window * 2:]

        state, rate, _ = self.get_session_state()
        if was_bad:
            log.info("SESSION: recorded BAD outcome (rev=%s skip=%s pnl=$%.2f) → %s (%.0f%%)",
                     was_reversal, was_manipulation_skip, pnl, state, rate * 100)

    def get_session_state(self):
        """Return (state_str, manipulation_rate, details_dict)."""
        recent = self._history[-self._window:] if self._history else []
        if len(recent) < 3:
            return "clean", 0.0, {"samples": len(recent)}

        last_3 = recent[-3:]
        last_5 = recent[-5:] if len(recent) >= 5 else recent

        bad_3 = sum(1 for _, b, _, _ in last_3 if b)
        bad_5 = sum(1 for _, b, _, _ in last_5 if b)

        rate_3 = bad_3 / len(last_3)
        rate_5 = bad_5 / len(last_5)
        rate = max(rate_3, rate_5)

        pnl_5 = sum(p for _, _, p, _ in last_5)

        if bad_3 >= 2 or (rate_5 >= 0.6 and len(last_5) >= 4):
            state = "manipulation_session"
        elif bad_3 >= 1 or rate_5 >= 0.3:
            state = "cautious"
        else:
            state = "clean"

        return state, rate, {
            "bad_last_3": bad_3,
            "bad_last_5": bad_5,
            "pnl_last_5": round(pnl_5, 2),
            "total_tracked": len(recent),
        }

    def confidence_adjustment(self):
        """Return a confidence adjustment based on session state."""
        state, rate, _ = self.get_session_state()
        if state == "clean":
            return +10
        elif state == "cautious":
            return -10
        else:
            return -25

    def get_size_multiplier(self):
        """Return a size multiplier based on session state."""
        state, _, details = self.get_session_state()
        if state == "clean" and details.get("pnl_last_5", 0) > 0:
            return 1.5
        elif state == "clean":
            return 1.2
        elif state == "cautious":
            return 0.8
        else:
            return 0.5
