"""Tests for the direct JSON + server-rendered SpeedX client."""
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.speedx.api import (
    SpeedXApiClient,
    SpeedXApiError,
    _events_from_flight,
    _warned,
)

from .payloads import ACTIVE_CODE, event


@pytest.fixture(autouse=True)
def _reset_warned():
    """Every one-shot warning fires again in each test, not just the first."""
    _warned.clear()
    yield
    _warned.clear()


def _response(status: int, *, json_body=None, text="", headers=None):
    response = AsyncMock(status=status, headers=headers or {})
    response.json = AsyncMock(return_value=json_body)
    response.text = AsyncMock(return_value=text)
    context = MagicMock()
    context.__aenter__ = AsyncMock(return_value=response)
    context.__aexit__ = AsyncMock(return_value=False)
    return context


def _session(post, get):
    session = MagicMock()
    session.post.return_value, session.get.return_value = post, get
    return session


async def test_uses_json_current_scan_then_authoritative_ssr_history():
    events = [event("57201", "2026-04-29T13:12:42", "LAST_MILE_DELIVERED")]
    flight = '<script>self.__next_f.push([1,' + json.dumps('x{"events":' + json.dumps(events) + '}') + '])</script>'
    session = _session(_response(200, json_body=[event("57113", "2026-04-29T08:46:00", "LAST_MILE_ENROUTE")]), _response(200, text=flight))
    parcel = await SpeedXApiClient(session).async_get_parcel(ACTIVE_CODE)
    assert parcel["source"] == "ssr"
    assert parcel["events"] == events
    body = session.post.call_args.kwargs["json"]
    assert body["trackingNumbers"] == [ACTIVE_CODE]
    assert body["visitorId"]


async def test_json_is_the_current_scan_fallback_when_ssr_is_unavailable():
    current = event("57201", "2026-04-29T13:12:42", "LAST_MILE_DELIVERED")
    parcel = await SpeedXApiClient(_session(_response(200, json_body=[current]), _response(200, text="<html>error</html>"))).async_get_parcel(ACTIVE_CODE)
    assert parcel["source"] == "json"
    assert parcel["events"] == [current]


@pytest.mark.parametrize("status", [403, 500, 429])
async def test_json_http_errors_raise(status):
    with pytest.raises(SpeedXApiError) as error:
        await SpeedXApiClient(_session(_response(status), _response(200))).async_get_parcel(ACTIVE_CODE)
    assert error.value.status_code == status


async def test_empty_json_and_no_events_is_a_retryable_error():
    with pytest.raises(SpeedXApiError):
        await SpeedXApiClient(_session(_response(200, json_body=[]), _response(200, text="<html></html>"))).async_get_parcel(ACTIVE_CODE)


async def test_malformed_json_and_ssr_rate_limit_are_reported():
    malformed = _response(200, json_body=[])
    malformed.__aenter__.return_value.json.side_effect = ValueError("bad JSON")
    with pytest.raises(SpeedXApiError, match="unparseable"):
        await SpeedXApiClient(_session(malformed, _response(200))).async_get_parcel(ACTIVE_CODE)
    current = event("57201", "2026-04-29T13:12:42", "LAST_MILE_DELIVERED")
    with pytest.raises(SpeedXApiError) as error:
        await SpeedXApiClient(_session(_response(200, json_body=[current]), _response(429, headers={"Retry-After": "12"}))).async_get_parcel(ACTIVE_CODE)
    assert error.value.retry_after == 12


def test_flight_parser_rejects_malformed_and_selects_one_complete_array():
    good = [event("57201", "2026-04-29T13:12:42", "LAST_MILE_DELIVERED")]
    html = '<script>self.__next_f.push(not-json)</script><script>self.__next_f.push([1,' + json.dumps('{"events":' + json.dumps(good) + '}') + '])</script>'
    assert _events_from_flight(html) == good
    assert _events_from_flight("<html></html>") is None
    assert _events_from_flight('<script>self.__next_f.push("not a list")</script>') is None


async def test_unexpected_json_current_scan_shape_raises():
    with pytest.raises(SpeedXApiError, match="unexpected"):
        await SpeedXApiClient(_session(_response(200, json_body={"not": "a list"}), _response(200))).async_get_parcel(ACTIVE_CODE)


async def test_history_http_error_raises():
    current = event("57201", "2026-04-29T13:12:42", "LAST_MILE_DELIVERED")
    with pytest.raises(SpeedXApiError) as error:
        await SpeedXApiClient(_session(_response(200, json_body=[current]), _response(500))).async_get_parcel(ACTIVE_CODE)
    assert error.value.status_code == 500


async def test_retry_after_header_non_numeric_is_none():
    with pytest.raises(SpeedXApiError) as error:
        await SpeedXApiClient(
            _session(_response(429, headers={"Retry-After": "not-a-number"}), _response(200))
        ).async_get_parcel(ACTIVE_CODE)
    assert error.value.retry_after is None


async def test_rate_limit_warns_once(caplog):
    caplog.set_level("WARNING")
    with pytest.raises(SpeedXApiError):
        await SpeedXApiClient(
            _session(_response(429, headers={"Retry-After": "5"}), _response(200))
        ).async_get_parcel(ACTIVE_CODE)
    assert sum("rate limited" in r.message for r in caplog.records) == 1
    assert ACTIVE_CODE not in caplog.text


async def test_rsc_missing_warns_once_and_falls_back_to_json(caplog):
    caplog.set_level("WARNING")
    current = event("57201", "2026-04-29T13:12:42", "LAST_MILE_DELIVERED")
    parcel = await SpeedXApiClient(
        _session(_response(200, json_body=[current]), _response(200, text="<html>generic error page</html>"))
    ).async_get_parcel(ACTIVE_CODE)
    assert parcel["source"] == "json"
    assert any("next_f.push" in r.message for r in caplog.records)


async def test_event_shape_changed_warns_once_when_rsc_present_but_undecodable(caplog):
    caplog.set_level("WARNING")
    current = event("57201", "2026-04-29T13:12:42", "LAST_MILE_DELIVERED")
    broken_flight = '<script>self.__next_f.push([1,"no events key here"])</script>'
    parcel = await SpeedXApiClient(
        _session(_response(200, json_body=[current]), _response(200, text=broken_flight))
    ).async_get_parcel(ACTIVE_CODE)
    assert parcel["source"] == "json"
    assert any("payload shape may have changed" in r.message for r in caplog.records)


async def test_empty_data_warns_once(caplog):
    caplog.set_level("WARNING")
    with pytest.raises(SpeedXApiError):
        await SpeedXApiClient(
            _session(_response(200, json_body=[]), _response(200, text="<html></html>"))
        ).async_get_parcel(ACTIVE_CODE)
    assert sum("no usable data" in r.message for r in caplog.records) == 1


async def test_rate_limit_warning_key_is_reused_on_a_second_429():
    with pytest.raises(SpeedXApiError):
        await SpeedXApiClient(
            _session(_response(429, headers={"Retry-After": "5"}), _response(200))
        ).async_get_parcel(ACTIVE_CODE)
    # Second 429 in the same session hits the "already warned" branch.
    with pytest.raises(SpeedXApiError):
        await SpeedXApiClient(
            _session(_response(429, headers={"Retry-After": "5"}), _response(200))
        ).async_get_parcel(ACTIVE_CODE)


async def test_history_retry_after_header_non_numeric_is_none():
    current = event("57201", "2026-04-29T13:12:42", "LAST_MILE_DELIVERED")
    with pytest.raises(SpeedXApiError) as error:
        await SpeedXApiClient(
            _session(_response(200, json_body=[current]), _response(429, headers={"Retry-After": "abc"}))
        ).async_get_parcel(ACTIVE_CODE)
    assert error.value.retry_after is None


def test_flight_parser_skips_an_events_key_with_undecodable_value():
    chunk = [1, 'x{"events": not-valid-json}', '{"events":' + json.dumps([]) + "}"]
    html = "<script>self.__next_f.push(" + json.dumps(chunk) + ")</script>"
    assert _events_from_flight(html) == []


async def test_current_scan_is_carried_through_on_ssr_success():
    current = event("57113", "2026-04-28T15:52:17", "LAST_MILE_ENROUTE")
    ssr_event = event("57201", "2026-04-29T13:12:42", "LAST_MILE_DELIVERED")
    flight = '<script>self.__next_f.push([1,' + json.dumps('{"events":' + json.dumps([ssr_event]) + '}') + '])</script>'
    parcel = await SpeedXApiClient(
        _session(_response(200, json_body=[current]), _response(200, text=flight))
    ).async_get_parcel(ACTIVE_CODE)
    assert parcel["current"] == current
    assert parcel["source"] == "ssr"
