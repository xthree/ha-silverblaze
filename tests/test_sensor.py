"""Freshness sensors: device layout, values, failure behaviour and restore."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta

from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.recorder import Recorder
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.silverblaze.api import CannotConnect
from custom_components.silverblaze.const import DOMAIN

from .fake_portal import FakePortal, local_midnight_utc

SP1 = "SP0000000001"
SP2 = "SP0000000002"
D0 = date(2026, 2, 1)
NOW = datetime(2026, 2, 10, 12, 0, tzinfo=UTC)


def day_values(n: int) -> list[float]:
    return [float(n * 100 + h + 1) for h in range(24)]


async def setup_entry(hass: HomeAssistant, entry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await wait_idle(hass, entry)


async def wait_idle(hass: HomeAssistant, entry) -> None:
    tasks = set(entry._background_tasks)
    if tasks:
        await asyncio.wait_for(asyncio.gather(*tasks), 5)
    await hass.async_block_till_done()


def entry_entities(hass: HomeAssistant, entry) -> list[er.RegistryEntry]:
    return er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)


def state_of(
    hass: HomeAssistant, entry, key: str, ident: str = f"idahopower_electric_{SP1.lower()}"
):
    ent = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{ident}_{key}")
    assert ent is not None
    return hass.states.get(ent)


async def test_device_and_two_entities(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_config_entry,
    fake_portal: FakePortal,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(NOW)
    fake_portal.add_service_point(SP1, D0, D0 + timedelta(days=2), {})
    for i in range(3):
        fake_portal.set_day(SP1, D0 + timedelta(days=i), day_values(i))
    await setup_entry(hass, mock_config_entry)

    devices = dr.async_entries_for_config_entry(dr.async_get(hass), mock_config_entry.entry_id)
    assert len(devices) == 1
    assert devices[0].name == "Idaho Power electric"
    assert devices[0].manufacturer == "Silverblaze"
    assert devices[0].model == "Idaho Power"
    assert devices[0].entry_type is dr.DeviceEntryType.SERVICE

    entities = entry_entities(hass, mock_config_entry)
    assert len(entities) == 2
    assert {e.entity_category for e in entities} == {er.EntityCategory.DIAGNOSTIC}

    latest = state_of(hass, mock_config_entry, "latest_data_hour")
    newest = local_midnight_utc(D0 + timedelta(days=2)) + timedelta(hours=23)
    assert datetime.fromisoformat(latest.state) == newest
    assert latest.name.endswith("Latest data hour")
    last = state_of(hass, mock_config_entry, "last_successful_update")
    assert datetime.fromisoformat(last.state) == NOW
    assert last.name.endswith("Last successful update")


async def test_failure_keeps_values_and_availability(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_config_entry,
    fake_portal: FakePortal,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(NOW)
    fake_portal.add_service_point(SP1, D0, D0, {D0: day_values(0)})
    await setup_entry(hass, mock_config_entry)
    before = {
        k: state_of(hass, mock_config_entry, k).state
        for k in ("latest_data_hour", "last_successful_update")
    }

    freezer.move_to(NOW + timedelta(hours=12))
    fake_portal.failing = CannotConnect("down")
    coordinator = mock_config_entry.runtime_data
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert not coordinator.last_update_success

    for key, value in before.items():
        state = state_of(hass, mock_config_entry, key)
        assert state.state == value
        assert state.state != STATE_UNAVAILABLE

    # A later success moves "last successful update" forward again.
    fake_portal.failing = None
    freezer.move_to(NOW + timedelta(hours=24))
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    last = state_of(hass, mock_config_entry, "last_successful_update")
    assert datetime.fromisoformat(last.state) == NOW + timedelta(hours=24)


async def test_values_restored_before_first_refresh(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_config_entry,
    fake_portal: FakePortal,
    freezer: FrozenDateTimeFactory,
) -> None:
    freezer.move_to(NOW)
    fake_portal.add_service_point(SP1, D0, D0, {D0: day_values(0)})
    await setup_entry(hass, mock_config_entry)
    before = {
        k: state_of(hass, mock_config_entry, k).state
        for k in ("latest_data_hour", "last_successful_update")
    }
    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)

    # The restart's first refresh stays blocked, so only restored values can show.
    release = fake_portal.block_on(fake_portal.calls["get_day"] + 1)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await asyncio.wait_for(fake_portal.blocked.wait(), 5)
    assert mock_config_entry.runtime_data.data is None
    assert len(entry_entities(hass, mock_config_entry)) == 2
    for key, value in before.items():
        state = state_of(hass, mock_config_entry, key)
        assert state.state == value
    release.set()
    await wait_idle(hass, mock_config_entry)


async def test_two_service_points_two_devices(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_config_entry,
    fake_portal: FakePortal,
) -> None:
    for sp in (SP1, SP2):
        fake_portal.add_service_point(sp, D0, D0, {D0: day_values(0)})
    await setup_entry(hass, mock_config_entry)

    devices = dr.async_entries_for_config_entry(dr.async_get(hass), mock_config_entry.entry_id)
    assert sorted(d.name for d in devices) == ["Idaho Power electric", "Idaho Power electric 2"]
    assert len(entry_entities(hass, mock_config_entry)) == 4


async def test_no_identifying_state_or_attributes(
    recorder_mock: Recorder,
    hass: HomeAssistant,
    mock_config_entry,
    fake_portal: FakePortal,
) -> None:
    fake_portal.add_service_point(SP1, D0, D0, {D0: day_values(0)})
    await setup_entry(hass, mock_config_entry)

    for entity in entry_entities(hass, mock_config_entry):
        state = hass.states.get(entity.entity_id)
        text = (state.state + repr(dict(state.attributes))).lower()
        for secret in (SP1.lower(), "user@example.com", "synthetic_password"):
            assert secret not in text
        assert set(state.attributes) <= {"device_class", "friendly_name"}
