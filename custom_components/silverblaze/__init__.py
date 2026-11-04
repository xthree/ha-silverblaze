"""The Silverblaze integration.

No module-level Home Assistant imports: the standalone tooling executes this
package ``__init__`` and must work without Home Assistant installed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from .coordinator import SilverblazeCoordinator

    type SilverblazeConfigEntry = ConfigEntry[SilverblazeCoordinator]

_PLATFORMS = ["sensor"]


async def _async_background_sync(coordinator: SilverblazeCoordinator) -> None:
    """Run the first refresh (the backfill) outside of setup, then scheduling starts.

    The coordinator handles every failure itself: an auth failure starts re-auth
    through the config entry, other expected errors are logged (class name only) and
    retried next cycle. Cancellation propagates untouched.
    """
    await coordinator.async_refresh()


async def async_setup_entry(hass: HomeAssistant, entry: SilverblazeConfigEntry) -> bool:
    """Set up Silverblaze from a config entry; the backfill runs in the background."""
    from .coordinator import SilverblazeCoordinator

    # Built here (inside setup) so Home Assistant ties the client session to the
    # entry and releases it on unload.
    coordinator = SilverblazeCoordinator(hass, entry)
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, _PLATFORMS)
    entry.async_create_background_task(
        hass, _async_background_sync(coordinator), name=f"silverblaze sync {entry.entry_id}"
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SilverblazeConfigEntry) -> bool:
    """Unload a config entry.

    Home Assistant cancels the entry's background tasks and detaches its client
    session as part of unloading.
    """
    return await hass.config_entries.async_unload_platforms(entry, _PLATFORMS)
