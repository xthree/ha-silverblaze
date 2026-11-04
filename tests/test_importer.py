"""Tests for the statistics importer."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from homeassistant.components.recorder.models.statistics import StatisticMeanType
from homeassistant.components.recorder.statistics import (
    statistics_during_period,
    valid_statistic_id,
)
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.silverblaze.api.models import HourlyRead
from custom_components.silverblaze.importer import (
    async_anchor_sum,
    async_first_stat_start,
    async_last_stat,
    async_stored_hours,
    async_write,
    build_metadata,
    merge_window,
    statistic_id,
    statistic_name,
)

T0 = datetime(2026, 3, 1, 0, tzinfo=UTC)


def h(n: int) -> datetime:
    return T0 + timedelta(hours=n)


def reads(*pairs: tuple[int, float]) -> list[HourlyRead]:
    return [HourlyRead(h(n), v) for n, v in pairs]


def sums(rows):
    return [r["sum"] for r in rows]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("SP0000000001", "silverblaze:idahopower_electric_sp0000000001"),
        ("SP-0000-0001", "silverblaze:idahopower_electric_sp_0000_0001"),
        ("__x__", "silverblaze:idahopower_electric_x"),
        ("Mixed Case.42", "silverblaze:idahopower_electric_mixed_case_42"),
    ],
)
def test_statistic_id(raw, expected):
    sid = statistic_id("idahopower", "electric", raw)
    assert sid == expected
    assert valid_statistic_id(sid)


def test_statistic_name_suffix():
    assert statistic_name("Idaho Power", "electric", 0, 1) == "Idaho Power electric consumption"
    assert statistic_name("Idaho Power", "electric", 0, 2) == "Idaho Power electric consumption"
    assert statistic_name("Idaho Power", "electric", 1, 2) == "Idaho Power electric consumption 2"
    assert statistic_name("Idaho Power", "electric", 2, 3) == "Idaho Power electric consumption 3"


def test_build_metadata():
    meta = build_metadata("silverblaze:a_b_c", "Name")
    assert meta["mean_type"] is StatisticMeanType.NONE
    assert meta["has_sum"] is True
    assert meta["source"] == "silverblaze"
    assert meta["statistic_id"] == "silverblaze:a_b_c"
    assert meta["name"] == "Name"
    assert meta["unit_of_measurement"] == "kWh"
    assert meta["unit_class"] == "energy"


def test_first_import_cumulative():
    rows = merge_window({}, reads((0, 1.0), (1, 2.0), (2, 0.5)), 10.0)
    assert [r["state"] for r in rows] == [1.0, 2.0, 0.5]
    assert sums(rows) == [11.0, 13.0, 13.5]
    assert [r["start"] for r in rows] == [h(0), h(1), h(2)]


def test_idempotent_remerge():
    portal = reads((0, 1.0), (1, 2.0), (2, 0.5))
    first = merge_window({}, portal, 0.0)
    stored = {r["start"]: r["state"] for r in first}
    assert merge_window(stored, portal, 0.0) == first


def test_correction_shifts_only_later_sums():
    stored = {h(0): 1.0, h(1): 1.0, h(2): 1.0}
    before = merge_window(stored, [], 0.0)
    after = merge_window(stored, reads((1, 1.5)), 0.0)
    assert sums(before) == [1.0, 2.0, 3.0]
    assert sums(after) == [1.0, 2.5, 3.5]


def test_late_filled_hour():
    stored = {h(0): 1.0, h(2): 1.0}
    rows = merge_window(stored, reads((1, 2.0)), 0.0)
    assert [r["start"] for r in rows] == [h(0), h(1), h(2)]
    assert sums(rows) == [1.0, 3.0, 4.0]


def test_stored_hour_missing_from_portal_kept():
    stored = {h(0): 1.0, h(1): 2.0, h(2): 3.0}
    rows = merge_window(stored, reads((0, 1.0), (2, 3.0)), 0.0)
    assert [r["state"] for r in rows] == [1.0, 2.0, 3.0]
    assert sums(rows) == [1.0, 3.0, 6.0]


def test_never_stored_missing_hour_absent_not_zero():
    rows = merge_window({}, reads((0, 1.0), (3, 2.0)), 0.0)
    assert [r["start"] for r in rows] == [h(0), h(3)]
    assert all(r["state"] != 0 for r in rows)
    assert sums(rows) == [1.0, 3.0]


def test_sums_never_decrease():
    rows = merge_window({h(5): 1.0}, reads((0, 0.0), (1, 2.0), (9, 1.0)), 4.0)
    s = sums(rows)
    assert s == sorted(s)


def test_empty_window():
    assert merge_window({}, [], 5.0) == []


SID = "silverblaze:testutil_electric_sp0000000001"


async def test_recorder_round_trip_and_gap_anchor(recorder_mock, hass: HomeAssistant):
    meta = build_metadata(SID, "Test electric consumption")
    assert await async_last_stat(hass, SID) is None
    assert await async_first_stat_start(hass, SID) is None
    assert await async_anchor_sum(hass, SID, h(0)) == 0.0

    rows = merge_window({}, reads((0, 1.0), (1, 2.0), (2, 3.0)), 0.0)
    await async_write(hass, meta, rows)
    await async_wait_recording_done(hass)

    last = await async_last_stat(hass, SID)
    assert last == (h(2), 6.0)
    assert await async_first_stat_start(hass, SID) == h(0)
    stored = await async_stored_hours(hass, SID, h(0), h(24))
    assert stored == {h(0): 1.0, h(1): 2.0, h(2): 3.0}

    read_back = await hass.async_add_executor_job(
        statistics_during_period, hass, h(0), None, {SID}, "hour", None, {"state", "sum"}
    )
    assert [r["sum"] for r in read_back[SID]] == [1.0, 3.0, 6.0]

    # Anchor across a 3-day gap: next window starts 3 days after the last stored hour.
    gap_start = h(2) + timedelta(days=3)
    assert await async_anchor_sum(hass, SID, gap_start) == 6.0
    assert await async_anchor_sum(hass, SID, h(1)) == 1.0
    assert await async_anchor_sum(hass, SID, h(0)) == 0.0

    # Rewrite with a correction: merge over stored, anchored before the window.
    anchor = await async_anchor_sum(hass, SID, h(1))
    rewritten = merge_window(stored_slice(stored, h(1)), reads((1, 2.5)), anchor)
    await async_write(hass, meta, rewritten)
    await async_wait_recording_done(hass)
    assert await async_last_stat(hass, SID) == (h(2), 6.5)


def stored_slice(stored, start):
    return {k: v for k, v in stored.items() if k >= start}
