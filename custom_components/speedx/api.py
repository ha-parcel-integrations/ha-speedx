"""Direct HTTP client for the public SpeedX consumer tracker."""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any

import aiohttp

from .const import TRACKING_API_URL, TRACKING_URL

_LOGGER = logging.getLogger(__name__)

# Where users report a broken assumption about this endpoint's contract.
NEW_ISSUE_URL = (
    "https://github.com/ha-parcel-integrations/ha-speedx/issues/new"
    "?template=unrecognised_status.yml"
)

# Keys already warned about, so each unconfirmed shape is logged only once
# per HA session instead of on every poll.
_warned: set[str] = set()


def _warn_once(key: str, message: str, *args: Any) -> None:
    if key in _warned:
        return
    _warned.add(key)
    _LOGGER.warning(message, *args)


def _warn_rate_limited(retry_after: float | None) -> None:
    """Warn once that SpeedX rate-limited a request, redacted of the code."""
    _warn_once(
        "rate-limited",
        "SpeedX responded with HTTP 429 (rate limited) — help us confirm its "
        "limits. Open an issue and paste this line: %s\n  retry_after=%s",
        NEW_ISSUE_URL,
        retry_after,
    )


def _warn_empty_data() -> None:
    """Warn once when neither the JSON scan nor the SSR page yielded data."""
    _warn_once(
        "empty-data",
        "SpeedX returned no usable data from either the JSON current scan or "
        "the SSR history page for a tracked code. Open an issue and paste "
        "this line: %s",
        NEW_ISSUE_URL,
    )


def _warn_rsc_missing() -> None:
    """Warn once when the SSR page no longer carries a Next Flight script."""
    _warn_once(
        "rsc-missing",
        "The SpeedX tracking page no longer contains a "
        "self.__next_f.push(...) script — the site may have changed shape. "
        "Open an issue and paste this line: %s",
        NEW_ISSUE_URL,
    )


def _warn_event_shape_changed() -> None:
    """Warn once when RSC chunks are present but no ``events`` array decodes."""
    _warn_once(
        "event-shape-changed",
        "The SpeedX tracking page's Next Flight script no longer carries a "
        "decodable 'events' array — its payload shape may have changed. Open "
        "an issue and paste this line: %s",
        NEW_ISSUE_URL,
    )


class SpeedXApiError(Exception):
    """Raised when a SpeedX API call returns an unexpected response."""

    def __init__(
        self,
        detail: str,
        *,
        status_code: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        """Store the status code and the ``Retry-After`` header, if any."""
        super().__init__(f"SpeedX API request failed: {detail}")
        self.detail = detail
        self.status_code = status_code
        self.retry_after = retry_after


def _events_from_flight(html: str) -> list[dict[str, Any]] | None:
    """Decode one complete ``events`` array from Next Flight script chunks."""
    decoder = json.JSONDecoder()
    for script in html.split("self.__next_f.push(")[1:]:
        chunk = script.split("</script>", 1)[0].rsplit(")", 1)[0].strip()
        try:
            pushed = json.loads(chunk)
        except (json.JSONDecodeError, TypeError):
            continue
        texts = (item for item in pushed if isinstance(item, str)) if isinstance(pushed, list) else ()
        for text in texts:
            start = text.find('"events":')
            if start < 0:
                continue
            try:
                value, _ = decoder.raw_decode(text[start + len('"events":') :].lstrip())
            except json.JSONDecodeError:
                continue
            if isinstance(value, list) and all(isinstance(event, dict) for event in value):
                return value
    return None


class SpeedXApiClient:
    """Fetch one configured tracking code without credentials or a browser."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        """Initialise the client with Home Assistant's shared session."""
        self._session = session

    async def _current_scan(self, tracking_code: str) -> dict[str, Any] | None:
        body = {"trackingNumbers": [tracking_code], "visitorId": str(uuid.uuid4())}
        async with self._session.post(TRACKING_API_URL, json=body, headers={"Accept": "application/json"}) as response:
            if response.status == 429:
                try:
                    retry_after = float(response.headers.get("Retry-After", ""))
                except ValueError:
                    retry_after = None
                _warn_rate_limited(retry_after)
                raise SpeedXApiError("HTTP 429", status_code=429, retry_after=retry_after)
            if response.status != 200:
                raise SpeedXApiError(f"HTTP {response.status}", status_code=response.status)
            try:
                payload = await response.json(content_type=None)
            except (ValueError, json.JSONDecodeError) as err:
                raise SpeedXApiError("unparseable JSON current scan") from err
        if not isinstance(payload, list):
            raise SpeedXApiError("unexpected JSON current-scan shape")
        return payload[0] if len(payload) == 1 and isinstance(payload[0], dict) else None

    async def _history(self, tracking_code: str) -> list[dict[str, Any]] | None:
        async with self._session.get(TRACKING_URL.format(tracking_code=tracking_code), headers={"Accept": "text/html"}) as response:
            if response.status == 429:
                try:
                    retry_after = float(response.headers.get("Retry-After", ""))
                except ValueError:
                    retry_after = None
                _warn_rate_limited(retry_after)
                raise SpeedXApiError("HTTP 429", status_code=429, retry_after=retry_after)
            if response.status != 200:
                raise SpeedXApiError(f"HTTP {response.status}", status_code=response.status)
            html = await response.text()
        if "self.__next_f.push(" not in html:
            _warn_rsc_missing()
            return None
        events = _events_from_flight(html)
        if events is None:
            _warn_event_shape_changed()
        return events

    async def async_get_parcel(self, tracking_code: str) -> dict[str, Any]:
        """Fetch current JSON then authoritative SSR history for one code."""
        current = await self._current_scan(tracking_code)
        events = await self._history(tracking_code)
        if events:
            return {"trackingNumber": tracking_code, "events": events, "current": current, "source": "ssr"}
        if current:
            return {"trackingNumber": current.get("trackingNumber") or tracking_code, "events": [current], "current": current, "source": "json"}
        _warn_empty_data()
        raise SpeedXApiError("empty or undecodable carrier response")
