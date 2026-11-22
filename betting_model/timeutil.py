"""Timezone-aware timestamps. Naive times are rejected, not guessed."""

from __future__ import annotations

from datetime import datetime, timezone


def parse_aware_datetime(value: object) -> datetime | None:
    """Parse an ISO timestamp that includes a timezone.

    Returns None when the value is missing, naive, or not a timestamp.
    Z is accepted as UTC. This never invents a timezone.
    """
    if value is None or not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def seconds_apart(left: datetime, right: datetime) -> float:
    return abs((left - right).total_seconds())
