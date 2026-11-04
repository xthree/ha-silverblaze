"""Diagnostic freshness sensors: one device per service point, two timestamp sensors each."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntityDescription,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .api import get_utility
from .const import CONF_UTILITY, DOMAIN
from .coordinator import COMMODITY, SilverblazeCoordinator, SilverblazeData
from .importer import statistic_name

if TYPE_CHECKING:
    from . import SilverblazeConfigEntry

PARALLEL_UPDATES = 0

_PREFIX = f"{DOMAIN}:"


@dataclass(frozen=True, kw_only=True)
class SilverblazeSensorDescription(SensorEntityDescription):
    """A timestamp sensor read from the coordinator data for one statistic."""

    value_fn: Callable[[SilverblazeData, str], datetime | None]


SENSORS: tuple[SilverblazeSensorDescription, ...] = (
    SilverblazeSensorDescription(
        key="latest_data_hour",
        translation_key="latest_data_hour",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data, sid: data.latest_data_hour.get(sid),
    ),
    SilverblazeSensorDescription(
        key="last_successful_update",
        translation_key="last_successful_update",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data, sid: data.last_success,
    ),
)


def _device_name(utility_name: str, index: int, total: int) -> str:
    """Reuse the statistic-name ordinal rule, without the trailing " consumption"."""
    full = statistic_name(utility_name, COMMODITY, index, total)
    suffix = full.removeprefix(f"{utility_name} {COMMODITY} consumption")
    return f"{utility_name} {COMMODITY}{suffix}"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SilverblazeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the sensors for every known service point, now and as they first appear."""
    coordinator = entry.runtime_data
    utility_name = get_utility(entry.data[CONF_UTILITY]).name()
    added: set[str] = set()

    def _add(idents: set[str]) -> None:
        ordered = sorted(added | idents)
        entities = []
        for index, ident in enumerate(ordered):
            if ident in added:
                continue
            added.add(ident)
            name = _device_name(utility_name, index, len(ordered))
            entities.extend(
                SilverblazeSensor(coordinator, description, ident, name, utility_name)
                for description in SENSORS
            )
        if entities:
            async_add_entities(entities)

    # Devices from before a restart: recreate their sensors right away so restored
    # values show before the first refresh completes.
    _add(
        {
            ident
            for device in dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
            for domain, ident in device.identifiers
            if domain == DOMAIN
        }
    )

    @callback
    def _async_new_points() -> None:
        if coordinator.data is not None:
            _add({sid.removeprefix(_PREFIX) for sid in coordinator.data.latest_data_hour})

    entry.async_on_unload(coordinator.async_add_listener(_async_new_points))
    _async_new_points()


class SilverblazeSensor(CoordinatorEntity[SilverblazeCoordinator], RestoreSensor):
    """A diagnostic timestamp for one service point."""

    entity_description: SilverblazeSensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: SilverblazeCoordinator,
        description: SilverblazeSensorDescription,
        ident: str,
        device_name: str,
        utility_name: str,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._sid = f"{_PREFIX}{ident}"
        self._value: datetime | None = None
        self._attr_unique_id = f"{ident}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, ident)},
            name=device_name,
            manufacturer="Silverblaze",
            model=utility_name,
            entry_type=DeviceEntryType.SERVICE,
        )

    async def async_added_to_hass(self) -> None:
        """Restore the last known value so it survives restarts and failed cycles."""
        await super().async_added_to_hass()
        if (last := await self.async_get_last_sensor_data()) is not None:
            restored = last.native_value
            if isinstance(restored, str):
                restored = dt_util.parse_datetime(restored)
            if isinstance(restored, datetime):
                self._value = restored
        self._refresh_value()

    def _refresh_value(self) -> None:
        data = self.coordinator.data
        if data is not None and (value := self.entity_description.value_fn(data, self._sid)):
            self._value = value

    @callback
    def _handle_coordinator_update(self) -> None:
        self._refresh_value()
        super()._handle_coordinator_update()

    @property
    def available(self) -> bool:
        """Available once a value exists, regardless of the last fetch result."""
        return self._value is not None

    @property
    def native_value(self) -> datetime | None:
        return self._value
