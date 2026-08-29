"""Canonical parcel shape, status mapping and list helpers.

Everything in this module is a **pure function** — no I/O, no Home Assistant
objects beyond the config entry's options. That is deliberate: it keeps the
carrier-specific mapping (which you rewrite per carrier) apart from the
coordinator (which is nearly identical everywhere), and it makes the mapping
trivially unit-testable without spinning up HA.

The carrier-specific :data:`_STATUS_MAP` and :func:`normalize_parcel` are kept
here. Everything else — the timestamp parsing, the history builder, the sort
contract, the delivered filter, the one-shot warnings for unmapped/unconfirmed
shapes — is suite-wide machinery and should be left alone.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from homeassistant.config_entries import ConfigEntry

from .const import (
    CONF_DELIVERED_FILTER_AMOUNT,
    CONF_DELIVERED_FILTER_TYPE,
    DEFAULT_DELIVERED_FILTER_AMOUNT,
    DEFAULT_DELIVERED_FILTER_TYPE,
    HISTORY_MAX_EVENTS,
    TRACKING_URL,
    ParcelStatus,
)

_LOGGER = logging.getLogger(__name__)

# Where users report a status we do not map yet. Rewritten by the bootstrap
# script; it must point at the carrier's own repo so the log line is
# copy-pasteable straight into a new issue.
#
# The ``?template=`` parameter matters: without it the link opens a blank form,
# and the report comes back missing the version and the log line we need.
NEW_ISSUE_URL = (
    "https://github.com/ha-parcel-integrations/ha-speedx/issues/new"
    "?template=unrecognised_status.yml"
)

# The observed SSR ``category`` values and their JSON-fallback ``eventCode``
# numbers, mapped onto ParcelStatus. The values must come from the canonical
# enum — never invent a new one. Prefer mapping too little over mapping
# wrongly: an unmapped value surfaces as ``unknown`` plus a one-shot warning
# that asks the user to report it, which is how the map grows.
_STATUS_MAP: dict[str, ParcelStatus] = {
    "PICKUP": ParcelStatus.REGISTERED,
    "LAST_MILE_ENROUTE": ParcelStatus.IN_TRANSIT,
    "LAST_MILE_DELIVERED": ParcelStatus.DELIVERED,
    "LAST_MILE_ATTEMPTED": ParcelStatus.PROBLEM,
    "LAST_MILE_UNDELIVERED": ParcelStatus.PROBLEM,
    "LAST_MILE_INTERCEPT": ParcelStatus.UNKNOWN,
    "ORIGIN_HANDLING": ParcelStatus.IN_TRANSIT,
    "50001": ParcelStatus.REGISTERED,
    "52001": ParcelStatus.IN_TRANSIT, "52002": ParcelStatus.IN_TRANSIT,
    "57101": ParcelStatus.IN_TRANSIT, "57102": ParcelStatus.IN_TRANSIT,
    "57104": ParcelStatus.IN_TRANSIT, "57110": ParcelStatus.IN_TRANSIT,
    "57112": ParcelStatus.IN_TRANSIT, "57113": ParcelStatus.IN_TRANSIT,
    "57201": ParcelStatus.DELIVERED, "57609": ParcelStatus.PROBLEM,
    "57406": ParcelStatus.UNKNOWN,
}

# Keys already warned about, so each unconfirmed shape is logged only once
# per HA session instead of on every poll.
_warned: set[str] = set()


def _warn_once(key: str, message: str, *args: Any) -> None:
    if key in _warned:
        return
    _warned.add(key)
    _LOGGER.warning(message, *args)


def _warn_unmapped_status(code: str) -> None:
    """Log an unmapped carrier status once, with a copy-paste issue link."""
    _warn_once(
        f"status:{code}",
        "Unrecognised SpeedX status — help us map it. Open an issue "
        "and paste this line: %s\n  status=%s → reported as 'unknown'",
        NEW_ISSUE_URL,
        code,
    )


def _warn_timestamp_shape(event: dict) -> None:
    """Warn once when an event's localTs/timeZone cannot resolve to a timestamp.

    Structure only — the event's keys, never its values (``localTs`` and
    ``timeZone`` can carry a recipient-adjacent detail).
    """
    _warn_once(
        "timestamp-shape",
        "A SpeedX event's localTs/timeZone did not resolve to an aware "
        "timestamp — no timestamp is published for it rather than risk an "
        "unanchored one. Open an issue and paste this line: %s\n  keys=%s",
        NEW_ISSUE_URL,
        sorted(event) if isinstance(event, dict) else type(event).__name__,
    )


def _warn_status_disagreement(json_status: ParcelStatus, ssr_status: ParcelStatus) -> None:
    """Warn once when the JSON current scan disagrees with the SSR history."""
    _warn_once(
        "status-disagreement",
        "SpeedX's JSON current-scan status disagrees with the SSR history's "
        "latest event status — help us confirm which one is authoritative. "
        "The SSR value is kept. Open an issue and paste this line: %s\n"
        "  json=%s ssr=%s",
        NEW_ISSUE_URL,
        json_status,
        ssr_status,
    )


def _warn_edd_present() -> None:
    """Warn once when SpeedX returns a populated ``edd`` field.

    EDD semantics are unconfirmed, so the value is withheld entirely; only its
    presence is logged, never the value.
    """
    _warn_once(
        "edd-present",
        "SpeedX returned a populated 'edd' field; its timestamp semantics "
        "are unconfirmed so no ETA is published from it. Open an issue and "
        "paste this line: %s",
        NEW_ISSUE_URL,
    )


def map_parcel_status(code: str | None) -> ParcelStatus:
    """Map a carrier status code to a canonical :class:`ParcelStatus`.

    ``None`` (a not-yet-scanned parcel) reports ``unknown`` silently; an
    unrecognised code reports ``unknown`` with a one-shot warning.
    """
    if not code:
        return ParcelStatus.UNKNOWN
    mapped = _STATUS_MAP.get(code)
    if mapped is not None:
        return mapped
    _warn_unmapped_status(code)
    return ParcelStatus.UNKNOWN


def map_event_status(code: str | None) -> ParcelStatus | None:
    """Map a history entry's status code to a canonical status, or ``None``.

    Unmapped codes keep ``status: null`` on the history entry (rather than
    ``unknown``, so a consumer can tell "no mapping" from "mapped to unknown")
    and warn once, reusing the parcel-status one-shot set.
    """
    if not code:
        return None
    mapped = _STATUS_MAP.get(code)
    if mapped is not None:
        return mapped
    _warn_unmapped_status(code)
    return None


def parse_iso(value: str | None) -> datetime | None:
    """Parse an ISO 8601 string to an aware datetime, or ``None`` on failure.

    Naive values are treated as UTC so a list always sorts without crashing on
    a mixed set.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def to_iso_timestamp(value: Any) -> str | None:
    """Return an ISO 8601 string for an API timestamp field.

    Numbers are treated as **epoch milliseconds** — the common case for the
    consumer APIs in this suite. Strings pass through untouched; their
    consumers are guarded by :func:`parse_iso`. Adjust the numeric branch if
    your carrier stamps in seconds.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    return str(value)


def format_dimensions(
    length: float | None, width: float | None, height: float | None
) -> dict[str, Any] | None:
    """Return the canonical ``dimensions`` dict, or ``None`` when incomplete.

    Units contract: **centimetres**, with ``text`` pre-formatted as
    ``"L x W x H cm"`` (integer values, lowercase ``x``) so dashboards can show
    a dimension without doing their own formatting. Convert before calling if
    the carrier reports millimetres or inches.
    """
    if length is None or width is None or height is None:
        return None
    return {
        "length": length,
        "width": width,
        "height": height,
        "text": f"{int(length)} x {int(width)} x {int(height)} cm",
    }


def _event_timestamp(event: dict) -> str | None:
    """Return an aware ISO timestamp from a local wall time and IANA zone.

    A bad/missing IANA zone or wall-time form never fabricates a UTC
    timestamp — it warns once and yields no timestamp instead. An event that
    carries neither field at all (a bare current-scan fallback row) is not a
    shape problem and stays silent.
    """
    local_ts, zone_name = event.get("localTs"), event.get("timeZone")
    if local_ts is None and zone_name is None:
        return None
    if not isinstance(local_ts, str) or not isinstance(zone_name, str):
        _warn_timestamp_shape(event)
        return None
    try:
        wall = datetime.fromisoformat(local_ts.replace("Z", "+00:00"))
        if wall.tzinfo is not None:
            _warn_timestamp_shape(event)
            return None
        zone = ZoneInfo(zone_name)
    except (ValueError, ZoneInfoNotFoundError):
        _warn_timestamp_shape(event)
        return None
    first, second = wall.replace(tzinfo=zone, fold=0), wall.replace(tzinfo=zone, fold=1)
    if first.utcoffset() != second.utcoffset():
        return None
    if first.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) != wall:
        _warn_timestamp_shape(event)
        return None
    return first.isoformat()


def _event_status(event: dict) -> ParcelStatus | None:
    category = event.get("category")
    if category == "LAST_MILE_ENROUTE" and "out for delivery" in str(event.get("eventDescription", "")).lower():
        return ParcelStatus.OUT_FOR_DELIVERY
    return map_event_status(str(category or event.get("eventCode") or "") or None)


def build_history(events: list | None, *, max_events: int = HISTORY_MAX_EVENTS) -> list[dict]:
    """Build the canonical ``history`` list from the carrier's event list.

    Each entry is ``{timestamp, status, raw_status}`` — identical across all
    suite carriers, and top-level (not under ``raw``) so it survives the
    aggregator's ``strip_raw()``. ``raw_status`` is the carrier's own event
    code — SpeedX has no separate human-readable text field. Sorted oldest →
    newest and capped to the most recent ``max_events``.
    """
    unique: list[dict] = []
    identities: set[tuple] = set()
    for event in events or []:
        if not isinstance(event, dict):
            continue
        identity = (event.get("eventCode"), event.get("localTs"), event.get("category"), event.get("eventDescription"))
        if identity in identities:
            continue
        identities.add(identity)
        unique.append(event)
    # Carrier SSR history is newest-first; reverse to the canonical order.
    history: list[dict] = []
    for event in reversed(unique):
        timestamp = _event_timestamp(event)
        entry = {
            "timestamp": timestamp,
            "status": _event_status(event),
            "raw_status": event.get("eventCode"),
        }
        history.append(entry)
    return history[-max_events:]


def tracking_url(tracking_code: str | None) -> str | None:
    """Construct the consumer tracking deep-link for a parcel."""
    if not tracking_code:
        return None
    return TRACKING_URL.format(tracking_code=tracking_code)


def normalize_parcel(raw: dict, *, include_history: bool = False) -> dict:
    """Return a carrier-agnostic parcel dict with the payload under ``raw``.

    The **keys of the returned dict are the contract**: every carrier in the
    suite returns exactly these, in this order, and the aggregator and
    cross-carrier dashboards depend on it. Set a key to ``None`` when the
    carrier does not expose it — never omit it.

    Rules worth keeping when you rewrite the body:

    * ``status`` is canonical, ``raw_status`` is the carrier's own text.
    * A delivered parcel has ``delivered_at`` set and ``planned_from`` /
      ``planned_to`` cleared — the ETA is meaningless once it has arrived.
    * ``planned_to`` is ``None`` for a point estimate; only fill it when the
      carrier genuinely reports a *window*.
    * ``weight`` is kilograms, ``dimensions`` centimetres (see
      :func:`format_dimensions`).
    * ``history`` is ``None`` when the option is off — the key still exists.
    """
    events = raw.get("events") if isinstance(raw.get("events"), list) else []
    newest = next((event for event in events if isinstance(event, dict)), {})
    tracking_code = raw.get("trackingNumber")
    status_code = newest.get("eventCode")
    status = _event_status(newest) or ParcelStatus.UNKNOWN
    delivered = status is ParcelStatus.DELIVERED

    delivered_at = _event_timestamp(newest) if status is ParcelStatus.DELIVERED else None

    current = raw.get("current") if isinstance(raw.get("current"), dict) else None
    if raw.get("source") == "ssr" and current is not None:
        json_status = _event_status(current)
        if json_status is not None and json_status is not status:
            _warn_status_disagreement(json_status, status)
    if current is not None and current.get("edd") is not None:
        _warn_edd_present()
    if any(event.get("edd") is not None for event in events if isinstance(event, dict)):
        _warn_edd_present()

    return {
        "carrier": "SpeedX",
        "barcode": tracking_code,
        "sender": None,
        "receiver": None,
        "status": status,
        "raw_status": status_code,
        "delivered": delivered,
        "delivered_at": delivered_at,
        "planned_from": None,
        "planned_to": None,
        "pickup": False,
        "pickup_point": None,
        "url": tracking_url(tracking_code),
        "weight": None,
        "dimensions": None,
        "history": build_history(events) if include_history else None,
        "raw": {key: newest[key] for key in ("eventCode", "category") if key in newest},
    }


def sort_parcels_by_ts(
    parcels: list[dict], key_field: str, *, descending: bool = False
) -> list[dict]:
    """Return normalised parcels sorted by the ISO timestamp at ``key_field``.

    The suite's sort contract: incoming/outgoing ascending on ``planned_from``,
    delivered descending on ``delivered_at``. Parcels whose value is missing or
    unparseable always sort to the end, regardless of ``descending``.
    """
    with_ts: list[tuple[datetime, dict]] = []
    without_ts: list[dict] = []
    for parcel in parcels:
        parsed = parse_iso(parcel.get(key_field))
        if parsed is None:
            without_ts.append(parcel)
        else:
            with_ts.append((parsed, parcel))
    with_ts.sort(key=lambda item: item[0], reverse=descending)
    return [parcel for _, parcel in with_ts] + without_ts


def apply_delivered_filter(parcels: list[dict], entry: ConfigEntry) -> list[dict]:
    """Trim the delivered list per the entry's retention option.

    ``parcels`` must already be sorted newest-first. ``days`` keeps deliveries
    from the last N days (an unparseable ``delivered_at`` is kept rather than
    silently dropped); the ``parcels`` type keeps the N most recent. Parcels
    stay *tracked* either way — this only controls what the delivered sensor
    shows.
    """
    options = entry.options
    filter_type = options.get(
        CONF_DELIVERED_FILTER_TYPE, DEFAULT_DELIVERED_FILTER_TYPE
    )
    amount = int(
        options.get(CONF_DELIVERED_FILTER_AMOUNT, DEFAULT_DELIVERED_FILTER_AMOUNT)
    )
    if filter_type == "days":
        cutoff = datetime.now(timezone.utc) - timedelta(days=amount)
        return [
            parcel
            for parcel in parcels
            if (parsed := parse_iso(parcel.get("delivered_at"))) is None
            or parsed >= cutoff
        ]
    return parcels[:amount]
