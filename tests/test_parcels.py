"""Privacy-safe SpeedX canonical mapping tests."""
import pytest

from custom_components.speedx.const import CAPABILITIES, ParcelStatus
from custom_components.speedx.parcels import (
    _warned,
    build_history,
    format_dimensions,
    map_event_status,
    map_parcel_status,
    normalize_parcel,
    to_iso_timestamp,
    tracking_url,
)

from .payloads import active_sample, delivered_sample, event


@pytest.fixture(autouse=True)
def _reset_warned():
    """Every one-shot warning fires again in each test, not just the first."""
    _warned.clear()
    yield
    _warned.clear()


def test_observed_status_map():
    assert map_parcel_status("50001") is ParcelStatus.REGISTERED
    assert map_parcel_status("57201") is ParcelStatus.DELIVERED
    assert map_parcel_status("57609") is ParcelStatus.PROBLEM
    assert map_parcel_status("LAST_MILE_UNDELIVERED") is ParcelStatus.PROBLEM
    assert map_parcel_status("57406") is ParcelStatus.UNKNOWN
    assert map_parcel_status("unseen") is ParcelStatus.UNKNOWN
    assert map_parcel_status(None) is ParcelStatus.UNKNOWN
    assert map_parcel_status("") is ParcelStatus.UNKNOWN


def test_tracking_url_is_none_without_a_code():
    assert tracking_url(None) is None
    assert tracking_url("") is None
    assert tracking_url("SPXTEST000001").endswith("SPXTEST000001")


def test_map_event_status_none_for_missing_or_unmapped_code():
    assert map_event_status(None) is None
    assert map_event_status("") is None
    assert map_event_status("57201") is ParcelStatus.DELIVERED
    assert map_event_status("brand-new-unmapped") is None


def test_to_iso_timestamp_handles_epoch_ms_strings_and_overflow():
    assert to_iso_timestamp(None) is None
    assert to_iso_timestamp("2026-04-29T13:12:42-04:00") == "2026-04-29T13:12:42-04:00"
    assert to_iso_timestamp(1777820862000).startswith("2026-")
    assert to_iso_timestamp(10**20) is None


def test_format_dimensions_requires_all_three_values():
    assert format_dimensions(None, 10, 10) is None
    assert format_dimensions(30, None, 10) is None
    assert format_dimensions(30, 20, None) is None
    assert format_dimensions(30, 20, 10) == {
        "length": 30,
        "width": 20,
        "height": 10,
        "text": "30 x 20 x 10 cm",
    }


def test_ssr_history_is_reversed_and_only_exact_duplicates_collapse():
    events = active_sample()["events"]
    duplicate = dict(events[0])
    repeated = event("57113", "2026-04-28T18:00:00", "LAST_MILE_ENROUTE")
    history = build_history(events + [duplicate, repeated])
    assert len(history) == 4
    assert history[0]["raw_status"] == "57113"
    assert history[-1]["raw_status"] == "57113"


def test_iana_timestamp_and_dst_fail_safely():
    normal = build_history([event("57201", "2026-04-29T13:12:42", "LAST_MILE_DELIVERED")])
    assert normal[0]["timestamp"].endswith("-04:00")
    ambiguous = build_history([event("57201", "2026-11-01T01:30:00", "LAST_MILE_DELIVERED")])
    assert ambiguous[0]["timestamp"] is None
    invalid_zone = event("57201", "2026-04-29T13:12:42", "LAST_MILE_DELIVERED")
    invalid_zone["timeZone"] = "Not/AZone"
    assert build_history([invalid_zone])[0]["timestamp"] is None


def test_normalization_uses_category_and_redacts_carrier_pii():
    parcel = normalize_parcel(delivered_sample(), include_history=True)
    assert parcel["status"] is ParcelStatus.DELIVERED
    assert parcel["delivered_at"].endswith("-04:00")
    assert parcel["sender"] is parcel["receiver"] is parcel["pickup_point"] is None
    assert parcel["weight"] is parcel["dimensions"] is None
    assert parcel["raw"] == {"eventCode": "57201", "category": "LAST_MILE_DELIVERED"}
    assert "location" not in str(parcel)
    assert "Invented City" not in str(parcel)
    assert CAPABILITIES == frozenset({"url", "history"})


def test_out_for_delivery_description_promotes_enroute_category():
    parcel = normalize_parcel(active_sample())
    assert parcel["status"] is ParcelStatus.OUT_FOR_DELIVERY


def test_unknown_shapes_and_categories_degrade_without_pii():
    assert build_history(["bad", {"eventCode": "new"}])[-1]["timestamp"] is None
    unknown = normalize_parcel({"trackingNumber": "SPXTEST999999", "events": [event("99999", "2026-04-29T13:12:42", "NEW")]})
    assert unknown["status"] is ParcelStatus.UNKNOWN


def test_timestamp_shape_warns_once_on_partial_or_aware_wall_time(caplog):
    caplog.set_level("WARNING")
    partial = event("57201", "2026-04-29T13:12:42", "LAST_MILE_DELIVERED")
    del partial["timeZone"]
    assert build_history([partial])[0]["timestamp"] is None

    aware = event("57201", "2026-04-29T13:12:42+00:00", "LAST_MILE_DELIVERED")
    assert build_history([aware])[0]["timestamp"] is None

    shape_warnings = [r for r in caplog.records if "localTs/timeZone" in r.message]
    assert len(shape_warnings) == 1
    for record in caplog.records:
        assert "Invented City" not in record.message
        assert "00000" not in record.message


def test_event_with_no_time_fields_at_all_is_silent():
    assert build_history([{"eventCode": "57201", "category": "LAST_MILE_DELIVERED"}])[0]["timestamp"] is None


def test_status_disagreement_warns_once_and_keeps_ssr_status(caplog):
    caplog.set_level("WARNING")
    current = event("57201", "2026-04-29T13:12:42", "LAST_MILE_DELIVERED")
    ssr_events = [event("57113", "2026-04-28T15:52:17", "LAST_MILE_ENROUTE")]
    raw = {"trackingNumber": "SPXTEST000001", "events": ssr_events, "current": current, "source": "ssr"}
    parcel = normalize_parcel(raw)
    assert parcel["status"] is ParcelStatus.IN_TRANSIT
    assert any("disagrees" in r.message for r in caplog.records)


def test_status_agreement_does_not_warn(caplog):
    caplog.set_level("WARNING")
    current = event("57113", "2026-04-28T15:52:17", "LAST_MILE_ENROUTE")
    ssr_events = [event("57113", "2026-04-28T15:52:17", "LAST_MILE_ENROUTE")]
    raw = {"trackingNumber": "SPXTEST000001", "events": ssr_events, "current": current, "source": "ssr"}
    normalize_parcel(raw)
    assert not any("disagrees" in r.message for r in caplog.records)


def test_json_source_never_checks_disagreement(caplog):
    caplog.set_level("WARNING")
    current = event("57201", "2026-04-29T13:12:42", "LAST_MILE_DELIVERED")
    raw = {"trackingNumber": "SPXTEST000001", "events": [current], "current": current, "source": "json"}
    normalize_parcel(raw)
    assert not any("disagrees" in r.message for r in caplog.records)


def test_edd_presence_warns_once_without_leaking_value(caplog):
    caplog.set_level("WARNING")
    with_edd = event("57113", "2026-04-28T15:52:17", "LAST_MILE_ENROUTE")
    with_edd["edd"] = "2026-04-30"
    raw = {"trackingNumber": "SPXTEST000001", "events": [with_edd], "current": None, "source": "ssr"}
    normalize_parcel(raw)
    raw_current = {"trackingNumber": "SPXTEST000001", "events": [event("57113", "2026-04-28T15:52:17", "LAST_MILE_ENROUTE")], "current": with_edd, "source": "ssr"}
    normalize_parcel(raw_current)
    matches = [r for r in caplog.records if "edd" in r.message.lower()]
    assert len(matches) == 1
    assert "2026-04-30" not in matches[0].message


def test_no_edd_field_never_warns(caplog):
    caplog.set_level("WARNING")
    normalize_parcel(active_sample())
    assert not any("edd" in r.message.lower() for r in caplog.records)
