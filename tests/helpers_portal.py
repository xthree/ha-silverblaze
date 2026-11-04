"""Builders for synthetic portal responses (no real data)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo


def make_day_response(
    local_date: date,
    tz: ZoneInfo,
    values: Sequence[float | None],
    *,
    epochs_ms: Sequence[int] | None = None,
) -> dict[str, Any]:
    """Build a ConsumptionV3/Data JSON body for one local day.

    Slot epochs default to consecutive UTC hours starting at local midnight, which is
    how the portal reports them (true UTC, so DST days stay correct). ``Category`` labels
    are deliberately wall-clock text the client must never use for time.
    """
    if epochs_ms is None:
        midnight = datetime(local_date.year, local_date.month, local_date.day, tzinfo=tz)
        start = midnight.astimezone(UTC)
        epochs_ms = [
            int((start + timedelta(hours=i)).timestamp() * 1000) for i in range(len(values))
        ]
    if len(epochs_ms) != len(values):
        raise ValueError("epochs_ms and values must have the same length")
    return {
        "TimeFrameTitle": "Synthetic day",
        "ResponseMeta": {"Resolution": "60min"},
        "AlignedSeriesData": {"forward/usage": list(values)},
        "IntervalLayout": {
            "ChartIntervals": [
                {"EpochMs": epoch, "Category": f"slot {i}"} for i, epoch in enumerate(epochs_ms)
            ]
        },
    }
