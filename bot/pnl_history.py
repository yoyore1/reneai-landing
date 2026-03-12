"""
Persist S3 PnL by date and hour (EST) for calendar display.
Format: { "YYYY-MM-DD": { "HH:00": pnl, ... }, ... }

Inverse bot uses a separate file so its PnL is not mixed with main S3.
"""

import json
from pathlib import Path

_history_path = Path(__file__).resolve().parent.parent / "pnl_history.json"
_history_path_inverse = Path(__file__).resolve().parent.parent / "pnl_history_inverse.json"


def append_pnl(date_key: str, hour_key: str, pnl: float) -> None:
    """Add PnL for a date+hour. Accumulates if multiple trades in same hour."""
    _append_to_path(_history_path, date_key, hour_key, pnl)


def append_pnl_inverse(date_key: str, hour_key: str, pnl: float) -> None:
    """Add PnL for inverse (underdog) bot — separate file."""
    _append_to_path(_history_path_inverse, date_key, hour_key, pnl)


def _append_to_path(path: Path, date_key: str, hour_key: str, pnl: float) -> None:
    data: dict = {}
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            data = {}
    if date_key not in data:
        data[date_key] = {}
    data[date_key][hour_key] = round(data[date_key].get(hour_key, 0) + pnl, 2)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except OSError:
        pass


def load_calendar(days: int = 30) -> dict:
    """Load PnL calendar for last N days. Returns { date: { hour: pnl } }."""
    return _load_calendar_from_path(_history_path)


def load_calendar_inverse(days: int = 30) -> dict:
    """Load inverse bot PnL calendar (separate from main S3)."""
    return _load_calendar_from_path(_history_path_inverse)


def _load_calendar_from_path(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}
