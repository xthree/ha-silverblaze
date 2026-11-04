"""A fake portal at the API boundary (no HTTP, no real data).

Patches the login module's ``login`` and ``SilverblazeClient.get_page_parameters`` /
``get_meta`` / ``get_day``. Driven by per-day value tables, with hooks to inject
errors on the Nth call of a method and to block a ``get_day`` call on an event.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import UTC, date, datetime, time, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

from custom_components.silverblaze.api import SilverblazeClient
from custom_components.silverblaze.api.models import (
    DayReads,
    HourlyRead,
    PageContext,
    Series,
    ServicePoint,
)
from custom_components.silverblaze.api.utilities import get_utility

DENVER = ZoneInfo("America/Denver")


def local_midnight_utc(day: date, tz: ZoneInfo = DENVER) -> datetime:
    return datetime.combine(day, time(), tzinfo=tz).astimezone(UTC)


class FakePortal:
    """Per-day value tables plus failure/blocking hooks."""

    def __init__(self, tz: ZoneInfo = DENVER) -> None:
        self.tz = tz
        # sp id -> (oldest local date, newest local date)
        self.spans: dict[str, tuple[date, date]] = {}
        # sp id -> local date -> hourly values (None = null slot)
        self.days: dict[str, dict[date, list[float | None]]] = {}
        self.calls: Counter[str] = Counter()
        self.day_calls: list[tuple[str, date]] = []
        self._failures: dict[tuple[str, int], Exception] = {}
        self.block_event: asyncio.Event | None = None
        self.block_on_day_call: int | None = None
        self.blocked = asyncio.Event()
        # While set, every login raises it (a failing period).
        self.failing: Exception | None = None

    # --- configuration ---------------------------------------------------------------

    def add_service_point(
        self,
        sp_id: str,
        oldest: date,
        newest: date,
        values: dict[date, Sequence[float | None]] | None = None,
        *,
        default: float | None = 1.0,
    ) -> None:
        """Declare a service point; days without an entry get 24 x ``default`` values.

        ``default=None`` leaves unlisted days unpublished (no slots at all).
        """
        self.spans[sp_id] = (oldest, newest)
        table: dict[date, list[float | None]] = {}
        if default is not None:
            day = oldest
            while day <= newest:
                table[day] = [default] * 24
                day += timedelta(days=1)
        for day, vals in (values or {}).items():
            table[day] = list(vals)
        self.days[sp_id] = table

    def set_day(self, sp_id: str, day: date, values: Sequence[float | None]) -> None:
        """Change (or add) one day's values between cycles."""
        self.days[sp_id][day] = list(values)

    def set_newest(self, sp_id: str, newest: date) -> None:
        """Advance the newest published day (unlisted new days get 24 x 1.0)."""
        oldest, old_newest = self.spans[sp_id]
        day = old_newest + timedelta(days=1)
        while day <= newest:
            self.days[sp_id].setdefault(day, [1.0] * 24)
            day += timedelta(days=1)
        self.spans[sp_id] = (oldest, max(newest, old_newest))

    def fail_on(self, method: str, nth: int, exc: Exception) -> None:
        """Raise ``exc`` on the ``nth`` (1-based) call of ``method``, once."""
        self._failures[(method, nth)] = exc

    def block_on(self, nth_day_call: int) -> asyncio.Event:
        """Block the ``nth`` ``get_day`` call until the returned event is set."""
        self.block_event = asyncio.Event()
        self.block_on_day_call = nth_day_call
        self.blocked = asyncio.Event()
        return self.block_event

    # --- patched API -----------------------------------------------------------------

    def _count(self, method: str) -> None:
        self.calls[method] += 1
        exc = self._failures.pop((method, self.calls[method]), None)
        if exc is not None:
            raise exc

    async def login(self, http, username, password) -> str:
        self._count("login")
        if self.failing is not None:
            raise self.failing
        return "SYNTHETIC_ENC"

    async def get_page_parameters(self, enc) -> PageContext:
        self._count("get_page_parameters")
        return PageContext(client_id=1, tab_key="SYNTHETIC_TAB", parameters_b64="SYNTHETIC_PARAMS")

    async def get_meta(self, ctx) -> tuple[ServicePoint, ...]:
        self._count("get_meta")
        points = []
        for sp_id, (oldest, newest) in self.spans.items():
            oldest_utc = local_midnight_utc(oldest, self.tz)
            newest_utc = local_midnight_utc(newest, self.tz) + timedelta(hours=23)
            series = Series(
                interval="60min",
                kind="usage",
                uom="kWh",
                commodity_code="1",
                oldest_utc=oldest_utc,
                newest_utc=newest_utc,
                oldest_raw=oldest_utc.isoformat(),
                newest_raw=newest_utc.isoformat(),
            )
            points.append(ServicePoint(id=sp_id, timezone_label="Mountain", series=(series,)))
        return tuple(points)

    async def get_day(self, ctx, sp, series, local_date, tz, commodity="electric") -> DayReads:
        self.day_calls.append((sp.id, local_date))
        self._count("get_day")
        if self.block_event is not None and self.calls["get_day"] == self.block_on_day_call:
            self.blocked.set()
            await self.block_event.wait()
        values = self.days[sp.id].get(local_date, [])
        start = local_midnight_utc(local_date, tz)
        reads = tuple(
            HourlyRead(start + timedelta(hours=i), float(v))
            for i, v in enumerate(values)
            if v is not None
        )
        return DayReads(
            local_date=local_date,
            reads=reads,
            slot_count=len(values),
            null_count=sum(v is None for v in values),
        )

    @contextmanager
    def patched(self, module_slug: str = "idahopower") -> Iterator[FakePortal]:
        portal = self

        async def get_page_parameters(_self, enc):
            return await portal.get_page_parameters(enc)

        async def get_meta(_self, ctx):
            return await portal.get_meta(ctx)

        async def get_day(_self, ctx, sp, series, local_date, tz, commodity="electric"):
            return await portal.get_day(ctx, sp, series, local_date, tz, commodity)

        async def login(http, username, password):
            return await portal.login(http, username, password)

        module_cls = get_utility(module_slug)
        with (
            patch.object(module_cls, "login", new=login),
            patch.object(SilverblazeClient, "get_page_parameters", new=get_page_parameters),
            patch.object(SilverblazeClient, "get_meta", new=get_meta),
            patch.object(SilverblazeClient, "get_day", new=get_day),
        ):
            yield portal
