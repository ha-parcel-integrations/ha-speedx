"""The parcel contract's checks, run over every normalised sample."""

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from custom_components.speedx.const import ParcelStatus
from custom_components.speedx.parcels import (
    is_pickup,
    normalize_parcel,
    parse_iso,
    planned_for_status,
    planned_window,
)

from .payloads import active_sample, delivered_sample, event

CANONICAL_KEYS = [
    "carrier",
    "barcode",
    "sender",
    "receiver",
    "contents",
    "status",
    "raw_status",
    "delivered",
    "delivered_at",
    "planned_from",
    "planned_to",
    "pickup",
    "pickup_point",
    "url",
    "weight",
    "dimensions",
    "history",
    "raw",
]

ZONE = ZoneInfo("America/New_York")


def _with_newest(code: str, category: str) -> dict:
    sample = active_sample()
    sample["events"].insert(0, event(code, "2026-04-29T14:00:00", category))
    return sample


SAMPLES = {
    "delivered": delivered_sample,
    "active": active_sample,
    "problem": lambda: _with_newest("57607", "LAST_MILE_ATTEMPTED"),
    "unknown": lambda: _with_newest("99999", "NEW"),
    "bare": lambda: {"trackingNumber": "SPXTEST000009"},
}


@pytest.fixture(params=SAMPLES)
def parcel(request) -> dict:
    return normalize_parcel(SAMPLES[request.param](), include_history=True)


def test_keys_and_order(parcel):
    assert list(parcel) == CANONICAL_KEYS


def test_delivered_follows_status(parcel):
    assert parcel["delivered"] is (parcel["status"] is ParcelStatus.DELIVERED)


def test_delivered_at_only_when_delivered(parcel):
    if parcel["delivered"]:
        assert parcel["delivered_at"] is not None
    else:
        assert parcel["delivered_at"] is None


def test_planned_window_is_ordered_and_anchored(parcel):
    if parcel["planned_to"] is not None:
        assert parcel["planned_from"] is not None
        assert parse_iso(parcel["planned_to"]) > parse_iso(parcel["planned_from"])


def test_planned_is_empty_when_the_parcel_is_done(parcel):
    if parcel["status"] in (ParcelStatus.DELIVERED, ParcelStatus.RETURNING):
        assert parcel["planned_from"] is None
        assert parcel["planned_to"] is None


def test_pickup_is_false_and_unnamed_without_a_pickup_event(parcel):
    assert parcel["pickup"] is False
    assert parcel["pickup_point"] is None


def test_a_delivered_parcel_stays_not_pickup():
    parcel = normalize_parcel(delivered_sample())
    assert parcel["delivered"] is True
    assert parcel["pickup"] is False


def test_the_window_is_emptied_for_a_finished_parcel():
    window = ("2099-01-01T10:00:00+00:00", "2099-01-01T23:59:59+00:00")
    for status in (
        ParcelStatus.DELIVERED,
        ParcelStatus.AT_PICKUP_POINT,
        ParcelStatus.RETURNING,
    ):
        assert planned_for_status(status, *window) == (None, None)
    assert planned_for_status(ParcelStatus.IN_TRANSIT, *window) == window


def test_a_moment_ends_at_the_end_of_that_day():
    start, end = planned_window("2099-04-29T13:00:00Z", None, zone=ZONE)
    stop = datetime.fromisoformat(end)
    assert (stop.hour, stop.minute, stop.second, stop.microsecond) == (23, 59, 59, 0)
    assert stop.date() == parse_iso(start).date()


def test_a_date_covers_the_whole_day():
    start, end = planned_window("2099-04-29", None, zone=ZONE)
    first, last = datetime.fromisoformat(start), datetime.fromisoformat(end)
    assert (first.hour, first.minute, first.second) == (0, 0, 0)
    assert (last.hour, last.minute, last.second, last.microsecond) == (23, 59, 59, 0)
    assert first.date() == last.date()


def test_pickup_is_sticky_after_the_point_was_reached():
    reached = [ParcelStatus.DELIVERED, ParcelStatus.AT_PICKUP_POINT]
    assert is_pickup(None, ParcelStatus.DELIVERED, reached) is True
    assert is_pickup(True, ParcelStatus.IN_TRANSIT) is True
    assert is_pickup(None, ParcelStatus.DELIVERED, [ParcelStatus.DELIVERED]) is False


def test_pickup_follows_an_event_that_maps_to_the_pickup_point(monkeypatch):
    monkeypatch.setattr(
        "custom_components.speedx.parcels.map_event_status",
        lambda code: ParcelStatus.AT_PICKUP_POINT if code == "PICKUP" else None,
    )
    parcel = normalize_parcel(active_sample())
    assert parcel["pickup"] is True
    assert parcel["pickup_point"] is None
