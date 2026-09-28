"""Aligned run windows (AD-9). Pure: the caller supplies ``scheduled_time`` and tunables."""

from __future__ import annotations

from datetime import datetime, timedelta

from detect.core.models import Tunables


def floor_minute(t: datetime) -> datetime:
    if t.tzinfo is None or t.utcoffset() != timedelta(0):
        raise ValueError(f"expected a tz-aware UTC datetime, got {t!r}")
    return t.replace(second=0, microsecond=0)


def window_bounds(scheduled_time: datetime, tunables: Tunables) -> tuple[datetime, datetime]:
    """``(window_start, window_end)``: end = floor_minute(scheduled_time) - ingest_lag_s; start = end - window_len_s."""
    end = floor_minute(scheduled_time) - timedelta(seconds=tunables["ingest_lag_s"])
    return end - timedelta(seconds=tunables["window_len_s"]), end
