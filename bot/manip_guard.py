"""
Manipulation Guard — Dynamic detection of market manipulation.

Tracks four signals:
1. Side Alternation: If sides flip N+ times in last 6 resolved markets
2. Hot Streak Trap: After N+ consecutive wins, next loss is more probable
3. Choppy Rate: If N%+ of last 10 markets were choppy/skipped
4. Reversal Rate: If N%+ of recent trades had false breakouts (other side hit 60c+)

When 2+ signals fire simultaneously, the guard pauses trading and tracks phantoms.
All thresholds are configurable via constructor.
"""

import json
import logging
import os
import time
from collections import deque
from dataclasses import dataclass, field, asdict
from pathlib import Path

log = logging.getLogger("manip_guard")

PERSIST_DIR = Path(__file__).resolve().parent.parent / "history"


@dataclass
class MarketRecord:
    timestamp: float
    side: str
    won: bool
    was_choppy: bool
    was_noleader: bool
    pnl: float = 0.0
    was_reversal: bool = False


class ManipulationGuard:

    def __init__(self, alternation_threshold=4, win_streak_threshold=5,
                 choppy_rate_threshold=0.30, cooldown_markets=2,
                 side_window=6, choppy_window=10,
                 reversal_rate_threshold=0.30, reversal_window=5,
                 bot_name: str = "", sister_bot: str = ""):
        self.SIDE_WINDOW = side_window
        self.ALTERNATION_THRESHOLD = alternation_threshold
        self.WIN_STREAK_THRESHOLD = win_streak_threshold
        self.CHOPPY_WINDOW = choppy_window
        self.CHOPPY_RATE_THRESHOLD = choppy_rate_threshold
        self.COOLDOWN_MARKETS = cooldown_markets
        self.REVERSAL_WINDOW = reversal_window
        self.REVERSAL_RATE_THRESHOLD = reversal_rate_threshold

        self._bot_name = bot_name
        self._sister_bot = sister_bot
        self._persist_path = PERSIST_DIR / f"guard_{bot_name}.json" if bot_name else None

        self._history: deque[MarketRecord] = deque(maxlen=20)
        self._consec_wins = 0
        self._cooldown_remaining = 0
        self._paused = False
        self._pause_reason = ""
        self._total_pauses = 0
        self._phantom_wins = 0
        self._phantom_losses = 0

        self._load()

    @property
    def is_paused(self) -> bool:
        return self._paused

    @property
    def pause_reason(self) -> str:
        return self._pause_reason

    @property
    def status_dict(self) -> dict:
        d = {
            "paused": self._paused,
            "reason": self._pause_reason,
            "consec_wins": self._consec_wins,
            "alternation": self._calc_alternation(),
            "choppy_rate": self._calc_choppy_rate(),
            "reversal_rate": self._calc_reversal_rate(),
            "cooldown_left": self._cooldown_remaining,
            "total_pauses": self._total_pauses,
            "phantom_wins": self._phantom_wins,
            "phantom_losses": self._phantom_losses,
            "signals": self._active_signals(),
        }
        return d

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

    def _calc_reversal_rate(self) -> float:
        traded = [r for r in self._history
                  if not r.was_choppy and not r.was_noleader]
        recent = traded[-self.REVERSAL_WINDOW:]
        if len(recent) < 3:
            return 0.0
        reversals = sum(1 for r in recent if r.was_reversal)
        return reversals / len(recent)

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
        rr = self._calc_reversal_rate()
        if rr >= self.REVERSAL_RATE_THRESHOLD:
            signals.append(f"reversal_rate={rr:.0%}")
        return signals

    def _save(self):
        if not self._persist_path:
            return
        try:
            PERSIST_DIR.mkdir(parents=True, exist_ok=True)
            data = {
                "history": [asdict(r) for r in self._history],
                "consec_wins": self._consec_wins,
                "cooldown_remaining": self._cooldown_remaining,
                "paused": self._paused,
                "pause_reason": self._pause_reason,
                "total_pauses": self._total_pauses,
                "phantom_wins": self._phantom_wins,
                "phantom_losses": self._phantom_losses,
            }
            tmp = self._persist_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2))
            tmp.replace(self._persist_path)
        except Exception as exc:
            log.warning("Guard persist save failed: %s", exc)

    def _load(self):
        if self._persist_path and self._persist_path.exists():
            try:
                data = json.loads(self._persist_path.read_text())
                for rec in data.get("history", []):
                    self._history.append(MarketRecord(**rec))
                self._consec_wins = data.get("consec_wins", 0)
                self._cooldown_remaining = data.get("cooldown_remaining", 0)
                self._paused = data.get("paused", False)
                self._pause_reason = data.get("pause_reason", "")
                self._total_pauses = data.get("total_pauses", 0)
                self._phantom_wins = data.get("phantom_wins", 0)
                self._phantom_losses = data.get("phantom_losses", 0)
                log.info("MANIP GUARD: Restored %d records from disk for '%s'",
                         len(self._history), self._bot_name)
            except Exception as exc:
                log.warning("Guard persist load failed: %s", exc)
        self._warm_from_sister()

    def _warm_from_sister(self):
        """Import recent market records from a sister bot's guard on startup."""
        if not self._sister_bot or not self._persist_path:
            return
        sister_path = PERSIST_DIR / f"guard_{self._sister_bot}.json"
        if not sister_path.exists():
            return
        try:
            sister_data = json.loads(sister_path.read_text())
            sister_history = sister_data.get("history", [])
            if not sister_history:
                return

            our_latest = max((r.timestamp for r in self._history), default=0)

            imported = 0
            for rec_dict in sister_history:
                ts = rec_dict.get("timestamp", 0)
                if ts > our_latest:
                    self._history.append(MarketRecord(**rec_dict))
                    imported += 1

            if imported > 0:
                sided = [r for r in self._history
                         if not r.was_choppy and not r.was_noleader]
                self._consec_wins = 0
                for r in reversed(sided):
                    if r.won:
                        self._consec_wins += 1
                    else:
                        break
                self._save()
                log.info(
                    "MANIP GUARD: Warmed %d records from sister '%s' | "
                    "history=%d consec_wins=%d",
                    imported, self._sister_bot,
                    len(self._history), self._consec_wins,
                )
        except Exception as exc:
            log.warning("Guard sister warmup failed: %s", exc)

    def record_market(self, side: str, won: bool, pnl: float,
                      was_choppy: bool = False, was_noleader: bool = False,
                      was_reversal: bool = False):
        """Record a resolved market (real trade, choppy skip, or no-leader skip)."""
        self._history.append(MarketRecord(
            timestamp=time.time(), side=side, won=won, pnl=pnl,
            was_choppy=was_choppy, was_noleader=was_noleader,
            was_reversal=was_reversal,
        ))

        if was_choppy or was_noleader:
            self._save()
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

        self._save()

    def record_phantom(self, won: bool):
        if won:
            self._phantom_wins += 1
        else:
            self._phantom_losses += 1
        self._save()

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
