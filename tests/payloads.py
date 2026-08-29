"""Invented, privacy-safe SpeedX tracking payloads for tests."""
from __future__ import annotations

ACTIVE_CODE = "SPXTEST000001"
DELIVERED_CODE = "SPXTEST000002"


def event(code: str, local_ts: str, category: str, description: str = "status update") -> dict:
    return {
        "trackingNumber": ACTIVE_CODE,
        "eventCode": code,
        "eventDescription": description,
        "eventSupplementalInfo": "private test text",
        "localTs": local_ts,
        "timeZone": "America/New_York",
        "location": "Invented City",
        "zipCode": "00000",
        "category": category,
    }


def active_sample(code: str = ACTIVE_CODE) -> dict:
    events = [
        event("57113", "2026-04-29T08:46:00", "LAST_MILE_ENROUTE", "Out for delivery"),
        event("57101", "2026-04-28T15:52:17", "LAST_MILE_ENROUTE"),
        event("50001", "2026-04-27T23:03:58", "PICKUP"),
    ]
    for item in events:
        item["trackingNumber"] = code
    return {"trackingNumber": code, "events": events, "source": "ssr"}


def delivered_sample(code: str = DELIVERED_CODE) -> dict:
    sample = active_sample(code)
    sample["events"].insert(0, event("57201", "2026-04-29T13:12:42", "LAST_MILE_DELIVERED"))
    sample["events"][0]["trackingNumber"] = code
    return sample
