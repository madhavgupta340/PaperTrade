"""Time helpers. Timestamps are stored as ISO-8601 UTC strings so they sort lexically."""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(dt: datetime | None = None) -> str:
    dt = (dt or utc_now()).astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def market_day_start_utc(now: datetime | None = None) -> str:
    """Start of the current US market calendar day (midnight New York time), as ISO UTC."""
    ny = (now or utc_now()).astimezone(NY)
    return iso_utc(ny.replace(hour=0, minute=0, second=0, microsecond=0))
