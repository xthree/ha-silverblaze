"""Plain data types returned by the Silverblaze client (no Home Assistant imports)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime


@dataclass(frozen=True)
class PageContext:
    """Result of the page-parameter handoff; ``parameters_b64`` is a bearer secret."""

    client_id: int | None
    tab_key: str
    parameters_b64: str = field(repr=False)


@dataclass(frozen=True)
class Series:
    """One time series of a service point and its available (AMI) span."""

    interval: str
    kind: str
    uom: str
    commodity_code: str
    oldest_utc: datetime
    newest_utc: datetime
    oldest_raw: str
    newest_raw: str


@dataclass(frozen=True)
class ServicePoint:
    """A metered service point; ``id`` is an account identifier and stays out of repr."""

    id: str = field(repr=False)
    timezone_label: str
    series: tuple[Series, ...]


@dataclass(frozen=True)
class HourlyRead:
    """One non-null hourly reading. Null hours are never represented."""

    start_utc: datetime
    kwh: float


@dataclass(frozen=True)
class DayReads:
    """The hourly reads of one local day."""

    local_date: date
    reads: tuple[HourlyRead, ...]
    slot_count: int
    null_count: int
