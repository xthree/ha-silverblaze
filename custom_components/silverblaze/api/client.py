"""Silverblaze portal client. All I/O goes through ``SilverblazeHttp``."""

from __future__ import annotations

import base64
import json
import logging
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .exceptions import CannotConnect, LoginFlowChanged, SessionExpired, UnexpectedResponse
from .http import API_HEADERS, HttpResponse, SilverblazeHttp
from .models import DayReads, HourlyRead, PageContext, Series, ServicePoint

_LOGGER = logging.getLogger(__name__)

SILVERBLAZE_BASE = "https://acewebsite.silverblaze.com"
GET_PAGE_PARAMETERS_URL = f"{SILVERBLAZE_BASE}/api/Page/GetPageParameters"
META_URL = f"{SILVERBLAZE_BASE}/api/DataDownload/Meta"
DATA_URL = f"{SILVERBLAZE_BASE}/api/ConsumptionV3/Data"

COMMODITY_CODES: dict[str, str] = {"electric": "1"}
_EXPIRED_STATUSES = frozenset({401, 403})
_EXPIRED_MARKER = "Invalid webtoken."


def _raise_if_session_expired(resp: HttpResponse) -> None:
    """Raise ``SessionExpired`` if the response says the web token is no longer valid.

    The single place that decides what "expired" looks like. About an hour after login
    the portal answers HTTP 200 with an empty body (no content type); observed on the
    Meta and Data endpoints (GetPageParameters is handled the same way). HTTP 401/403
    and a body carrying the portal's invalid-token message count too. An empty body on
    any other status is not a session problem and is left to the caller.
    """
    if (
        resp.status in _EXPIRED_STATUSES
        or _EXPIRED_MARKER in resp.text
        or (resp.status == 200 and not resp.text.strip())
    ):
        raise SessionExpired("The web token is no longer valid")


def _parse_utc(raw: Any) -> datetime:
    """Parse an ISO timestamp from the portal as an aware UTC datetime."""
    if not isinstance(raw, str):
        raise UnexpectedResponse("Meta response has an invalid AMI time span")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise UnexpectedResponse("Meta response has an invalid AMI time span") from None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _parse_series(item: Any) -> Series:
    if not isinstance(item, dict):
        raise UnexpectedResponse("Meta response has an invalid time series")
    spec = item.get("SeriesSpec")
    span = item.get("AmiTimeSpan")
    if not isinstance(spec, dict) or not isinstance(span, dict):
        raise UnexpectedResponse("Meta response has an invalid time series")
    interval, kind, uom = item.get("Interval"), item.get("Kind"), spec.get("uom")
    commodity = spec.get("Commodity")
    oldest_raw, newest_raw = span.get("Oldest"), span.get("Newest")
    if (
        not isinstance(interval, str)
        or not isinstance(kind, str)
        or not isinstance(uom, str)
        or commodity is None
        or not isinstance(oldest_raw, str)
        or not isinstance(newest_raw, str)
    ):
        raise UnexpectedResponse("Meta response has an invalid time series")
    return Series(
        interval=interval,
        kind=kind,
        uom=uom,
        commodity_code=str(commodity),
        oldest_utc=_parse_utc(oldest_raw),
        newest_utc=_parse_utc(newest_raw),
        oldest_raw=oldest_raw,
        newest_raw=newest_raw,
    )


def hourly_series(sp: ServicePoint, commodity: str = "electric") -> Series | None:
    """Return the hourly usage series (kWh) of ``sp`` for ``commodity``, if present."""
    code = COMMODITY_CODES[commodity]
    for series in sp.series:
        if (
            series.interval == "60min"
            and series.kind == "usage"
            and series.uom.lower() == "kwh"
            and series.commodity_code == code
        ):
            return series
    return None


class SilverblazeClient:
    """Talks to the Silverblaze API using the ``enc`` token from a utility login."""

    def __init__(self, http: SilverblazeHttp) -> None:
        self._http = http

    async def get_page_parameters(self, enc: str) -> PageContext:
        """Exchange ``enc`` for the page context used by every later API call."""
        resp = await self._http.request(
            "GET", GET_PAGE_PARAMETERS_URL, params={"enc": enc}, headers=API_HEADERS
        )
        _raise_if_session_expired(resp)
        if resp.status != 200:
            raise CannotConnect(f"GetPageParameters returned HTTP {resp.status}")
        try:
            body: Any = json.loads(resp.text)
        except ValueError:
            raise UnexpectedResponse("GetPageParameters returned a non-JSON body") from None
        if not isinstance(body, dict):
            raise UnexpectedResponse("GetPageParameters returned an unexpected JSON shape")

        layout = body.get("Layout")
        raw = layout.get("Parameters") if isinstance(layout, dict) else None
        if not isinstance(raw, str) or not raw:
            raise LoginFlowChanged("GetPageParameters response has no Layout.Parameters")

        # The portal's own web app sends btoa(Layout.Parameters) on every widget call;
        # sending the raw value instead is rejected with HTTP 500. btoa works on
        # latin-1 characters, hence the explicit latin-1 encode.
        try:
            parameters_b64 = base64.b64encode(raw.encode("latin-1")).decode("ascii")
        except UnicodeEncodeError:
            raise LoginFlowChanged("Layout.Parameters is not latin-1 encodable") from None

        client_id = body.get("ClientId")
        tab_key = layout.get("TabKey")
        return PageContext(
            client_id=client_id if isinstance(client_id, int) else None,
            tab_key=tab_key if isinstance(tab_key, str) else "",
            parameters_b64=parameters_b64,
        )

    async def get_meta(self, ctx: PageContext) -> tuple[ServicePoint, ...]:
        """Return the account's service points with their series and AMI spans."""
        resp = await self._http.request(
            "GET",
            META_URL,
            params={
                "tabkey": "null",
                "parameters": ctx.parameters_b64,
                "customerByAccountId": "true",
            },
            headers=API_HEADERS,
        )
        _raise_if_session_expired(resp)
        if resp.status != 200:
            raise CannotConnect(f"Meta returned HTTP {resp.status}")
        body = _json_object(resp.text, "Meta")
        inner = body.get("body")
        metadata = inner.get("Metadata") if isinstance(inner, dict) else None
        if not isinstance(metadata, list):
            raise UnexpectedResponse("Meta response has no Metadata list")
        points: list[ServicePoint] = []
        for entry in metadata:
            if not isinstance(entry, dict):
                raise UnexpectedResponse("Meta response has an invalid service point")
            sp_id, tz_label, raw_series = (
                entry.get("CollectionId"),
                entry.get("Timezone"),
                entry.get("TimeSeries"),
            )
            if (
                not isinstance(sp_id, str)
                or not sp_id
                or not isinstance(tz_label, str)
                or not isinstance(raw_series, list)
            ):
                raise UnexpectedResponse("Meta response has an invalid service point")
            points.append(
                ServicePoint(
                    id=sp_id,
                    timezone_label=tz_label,
                    series=tuple(_parse_series(item) for item in raw_series),
                )
            )
        return tuple(points)

    async def get_day(
        self,
        ctx: PageContext,
        sp: ServicePoint,
        series: Series,
        local_date: date,
        tz: ZoneInfo,
        commodity: str = "electric",
    ) -> DayReads:
        """Fetch the hourly reads of one local day; null slots are skipped, never zeroed."""
        params = {
            "tabKey": ctx.tab_key,
            "parameters": ctx.parameters_b64,
            "commodity": commodity,
            "resolution": "60min",
            # The server keys the day off endDate's UTC date.
            "startDate": f"{(local_date - timedelta(days=1)).isoformat()}T18:00:00.000Z",
            "endDate": f"{local_date.isoformat()}T17:59:00.000Z",
            "collectionId": sp.id,
            "netBillingStartDate": "",
            "collectionType": "ServicePointId",
            "clientTimezone": sp.timezone_label,
            "oldestDate": series.oldest_raw,
            "newestDate": series.newest_raw,
            "chartId": "houravg",
            "isBillPeriodSelected": "false",
            # Load-bearing: without paging=true the server ignores the dates and
            # silently returns the newest published day instead.
            "paging": "true",
            "meterSelected": "false",
            "drilledFromChartId": "",
            "returning": "false",
            "monthCount": "0",
            "drilling": "false",
        }
        resp = await self._http.request("GET", DATA_URL, params=params, headers=API_HEADERS)
        _raise_if_session_expired(resp)
        if resp.status != 200:
            raise CannotConnect(f"Data returned HTTP {resp.status}")
        body = _json_object(resp.text, "Data")

        aligned = body.get("AlignedSeriesData")
        values = aligned.get("forward/usage") if isinstance(aligned, dict) else None
        layout = body.get("IntervalLayout")
        intervals = layout.get("ChartIntervals") if isinstance(layout, dict) else None
        values = [] if values is None else values
        intervals = [] if intervals is None else intervals
        if not isinstance(values, list) or not isinstance(intervals, list):
            raise UnexpectedResponse("Data response has an invalid series shape")
        if len(values) != len(intervals):
            raise UnexpectedResponse("Data response slot and value counts differ")

        by_epoch: dict[int, float | None] = {}
        duplicates = 0
        for value, slot in zip(values, intervals, strict=True):
            epoch = slot.get("EpochMs") if isinstance(slot, dict) else None
            if isinstance(epoch, bool) or not isinstance(epoch, int | float):
                raise UnexpectedResponse("Data response slot has no EpochMs")
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int | float)
            ):
                raise UnexpectedResponse("Data response has a non-numeric value")
            key = int(epoch)
            if key in by_epoch:
                duplicates += 1
                # Non-null wins over null; between two non-nulls the later slot wins.
                if value is None and by_epoch[key] is not None:
                    continue
            by_epoch[key] = None if value is None else float(value)

        if duplicates:
            _LOGGER.warning(
                "Dropped %d duplicate hourly slot(s) on %s", duplicates, local_date.isoformat()
            )

        reads: list[HourlyRead] = []
        for epoch, value in sorted(by_epoch.items()):
            if value is None:
                continue
            start = datetime.fromtimestamp(epoch / 1000, UTC)
            if start.astimezone(tz).date() != local_date:
                # The portal answered for a different day than requested.
                raise UnexpectedResponse("Data response is for a different day than requested")
            reads.append(HourlyRead(start_utc=start, kwh=value))

        result = DayReads(
            local_date=local_date,
            reads=tuple(reads),
            slot_count=len(values),
            null_count=sum(v is None for v in values),
        )
        _LOGGER.debug(
            "Fetched %s: %d slots, %d non-null, first=%s last=%s",
            local_date.isoformat(),
            result.slot_count,
            len(reads),
            reads[0].start_utc.isoformat() if reads else None,
            reads[-1].start_utc.isoformat() if reads else None,
        )
        return result


def _json_object(text: str, what: str) -> dict[str, Any]:
    try:
        body: Any = json.loads(text)
    except ValueError:
        raise UnexpectedResponse(f"{what} returned a non-JSON body") from None
    if not isinstance(body, dict):
        raise UnexpectedResponse(f"{what} returned an unexpected JSON shape")
    return body
