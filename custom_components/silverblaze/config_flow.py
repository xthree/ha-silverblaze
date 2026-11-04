"""Config flow for Silverblaze."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from datetime import date
from typing import Any
from zoneinfo import ZoneInfo

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.selector import (
    DateSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from homeassistant.util import dt as dt_util

from .api import (
    CannotConnect,
    InvalidAuth,
    LoginFlowChanged,
    SessionExpired,
    SilverblazeClient,
    SilverblazeError,
    SilverblazeHttp,
    UtilityModule,
    build_allowlist,
    get_utilities,
    get_utility,
    open_session,
)
from .api.client import hourly_series
from .api.models import ServicePoint
from .const import (
    CONF_IMPORT_FROM,
    CONF_UPDATE_INTERVAL,
    CONF_UTILITY,
    DEFAULT_UPDATE_INTERVAL_HOURS,
    DOMAIN,
    MAX_UPDATE_INTERVAL_HOURS,
    MIN_UPDATE_INTERVAL_HOURS,
)

_LOGGER = logging.getLogger(__name__)

HISTORY_START_TIMEOUT = 15  # seconds allowed for reading the history start during setup
CONF_HISTORY = "history"
HISTORY_ALL = "all"
HISTORY_FROM_DATE = "from_date"

_MONTH_NAMES = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


def _format_date(day: date) -> str:
    """Format a date as "January 1, 2025" (fixed English names, no locale lookup)."""
    return f"{_MONTH_NAMES[day.month - 1]} {day.day}, {day.year}"


def _history_start(points: tuple[ServicePoint, ...], tz: ZoneInfo) -> date | None:
    """Return the earliest local day with hourly usage across all service points."""
    days = [
        series.oldest_utc.astimezone(tz).date()
        for sp in points
        if (series := hourly_series(sp)) is not None
    ]
    return min(days) if days else None


async def _today(module_cls: type[UtilityModule]) -> date:
    """Return today's date in the utility's time zone."""
    tz = await dt_util.async_get_time_zone(module_cls.timezone())
    return dt_util.now(tz).date() if tz else dt_util.now().date()


async def _validate_login(
    hass: HomeAssistant, data: dict[str, Any], *, fetch_history_start: bool = False
) -> date | None:
    """Log in and fetch the page parameters; raise the client errors on failure.

    With ``fetch_history_start`` the account's first day of hourly usage is also read;
    it is None when that cannot be determined (this never fails the login).
    """
    module_cls = get_utility(data[CONF_UTILITY])
    # Dedicated cookie jar per validation so no state leaks between attempts.
    # auto_cleanup=False: we release the session ourselves. HA shares its connector,
    # so the session is detached (never close()d, which HA forbids) in ``finally``.
    session = async_create_clientsession(hass, auto_cleanup=False, cookie_jar=aiohttp.CookieJar())
    try:
        http = SilverblazeHttp(session, build_allowlist(module_cls.login_endpoints()))
        ctx = await open_session(http, module_cls, data[CONF_USERNAME], data[CONF_PASSWORD])
        if not fetch_history_start:
            return None
        tz = await dt_util.async_get_time_zone(module_cls.timezone())
        if tz is None:
            return None
        try:
            async with asyncio.timeout(HISTORY_START_TIMEOUT):
                points = await SilverblazeClient(http).get_meta(ctx)
        except TimeoutError, SilverblazeError:
            _LOGGER.debug("Could not read the usage history start; continuing without it")
            return None
        return _history_start(points, tz)
    finally:
        session.detach()


async def _validate_login_errors(
    hass: HomeAssistant, data: dict[str, Any], *, fetch_history_start: bool = False
) -> tuple[str | None, date | None]:
    """Validate the login; return (error key, None) on failure or (None, history start)."""
    try:
        start = await _validate_login(hass, data, fetch_history_start=fetch_history_start)
    except InvalidAuth:
        return "invalid_auth", None
    except LoginFlowChanged:
        return "login_flow_changed", None
    except CannotConnect, SessionExpired:
        # SessionExpired has no error key of its own; it means the portal
        # handoff did not hold, which is a connection-level failure here.
        return "cannot_connect", None
    except Exception:
        _LOGGER.exception("Unexpected error validating Silverblaze login")
        return "unknown", None
    return None, start


class SilverblazeOptionsFlow(OptionsFlowWithReload):
    """Options: history start date and update interval (saving reloads the entry)."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Show the options menu; clearing the start date is offered only when one is set."""
        menu = ["settings"]
        if self.config_entry.options.get(CONF_IMPORT_FROM):
            menu.append("import_all")
        return self.async_show_menu(step_id="init", menu_options=menu)

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show and validate the settings form."""
        errors: dict[str, str] = {}
        options = self.config_entry.options

        if user_input is not None:
            interval = int(user_input[CONF_UPDATE_INTERVAL])
            if not MIN_UPDATE_INTERVAL_HOURS <= interval <= MAX_UPDATE_INTERVAL_HOURS:
                errors[CONF_UPDATE_INTERVAL] = "interval_out_of_range"
            if import_from := user_input.get(CONF_IMPORT_FROM):
                module_cls = get_utility(self.config_entry.data[CONF_UTILITY])
                if date.fromisoformat(str(import_from)) > await _today(module_cls):
                    errors[CONF_IMPORT_FROM] = "date_in_future"
            if not errors:
                new_options: dict[str, Any] = {CONF_UPDATE_INTERVAL: interval}
                if import_from:
                    new_options[CONF_IMPORT_FROM] = str(import_from)
                return self.async_create_entry(data=new_options)

        schema = vol.Schema(
            {
                vol.Optional(CONF_IMPORT_FROM): DateSelector(),
                vol.Required(
                    CONF_UPDATE_INTERVAL, default=DEFAULT_UPDATE_INTERVAL_HOURS
                ): NumberSelector(NumberSelectorConfig(mode=NumberSelectorMode.BOX, step=1)),
            }
        )
        if user_input is not None:
            suggested = user_input
        else:
            suggested = {
                CONF_IMPORT_FROM: options.get(CONF_IMPORT_FROM),
                CONF_UPDATE_INTERVAL: options.get(
                    CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL_HOURS
                ),
            }
        return self.async_show_form(
            step_id="settings",
            data_schema=self.add_suggested_values_to_schema(schema, suggested),
            errors=errors,
        )

    async def async_step_import_all(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm removing the start date (nothing already imported is deleted)."""
        options = self.config_entry.options
        raw = options.get(CONF_IMPORT_FROM)
        if not raw:
            # Stale menu: the date was removed in the meantime; keep the options as they are.
            return self.async_create_entry(data=dict(options))
        if user_input is not None:  # the confirmation submits {}; never test its truthiness
            return self.async_create_entry(
                data={k: v for k, v in options.items() if k != CONF_IMPORT_FROM}
            )
        try:
            current = _format_date(date.fromisoformat(str(raw)))
        except ValueError:
            current = str(raw)
        return self.async_show_form(
            step_id="import_all",
            data_schema=vol.Schema({}),
            description_placeholders={"current_date": current},
        )


class SilverblazeConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Silverblaze."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the flow."""
        self._utility: str | None = None
        self._data: dict[str, Any] | None = None
        self._history_start: date | None = None

    @staticmethod
    def async_get_options_flow(config_entry: ConfigEntry) -> SilverblazeOptionsFlow:
        """Return the options flow."""
        return SilverblazeOptionsFlow()

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """Start re-authentication after the stored password stopped working."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the new password only and validate it like the credentials step."""
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        utility = get_utility(entry.data[CONF_UTILITY])

        if user_input is not None:
            data = {**entry.data, CONF_PASSWORD: user_input[CONF_PASSWORD]}
            error, _ = await _validate_login_errors(self.hass, data)
            if error:
                errors["base"] = error
            else:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_PASSWORD: user_input[CONF_PASSWORD]}
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_PASSWORD): TextSelector(
                        TextSelectorConfig(
                            type=TextSelectorType.PASSWORD, autocomplete="current-password"
                        )
                    )
                }
            ),
            errors=errors,
            description_placeholders={
                "utility_name": utility.name(),
                CONF_USERNAME: entry.data[CONF_USERNAME],
            },
        )

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Choose the utility."""
        if user_input is not None:
            self._utility = user_input[CONF_UTILITY]
            return await self.async_step_credentials()

        options = [
            SelectOptionDict(value=slug, label=cls.name())
            for slug, cls in sorted(get_utilities().items())
        ]
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_UTILITY): SelectSelector(
                        SelectSelectorConfig(options=options, mode=SelectSelectorMode.DROPDOWN)
                    )
                }
            ),
        )

    async def async_step_credentials(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect and validate the portal credentials."""
        if self._utility is None:
            return await self.async_step_user()
        errors: dict[str, str] = {}

        if user_input is not None:
            username = user_input[CONF_USERNAME]
            # raise_on_progress=False: a setup abandoned mid-dialog (page refresh) stays open
            # on the server and must not block a new attempt. Duplicate entries are still
            # refused here and again in _create_entry; finishing one setup closes the others.
            await self.async_set_unique_id(
                f"{self._utility}_{username.casefold()}", raise_on_progress=False
            )
            self._abort_if_unique_id_configured()

            data = {
                CONF_UTILITY: self._utility,
                CONF_USERNAME: username,
                CONF_PASSWORD: user_input[CONF_PASSWORD],
            }
            error, start = await _validate_login_errors(self.hass, data, fetch_history_start=True)
            if error:
                errors["base"] = error
            else:
                self._data = data
                self._history_start = start
                return await self.async_step_history()

        schema = vol.Schema(
            {
                vol.Required(CONF_USERNAME): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.TEXT, autocomplete="username")
                ),
                vol.Required(CONF_PASSWORD): TextSelector(
                    TextSelectorConfig(
                        type=TextSelectorType.PASSWORD, autocomplete="current-password"
                    )
                ),
            }
        )
        suggested = {CONF_USERNAME: user_input[CONF_USERNAME]} if user_input else None
        return self.async_show_form(
            step_id="credentials",
            data_schema=self.add_suggested_values_to_schema(schema, suggested),
            errors=errors,
            description_placeholders={"utility_name": get_utility(self._utility).name()},
        )

    async def async_step_history(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask how much usage history to import."""
        if self._data is None or self._utility is None:
            return await self.async_step_user()
        if user_input is not None:
            if user_input[CONF_HISTORY] == HISTORY_FROM_DATE:
                return await self.async_step_start_date()
            return self._create_entry(options={})

        placeholders = {"utility_name": get_utility(self._utility).name()}
        step_id = "history_unknown"
        if self._history_start is not None:
            step_id = "history"
            placeholders["history_start"] = _format_date(self._history_start)
        return self.async_show_form(
            step_id=step_id,
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HISTORY, default=HISTORY_ALL): SelectSelector(
                        SelectSelectorConfig(
                            options=[HISTORY_ALL, HISTORY_FROM_DATE],
                            translation_key="history_choice",
                            mode=SelectSelectorMode.LIST,
                        )
                    )
                }
            ),
            description_placeholders=placeholders,
        )

    async def async_step_history_unknown(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Same choice as the history step, shown when the history start is unknown."""
        return await self.async_step_history(user_input)

    async def async_step_start_date(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the first day to import."""
        if self._data is None or self._utility is None:
            return await self.async_step_user()
        errors: dict[str, str] = {}
        if user_input is not None:
            chosen = date.fromisoformat(str(user_input[CONF_IMPORT_FROM]))
            if chosen > await _today(get_utility(self._utility)):
                errors[CONF_IMPORT_FROM] = "date_in_future"
            else:
                return self._create_entry(options={CONF_IMPORT_FROM: chosen.isoformat()})
        return self.async_show_form(
            step_id="start_date",
            data_schema=vol.Schema({vol.Required(CONF_IMPORT_FROM): DateSelector()}),
            errors=errors,
        )

    def _create_entry(self, options: dict[str, Any]) -> ConfigFlowResult:
        """Create the entry; another one may have been added since the credentials step."""
        assert self._data is not None and self._utility is not None
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=get_utility(self._utility).name(), data=self._data, options=options
        )
