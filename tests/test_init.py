"""Setup/unload smoke test and import-hygiene check."""

from __future__ import annotations

import asyncio
import logging
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from homeassistant import loader
from homeassistant.components.recorder import Recorder
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

import custom_components.silverblaze as integration
from custom_components.silverblaze.api import (
    CannotConnect,
    EndpointNotAllowed,
    InvalidAuth,
    SessionExpired,
)
from custom_components.silverblaze.const import DOMAIN
from custom_components.silverblaze.importer import statistic_id

from .fake_portal import FakePortal, local_midnight_utc

SP1 = "SP0000000001"
SID1 = statistic_id("idahopower", "electric", SP1)
D0 = date(2026, 2, 1)


def d(n: int) -> date:
    return D0 + timedelta(days=n)


def day_values(n: int) -> list[float]:
    return [float(n * 100 + h + 1) for h in range(24)]


async def stored_rows(hass: HomeAssistant) -> list[tuple[datetime, float]]:
    await async_wait_recording_done(hass)
    result = await hass.async_add_executor_job(
        lambda: statistics_during_period(
            hass,
            datetime(2000, 1, 1, tzinfo=UTC),
            datetime(2100, 1, 1, tzinfo=UTC),
            {SID1},
            "hour",
            None,
            {"sum"},
        )
    )
    return [(datetime.fromtimestamp(r["start"], UTC), r["sum"]) for r in result.get(SID1, [])]


async def wait_for(event: asyncio.Event) -> None:
    async with asyncio.timeout(5):
        await event.wait()


async def wait_idle(entry) -> None:
    """Wait for the entry's background tasks to finish (they may already have)."""
    tasks = background_tasks(entry)
    if tasks:
        await asyncio.wait_for(asyncio.gather(*tasks), 5)


def background_tasks(entry) -> set:
    return set(entry._background_tasks)


async def test_setup_returns_while_backfill_blocked(
    recorder_mock: Recorder, hass: HomeAssistant, mock_config_entry, fake_portal: FakePortal
) -> None:
    """Setup completes with the backfill still pending; unload cancels it cleanly."""
    fake_portal.add_service_point(SP1, d(0), d(2), {d(i): day_values(i) for i in range(3)})
    release = fake_portal.block_on(2)
    mock_config_entry.add_to_hass(hass)

    async with asyncio.timeout(5):
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    assert mock_config_entry.state is ConfigEntryState.LOADED
    await wait_for(fake_portal.blocked)
    (task,) = background_tasks(mock_config_entry)
    assert not task.done()
    session = mock_config_entry.runtime_data._session
    assert not session.closed

    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    assert mock_config_entry.state is ConfigEntryState.NOT_LOADED
    assert task.cancelled()
    assert background_tasks(mock_config_entry) == set()
    # Detached sessions have no connector left.
    assert session.connector is None
    release.set()


async def test_restart_mid_backfill_resumes_exactly(
    recorder_mock: Recorder, hass: HomeAssistant, mock_config_entry, fake_portal: FakePortal
) -> None:
    """Unload after some days, set up again: every hour present once, exact sum."""
    vals = {d(i): day_values(i) for i in range(4)}
    vals[d(1)][3] = None
    fake_portal.add_service_point(SP1, d(0), d(3), vals)
    release = fake_portal.block_on(3)
    mock_config_entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await wait_for(fake_portal.blocked)
    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    release.set()
    first = await stored_rows(hass)
    assert 0 < len(first) < 24 * 4

    fake_portal.block_event = None
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await wait_idle(mock_config_entry)

    got = await stored_rows(hass)
    expected_hours = [
        local_midnight_utc(day) + timedelta(hours=i)
        for day in sorted(vals)
        for i, v in enumerate(vals[day])
        if v is not None
    ]
    assert [t for t, _ in got] == expected_hours
    assert len({t for t, _ in got}) == len(got)
    assert got[-1][1] == sum(v for day in vals.values() for v in day if v is not None)


async def test_concurrent_sync_waits_for_backfill(
    recorder_mock: Recorder, hass: HomeAssistant, mock_config_entry, fake_portal: FakePortal
) -> None:
    """A second async_sync() waits on the entry lock until the backfill finishes."""
    fake_portal.add_service_point(SP1, d(0), d(1), {d(i): day_values(i) for i in range(2)})
    release = fake_portal.block_on(1)
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await wait_for(fake_portal.blocked)
    coordinator = mock_config_entry.runtime_data
    assert coordinator.sync_lock.locked()

    second = hass.async_create_task(coordinator.async_sync())
    await asyncio.sleep(0.05)
    assert not second.done()
    assert fake_portal.calls["login"] == 1  # the second run has not started

    release.set()
    await asyncio.wait_for(second, 5)
    # Backfill: d0, d1; then the second run re-reads its 30-day window (the whole short history).
    assert fake_portal.calls["login"] == 2
    assert [c[1] for c in fake_portal.day_calls][:2] == [d(0), d(1)]
    got = await stored_rows(hass)
    assert len(got) == 48
    assert got[-1][1] == sum(sum(day_values(i)) for i in range(2))
    await hass.config_entries.async_unload(mock_config_entry.entry_id)


async def test_endpoint_not_allowed_is_logged_with_traceback(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_config_entry,
    fake_portal: FakePortal,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake_portal.add_service_point(SP1, d(0), d(0))
    fake_portal.fail_on("get_meta", 1, EndpointNotAllowed("https://evil.example/x"))
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await wait_idle(mock_config_entry)

    record = next(r for r in caplog.records if "outside the allowlist" in r.getMessage())
    assert record.levelno == logging.ERROR
    assert record.exc_info is not None
    assert mock_config_entry.state is ConfigEntryState.LOADED


@pytest.mark.parametrize(
    "exc", [SessionExpired("SYNTHETIC_SECRET"), CannotConnect("SYNTHETIC_SECRET")]
)
async def test_silverblaze_error_logged_redacted(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_config_entry,
    fake_portal: FakePortal,
    caplog: pytest.LogCaptureFixture,
    exc: Exception,
) -> None:
    fake_portal.add_service_point(SP1, d(0), d(0))
    fake_portal.fail_on("login", 1, exc)
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await wait_idle(mock_config_entry)

    record = next(r for r in caplog.records if "ended early" in r.getMessage())
    assert record.levelno == logging.WARNING
    assert record.exc_info is None
    assert type(exc).__name__ in record.getMessage()
    assert "SYNTHETIC_SECRET" not in caplog.text
    assert mock_config_entry.state is ConfigEntryState.LOADED


async def test_invalid_auth_on_first_refresh_starts_reauth(
    recorder_mock: Recorder, hass: HomeAssistant, mock_config_entry, fake_portal: FakePortal
) -> None:
    fake_portal.add_service_point(SP1, d(0), d(1), {d(i): day_values(i) for i in range(2)})
    fake_portal.failing = InvalidAuth()
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await wait_idle(mock_config_entry)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert len(list(mock_config_entry.async_get_active_flows(hass, {SOURCE_REAUTH}))) == 1
    assert fake_portal.calls["login"] == 1
    assert await stored_rows(hass) == []
    await hass.config_entries.async_unload(mock_config_entry.entry_id)


async def test_reauth_success_reloads_and_catches_up(
    recorder_mock: Recorder, hass: HomeAssistant, mock_config_entry, fake_portal: FakePortal
) -> None:
    fake_portal.add_service_point(SP1, d(0), d(1), {d(i): day_values(i) for i in range(2)})
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await wait_idle(mock_config_entry)
    assert len(await stored_rows(hass)) == 48

    # The password stops working while the portal publishes more days.
    for i in range(2, 5):
        fake_portal.set_newest(SP1, d(i))
        fake_portal.set_day(SP1, d(i), day_values(i))
    fake_portal.failing = InvalidAuth()
    await mock_config_entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    (flow,) = mock_config_entry.async_get_active_flows(hass, {SOURCE_REAUTH})
    assert len(await stored_rows(hass)) == 48  # nothing imported, nothing lost

    fake_portal.failing = None
    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"], {"password": "new-password"}
    )
    assert result["reason"] == "reauth_successful"
    await hass.async_block_till_done()
    await wait_idle(mock_config_entry)

    assert mock_config_entry.state is ConfigEntryState.LOADED
    got = await stored_rows(hass)
    assert len(got) == 24 * 5
    assert got[-1][1] == sum(sum(day_values(i)) for i in range(5))
    await hass.config_entries.async_unload(mock_config_entry.entry_id)


async def test_loader_finds_integration(hass: HomeAssistant) -> None:
    """Home Assistant's loader resolves the integration from this repository."""
    integration_info = await loader.async_get_integration(hass, DOMAIN)
    assert integration_info.domain == DOMAIN
    assert integration_info.pkg_path == "custom_components.silverblaze"
    assert Path(integration_info.file_path) == Path(integration.__file__).parent


def test_package_imports_without_homeassistant() -> None:
    """Importing the package must not pull in homeassistant."""
    code = (
        "import sys; import custom_components.silverblaze; "
        "import custom_components.silverblaze.const; "
        "sys.exit(1 if any(m == 'homeassistant' or m.startswith('homeassistant.') "
        "for m in sys.modules) else 0)"
    )
    result = subprocess.run([sys.executable, "-c", code], check=False, capture_output=True)
    assert result.returncode == 0, result.stderr.decode()
