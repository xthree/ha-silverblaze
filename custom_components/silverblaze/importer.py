"""Statistics importer: statistic IDs, metadata, merge/running-sum math and recorder I/O.

The pure functions (``statistic_id``, ``statistic_name``, ``build_metadata``,
``merge_window``) have no I/O. The ``async_*`` helpers at the bottom talk to the
recorder, always off the event loop. Nothing here ever deletes statistics, and a
missing hour is never written as zero.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime, timedelta

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.db_schema import Statistics, StatisticsMeta
from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
from homeassistant.components.recorder.models.statistics import StatisticMeanType
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.recorder import session_scope
from homeassistant.util.unit_conversion import EnergyConverter
from sqlalchemy import func, select

from .api.models import HourlyRead
from .const import DOMAIN

_NON_SLUG = re.compile(r"[^a-z0-9]+")
_MAX_LOOKBACK = timedelta(days=36500)


def _sanitise(value: str) -> str:
    """Lower-case, map non-alphanumerics to ``_``, collapse repeats, strip the ends."""
    return _NON_SLUG.sub("_", value.lower()).strip("_")


def statistic_id(utility_slug: str, commodity: str, service_point_id: str) -> str:
    """Return ``silverblaze:<slug>_<commodity>_<sanitised service point id>``."""
    object_id = "_".join(
        part
        for part in (_sanitise(utility_slug), _sanitise(commodity), _sanitise(service_point_id))
        if part
    )
    return f"{DOMAIN}:{object_id}"


def statistic_name(utility_name: str, commodity: str, index: int, total: int) -> str:
    """Return the display name; the 2nd+ service point (0-based ``index``) gets an ordinal."""
    name = f"{utility_name} {commodity} consumption"
    if total > 1 and index > 0:
        name = f"{name} {index + 1}"
    return name


def build_metadata(statistic_id_: str, name: str) -> StatisticMetaData:
    """Return external-statistics metadata for an hourly kWh consumption series."""
    return StatisticMetaData(
        mean_type=StatisticMeanType.NONE,
        has_sum=True,
        name=name,
        source=DOMAIN,
        statistic_id=statistic_id_,
        unit_class=EnergyConverter.UNIT_CLASS,
        unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
    )


def merge_window(
    stored: Mapping[datetime, float],
    portal: Iterable[HourlyRead],
    anchor_sum: float,
) -> list[StatisticData]:
    """Merge portal reads over stored hours and recompute the running sum forward.

    Portal values win; a stored hour with no portal read is kept; an hour that was
    never stored and has no portal read is absent (never zero-filled). ``sum`` starts
    from ``anchor_sum`` (the running total just before the earliest hour).
    """
    states: dict[datetime, float] = dict(stored)
    for read in portal:
        states[read.start_utc] = read.kwh
    total = anchor_sum
    rows: list[StatisticData] = []
    for start in sorted(states):
        state = states[start]
        total += state
        rows.append(StatisticData(start=start, state=state, sum=total))
    return rows


# --- recorder I/O (off the event loop) -------------------------------------------------


async def async_last_stat(hass: HomeAssistant, sid: str) -> tuple[datetime, float] | None:
    """Return ``(start, sum)`` of the newest stored hour, or None."""
    result = await get_instance(hass).async_add_executor_job(
        get_last_statistics, hass, 1, sid, True, {"sum"}
    )
    rows = result.get(sid)
    if not rows:
        return None
    row = rows[0]
    return datetime.fromtimestamp(row["start"], UTC), float(row.get("sum") or 0.0)


def _first_stat_start(hass: HomeAssistant, sid: str) -> datetime | None:
    with session_scope(hass=hass, read_only=True) as session:
        ts = session.execute(
            select(func.min(Statistics.start_ts))
            .join(StatisticsMeta, Statistics.metadata_id == StatisticsMeta.id)
            .where(StatisticsMeta.statistic_id == sid)
        ).scalar()
    return None if ts is None else datetime.fromtimestamp(ts, UTC)


async def async_first_stat_start(hass: HomeAssistant, sid: str) -> datetime | None:
    """Return the start of the oldest stored hour, or None."""
    return await get_instance(hass).async_add_executor_job(_first_stat_start, hass, sid)


async def async_stored_hours(
    hass: HomeAssistant, sid: str, start: datetime, end: datetime
) -> dict[datetime, float]:
    """Return ``{hour start: state}`` for stored hours in ``[start, end)``."""
    result = await get_instance(hass).async_add_executor_job(
        statistics_during_period, hass, start, end, {sid}, "hour", None, {"state"}
    )
    return {
        datetime.fromtimestamp(row["start"], UTC): float(row["state"])
        for row in result.get(sid, [])
        if row.get("state") is not None
    }


async def async_anchor_sum(hass: HomeAssistant, sid: str, before: datetime) -> float:
    """Return ``sum`` of the latest stored hour starting before ``before`` (0.0 if none).

    The lookback doubles from one day until a row is found or the first stored hour
    has been passed, so it works across arbitrarily long gaps.
    """
    first = await async_first_stat_start(hass, sid)
    if first is None or first >= before:
        return 0.0
    span = timedelta(days=1)
    while True:
        begin = max(before - span, first)
        result = await get_instance(hass).async_add_executor_job(
            statistics_during_period, hass, begin, before, {sid}, "hour", None, {"sum"}
        )
        rows = [r for r in result.get(sid, []) if r.get("sum") is not None]
        if rows:
            return float(max(rows, key=lambda r: r["start"])["sum"])
        if begin <= first or span >= _MAX_LOOKBACK:
            return 0.0
        span *= 2


async def async_write(
    hass: HomeAssistant, metadata: StatisticMetaData, rows: Iterable[StatisticData]
) -> None:
    """Queue an external-statistics import (the recorder applies it asynchronously)."""
    async_add_external_statistics(hass, metadata, list(rows))
