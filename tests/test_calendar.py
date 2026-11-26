"""Tests for the SpeedX deliveries calendar."""

from datetime import UTC, date, datetime
from unittest.mock import MagicMock

from homeassistant.components.calendar import CalendarEvent

from custom_components.speedx.calendar import SpeedXDeliveriesCalendar


def _entry(entry_id: str = "e1") -> MagicMock:
    entry = MagicMock()
    entry.entry_id = entry_id
    return entry


def _coordinator(data: list[dict]) -> MagicMock:
    coordinator = MagicMock()
    coordinator.data = data
    return coordinator


def _parcel(
    barcode, planned_from=None, planned_to=None, pickup=False, pickup_point=None
):
    return {
        "barcode": barcode,
        "sender": "Example Sender",
        "status": "out_for_delivery",
        "planned_from": planned_from,
        "planned_to": planned_to,
        "pickup": pickup,
        "pickup_point": pickup_point,
        "url": "https://track/1",
    }


def _cal(data):
    return SpeedXDeliveriesCalendar(_coordinator(data), _entry())


def test_event_returns_earliest_upcoming():
    cal = _cal(
        [
            _parcel("LATE", planned_from="2099-01-02T10:00:00Z"),
            _parcel("SOON", planned_from="2099-01-01T10:00:00Z"),
        ]
    )
    event = cal.event
    assert event.uid == "SOON"
    assert event.summary == "Example Sender"


def test_event_title_prefers_contents_then_sender_then_fallback():
    soon = "2099-01-01T10:00:00Z"
    with_contents = {**_parcel("A", planned_from=soon), "contents": "Running shoes"}
    assert _cal([with_contents]).event.summary == "Running shoes"
    no_sender = {**_parcel("A", planned_from=soon), "sender": None}
    assert _cal([no_sender]).event.summary == "Parcel A"
    nothing = {**no_sender, "barcode": ""}
    assert _cal([nothing]).event.summary == "SpeedX parcel"


def test_event_none_when_no_planned():
    assert _cal([_parcel("A")]).event is None


def test_moment_gets_one_hour_duration():
    cal = _cal([_parcel("A", planned_from="2099-01-01T10:00:00Z")])
    events = cal._events()
    assert events[0].end == datetime(2099, 1, 1, 11, 0, tzinfo=UTC)


def test_interval_uses_window():
    cal = _cal(
        [
            _parcel(
                "A",
                planned_from="2099-01-01T10:00:00Z",
                planned_to="2099-01-01T12:00:00Z",
            )
        ]
    )
    assert cal._events()[0].end == datetime(2099, 1, 1, 12, 0, tzinfo=UTC)


def test_pickup_sets_location():
    cal = _cal(
        [
            _parcel(
                "A",
                planned_from="2099-01-01T10:00:00Z",
                pickup=True,
                pickup_point="SpeedX Shop",
            )
        ]
    )
    assert cal._events()[0].location == "SpeedX Shop"


def test_naive_planned_from_treated_as_utc():
    cal = _cal([_parcel("A", planned_from="2099-01-01T10:00:00")])  # no tz
    assert cal._events()[0].start == datetime(2099, 1, 1, 10, 0, tzinfo=UTC)


def test_unparseable_planned_from_skipped():
    assert _cal([_parcel("A", planned_from="not-a-date")]).event is None


async def test_get_events_filters_by_range():
    cal = _cal(
        [
            _parcel("PAST", planned_from="2000-01-01T10:00:00Z"),
            _parcel("FUTURE", planned_from="2099-01-01T10:00:00Z"),
        ]
    )
    start = datetime(2098, 1, 1, tzinfo=UTC)
    end = datetime(2100, 1, 1, tzinfo=UTC)
    events = await cal.async_get_events(MagicMock(), start, end)
    assert {e.uid for e in events} == {"FUTURE"}


def test_whole_day_window_is_an_all_day_event():
    cal = _cal(
        [
            _parcel(
                "A",
                planned_from="2099-01-01T00:00:00+00:00",
                planned_to="2099-01-01T23:59:59+00:00",
            )
        ]
    )
    event = cal._events()[0]
    assert (event.start, event.end) == (date(2099, 1, 1), date(2099, 1, 2))


def test_a_moment_stretched_to_the_end_of_day_stays_timed():
    cal = _cal(
        [
            _parcel(
                "A",
                planned_from="2099-01-01T10:00:00+00:00",
                planned_to="2099-01-01T23:59:59+00:00",
            )
        ]
    )
    assert isinstance(cal._events()[0].start, datetime)


def _injected(*events):
    cal = _cal([])
    cal._events = lambda: list(events)
    return cal


def _all_day():
    return CalendarEvent(start=date(2099, 1, 1), end=date(2099, 1, 2), summary="day")


def _timed():
    return CalendarEvent(
        start=datetime(2099, 1, 3, 10, tzinfo=UTC),
        end=datetime(2099, 1, 3, 11, tzinfo=UTC),
        summary="timed",
    )


def test_event_handles_all_day_and_timed():
    assert _injected(_timed(), _all_day()).event.summary == "day"
    assert _injected(_all_day()).event.summary == "day"


async def test_async_get_events_handles_all_day_and_timed():
    cal = _injected(_all_day(), _timed())
    wide = await cal.async_get_events(
        None, datetime(2098, 12, 1, tzinfo=UTC), datetime(2099, 2, 1, tzinfo=UTC)
    )
    assert {e.summary for e in wide} == {"day", "timed"}
    narrow = await cal.async_get_events(
        None, datetime(2099, 1, 3, tzinfo=UTC), datetime(2099, 2, 1, tzinfo=UTC)
    )
    assert [e.summary for e in narrow] == ["timed"]
