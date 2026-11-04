"""Tests for the sync engine (recorder enabled, fake portal at the API boundary)."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from contextlib import ExitStack
from datetime import UTC, date, datetime, timedelta
from functools import partial
from unittest.mock import patch

import pytest
from aioresponses import CallbackResult
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.recorder import Recorder
from homeassistant.components.recorder.statistics import (
    get_metadata,
    statistics_during_period,
)
from homeassistant.config_entries import SOURCE_REAUTH
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.silverblaze.api import (
    CannotConnect,
    InvalidAuth,
    LoginFlowChanged,
    SessionExpired,
    SilverblazeHttp,
    UnexpectedResponse,
)
from custom_components.silverblaze.api.utilities import get_utility
from custom_components.silverblaze.coordinator import SilverblazeCoordinator
from custom_components.silverblaze.importer import statistic_id

from .conftest import load_fixture_json, load_fixture_text
from .fake_portal import FakePortal, local_midnight_utc
from .helpers_portal import make_day_response

SP1 = "SP0000000001"
SP2 = "SP0000000002"
SID1 = statistic_id("idahopower", "electric", SP1)
SID2 = statistic_id("idahopower", "electric", SP2)
D0 = date(2026, 2, 1)


B0 = date(2026, 4, 1)


def d(n: int) -> date:
    return D0 + timedelta(days=n)


def b(n: int) -> date:
    return B0 + timedelta(days=n)


def day_values(n: int) -> list[float]:
    """Distinct 24 values for day n."""
    return [float(n * 100 + h + 1) for h in range(24)]


async def make(hass: HomeAssistant, entry, options=None) -> SilverblazeCoordinator:
    entry.add_to_hass(hass)
    if options:
        hass.config_entries.async_update_entry(entry, options=options)
    return SilverblazeCoordinator(hass, entry)


async def rows(hass: HomeAssistant, sid: str) -> list[tuple[datetime, float, float]]:
    await async_wait_recording_done(hass)
    result = await hass.async_add_executor_job(
        lambda: statistics_during_period(
            hass,
            datetime(2000, 1, 1, tzinfo=UTC),
            datetime(2100, 1, 1, tzinfo=UTC),
            {sid},
            "hour",
            None,
            {"state", "sum"},
        )
    )
    return [
        (datetime.fromtimestamp(r["start"], UTC), r["state"], r["sum"]) for r in result.get(sid, [])
    ]


def expected(spans: list[tuple[date, list[float | None]]]) -> list[tuple[datetime, float, float]]:
    out = []
    total = 0.0
    for day, vals in spans:
        start = local_midnight_utc(day)
        for i, v in enumerate(vals):
            if v is None:
                continue
            total += v
            out.append((start + timedelta(hours=i), v, total))
    return out


async def test_first_run_oldest_to_newest(
    recorder_mock: Recorder, hass: HomeAssistant, mock_config_entry, fake_portal: FakePortal
) -> None:
    vals1 = day_values(1)
    vals1[5] = None
    vals1[6] = None
    fake_portal.add_service_point(
        SP1, d(0), d(3), {d(0): day_values(0), d(1): vals1, d(2): day_values(2), d(3): []}
    )
    coordinator = await make(hass, mock_config_entry)
    data = await coordinator.async_sync()

    got = await rows(hass, SID1)
    # d(3) is published as an empty day: nothing written, no zero fill.
    assert got == expected([(d(0), day_values(0)), (d(1), vals1), (d(2), day_values(2))])
    assert len(got) == 24 * 3 - 2
    assert data.latest_data_hour[SID1] == got[-1][0]
    assert data.last_success is not None
    assert fake_portal.calls["login"] == 1
    assert [c[1] for c in fake_portal.day_calls] == [d(0), d(1), d(2), d(3)]


async def test_unpublished_days_absent(
    recorder_mock: Recorder, hass: HomeAssistant, mock_config_entry, fake_portal: FakePortal
) -> None:
    fake_portal.add_service_point(
        SP1, d(0), d(2), {d(0): day_values(0), d(2): day_values(2)}, default=None
    )
    coordinator = await make(hass, mock_config_entry)
    await coordinator.async_sync()
    got = await rows(hass, SID1)
    assert got == expected([(d(0), day_values(0)), (d(2), day_values(2))])


async def test_spring_dst_day(
    recorder_mock: Recorder, hass: HomeAssistant, mock_config_entry, fake_portal: FakePortal
) -> None:
    spring = date(2026, 3, 8)
    vals = [float(i + 1) for i in range(23)]
    fake_portal.add_service_point(SP1, spring, spring, {spring: vals})
    coordinator = await make(hass, mock_config_entry)
    await coordinator.async_sync()
    got = await rows(hass, SID1)
    assert len(got) == 23
    # Midnight MST = 07:00Z; 03:00 MDT (the 3rd slot) = 09:00Z.
    assert got[2][0] == datetime(2026, 3, 8, 9, tzinfo=UTC)
    assert got[0][0] == datetime(2026, 3, 8, 7, tzinfo=UTC)
    assert got[-1][2] == sum(vals)


@pytest.mark.parametrize("zone", ["UTC", "America/Denver"])
async def test_time_zone_comes_from_module(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_config_entry,
    fake_portal: FakePortal,
    zone: str,
) -> None:
    await hass.config.async_set_time_zone(zone)
    spring = date(2026, 3, 8)
    fake_portal.add_service_point(
        SP1, spring - timedelta(days=1), spring + timedelta(days=1), default=2.0
    )
    fake_portal.days[SP1][spring] = [2.0] * 23
    coordinator = await make(hass, mock_config_entry)
    await coordinator.async_sync()
    got = await rows(hass, SID1)
    assert got == expected(
        [
            (spring - timedelta(days=1), [2.0] * 24),
            (spring, [2.0] * 23),
            (spring + timedelta(days=1), [2.0] * 24),
        ]
    )
    # The portal was asked for module-local dates regardless of the HA zone.
    assert [c[1] for c in fake_portal.day_calls] == [
        spring - timedelta(days=1),
        spring,
        spring + timedelta(days=1),
    ]


async def test_two_service_points(
    recorder_mock: Recorder, hass: HomeAssistant, mock_config_entry, fake_portal: FakePortal
) -> None:
    fake_portal.add_service_point(SP1, d(0), d(1), default=1.0)
    fake_portal.add_service_point(SP2, d(0), d(1), default=3.0)
    coordinator = await make(hass, mock_config_entry)
    await coordinator.async_sync()
    await async_wait_recording_done(hass)
    meta = await hass.async_add_executor_job(lambda: get_metadata(hass, statistic_ids={SID1, SID2}))
    assert meta[SID1][1]["name"] == "Idaho Power electric consumption"
    assert meta[SID2][1]["name"] == "Idaho Power electric consumption 2"
    assert (await rows(hass, SID1))[-1][2] == 48.0
    assert (await rows(hass, SID2))[-1][2] == 144.0


async def test_two_service_points_sorted_order(
    recorder_mock: Recorder, hass: HomeAssistant, mock_config_entry, fake_portal: FakePortal
) -> None:
    # Meta lists SP2 first; naming still follows sorted SP order.
    fake_portal.add_service_point(SP2, d(0), d(1), default=3.0)
    fake_portal.add_service_point(SP1, d(0), d(1), default=1.0)
    assert list(fake_portal.spans) == [SP2, SP1]
    coordinator = await make(hass, mock_config_entry)
    await coordinator.async_sync()
    await async_wait_recording_done(hass)
    meta = await hass.async_add_executor_job(lambda: get_metadata(hass, statistic_ids={SID1, SID2}))
    assert meta[SID1][1]["name"] == "Idaho Power electric consumption"
    assert meta[SID2][1]["name"] == "Idaho Power electric consumption 2"


async def test_unparseable_import_from_is_ignored(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_config_entry,
    fake_portal: FakePortal,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake_portal.add_service_point(SP1, d(0), d(1), default=1.0)
    coordinator = await make(hass, mock_config_entry, {"import_from": "not-a-date"})
    await coordinator.async_sync()
    assert len(await rows(hass, SID1)) == 48
    assert "Ignoring an unparseable import-from date" in caplog.text
    assert "not-a-date" not in caplog.text


async def test_import_from_bounds_start(
    recorder_mock: Recorder, hass: HomeAssistant, mock_config_entry, fake_portal: FakePortal
) -> None:
    fake_portal.add_service_point(SP1, d(0), d(4), default=1.0)
    coordinator = await make(hass, mock_config_entry, {"import_from": d(2).isoformat()})
    await coordinator.async_sync()
    got = await rows(hass, SID1)
    assert got[0][0] == local_midnight_utc(d(2))
    assert len(got) == 24 * 3


async def test_import_from_earlier_than_oldest(
    recorder_mock: Recorder, hass: HomeAssistant, mock_config_entry, fake_portal: FakePortal
) -> None:
    fake_portal.add_service_point(SP1, d(2), d(3), default=1.0)
    coordinator = await make(hass, mock_config_entry, {"import_from": d(0).isoformat()})
    await coordinator.async_sync()
    got = await rows(hass, SID1)
    assert got[0][0] == local_midnight_utc(d(2))
    assert [c[1] for c in fake_portal.day_calls] == [d(2), d(3)]


async def test_session_expiry_relogs_in_once(
    recorder_mock: Recorder, hass: HomeAssistant, mock_config_entry, fake_portal: FakePortal
) -> None:
    fake_portal.add_service_point(SP1, d(0), d(3), default=1.0)
    fake_portal.fail_on("get_day", 3, SessionExpired())
    coordinator = await make(hass, mock_config_entry)
    await coordinator.async_sync()
    assert fake_portal.calls["login"] == 2
    days = [c[1] for c in fake_portal.day_calls]
    assert days == [d(0), d(1), d(2), d(2), d(3)]  # only the retried day repeats
    got = await rows(hass, SID1)
    assert len(got) == 96
    assert got[-1][2] == 96.0


async def test_meta_expiry_relogs_in_once(
    recorder_mock: Recorder, hass: HomeAssistant, mock_config_entry, fake_portal: FakePortal
) -> None:
    fake_portal.add_service_point(SP1, d(0), d(1), default=1.0)
    fake_portal.fail_on("get_meta", 1, SessionExpired())
    coordinator = await make(hass, mock_config_entry)
    await coordinator.async_sync()
    assert fake_portal.calls["login"] == 2
    assert len(await rows(hass, SID1)) == 48


async def test_expiry_during_relogin_fails_run(
    recorder_mock: Recorder, hass: HomeAssistant, mock_config_entry, fake_portal: FakePortal
) -> None:
    fake_portal.add_service_point(SP1, d(0), d(4), default=1.0)
    fake_portal.fail_on("get_day", 3, SessionExpired())
    fake_portal.fail_on("get_page_parameters", 2, SessionExpired())
    coordinator = await make(hass, mock_config_entry)
    with pytest.raises(CannotConnect):
        await coordinator.async_sync()
    assert fake_portal.calls["login"] == 2
    assert len(await rows(hass, SID1)) == 48  # days 0 and 1 kept


async def test_two_expiries_in_one_run_each_relogin(
    recorder_mock: Recorder, hass: HomeAssistant, mock_config_entry, fake_portal: FakePortal
) -> None:
    fake_portal.add_service_point(SP1, d(0), d(4), default=1.0)
    fake_portal.fail_on("get_day", 2, SessionExpired())
    fake_portal.fail_on("get_day", 4, SessionExpired())
    coordinator = await make(hass, mock_config_entry)
    await coordinator.async_sync()
    assert fake_portal.calls["login"] == 3
    days = [c[1] for c in fake_portal.day_calls]
    assert days == [d(0), d(1), d(1), d(2), d(2), d(3), d(4)]
    got = await rows(hass, SID1)
    assert len(got) == 120
    assert [r[2] for r in got] == [float(i + 1) for i in range(120)]


async def test_relogin_cap_stops_run(
    recorder_mock: Recorder, hass: HomeAssistant, mock_config_entry, fake_portal: FakePortal
) -> None:
    fake_portal.add_service_point(SP1, d(0), d(9), default=1.0)
    for nth in (2, 4, 6, 8):  # every other get_day call expires
        fake_portal.fail_on("get_day", nth, SessionExpired())
    coordinator = await make(hass, mock_config_entry)
    with (
        patch("custom_components.silverblaze.coordinator.MAX_RELOGINS_PER_RUN", 2),
        pytest.raises(CannotConnect),
    ):
        await coordinator.async_sync()
    assert fake_portal.calls["login"] == 3  # the cap plus the first login
    assert len(await rows(hass, SID1)) == 72  # days 0, 1 and 2 kept


async def test_relogin_cap_is_per_run(
    recorder_mock: Recorder, hass: HomeAssistant, mock_config_entry, fake_portal: FakePortal
) -> None:
    fake_portal.add_service_point(SP1, d(0), d(3), flat(4))
    fake_portal.fail_on("get_day", 2, SessionExpired())  # run 1: one expiry
    fake_portal.fail_on("get_day", 7, SessionExpired())  # run 2: one expiry
    coordinator = await make(hass, mock_config_entry)
    with patch("custom_components.silverblaze.coordinator.MAX_RELOGINS_PER_RUN", 1):
        await coordinator.async_sync()
        await coordinator.async_sync()
    assert fake_portal.calls["login"] == 4
    assert len(await rows(hass, SID1)) == 96


async def test_transient_failure_keeps_progress_and_resumes(
    recorder_mock: Recorder, hass: HomeAssistant, mock_config_entry, fake_portal: FakePortal
) -> None:
    all_days = [(d(n), day_values(n)) for n in range(5)]
    fake_portal.add_service_point(SP1, d(0), d(4), {d(n): day_values(n) for n in range(5)})
    fake_portal.fail_on("get_day", 4, CannotConnect("boom"))
    coordinator = await make(hass, mock_config_entry)
    with pytest.raises(CannotConnect):
        await coordinator.async_sync()
    assert await rows(hass, SID1) == expected(all_days[:3])

    fake_portal.day_calls.clear()
    await coordinator.async_sync()
    # The whole span is inside the 30-day correction window, so it is re-read.
    assert [c[1] for c in fake_portal.day_calls] == [d(n) for n in range(5)]
    got = await rows(hass, SID1)
    assert got == expected(all_days)
    assert len({r[0] for r in got}) == len(got) == 120


async def test_resume_with_existing_hours_only_touches_its_window(
    recorder_mock: Recorder, hass: HomeAssistant, mock_config_entry, fake_portal: FakePortal
) -> None:
    """Stored hours of earlier days must not be rewritten or double-counted."""
    fake_portal.add_service_point(SP1, d(0), d(2), {d(n): day_values(n) for n in range(3)})
    coordinator = await make(hass, mock_config_entry)
    await coordinator.async_sync()
    await async_wait_recording_done(hass)

    # Portal grows to d(5); the last stored day is revised with some null hours
    # (stored values stay) and a changed value.
    revised = day_values(2)
    revised[0] = 999.0
    revised[10] = None
    fake_portal.add_service_point(
        SP1, d(0), d(5), {**{d(n): day_values(n) for n in range(6)}, d(2): revised}
    )
    fake_portal.day_calls.clear()
    await coordinator.async_sync()

    day2 = list(day_values(2))
    day2[0] = 999.0  # portal wins; day2[10] stays at its stored value
    got = await rows(hass, SID1)
    assert got == expected(
        [(d(0), day_values(0)), (d(1), day_values(1)), (d(2), day2)]
        + [(d(n), day_values(n)) for n in range(3, 6)]
    )
    assert len(got) == 144


async def test_logs_have_no_identifiers_or_secrets(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_config_entry,
    fake_portal: FakePortal,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="custom_components.silverblaze")
    fake_portal.add_service_point(SP1, d(0), d(11), default=1.0)
    fake_portal.fail_on("get_day", 3, SessionExpired())
    coordinator = await make(hass, mock_config_entry)
    await coordinator.async_sync()
    text = "\n".join(
        r.getMessage() for r in caplog.records if r.name.startswith("custom_components.silverblaze")
    )
    assert "Backfilling 12 days for Idaho Power electric" in text
    assert "Finished importing 12 days" in text
    assert re.search(r"Imported \d+ of 12 days for Idaho Power electric", text)
    for secret in (
        SP1,
        SP1.lower(),
        "SYNTHETIC_ENC",
        "SYNTHETIC_PARAMS",
        "SYNTHETIC_TAB",
        "user@example.com",
        "SYNTHETIC_PASSWORD",
    ):
        assert secret not in text


# --- scheduled cycle / correction window (Story 8) --------------------------------------

LONG = 33  # longer than the 30-day correction window; ends before the spring DST day


def flat(n: int, value: float = 1.0) -> dict[date, list[float]]:
    return {d(i): [value] * 24 for i in range(n)}


async def test_identical_cycles_are_idempotent(
    recorder_mock: Recorder, hass: HomeAssistant, mock_config_entry, fake_portal: FakePortal
) -> None:
    fake_portal.add_service_point(
        SP1, d(0), d(LONG), {d(i): day_values(i) for i in range(LONG + 1)}
    )
    coordinator = await make(hass, mock_config_entry)
    await coordinator.async_sync()
    first = await rows(hass, SID1)
    await coordinator.async_sync()
    second = await rows(hass, SID1)
    assert len(first) == 24 * (LONG + 1)
    assert second == first


async def test_correction_inside_window_shifts_later_sums(
    recorder_mock: Recorder, hass: HomeAssistant, mock_config_entry, fake_portal: FakePortal
) -> None:
    fake_portal.add_service_point(SP1, d(0), d(LONG), {d(i): [1.0] * 24 for i in range(LONG + 1)})
    coordinator = await make(hass, mock_config_entry)
    await coordinator.async_sync()
    before = await rows(hass, SID1)

    fixed = d(LONG - 5)
    vals = [1.0] * 24
    vals[7] = 1.5
    fake_portal.set_day(SP1, fixed, vals)
    fake_portal.day_calls.clear()
    await coordinator.async_sync()
    after = await rows(hass, SID1)

    assert len(after) == len(before)
    hour = local_midnight_utc(fixed) + timedelta(hours=7)
    for b, a in zip(before, after, strict=True):
        assert a[0] == b[0]
        if a[0] < hour:
            assert a == b
        elif a[0] == hour:
            assert a[1] == 1.5
            assert a[2] == b[2] + 0.5
        else:
            assert a[1] == b[1]
            assert a[2] == b[2] + 0.5
    # Window: newest - 30 days .. newest.
    assert fake_portal.day_calls[0][1] == d(LONG - 30)
    assert fake_portal.day_calls[-1][1] == d(LONG)


async def test_late_filled_hour_is_inserted(
    recorder_mock: Recorder, hass: HomeAssistant, mock_config_entry, fake_portal: FakePortal
) -> None:
    gap = d(LONG - 3)
    vals = [1.0] * 24
    vals[4] = None
    fake_portal.add_service_point(SP1, d(0), d(LONG), {**flat(LONG + 1), gap: vals})
    coordinator = await make(hass, mock_config_entry)
    await coordinator.async_sync()
    before = await rows(hass, SID1)
    hour = local_midnight_utc(gap) + timedelta(hours=4)
    assert hour not in {r[0] for r in before}

    fake_portal.set_day(SP1, gap, [1.0] * 24)
    await coordinator.async_sync()
    after = await rows(hass, SID1)

    assert len(after) == len(before) + 1
    by_hour = {r[0]: r for r in after}
    assert by_hour[hour][1] == 1.0
    old = {r[0]: r for r in before}
    for t, row in by_hour.items():
        if t < hour:
            assert row == old[t]
        elif t > hour:
            assert row[2] == old[t][2] + 1.0


async def test_hour_now_null_is_kept_and_total_never_decreases(
    recorder_mock: Recorder, hass: HomeAssistant, mock_config_entry, fake_portal: FakePortal
) -> None:
    fake_portal.add_service_point(SP1, d(0), d(LONG), flat(LONG + 1))
    coordinator = await make(hass, mock_config_entry)
    await coordinator.async_sync()
    before = await rows(hass, SID1)

    day = d(LONG - 2)
    vals: list[float | None] = [1.0] * 24
    vals[9] = None
    vals[10] = None
    fake_portal.set_day(SP1, day, vals)
    await coordinator.async_sync()
    after = await rows(hass, SID1)

    assert after == before
    sums = [r[2] for r in after]
    assert sums == sorted(sums)


async def test_catch_up_after_long_outage_has_no_gap(
    recorder_mock: Recorder, hass: HomeAssistant, mock_config_entry, fake_portal: FakePortal
) -> None:
    # A base date after the spring DST day: the fake emits 24 values per day.
    fake_portal.add_service_point(SP1, b(0), b(1))
    coordinator = await make(hass, mock_config_entry)
    await coordinator.async_sync()

    fake_portal.failing = CannotConnect("down")
    for _ in range(3):
        with pytest.raises(CannotConnect):
            await coordinator.async_sync()
    fake_portal.failing = None
    fake_portal.set_newest(SP1, b(42))  # a gap of 41 days past the last stored day
    fake_portal.day_calls.clear()
    await coordinator.async_sync()

    got = await rows(hass, SID1)
    expected_hours = [local_midnight_utc(b(0)) + timedelta(hours=h) for h in range(24 * 43)]
    assert [r[0] for r in got] == expected_hours
    assert [r[2] for r in got] == [float(i + 1) for i in range(24 * 43)]
    # Window starts at the last stored day (re-read), older than newest - 30.
    assert fake_portal.day_calls[0][1] == b(1)


async def test_catch_up_fills_hours_null_on_the_last_stored_day(
    recorder_mock: Recorder, hass: HomeAssistant, mock_config_entry, fake_portal: FakePortal
) -> None:
    """Hours published late on the newest stored day survive a >30-day outage."""
    day = b(2)
    partial: list[float | None] = [2.0] * 12 + [None] * 12
    fake_portal.add_service_point(SP1, b(0), day, {day: partial})
    coordinator = await make(hass, mock_config_entry)
    await coordinator.async_sync()
    before = await rows(hass, SID1)
    assert len(before) == 24 * 2 + 12

    fake_portal.failing = CannotConnect("down")
    with pytest.raises(CannotConnect):
        await coordinator.async_sync()
    fake_portal.failing = None
    fake_portal.set_day(SP1, day, [2.0] * 12 + [3.0] * 12)
    fake_portal.set_newest(SP1, day + timedelta(days=34))
    fake_portal.day_calls.clear()
    await coordinator.async_sync()

    got = await rows(hass, SID1)
    assert fake_portal.day_calls[0][1] == day
    hours = [local_midnight_utc(b(0)) + timedelta(hours=h) for h in range(24 * 37)]
    assert [r[0] for r in got] == hours
    assert {r[0] for r in got} >= {local_midnight_utc(day) + timedelta(hours=h) for h in range(24)}
    previous = 0.0
    for _, state, total in got:
        assert total == previous + state
        previous = total
    portal_total = sum(v for vals in fake_portal.days[SP1].values() for v in vals if v is not None)
    assert got[-1][2] == portal_total
    # Everything before the late-filled hours is untouched.
    assert got[: len(before)] == before


async def test_correction_older_than_window_not_applied(
    recorder_mock: Recorder, hass: HomeAssistant, mock_config_entry, fake_portal: FakePortal
) -> None:
    fake_portal.add_service_point(SP1, d(0), d(LONG), flat(LONG + 1))
    coordinator = await make(hass, mock_config_entry)
    await coordinator.async_sync()
    before = await rows(hass, SID1)

    fake_portal.set_day(SP1, d(1), [9.0] * 24)  # far older than newest - 30
    fake_portal.day_calls.clear()
    await coordinator.async_sync()

    assert await rows(hass, SID1) == before
    assert d(1) not in [c[1] for c in fake_portal.day_calls]


async def test_scheduled_refresh_fires_after_interval(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_config_entry,
    fake_portal: FakePortal,
    freezer: FrozenDateTimeFactory,
) -> None:
    fake_portal.add_service_point(SP1, d(0), d(2), flat(3))
    coordinator = await make(hass, mock_config_entry)
    await coordinator.async_refresh()
    assert fake_portal.calls["login"] == 1
    assert coordinator.data.last_success is not None

    fake_portal.set_day(SP1, d(2), [2.0] * 24)
    freezer.tick(coordinator.update_interval + timedelta(seconds=1))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done(wait_background_tasks=True)

    assert fake_portal.calls["login"] == 2  # a fresh login per cycle
    got = await rows(hass, SID1)
    assert got[-1][1] == 2.0
    await coordinator.async_shutdown()


async def test_refresh_waits_for_running_sync(
    recorder_mock: Recorder, hass: HomeAssistant, mock_config_entry, fake_portal: FakePortal
) -> None:
    fake_portal.add_service_point(SP1, d(0), d(1), flat(2))
    release = fake_portal.block_on(1)
    coordinator = await make(hass, mock_config_entry)
    first = hass.async_create_task(coordinator.async_sync())
    await asyncio.wait_for(fake_portal.blocked.wait(), 5)
    second = hass.async_create_task(coordinator.async_refresh())
    await asyncio.sleep(0.05)
    assert not second.done()
    assert fake_portal.calls["login"] == 1

    release.set()
    await asyncio.wait_for(asyncio.gather(first, second), 5)
    assert fake_portal.calls["login"] == 2
    assert len(await rows(hass, SID1)) == 48
    await coordinator.async_shutdown()


async def test_last_values_retained_after_failed_cycle(
    recorder_mock: Recorder, hass: HomeAssistant, mock_config_entry, fake_portal: FakePortal
) -> None:
    fake_portal.add_service_point(SP1, d(0), d(2), flat(3))
    coordinator = await make(hass, mock_config_entry)
    await coordinator.async_refresh()
    data = coordinator.data
    assert data.latest_data_hour[SID1] == local_midnight_utc(d(2)) + timedelta(hours=23)
    assert data.last_success is not None

    fake_portal.set_newest(SP1, d(4))
    fake_portal.fail_on("get_day", 5, CannotConnect("boom"))  # fails mid-cycle
    await coordinator.async_refresh()
    assert not coordinator.last_update_success
    assert coordinator.data is data
    assert coordinator.data.latest_data_hour[SID1] == local_midnight_utc(d(2)) + timedelta(hours=23)
    assert coordinator.data.last_success == data.last_success

    await coordinator.async_refresh()
    assert coordinator.last_update_success
    assert coordinator.data.latest_data_hour[SID1] == local_midnight_utc(d(4)) + timedelta(hours=23)
    await coordinator.async_shutdown()


async def test_regular_cycle_logs_one_info_line(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_config_entry,
    fake_portal: FakePortal,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="custom_components.silverblaze")
    fake_portal.add_service_point(SP1, d(0), d(LONG), flat(LONG + 1))
    coordinator = await make(hass, mock_config_entry)
    await coordinator.async_sync()
    caplog.clear()
    await coordinator.async_sync()
    msgs = [r.getMessage() for r in caplog.records if r.name.startswith("custom_components")]
    assert msgs == ["Updated 31 days for Idaho Power electric"]


async def test_expiry_handling_is_per_call_across_runs(
    recorder_mock: Recorder, hass: HomeAssistant, mock_config_entry, fake_portal: FakePortal
) -> None:
    fake_portal.add_service_point(SP1, d(0), d(3), flat(4))
    fake_portal.fail_on("get_day", 2, SessionExpired())  # run 1: one expiry
    fake_portal.fail_on("get_day", 7, SessionExpired())  # run 2: one expiry
    coordinator = await make(hass, mock_config_entry)
    await coordinator.async_sync()
    await coordinator.async_sync()
    assert fake_portal.calls["login"] == 4
    assert len(await rows(hass, SID1)) == 96


async def test_retry_right_after_relogin_expiring_again_fails(
    recorder_mock: Recorder, hass: HomeAssistant, mock_config_entry, fake_portal: FakePortal
) -> None:
    fake_portal.add_service_point(SP1, d(0), d(3), flat(4))
    fake_portal.fail_on("get_day", 2, SessionExpired())
    fake_portal.fail_on("get_day", 3, SessionExpired())  # the retried call itself
    coordinator = await make(hass, mock_config_entry)
    with pytest.raises(CannotConnect):
        await coordinator.async_sync()
    assert fake_portal.calls["login"] == 2


async def test_unload_mid_refresh_leaves_no_timer(
    recorder_mock: Recorder, hass: HomeAssistant, mock_config_entry, fake_portal: FakePortal
) -> None:
    fake_portal.add_service_point(SP1, d(0), d(2))
    release = fake_portal.block_on(2)
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await asyncio.wait_for(fake_portal.blocked.wait(), 5)
    coordinator = mock_config_entry.runtime_data

    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    release.set()
    await hass.async_block_till_done()

    assert coordinator._unsub_refresh is None


# --- options and failure mapping ---


def varied(n: int) -> dict[date, list[float]]:
    return {d(i): [float(i * 100 + h + 1) for h in range(24)] for i in range(n)}


def reauth_flows(hass: HomeAssistant, entry) -> list:
    return list(entry.async_get_active_flows(hass, {SOURCE_REAUTH}))


async def test_interval_from_options_drives_scheduling(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_config_entry,
    fake_portal: FakePortal,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="custom_components.silverblaze")
    fake_portal.add_service_point(SP1, d(0), d(1), flat(2))
    coordinator = await make(hass, mock_config_entry, {"update_interval_hours": 6})
    assert coordinator.update_interval == timedelta(hours=6)
    assert any(
        re.fullmatch(r"Next refresh in 6 hours \(at \d{4}-\d\d-\d\d \d\d:\d\d UTC\)", m)
        for m in (r.getMessage() for r in caplog.records)
    )
    caplog.clear()
    await coordinator.async_refresh()
    assert any("Next refresh in 6 hours" in r.getMessage() for r in caplog.records)
    assert fake_portal.calls["login"] == 1

    freezer.tick(timedelta(hours=5, minutes=59))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done(wait_background_tasks=True)
    assert fake_portal.calls["login"] == 1
    freezer.tick(timedelta(minutes=2))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done(wait_background_tasks=True)
    assert fake_portal.calls["login"] == 2
    await coordinator.async_shutdown()


async def test_interval_defaults_to_twelve_hours(
    recorder_mock: Recorder, hass: HomeAssistant, mock_config_entry, fake_portal: FakePortal
) -> None:
    coordinator = await make(hass, mock_config_entry)
    assert coordinator.update_interval == timedelta(hours=12)
    await coordinator.async_shutdown()


async def test_import_from_moved_earlier_rewrites_series_consistently(
    recorder_mock: Recorder, hass: HomeAssistant, mock_config_entry, fake_portal: FakePortal
) -> None:
    n = 34  # d(0)..d(3) fall outside the 30-day correction window; stays before the DST day
    fake_portal.add_service_point(SP1, d(0), d(n - 1), varied(n))
    coordinator = await make(hass, mock_config_entry, {"import_from": d(3).isoformat()})
    await coordinator.async_sync()
    before = await rows(hass, SID1)
    assert before[0][0] == local_midnight_utc(d(3))
    assert len(before) == 24 * (n - 3)

    hass.config_entries.async_update_entry(
        mock_config_entry, options={"import_from": d(0).isoformat()}
    )
    fake_portal.day_calls.clear()
    await SilverblazeCoordinator(hass, mock_config_entry).async_sync()
    after = await rows(hass, SID1)

    assert after == expected([(d(i), varied(n)[d(i)]) for i in range(n)])
    assert d(0) in {c[1] for c in fake_portal.day_calls}
    # Later hours keep their values; the sums moved by exactly the new earlier total.
    shift = sum(sum(varied(n)[d(i)]) for i in range(3))
    tail = after[24 * 3 :]
    assert [(t, v) for t, v, _ in tail] == [(t, v) for t, v, _ in before]
    assert [round(s - b_s, 6) for (_, _, s), (_, _, b_s) in zip(tail, before, strict=True)] == [
        shift
    ] * len(before)


@pytest.mark.parametrize("same_instance", [True, False])
async def test_import_all_imports_earlier_days(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_config_entry,
    fake_portal: FakePortal,
    same_instance: bool,
) -> None:
    n = 34  # d(0)..d(3) fall outside the 30-day correction window; stays before the DST day
    fake_portal.add_service_point(SP1, d(0), d(n - 1), varied(n))
    options = {"import_from": d(3).isoformat(), "update_interval_hours": 8}
    coordinator = await make(hass, mock_config_entry, options)
    await coordinator.async_sync()
    before = await rows(hass, SID1)
    assert before[0][0] == local_midnight_utc(d(3))

    # Exactly what "Import all available history" stores: the date removed, interval kept.
    hass.config_entries.async_update_entry(mock_config_entry, options={"update_interval_hours": 8})
    if not same_instance:
        coordinator = SilverblazeCoordinator(hass, mock_config_entry)
    await coordinator.async_sync()
    after = await rows(hass, SID1)

    assert after == expected([(d(i), varied(n)[d(i)]) for i in range(n)])
    tail = after[24 * 3 :]
    assert [(t, v) for t, v, _ in tail] == [(t, v) for t, v, _ in before]


async def test_import_from_moved_later_deletes_nothing(
    recorder_mock: Recorder, hass: HomeAssistant, mock_config_entry, fake_portal: FakePortal
) -> None:
    n = 34
    fake_portal.add_service_point(SP1, d(0), d(n - 1), varied(n))
    coordinator = await make(hass, mock_config_entry)
    await coordinator.async_sync()
    before = await rows(hass, SID1)
    assert len(before) == 24 * n

    hass.config_entries.async_update_entry(
        mock_config_entry, options={"import_from": d(30).isoformat()}
    )
    await SilverblazeCoordinator(hass, mock_config_entry).async_sync()
    assert await rows(hass, SID1) == before


async def test_invalid_auth_starts_reauth_and_stops_polling(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_config_entry,
    fake_portal: FakePortal,
    freezer: FrozenDateTimeFactory,
) -> None:
    fake_portal.add_service_point(SP1, d(0), d(2), varied(3))
    coordinator = await make(hass, mock_config_entry)
    await coordinator.async_refresh()
    before = await rows(hass, SID1)
    assert coordinator._unsub_refresh is not None

    fake_portal.failing = InvalidAuth()
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert not coordinator.last_update_success
    assert len(reauth_flows(hass, mock_config_entry)) == 1
    assert coordinator._unsub_refresh is None

    logins = fake_portal.calls["login"]
    freezer.tick(timedelta(hours=13))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done(wait_background_tasks=True)
    assert fake_portal.calls["login"] == logins  # no further scheduled refresh
    assert await rows(hass, SID1) == before
    await coordinator.async_shutdown()


@pytest.mark.parametrize("exc", [CannotConnect("x"), UnexpectedResponse("x"), SessionExpired("x")])
async def test_transient_failure_retries_without_reauth(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_config_entry,
    fake_portal: FakePortal,
    exc: Exception,
) -> None:
    """Includes a bare SessionExpired from the first login: cannot_connect, not re-auth."""
    fake_portal.add_service_point(SP1, d(0), d(2), varied(3))
    coordinator = await make(hass, mock_config_entry)
    await coordinator.async_refresh()
    before = await rows(hass, SID1)

    fake_portal.failing = exc
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert not coordinator.last_update_success
    err = coordinator.last_exception
    assert isinstance(err, UpdateFailed)
    assert err.translation_key == "cannot_connect"
    assert str(err) == type(exc).__name__
    assert reauth_flows(hass, mock_config_entry) == []
    assert coordinator._unsub_refresh is not None  # next refresh still scheduled
    assert await rows(hass, SID1) == before

    fake_portal.failing = None
    await coordinator.async_refresh()
    assert coordinator.last_update_success
    await coordinator.async_shutdown()


async def test_login_flow_changed_warns_once_and_never_reauths(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_config_entry,
    fake_portal: FakePortal,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake_portal.add_service_point(SP1, d(0), d(2), varied(3))
    coordinator = await make(hass, mock_config_entry)
    await coordinator.async_refresh()
    before = await rows(hass, SID1)

    caplog.clear()
    fake_portal.failing = LoginFlowChanged("SYNTHETIC_SECRET")
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    warnings = [r for r in caplog.records if "login flow changed" in r.getMessage()]
    assert len(warnings) == 1
    assert warnings[0].levelno == logging.WARNING
    assert warnings[0].getMessage() == (
        "Idaho Power login flow changed in a way this version does not understand; "
        "will retry next cycle"
    )
    assert "SYNTHETIC_SECRET" not in caplog.text
    err = coordinator.last_exception
    assert isinstance(err, UpdateFailed)
    assert err.translation_key == "login_flow_changed"
    assert reauth_flows(hass, mock_config_entry) == []
    assert coordinator._unsub_refresh is not None
    assert await rows(hass, SID1) == before
    await coordinator.async_shutdown()


async def _second_cycle_day_requests(
    hass: HomeAssistant, entry, portal: FakePortal, values, n: int, options=None
) -> tuple[int, int]:
    """Run two cycles on one coordinator; return (first-cycle, second-cycle) day requests."""
    portal.add_service_point(SP1, d(0), d(n - 1), values)
    coordinator = await make(hass, entry, options)
    await coordinator.async_sync()
    portal.day_calls.clear()
    await coordinator.async_sync()
    second = len(portal.day_calls)
    # Even a fresh coordinator (HA restart) re-probes at most the one empty gap day.
    portal.day_calls.clear()
    await SilverblazeCoordinator(hass, entry).async_sync()
    return second, len(portal.day_calls)


async def test_partial_leading_day_does_not_retrigger_rewrite(
    recorder_mock: Recorder, hass: HomeAssistant, mock_config_entry, fake_portal: FakePortal
) -> None:
    n = 34
    values = {d(i): day_values(i) for i in range(n)}
    values[d(0)] = [None] * 10 + day_values(0)[10:]
    same, fresh = await _second_cycle_day_requests(hass, mock_config_entry, fake_portal, values, n)
    assert same <= 31
    assert fresh <= 31 + 1


async def test_empty_leading_day_does_not_retrigger_rewrite(
    recorder_mock: Recorder, hass: HomeAssistant, mock_config_entry, fake_portal: FakePortal
) -> None:
    n = 34
    values = {d(i): day_values(i) for i in range(n)}
    values[d(0)] = []
    same, fresh = await _second_cycle_day_requests(hass, mock_config_entry, fake_portal, values, n)
    assert same == 31  # memo set on the empty-gap path: just the correction window
    assert fresh == 31 + 1  # the one empty gap day is re-probed once after a restart


async def test_import_from_on_empty_day_does_not_retrigger_rewrite(
    recorder_mock: Recorder, hass: HomeAssistant, mock_config_entry, fake_portal: FakePortal
) -> None:
    n = 34
    values = {d(i): day_values(i) for i in range(n)}
    values[d(2)] = []
    same, fresh = await _second_cycle_day_requests(
        hass, mock_config_entry, fake_portal, values, n, {"import_from": d(2).isoformat()}
    )
    assert same == 31
    assert fresh == 31 + 1


async def test_interrupted_moved_earlier_rewrite_leaves_no_sum_step(
    recorder_mock: Recorder, hass: HomeAssistant, mock_config_entry, fake_portal: FakePortal
) -> None:
    n = 80  # well past the 31-day correction window; base date avoids DST
    fake_portal.add_service_point(SP1, b(0), b(n - 1), {b(i): day_values(i) for i in range(n)})
    coordinator = await make(hass, mock_config_entry, {"import_from": b(3).isoformat()})
    await coordinator.async_sync()
    hass.config_entries.async_update_entry(mock_config_entry, options={})

    fake_portal.fail_on("get_day", fake_portal.calls["get_day"] + 6, CannotConnect("x"))
    with pytest.raises(CannotConnect):
        await SilverblazeCoordinator(hass, mock_config_entry).async_sync()
    # Nothing was written by the interrupted rewrite.
    assert (await rows(hass, SID1))[0][0] == local_midnight_utc(b(3))

    await SilverblazeCoordinator(hass, mock_config_entry).async_sync()
    got = await rows(hass, SID1)
    assert len(got) == 24 * n
    prev = 0.0
    for _, state, total in got:
        assert total == pytest.approx(prev + state)
        prev = total
    assert prev == pytest.approx(sum(sum(day_values(i)) for i in range(n)))


async def test_interrupted_rewrite_is_retried_by_the_same_coordinator(
    recorder_mock: Recorder, hass: HomeAssistant, mock_config_entry, fake_portal: FakePortal
) -> None:
    n = 80
    fake_portal.add_service_point(SP1, b(0), b(n - 1), {b(i): day_values(i) for i in range(n)})
    first = await make(hass, mock_config_entry, {"import_from": b(3).isoformat()})
    await first.async_sync()
    hass.config_entries.async_update_entry(mock_config_entry, options={})

    coordinator = SilverblazeCoordinator(hass, mock_config_entry)
    fake_portal.fail_on("get_day", fake_portal.calls["get_day"] + 10, CannotConnect("x"))
    with pytest.raises(CannotConnect):
        await coordinator.async_sync()
    assert SID1 not in coordinator._checked_down_to
    assert (await rows(hass, SID1))[0][0] == local_midnight_utc(b(3))

    await coordinator.async_sync()  # same instance: must redo the rewrite
    got = await rows(hass, SID1)
    assert got[0][0] == local_midnight_utc(b(0))
    assert len(got) == 24 * n
    prev = 0.0
    for _, state, total in got:
        assert total == pytest.approx(prev + state)
        prev = total
    assert prev == pytest.approx(sum(sum(day_values(i)) for i in range(n)))
    assert coordinator._checked_down_to[SID1] == b(0)


async def test_rewrite_cycle_requests_each_day_exactly_once(
    recorder_mock: Recorder, hass: HomeAssistant, mock_config_entry, fake_portal: FakePortal
) -> None:
    n = 80
    values = {b(i): day_values(i) for i in range(n)}
    values[b(0)] = []
    values[b(1)] = []
    values[b(2)] = [None] * 7 + day_values(2)[7:]
    fake_portal.add_service_point(SP1, b(0), b(n - 1), values)
    first = await make(hass, mock_config_entry, {"import_from": b(5).isoformat()})
    await first.async_sync()
    hass.config_entries.async_update_entry(mock_config_entry, options={})

    fake_portal.day_calls.clear()
    await SilverblazeCoordinator(hass, mock_config_entry).async_sync()
    requested = [day for _, day in fake_portal.day_calls]
    assert len(requested) == n
    assert sorted(requested) == [b(i) for i in range(n)]  # no (service point, day) twice


async def test_empty_gap_probe_is_remembered_by_the_same_coordinator(
    recorder_mock: Recorder, hass: HomeAssistant, mock_config_entry, fake_portal: FakePortal
) -> None:
    n = 40
    values = {b(i): day_values(i) for i in range(n)}
    values[b(0)] = []  # the one gap day below the stored series has no data
    fake_portal.add_service_point(SP1, b(0), b(n - 1), values)
    first = await make(hass, mock_config_entry, {"import_from": b(1).isoformat()})
    await first.async_sync()
    before = await rows(hass, SID1)
    hass.config_entries.async_update_entry(mock_config_entry, options={})

    coordinator = SilverblazeCoordinator(hass, mock_config_entry)
    fake_portal.day_calls.clear()
    await coordinator.async_sync()  # probes the empty gap day plus the correction window
    assert len(fake_portal.day_calls) == 1 + 31
    assert coordinator._checked_down_to[SID1] == b(0)

    fake_portal.day_calls.clear()
    await coordinator.async_sync()
    assert len(fake_portal.day_calls) == 31  # no re-probe

    fake_portal.day_calls.clear()
    await SilverblazeCoordinator(hass, mock_config_entry).async_sync()
    assert len(fake_portal.day_calls) == 31 + 1  # restart: exactly one re-probe
    assert await rows(hass, SID1) == before


async def test_no_next_refresh_log_when_polling_disabled(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_config_entry,
    fake_portal: FakePortal,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="custom_components.silverblaze")
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(mock_config_entry, pref_disable_polling=True)
    coordinator = SilverblazeCoordinator(hass, mock_config_entry)
    assert not any("Next refresh" in r.getMessage() for r in caplog.records)
    await coordinator.async_shutdown()


async def test_unknown_utility_time_zone_is_cannot_connect(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_config_entry,
    fake_portal: FakePortal,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_portal.add_service_point(SP1, d(0), d(2), varied(3))
    coordinator = await make(hass, mock_config_entry)
    await coordinator.async_refresh()
    before = await rows(hass, SID1)
    calls_before = len(fake_portal.day_calls)

    async def unknown_zone(_name):
        return None

    monkeypatch.setattr(dt_util, "async_get_time_zone", unknown_zone)
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert not coordinator.last_update_success
    err = coordinator.last_exception
    assert isinstance(err, UpdateFailed)
    assert err.translation_key == "cannot_connect"
    assert str(err) == "CannotConnect"
    assert reauth_flows(hass, mock_config_entry) == []
    assert len(fake_portal.day_calls) == calls_before
    assert await rows(hass, SID1) == before
    await coordinator.async_shutdown()


# --- real client: an empty HTTP 200 reply means the session expired ---

_SB = "https://acewebsite.silverblaze.com"
_GPP_URL = re.compile(rf"^{re.escape(_SB)}/api/Page/GetPageParameters(\?.*)?$")
_META_URL = re.compile(rf"^{re.escape(_SB)}/api/DataDownload/Meta(\?.*)?$")
_DATA_URL = re.compile(rf"^{re.escape(_SB)}/api/ConsumptionV3/Data(\?.*)?$")


async def _no_sleep(_seconds: float) -> None:
    return None


class _EmptyReplyPortal:
    """Real client over mocked HTTP; chosen Data calls (1-based) answer 200 with no body."""

    def __init__(self, mock, empty_on: set[int]) -> None:
        self.logins = 0
        self.data_calls = 0
        self.empty_on = empty_on
        meta = json.loads(load_fixture_text("silverblaze/meta_one_sp.json"))
        span = meta["body"]["Metadata"][0]["TimeSeries"][0]["AmiTimeSpan"]
        span["Oldest"] = "2026-02-01T07:00:00Z"  # local midnight, Feb 1 (MST)
        span["Newest"] = "2026-02-05T06:00:00Z"  # 23:00 local, Feb 4
        mock.get(
            _GPP_URL,
            status=200,
            repeat=True,
            body=load_fixture_text("silverblaze/get_page_parameters.json"),
        )
        mock.get(_META_URL, status=200, repeat=True, body=json.dumps(meta))
        mock.get(_DATA_URL, repeat=True, callback=self._data)

    async def login(self, http, username, password) -> str:
        self.logins += 1
        return "SYNTHETIC_ENC"

    def _data(self, url, **kwargs):
        self.data_calls += 1
        if self.data_calls in self.empty_on:
            return CallbackResult(status=200, body="")
        day = date.fromisoformat(url.query["endDate"][:10])
        body = make_day_response(day, dt_util.get_time_zone("America/Denver"), [1.0] * 24)
        return CallbackResult(status=200, body=json.dumps(body))

    def patched(self):
        stack = ExitStack()
        stack.enter_context(patch.object(get_utility("idahopower"), "login", new=self.login))
        stack.enter_context(
            patch(
                "custom_components.silverblaze.coordinator.SilverblazeHttp",
                partial(SilverblazeHttp, sleep=_no_sleep),
            )
        )
        return stack


async def test_empty_reply_relogs_in_and_run_completes(
    recorder_mock: Recorder, hass: HomeAssistant, mock_config_entry, mock_aioresponse
) -> None:
    portal = _EmptyReplyPortal(mock_aioresponse, {2})
    with portal.patched():
        coordinator = await make(hass, mock_config_entry)
        await coordinator.async_sync()
    assert portal.logins == 2
    assert len(await rows(hass, SID1)) == 24 * 4


async def test_two_empty_replies_relogin_each_time(
    recorder_mock: Recorder, hass: HomeAssistant, mock_config_entry, mock_aioresponse
) -> None:
    portal = _EmptyReplyPortal(mock_aioresponse, {2, 4})
    with portal.patched():
        coordinator = await make(hass, mock_config_entry)
        await coordinator.async_sync()
    assert portal.logins == 3
    assert len(await rows(hass, SID1)) == 24 * 4


async def test_empty_reply_after_relogin_fails_run(
    recorder_mock: Recorder, hass: HomeAssistant, mock_config_entry, mock_aioresponse
) -> None:
    portal = _EmptyReplyPortal(mock_aioresponse, {3, 4})  # third day, and its retry
    with portal.patched():
        coordinator = await make(hass, mock_config_entry)
        with pytest.raises(CannotConnect):
            await coordinator.async_sync()
    assert portal.logins == 2
    assert len(await rows(hass, SID1)) == 24 * 2  # earlier days kept


# --- real client: a fall-back day reported as 24 hourly values ---

_FALL_EPOCHS = [datetime(2026, 11, 1, 6, tzinfo=UTC)] + [
    datetime(2026, 11, 1, 8, tzinfo=UTC) + timedelta(hours=i) for i in range(23)
]


class _FallBackPortal(_EmptyReplyPortal):
    """Real client over mocked HTTP; 2026-11-01 answers with the portal's 24-slot shape."""

    def __init__(self, mock) -> None:
        self.logins = 0
        self.data_calls = 0
        meta = json.loads(load_fixture_text("silverblaze/meta_one_sp.json"))
        span = meta["body"]["Metadata"][0]["TimeSeries"][0]["AmiTimeSpan"]
        span["Oldest"] = "2026-10-31T06:00:00Z"  # local midnight, Oct 31 (MDT)
        span["Newest"] = "2026-11-03T06:00:00Z"  # 23:00 local, Nov 2 (MST)
        mock.get(
            _GPP_URL,
            status=200,
            repeat=True,
            body=load_fixture_text("silverblaze/get_page_parameters.json"),
        )
        mock.get(_META_URL, status=200, repeat=True, body=json.dumps(meta))
        mock.get(_DATA_URL, repeat=True, callback=self._data)

    def _data(self, url, **kwargs):
        day = date.fromisoformat(url.query["endDate"][:10])
        tz = dt_util.get_time_zone("America/Denver")
        if day == date(2026, 11, 1):
            # Same shape the client test reads: 24 values, first 1:00 absent, second 1:00 zero.
            body = load_fixture_json("silverblaze/day_dst_fall_2026-11-01_24_slots.json")
        else:
            body = make_day_response(day, tz, [1.0] * 24)
        return CallbackResult(status=200, body=json.dumps(body))


@pytest.mark.parametrize("zone", ["UTC", "America/Denver"])
async def test_fall_back_day_stored_as_reported(
    recorder_mock: Recorder, hass: HomeAssistant, mock_config_entry, mock_aioresponse, zone
) -> None:
    await hass.config.async_set_time_zone(zone)
    portal = _FallBackPortal(mock_aioresponse)
    with portal.patched():
        coordinator = await make(hass, mock_config_entry)
        await coordinator.async_sync()
        got = await rows(hass, SID1)

        nov1 = [
            r
            for r in got
            if datetime(2026, 11, 1, 6, tzinfo=UTC) <= r[0] < _FALL_EPOCHS[-1] + timedelta(hours=1)
        ]
        assert [r[0] for r in nov1] == _FALL_EPOCHS
        starts = [r[0] for r in got]
        assert len(starts) == len(set(starts))
        assert datetime(2026, 11, 1, 7, tzinfo=UTC) not in starts
        by_start = {r[0]: r for r in got}
        assert by_start[datetime(2026, 11, 1, 8, tzinfo=UTC)][1] == 0.0
        assert sum(r[1] for r in nov1) == 25.0

        previous = 0.0
        for _, state, total in got:
            assert total == pytest.approx(previous + state)
            previous = total
        assert got[0][0] == datetime(2026, 10, 31, 6, tzinfo=UTC)
        assert got[-1][0] == datetime(2026, 11, 3, 6, tzinfo=UTC)
        nov2 = [r for r in got if r[0] >= datetime(2026, 11, 2, 7, tzinfo=UTC)]
        assert nov2[0][0] == datetime(2026, 11, 2, 7, tzinfo=UTC)

        await coordinator.async_sync()  # correction-window re-read
        assert await rows(hass, SID1) == got
