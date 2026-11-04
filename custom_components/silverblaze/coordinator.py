"""Sync engine: login, meta, per-day fetch and write; resumable from stored statistics.

Nothing is persisted between runs (no ``enc``, parameters or cookies): every run logs
in fresh. Resume state is derived solely from the recorder's stored statistics. The
time zone comes only from the utility's login module, never from Home Assistant's
configured zone.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import TypeVar
from zoneinfo import ZoneInfo

import aiohttp
from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticData
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    CannotConnect,
    EndpointNotAllowed,
    InvalidAuth,
    LoginFlowChanged,
    PageContext,
    SessionExpired,
    SilverblazeClient,
    SilverblazeError,
    SilverblazeHttp,
    build_allowlist,
    get_utility,
    open_session,
)
from .api.client import hourly_series
from .api.models import DayReads, Series, ServicePoint
from .const import (
    CONF_IMPORT_FROM,
    CONF_UPDATE_INTERVAL,
    CONF_UTILITY,
    CORRECTION_WINDOW_DAYS,
    DEFAULT_UPDATE_INTERVAL_HOURS,
    DOMAIN,
)
from .importer import (
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

_LOGGER = logging.getLogger(__name__)

COMMODITY = "electric"
# Upper bound on re-logins in one run (about ten hours of backfill at the portal's
# roughly one-hour session lifetime), so repeated empty replies cannot loop sign-ins.
MAX_RELOGINS_PER_RUN = 10

_T = TypeVar("_T")


@dataclass
class SilverblazeData:
    """Per-statistic freshness, consumed by the diagnostic sensors."""

    latest_data_hour: dict[str, datetime] = field(default_factory=dict)
    last_success: datetime | None = None


def _day_start_utc(day: date, tz: ZoneInfo) -> datetime:
    return datetime.combine(day, time(), tzinfo=tz).astimezone(UTC)


def _parse_import_from(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value:
        try:
            return date.fromisoformat(value)
        except ValueError:
            _LOGGER.warning("Ignoring an unparseable import-from date")
    return None


def _interval_hours(value: object) -> int:
    """The configured refresh interval in hours (the default when absent or invalid)."""
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return DEFAULT_UPDATE_INTERVAL_HOURS


class SilverblazeCoordinator(DataUpdateCoordinator[SilverblazeData]):
    """Runs the sync for one config entry."""

    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=DOMAIN,
            update_interval=timedelta(
                hours=_interval_hours(config_entry.options.get(CONF_UPDATE_INTERVAL))
            ),
        )
        self._log_next_refresh()
        self._module_cls = get_utility(config_entry.data[CONF_UTILITY])
        # Dedicated cookie jar; cleared before every login.
        self._session: aiohttp.ClientSession = async_create_clientsession(
            hass, cookie_jar=aiohttp.CookieJar()
        )
        self._http = SilverblazeHttp(
            self._session, build_allowlist(self._module_cls.login_endpoints())
        )
        self._lock = asyncio.Lock()
        self._latest: dict[str, datetime] = {}
        self._ctx: PageContext | None = None
        self._relogins = 0
        # Per statistic id: the earliest date already probed for portal data (runtime
        # only, so an empty stretch is not re-probed every cycle).
        self._checked_down_to: dict[str, date] = {}

        @callback
        def _keep_polling() -> None:
            """No-op listener: polling only runs while something is listening."""

        # The statistics import must keep running even when no entity listens. The
        # listener is removed on unload (before the base class shuts down) so a
        # refresh finishing mid-unload cannot re-arm the timer.
        config_entry.async_on_unload(self.async_add_listener(_keep_polling))

    def _log_next_refresh(self) -> None:
        if self.update_interval is None or self.config_entry.pref_disable_polling:
            return
        _LOGGER.debug(
            "Next refresh in %g hours (at %s UTC)",
            self.update_interval.total_seconds() / 3600,
            (dt_util.utcnow() + self.update_interval).strftime("%Y-%m-%d %H:%M"),
        )

    @callback
    def _schedule_refresh(self) -> None:
        """Schedule the next refresh and log when it is due."""
        super()._schedule_refresh()
        if self._unsub_refresh is not None:
            self._log_next_refresh()

    @property
    def sync_lock(self) -> asyncio.Lock:
        """The per-entry lock held for the whole of a sync run."""
        return self._lock

    async def _async_update_data(self) -> SilverblazeData:
        """Run one cycle under the entry lock; on failure ``data`` is left untouched.

        Only the exception class is surfaced: its text can carry request details.
        """
        try:
            return await self.async_sync()
        except EndpointNotAllowed:
            # A request outside the allowlist is a bug, never an expected failure.
            _LOGGER.exception("Sync tried a request outside the allowlist")
            raise UpdateFailed("EndpointNotAllowed") from None
        except InvalidAuth:
            # Only a rejected password starts re-auth (and stops scheduled refreshes).
            raise ConfigEntryAuthFailed from None
        except LoginFlowChanged:
            _LOGGER.warning(
                "Idaho Power login flow changed in a way this version does not "
                "understand; will retry next cycle"
            )
            raise UpdateFailed(
                "LoginFlowChanged",
                translation_domain=DOMAIN,
                translation_key="login_flow_changed",
            ) from None
        except SilverblazeError as err:
            # CannotConnect, UnexpectedResponse, a bare SessionExpired (first login or
            # page context) and anything else: retry next cycle, never re-auth.
            _LOGGER.warning("Sync ended early: %s", type(err).__name__)
            raise UpdateFailed(
                type(err).__name__,
                translation_domain=DOMAIN,
                translation_key="cannot_connect",
            ) from None

    async def _login(self) -> PageContext:
        self._session.cookie_jar.clear()
        return await open_session(
            self._http,
            self._module_cls,
            self.config_entry.data["username"],
            self.config_entry.data["password"],
        )

    async def _portal(self, call: Callable[[PageContext], Awaitable[_T]]) -> _T:
        """Run a portal call; on expiry log in again and retry it once."""
        assert self._ctx is not None
        try:
            return await call(self._ctx)
        except SessionExpired:
            pass
        if self._relogins >= MAX_RELOGINS_PER_RUN:
            raise CannotConnect("Too many session expiries in one run")
        self._relogins += 1
        _LOGGER.debug("Session expired; logging in again")
        try:
            self._ctx = await self._login()
            return await call(self._ctx)
        except SessionExpired:
            raise CannotConnect("Session expired again during the run") from None

    async def _fetch_day(
        self,
        client: SilverblazeClient,
        sp: ServicePoint,
        series: Series,
        day: date,
        tz: ZoneInfo,
    ) -> DayReads:
        return await self._portal(lambda ctx: client.get_day(ctx, sp, series, day, tz, COMMODITY))

    async def async_sync(self) -> SilverblazeData:
        """Import every missing day for every hourly electric series."""
        async with self._lock:
            return await self._sync()

    async def _sync(self) -> SilverblazeData:
        module = self._module_cls
        tz = await dt_util.async_get_time_zone(module.timezone())
        if tz is None:
            raise CannotConnect("Unknown utility time zone")
        import_from = _parse_import_from(self.config_entry.options.get(CONF_IMPORT_FROM))

        # Let queued recorder writes land so the resume point is deterministic.
        await get_instance(self.hass).async_block_till_done()

        self._ctx = await self._login()
        self._relogins = 0
        client = SilverblazeClient(self._http)
        points = await self._portal(client.get_meta)
        targets = [(sp, hourly_series(sp, COMMODITY)) for sp in points]
        targets = sorted(((sp, s) for sp, s in targets if s is not None), key=lambda t: t[0].id)

        for index, (sp, series) in enumerate(targets):
            sid = statistic_id(module.slug(), COMMODITY, sp.id)
            name = statistic_name(module.name(), COMMODITY, index, len(targets))
            metadata = build_metadata(sid, name)
            label = f"{module.name()} {COMMODITY}"

            lower = series.oldest_utc.astimezone(tz).date()
            if import_from is not None:
                lower = max(lower, import_from)
            end = series.newest_utc.astimezone(tz).date()

            last = await async_last_stat(self.hass, sid)
            first = await async_first_stat_start(self.hass, sid) if last else None
            rewrite = False
            prefetched: dict[date, DayReads] = {}
            if last is None:
                start = lower
                backfill = True
            else:
                self._latest[sid] = last[0]
                start = None
                first_date = first.astimezone(tz).date() if first is not None else None
                checked = self._checked_down_to.get(sid)
                if (
                    first_date is not None
                    and lower < first_date
                    and (checked is None or lower < checked)
                ):
                    # Import-from moved earlier than the first stored day: probe only
                    # the gap days; the series is rewritten only if any has data.
                    gap_end = min(first_date - timedelta(days=1), end)
                    gap_days = [
                        lower + timedelta(days=i) for i in range((gap_end - lower).days + 1)
                    ]
                    gap_reads = False
                    for day in gap_days:
                        result = await self._fetch_day(client, sp, series, day, tz)
                        prefetched[day] = result
                        gap_reads = gap_reads or bool(result.reads)
                    if gap_reads:
                        rewrite = True
                        start = lower
                        backfill = True
                    else:
                        self._checked_down_to[sid] = lower
                if start is None:
                    last_day = last[0].astimezone(tz).date()
                    start = max(
                        lower,
                        min(end - timedelta(days=CORRECTION_WINDOW_DAYS), last_day),
                    )
                    backfill = (end - start).days + 1 > CORRECTION_WINDOW_DAYS + 1
            if start > end:
                continue

            start_utc = _day_start_utc(start, tz)
            # Stored hours for the whole window are read once; the anchor is the sum
            # just before the window, and everything from there is rewritten forward.
            stored = await async_stored_hours(
                self.hass, sid, start_utc, _day_start_utc(end + timedelta(days=1), tz)
            )
            running = await async_anchor_sum(self.hass, sid, start_utc)
            # A moved-earlier rewrite is all-or-nothing: rows are buffered and written
            # in one recorder call, so an interruption can never leave a sum step.
            pending: list[StatisticData] = []

            total_days = (end - start).days + 1
            step = min(30, max(1, total_days // 10))
            if backfill and total_days > 1:
                _LOGGER.info("Backfilling %d days for %s", total_days, label)

            for done, offset in enumerate(range(total_days), start=1):
                day = start + timedelta(days=offset)
                result = prefetched.pop(day, None)
                if result is None:
                    result = await self._fetch_day(client, sp, series, day, tz)

                lo, hi = _day_start_utc(day, tz), _day_start_utc(day + timedelta(days=1), tz)
                # merge_window uses every stored key and anchors before the earliest:
                # pass only this day's stored hours with the sum from before the day.
                window = {t: v for t, v in stored.items() if lo <= t < hi}
                rows = merge_window(window, result.reads, running)
                if rows:
                    if rewrite:
                        pending.extend(rows)
                    else:
                        await async_write(self.hass, metadata, rows)
                        self._latest[sid] = rows[-1]["start"]
                    running = rows[-1]["sum"]

                if backfill and total_days > 1 and (done % step == 0 or done == total_days):
                    if done < total_days:
                        _LOGGER.info("Imported %d of %d days for %s", done, total_days, label)
                    else:
                        _LOGGER.info("Finished importing %d days for %s", total_days, label)

            if rewrite and pending:
                await async_write(self.hass, metadata, pending)
                self._latest[sid] = pending[-1]["start"]
            if rewrite or last is None:
                self._checked_down_to[sid] = lower

            if not backfill:
                _LOGGER.info("Updated %d days for %s", total_days, label)

        return SilverblazeData(latest_data_hour=dict(self._latest), last_success=datetime.now(UTC))
