"""GetPageParameters handoff and model repr hygiene."""

from __future__ import annotations

import base64
import json
import logging
import re
from datetime import UTC, date, datetime
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import aiohttp
import pytest

from custom_components.silverblaze.api import (
    API_HEADERS,
    CannotConnect,
    LoginFlowChanged,
    SessionExpired,
    SilverblazeHttp,
    UnexpectedResponse,
    build_allowlist,
)
from custom_components.silverblaze.api.client import (
    DATA_URL,
    GET_PAGE_PARAMETERS_URL,
    META_URL,
    SilverblazeClient,
    hourly_series,
)
from custom_components.silverblaze.api.models import PageContext, Series, ServicePoint

from .conftest import load_fixture_json, load_fixture_text
from .helpers_portal import make_day_response

ENC = "SYNTHETIC_ENC_TOKEN"
URL = f"{GET_PAGE_PARAMETERS_URL}?{urlencode({'enc': ENC})}"


async def _no_sleep(_seconds: float) -> None:
    return None


@pytest.fixture
async def client():
    async with aiohttp.ClientSession() as session:
        http = SilverblazeHttp(
            session, build_allowlist([]), min_interval=0, sleep=_no_sleep, clock=lambda: 0.0
        )
        yield SilverblazeClient(http)


async def test_parameters_are_base64_of_layout_parameters(client, mock_aioresponse):
    fixture = load_fixture_text("silverblaze/get_page_parameters.json")
    mock_aioresponse.get(URL, status=200, body=fixture)

    ctx = await client.get_page_parameters(ENC)

    assert ctx.parameters_b64 == "U1lOVEhFVElDX0xBWU9VVF9QQVJBTUVURVJT"
    assert base64.b64decode(ctx.parameters_b64) == b"SYNTHETIC_LAYOUT_PARAMETERS"
    assert ctx.client_id == 999
    assert ctx.tab_key == "tab.myusage"


async def test_parameters_encoded_as_latin1(client, mock_aioresponse):
    body = {"ClientId": 1, "Layout": {"TabKey": "t", "Parameters": "café"}}
    mock_aioresponse.get(URL, status=200, body=json.dumps(body))

    ctx = await client.get_page_parameters(ENC)

    assert ctx.parameters_b64 == base64.b64encode(b"caf\xe9").decode()


async def test_request_carries_enc_and_api_headers(client, mock_aioresponse):
    mock_aioresponse.get(
        URL, status=200, body=load_fixture_text("silverblaze/get_page_parameters.json")
    )

    await client.get_page_parameters(ENC)

    ((key, calls),) = mock_aioresponse.requests.items()
    assert key[0] == "GET"
    assert key[1].query["enc"] == ENC
    assert key[1].path == "/api/Page/GetPageParameters"
    sent = calls[0].kwargs["headers"]
    assert dict(sent) == dict(API_HEADERS)


@pytest.mark.parametrize(
    "body",
    [
        {"ClientId": 1},
        {"ClientId": 1, "Layout": None},
        {"ClientId": 1, "Layout": {"TabKey": "t"}},
        {"ClientId": 1, "Layout": {"TabKey": "t", "Parameters": ""}},
        {"ClientId": 1, "Layout": {"TabKey": "t", "Parameters": None}},
    ],
)
async def test_missing_parameters_is_flow_changed(client, mock_aioresponse, body):
    mock_aioresponse.get(URL, status=200, body=json.dumps(body))

    with pytest.raises(LoginFlowChanged):
        await client.get_page_parameters(ENC)


async def test_non_json_is_unexpected_response(client, mock_aioresponse):
    mock_aioresponse.get(URL, status=200, body="<html>nope</html>")

    with pytest.raises(UnexpectedResponse):
        await client.get_page_parameters(ENC)


async def test_non_object_json_is_unexpected_response(client, mock_aioresponse):
    mock_aioresponse.get(URL, status=200, body="[1, 2]")

    with pytest.raises(UnexpectedResponse):
        await client.get_page_parameters(ENC)


async def test_non_200_is_cannot_connect(client, mock_aioresponse):
    mock_aioresponse.get(URL, status=404, body="missing")

    with pytest.raises(CannotConnect):
        await client.get_page_parameters(ENC)


def test_fixture_is_valid_json():
    assert load_fixture_json("silverblaze/get_page_parameters.json")["ClientId"] == 999


def test_page_context_repr_hides_parameters():
    ctx = PageContext(client_id=999, tab_key="tab.myusage", parameters_b64="SECRETBLOB")
    assert "SECRETBLOB" not in repr(ctx)
    assert "tab.myusage" in repr(ctx)


def test_service_point_repr_hides_id():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    series = Series("hourly", "usage", "kWh", "E", now, now, "raw1", "raw2")
    sp = ServicePoint(id="SP0000000001", timezone_label="Mountain", series=(series,))
    assert "SP0000000001" not in repr(sp)
    assert "Mountain" in repr(sp)


# --- Meta, one-day data, session expiry -------------------------------------------------

CTX = PageContext(client_id=999, tab_key="tab.myusage", parameters_b64="U1lOVEhFVElD")
MST = ZoneInfo("America/Denver")
SP_ID = "SP0000000001"


def _mock_meta(mock, fixture="meta_one_sp.json", **kw):
    kw.setdefault("status", 200)
    kw.setdefault("body", load_fixture_text(f"silverblaze/{fixture}"))
    mock.get(re.compile(re.escape(META_URL) + r"(\?.*)?$"), **kw)


def _mock_data(mock, fixture="day_normal.json", **kw):
    kw.setdefault("status", 200)
    kw.setdefault("body", load_fixture_text(f"silverblaze/{fixture}"))
    mock.get(re.compile(re.escape(DATA_URL) + r"(\?.*)?$"), **kw)


async def _sp_and_series(client, mock):
    _mock_meta(mock)
    (sp,) = await client.get_meta(CTX)
    series = hourly_series(sp)
    assert series is not None
    return sp, series


async def test_meta_one_service_point(client, mock_aioresponse):
    _mock_meta(mock_aioresponse)

    (sp,) = await client.get_meta(CTX)

    assert sp.id == SP_ID
    assert sp.timezone_label == "MST"
    (series,) = sp.series
    assert (series.interval, series.kind, series.uom, series.commodity_code) == (
        "60min",
        "usage",
        "kwh",
        "1",
    )
    assert series.oldest_raw == "2025-01-01T07:00:00Z"
    assert series.newest_utc == datetime(2026, 11, 2, 6, tzinfo=UTC)
    assert hourly_series(sp) is series
    assert hourly_series(sp, "electric") is series


async def test_meta_two_service_points(client, mock_aioresponse):
    _mock_meta(mock_aioresponse, "meta_two_sp.json")

    points = await client.get_meta(CTX)

    assert [p.id for p in points] == ["SP0000000001", "SP0000000002"]


async def test_meta_request_shape(client, mock_aioresponse):
    _mock_meta(mock_aioresponse)

    await client.get_meta(CTX)

    ((key, calls),) = mock_aioresponse.requests.items()
    assert key[1].path == "/api/DataDownload/Meta"
    assert dict(key[1].query) == {
        "tabkey": "null",
        "parameters": CTX.parameters_b64,
        "customerByAccountId": "true",
    }
    assert dict(calls[0].kwargs["headers"]) == dict(API_HEADERS)


@pytest.mark.parametrize(
    "body",
    ["not json", "[]", "{}", '{"body": {"Metadata": [{"CollectionId": "x"}]}}'],
)
async def test_meta_bad_shape_is_unexpected(client, mock_aioresponse, body):
    _mock_meta(mock_aioresponse, body=body)

    with pytest.raises(UnexpectedResponse):
        await client.get_meta(CTX)


async def test_meta_non_200_is_cannot_connect(client, mock_aioresponse):
    _mock_meta(mock_aioresponse, status=404, body="missing")

    with pytest.raises(CannotConnect) as err:
        await client.get_meta(CTX)
    assert not isinstance(err.value, SessionExpired)


def test_hourly_series_requires_hourly_kwh_electric():
    now = datetime(2026, 1, 1, tzinfo=UTC)
    daily = Series("1day", "usage", "kwh", "1", now, now, "a", "b")
    gas = Series("60min", "usage", "kwh", "2", now, now, "a", "b")
    demand = Series("60min", "demand", "kwh", "1", now, now, "a", "b")
    sp = ServicePoint(id=SP_ID, timezone_label="MST", series=(daily, gas, demand))
    assert hourly_series(sp) is None


async def test_data_request_parameters(client, mock_aioresponse):
    sp, series = await _sp_and_series(client, mock_aioresponse)
    _mock_data(mock_aioresponse)

    await client.get_day(CTX, sp, series, date(2026, 1, 15), MST)

    key, calls = next(
        (k, c)
        for k, c in mock_aioresponse.requests.items()
        if "Data" in k[1].path and "Meta" not in k[1].path
    )
    assert key[1].path == "/api/ConsumptionV3/Data"
    assert dict(key[1].query) == {
        "tabKey": "tab.myusage",
        "parameters": CTX.parameters_b64,
        "commodity": "electric",
        "resolution": "60min",
        "startDate": "2026-01-14T18:00:00.000Z",
        "endDate": "2026-01-15T17:59:00.000Z",
        "collectionId": SP_ID,
        "netBillingStartDate": "",
        "collectionType": "ServicePointId",
        "clientTimezone": "MST",
        "oldestDate": "2025-01-01T07:00:00Z",
        "newestDate": "2026-11-02T06:00:00Z",
        "chartId": "houravg",
        "isBillPeriodSelected": "false",
        "paging": "true",
        "meterSelected": "false",
        "drilledFromChartId": "",
        "returning": "false",
        "monthCount": "0",
        "drilling": "false",
    }
    sent = dict(calls[0].kwargs["headers"])
    assert sent["Pragma"] == "no-cache"
    assert sent["Cache-Control"] == "no-cache"


async def test_normal_day(client, mock_aioresponse):
    sp, series = await _sp_and_series(client, mock_aioresponse)
    _mock_data(mock_aioresponse)

    day = await client.get_day(CTX, sp, series, date(2026, 1, 15), MST)

    assert len(day.reads) == 24
    assert (day.slot_count, day.null_count) == (24, 0)
    assert day.reads[0].start_utc == datetime(2026, 1, 15, 7, tzinfo=UTC)
    assert day.reads[-1].start_utc == datetime(2026, 1, 16, 6, tzinfo=UTC)
    assert sum(r.kwh for r in day.reads) == 18.0


async def test_null_slots_are_absent(client, mock_aioresponse):
    sp, series = await _sp_and_series(client, mock_aioresponse)
    _mock_data(mock_aioresponse, "day_with_nulls.json")

    day = await client.get_day(CTX, sp, series, date(2026, 1, 15), MST)

    assert (day.slot_count, day.null_count, len(day.reads)) == (24, 2, 22)
    starts = {r.start_utc for r in day.reads}
    assert datetime(2026, 1, 15, 10, tzinfo=UTC) not in starts
    assert datetime(2026, 1, 15, 11, tzinfo=UTC) not in starts


async def test_empty_day(client, mock_aioresponse):
    sp, series = await _sp_and_series(client, mock_aioresponse)
    _mock_data(mock_aioresponse, "day_empty.json")

    day = await client.get_day(CTX, sp, series, date(2026, 1, 15), MST)

    assert day.reads == ()
    assert (day.slot_count, day.null_count) == (0, 0)


def _spring_epochs(null_epoch_hour: int | None) -> list[int]:
    """Epochs for 2026-03-08 (America/Denver): 07:00Z is local midnight, 09:00Z is 03:00 MDT."""
    base = int(datetime(2026, 3, 8, 7, tzinfo=UTC).timestamp() * 1000)
    hour = 3_600_000
    if null_epoch_hour is None:
        return [base + i * hour for i in range(23)]
    # 24 slots: local 00:00, 01:00, [null 02:00], 03:00 ... 23:00
    epochs = [base, base + hour, base + null_epoch_hour * hour]
    epochs += [base + (2 + i) * hour for i in range(21)]
    return epochs


@pytest.mark.parametrize(
    ("shape", "expect_warning"),
    [("null_shares_0900z", True), ("null_shares_0800z", True), ("23_slots_no_null", False)],
)
async def test_spring_forward_day(client, mock_aioresponse, caplog, shape, expect_warning):
    sp, series = await _sp_and_series(client, mock_aioresponse)
    caplog.set_level(logging.DEBUG)
    if shape == "null_shares_0900z":
        _mock_data(mock_aioresponse, "day_dst_spring_2026-03-08.json")
    else:
        if shape == "null_shares_0800z":
            epochs = _spring_epochs(1)
            values = [1.0, 1.0, None] + [1.0] * 21
        else:
            epochs = _spring_epochs(None)
            values = [1.0] * 23
        body = make_day_response(date(2026, 3, 8), MST, values, epochs_ms=epochs)
        _mock_data(mock_aioresponse, body=json.dumps(body))

    day = await client.get_day(CTX, sp, series, date(2026, 3, 8), MST)

    assert len(day.reads) == 23
    assert day.reads[0].start_utc == datetime(2026, 3, 8, 7, tzinfo=UTC)
    starts = {r.start_utc for r in day.reads}
    assert datetime(2026, 3, 8, 9, tzinfo=UTC) in starts  # 03:00 MDT
    assert sum(r.kwh for r in day.reads) == 23.0
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == (1 if expect_warning else 0)
    if expect_warning:
        assert "Dropped 1 duplicate hourly slot(s) on 2026-03-08" in warnings[0].getMessage()


async def test_fall_back_day_25_slots(client, mock_aioresponse):
    sp, series = await _sp_and_series(client, mock_aioresponse)
    _mock_data(mock_aioresponse, "day_dst_fall_2026-11-01_25_slots.json")

    day = await client.get_day(CTX, sp, series, date(2026, 11, 1), MST)

    assert len(day.reads) == 25
    starts = [r.start_utc for r in day.reads]
    assert len(set(starts)) == 25
    assert datetime(2026, 11, 1, 7, tzinfo=UTC) in starts
    assert datetime(2026, 11, 1, 8, tzinfo=UTC) in starts
    assert sum(r.kwh for r in day.reads) == 25.0


async def test_fall_back_day_24_slots(client, mock_aioresponse, caplog):
    sp, series = await _sp_and_series(client, mock_aioresponse)
    _mock_data(mock_aioresponse, "day_dst_fall_2026-11-01_24_slots.json")
    caplog.set_level(logging.DEBUG)

    day = await client.get_day(CTX, sp, series, date(2026, 11, 1), MST)

    assert day.slot_count == 24
    assert day.null_count == 0
    assert len(day.reads) == 24
    starts = [r.start_utc for r in day.reads]
    assert len(set(starts)) == 24
    assert datetime(2026, 11, 1, 7, tzinfo=UTC) not in starts  # first 01:00 MDT: no slot
    by_start = {r.start_utc: r.kwh for r in day.reads}
    assert by_start[datetime(2026, 11, 1, 8, tzinfo=UTC)] == 0.0  # zero kept, not null
    assert by_start[datetime(2026, 11, 1, 9, tzinfo=UTC)] == 3.0
    assert starts[0] == datetime(2026, 11, 1, 6, tzinfo=UTC)
    assert starts[-1] == datetime(2026, 11, 2, 6, tzinfo=UTC)
    assert sum(by_start.values()) == 25.0
    assert [r for r in caplog.records if r.levelno == logging.WARNING] == []


async def test_duplicate_epoch_keeps_one_and_warns(client, mock_aioresponse, caplog):
    sp, series = await _sp_and_series(client, mock_aioresponse)
    _mock_data(mock_aioresponse, "day_duplicate_epoch.json")
    caplog.set_level(logging.DEBUG)

    day = await client.get_day(CTX, sp, series, date(2026, 1, 15), MST)

    starts = [r.start_utc for r in day.reads]
    assert len(starts) == len(set(starts)) == 23
    assert datetime(2026, 1, 15, 13, tzinfo=UTC) in starts  # non-null duplicate kept
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "Dropped 1 duplicate hourly slot(s) on 2026-01-15" in warnings[0].getMessage()


async def test_duplicate_epoch_both_non_null_later_wins(client, mock_aioresponse):
    sp, series = await _sp_and_series(client, mock_aioresponse)
    body = make_day_response(date(2026, 1, 15), MST, [1.0, 2.0])
    body["IntervalLayout"]["ChartIntervals"][1]["EpochMs"] = body["IntervalLayout"][
        "ChartIntervals"
    ][0]["EpochMs"]
    _mock_data(mock_aioresponse, body=json.dumps(body))

    day = await client.get_day(CTX, sp, series, date(2026, 1, 15), MST)

    assert [r.kwh for r in day.reads] == [2.0]


async def test_wrong_day_response_is_unexpected(client, mock_aioresponse):
    sp, series = await _sp_and_series(client, mock_aioresponse)
    # Portal ignored the dates and answered with a different day.
    other = make_day_response(date(2026, 1, 20), MST, [1.0] * 24)
    _mock_data(mock_aioresponse, body=json.dumps(other))

    with pytest.raises(UnexpectedResponse):
        await client.get_day(CTX, sp, series, date(2026, 1, 15), MST)


async def test_length_mismatch_is_unexpected(client, mock_aioresponse):
    sp, series = await _sp_and_series(client, mock_aioresponse)
    body = make_day_response(date(2026, 1, 15), MST, [1.0] * 24)
    body["AlignedSeriesData"]["forward/usage"].pop()
    _mock_data(mock_aioresponse, body=json.dumps(body))

    with pytest.raises(UnexpectedResponse):
        await client.get_day(CTX, sp, series, date(2026, 1, 15), MST)


async def test_timestamps_ignore_category_labels(client, mock_aioresponse):
    sp, series = await _sp_and_series(client, mock_aioresponse)
    body = make_day_response(date(2026, 1, 15), MST, [1.0] * 3)
    for slot in body["IntervalLayout"]["ChartIntervals"]:
        slot["Category"] = "12 AM"
        slot["Hour"] = 0
    _mock_data(mock_aioresponse, body=json.dumps(body))

    day = await client.get_day(CTX, sp, series, date(2026, 1, 15), MST)

    assert [r.start_utc.hour for r in day.reads] == [7, 8, 9]


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (200, load_fixture_text("silverblaze/session_expired.txt")),
        (401, "denied"),
        (403, "denied"),
        (200, '{"Message": "Invalid webtoken."}'),
        (200, ""),
        (200, "   "),
    ],
)
async def test_session_expiry_variants(client, mock_aioresponse, status, body):
    sp, series = await _sp_and_series(client, mock_aioresponse)
    _mock_meta(mock_aioresponse, status=status, body=body)
    _mock_data(mock_aioresponse, status=status, body=body)
    mock_aioresponse.get(URL, status=status, body=body)

    with pytest.raises(SessionExpired):
        await client.get_meta(CTX)
    with pytest.raises(SessionExpired):
        await client.get_day(CTX, sp, series, date(2026, 1, 15), MST)
    with pytest.raises(SessionExpired):
        await client.get_page_parameters(ENC)


async def test_empty_body_with_other_status_is_not_expiry(client, mock_aioresponse):
    sp, series = await _sp_and_series(client, mock_aioresponse)
    _mock_meta(mock_aioresponse, status=404, body="")
    _mock_data(mock_aioresponse, status=404, body="")

    with pytest.raises(CannotConnect):
        await client.get_meta(CTX)
    with pytest.raises(CannotConnect):
        await client.get_day(CTX, sp, series, date(2026, 1, 15), MST)


async def test_logs_hold_no_identifiers(client, mock_aioresponse, caplog):
    caplog.set_level(logging.DEBUG)
    sp, series = await _sp_and_series(client, mock_aioresponse)
    body = make_day_response(date(2026, 1, 15), MST, [7.125] * 24)
    body["IntervalLayout"]["ChartIntervals"][5]["EpochMs"] = body["IntervalLayout"][
        "ChartIntervals"
    ][4]["EpochMs"]
    _mock_data(mock_aioresponse, body=json.dumps(body))

    await client.get_day(CTX, sp, series, date(2026, 1, 15), MST)

    assert caplog.records
    assert any("2026-01-15" in r.getMessage() for r in caplog.records)
    assert any("duplicate" in r.getMessage() for r in caplog.records)
    text = caplog.text
    assert SP_ID not in text
    assert CTX.parameters_b64 not in text
    assert "parameters" not in text
    assert "7.125" not in text
