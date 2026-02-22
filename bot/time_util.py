"""
All bot times in EST (America/New_York).
Daily calendar: days with hours for logging/tracking.
"""

from datetime import datetime, timedelta

from zoneinfo import ZoneInfo

EST = ZoneInfo("America/New_York")


def now_est() -> datetime:
    return datetime.now(EST)


def hour_key_est() -> str:
    """Current hour in EST, e.g. '14:00'."""
    return now_est().strftime("%H:00")


def date_key_est() -> str:
    """Current date in EST, e.g. '2025-02-20'."""
    return now_est().strftime("%Y-%m-%d")


def datetime_est(epoch: float) -> datetime:
    """Convert epoch to datetime in EST."""
    return datetime.fromtimestamp(epoch, tz=EST)


def format_time_est(epoch: float) -> str:
    """Format epoch as HH:MM:SS EST."""
    if epoch <= 0:
        return "--"
    return datetime_est(epoch).strftime("%H:%M:%S")


def daily_calendar_lines(days: int = 7) -> list:
    """Return list of lines for a daily calendar (EST): each day with 24 hours."""
    lines = ["# 📅 Daily calendar (EST) — days with hours", ""]
    now = now_est()
    for d in range(days):
        dt = now - timedelta(days=d)
        date_str = dt.strftime("%Y-%m-%d")
        hours = " ".join("%02d:00" % h for h in range(24))
        lines.append("%s: %s" % (date_str, hours))
    return lines


def write_daily_calendar(path: str = "daily_calendar_EST.txt", days: int = 7) -> None:
    """Write daily calendar (EST) to a file."""
    with open(path, "w") as f:
        f.write("\n".join(daily_calendar_lines(days)))
        f.write("\n")
