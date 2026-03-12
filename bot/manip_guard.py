"""
Manipulation Guard — Dynamic detection of market manipulation.

Tracks three signals:
1. Side Alternation: If sides flip N+ times in last 6 resolved markets
2. Hot Streak Trap: After N+ consecutive wins, next loss is more probable
3. Choppy Rate: If N%+ of last 10 markets were choppy/skipped

When 2+ signals fire simultaneously, the guard pauses trading and tracks phantoms.
All thresholds are configurable via constructor.
"""

import logging
import time
from collections import deque
from dataclasses import dataclass, field

log = logging.getLogger("manip_guard")


@dataclass
class MarketRecord:
    timestamp: float
    side: str
    won: bool
    was_choppy: bool
    was_noleader: bool
    pnl: float = 0.0


class ManipulationGuard:

    def __init__(self, alternation_threshold=4, win_streak_threshold=5,
                 choppy_rate_threshold=0.30, cooldown_markets=2,
                 side_window=6, choppy_window=10):
        self.SIDE_WINDOW = side_window
        self.ALTERNATION_THRESHOLD = alternation_threshold
        self.WIN_STREAK_THRESHOLD = win_streak_threshold
        self.CHOPPY_WINDOW = choppy_window
        self.CHOPPY_RATE_THRESHOLD = choppy_rate_threshold
        self.COOLDOWN_MARKETS = cooldown_markets

        self._history: deque[MarketRecord] = deque(maxlen=20)
        self._consec_wins = 0
        self._cooldown_remaining = 0
        self._paused = False
        self._pause_reason = ""
        self._total_pauses = 0
        self._phantom_wins = 0
        self._phantom_losses = 0

    @property
    def is_paused(self) -> bool:
        return self._paused

    @property
    def pause_reason(self) -> str:
        return self._pause_reason

    @property
    def status_dict(self) -> dict:
        return {
            "paused": self._paused,
            "reason": self._pause_reason,
            "consec_wins": self._consec_wins,
            "alternation": self._calc_alternation(),
            "choppy_rate": self._calc_choppy_rate(),
            "cooldown_left": self._cooldown_remaining,
            "total_pauses": self._total_pauses,
            "phantom_wins": self._phantom_wins,
            "phantom_losses": self._phantom_losses,
            "signals": self._active_signals(),
        }

    def _calc_alternation(self) -> int:
        sided = [r for r in self._history if not r.was_choppy and not r.was_noleader]
        recent = list(sided)[-self.SIDE_WINDOW:]
        if len(recent) < 3:
            return 0
        alts = 0
        for i in range(1, len(recent)):
            if recent[i].side != recent[i - 1].side:
                alts += 1
        return alts

    def _calc_choppy_rate(self) -> float:
        recent = list(self._history)[-self.CHOPPY_WINDOW:]
        if not recent:
            return 0.0
        choppy = sum(1 for r in recent if r.was_choppy or r.was_noleader)
        return choppy / len(recent)

    def _active_signals(self) -> list:
        signals = []
        alt = self._calc_alternation()
        if alt >= self.ALTERNATION_THRESHOLD:
            signals.append(f"alternation={alt}/{self.SIDE_WINDOW}")
        if self._consec_wins >= self.WIN_STREAK_THRESHOLD:
            signals.append(f"hot_streak={self._consec_wins}")
        cr = self._calc_choppy_rate()
        if cr >= self.CHOPPY_RATE_THRESHOLD:
            signals.append(f"choppy_rate={cr:.0%}")
        return signals

    def record_market(self, side: str, won: bool, pnl: float,
                      was_choppy: bool = False, was_noleader: bool = False):
        """Record a resolved market (real trade, choppy skip, or no-leader skip)."""
        self._history.append(MarketRecord(
            timestamp=time.time(), side=side, won=won, pnl=pnl,
            was_choppy=was_choppy, was_noleader=was_noleader,
        ))

        if was_choppy or was_noleader:
            return

        if won:
            self._consec_wins += 1
        else:
            self._consec_wins = 0

        if self._cooldown_remaining > 0:
            self._cooldown_remaining -= 1
            if self._cooldown_remaining <= 0:
                self._paused = False
                self._pause_reason = ""
                log.info("MANIP GUARD: Cooldown ended, resuming trading")

    def record_phantom(self, won: bool):
        if won:
            self._phantom_wins += 1
        else:
            self._phantom_losses += 1

    def should_skip(self, entry_price: float = 0.0, entry_gate: float = 1.0) -> tuple:
        """
        Check if we should skip the next trade.
        entry_gate: if entry_price >= entry_gate, NEVER skip (high-confidence override).
        Returns (skip: bool, reason: str).
        """
        if entry_price >= entry_gate:
            return False, ""

        if self._cooldown_remaining > 0:
            return True, self._pause_reason

        signals = self._active_signals()

        if len(signals) >= 2:
            reason = "manip_guard: " + " + ".join(signals)
            self._paused = True
            self._pause_reason = reason
            self._cooldown_remaining = self.COOLDOWN_MARKETS
            self._total_pauses += 1
            log.warning("MANIP GUARD TRIGGERED: %s — pausing for %d markets", reason, self.COOLDOWN_MARKETS)
            return True, reason

        if self._calc_alternation() >= self.ALTERNATION_THRESHOLD + 1:
            reason = f"manip_guard: extreme alternation={self._calc_alternation()}"
            self._paused = True
            self._pause_reason = reason
            self._cooldown_remaining = self.COOLDOWN_MARKETS
            self._total_pauses += 1
            log.warning("MANIP GUARD TRIGGERED: %s — pausing for %d markets", reason, self.COOLDOWN_MARKETS)
            return True, reason

        self._paused = False
        self._pause_reason = ""
        return False, ""
