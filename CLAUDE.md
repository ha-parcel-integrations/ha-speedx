# Working in this repository

Home Assistant custom integration for **SpeedX** parcel tracking.
Distributed via HACS; not part of HA core. One carrier in the
[ha-parcel-integrations](https://github.com/ha-parcel-integrations) suite,
**generated from ha-carrier-template** — everything outside *Carrier-specific
notes* is suite-wide; when in doubt check the template or a sibling repo.
No DTO layer.

## Shared conventions — fetch when relevant

Suite-wide rules live in
[`.github/CONVENTIONS.md`](https://github.com/ha-parcel-integrations/.github/blob/main/CONVENTIONS.md)
and are **not** repeated here. Don't fetch it every session — fetch it **before**
you act in one of these areas:

| Before you … | Fetch `CONVENTIONS.md` § |
|---|---|
| touch entities, sensors, config/options flow, coordinator, diagnostics, translations | *Home Assistant developer docs* (its table points on to the canonical HA page — don't rely on memory) |
| add/rename a parcel field, a `ParcelStatus`, or a bus event; change the sort/first-refresh; touch unmapped-status logging | *Parcel contract* — exact key set, units, sort, events + suppression; `test_parcels.py::test_normalize_publishes_exactly_the_canonical_keys` guards the key set |
| change which optional field this carrier populates vs. always returns `None` | Update `const.py`'s `CAPABILITIES` in the same commit — it feeds the comparison table on the docs site, so a field that starts (or stops) coming back non-null and isn't reflected there is a wrong claim on the website, not just a stale comment. If this carrier has more than one backend (a country-specific transport, not just a config option) with genuinely different field support, `CAPABILITIES` should be a `CAPABILITIES_BY_VARIANT` dict instead — one frozenset per backend, so a field only some backends populate doesn't get silently intersected away or overclaimed for the rest. See ha-dpd's or ha-gls's `const.py` for a live example |
| ship anything while below 1.0.0 (unconfirmed data) | *Pre-1.0 releases* — one-shot WARNINGs for every guessed shape/code |
| consider "fixing" a lint/pattern the skill flags (poll interval, inline client, sync requests) | *Deliberate skill divergences* — likely intentional, don't re-flag |
| commit, bump, tag, release, or write release notes; add a feature without a test | *Workflow / Commits / Versioning / Testing* |

**Structure, options flow, dynamic polling and module layout are suite-wide**
and identical in every carrier — the authoritative spec is
[`ha-carrier-template/scaffold/CLAUDE.md`](https://github.com/ha-parcel-integrations/ha-carrier-template/blob/main/scaffold/CLAUDE.md).
This repo follows it exactly.

**Suite-wide tripwires, kept inline on purpose:**
- **First refresh in `__init__.py`, before `async_forward_entry_setups`** — from
  a forwarded platform HA can't catch `ConfigEntryNotReady` and half-sets-up the
  entry. Runtime-only; tests don't catch a regression.
- **Setup stale-entity sweep is scoped to `domain == "sensor"` and skips
  `non_parcel_unique_ids`** — else it deletes the refresh button / the
  summary+diagnostic sensors. Add a new non-parcel sensor's unique_id to the set.
- **Per-parcel sensors are removed by the summary sensor** via
  `entity_registry.async_remove` (self-removal races and leaves ghosts).

## Carrier-specific notes

**Pickup.** Both `awaiting_pickup` and `en_route_to_pickup_point` are exposed
(they read 0 until a pickup parcel appears). `pickup` is
`is_pickup(None, status, event_statuses)`, so it only turns true once an event
maps to `AT_PICKUP_POINT`; `pickup_point` stays `None` because no real pickup
point name has been seen. **Pending:** no pickup-point status or code has been
observed yet, so no code maps to `AT_PICKUP_POINT`; once a real parcel shows
one, map it in `map_event_status`, and move `pickup_point` into `CAPABILITIES`
when a real name appears.

`raw` is the full record (the tracking payload as fetched, including events
with locations and free text); `_unrecorded_attributes` keeps it out of the
recorder and diagnostics redact it.

**Delivery window.** SpeedX's `edd` is unconfirmed and withheld, so
`planned_from` / `planned_to` stay `None` (`delivery_window` is pending). The
shared window helpers are in `parcels.py` for when it is confirmed; a date or
moment must then go through `planned_window` with the parcel's own zone.

SpeedX uses anonymous, direct HTTP consumer tracking; no credentials or browser
automation are used. A refresh requests the JSON current scan and then the
server-rendered history page. The SSR history is authoritative; a valid JSON
scan is only a current-state fallback. Empty responses and parsing failures are
retryable carrier failures, so cached parcel state stays visible.

Locations, postcodes, descriptions, supplemental event text, timestamps and
EDD are redacted from diagnostics. ETA, pickup point,
sender/receiver, weight and dimensions stay `None` until verified.

**Pre-1.0 WARNING obligations** (`parcels.py`'s `_warn_once`/`_warned`; `api.py`
keeps its own separate pair of the same idiom): an unmapped status/category
(`_warn_unmapped_status`), a SSR page that stops carrying a Next Flight
`self.__next_f.push(...)` script at all (`_warn_rsc_missing` in `api.py`,
distinct from zero events found), RSC present but no decodable `events` array
— a changed payload shape (`_warn_event_shape_changed` in `api.py`), an
event's `localTs`/`timeZone` that fails to resolve to an aware timestamp
(`_warn_timestamp_shape` in `parcels.py` — keys only, never values, and no UTC
is fabricated), the JSON current-scan status disagreeing with the SSR
history's latest event status (`_warn_status_disagreement` in `parcels.py` —
the SSR value is always kept), a populated `edd` field appearing at all
(`_warn_edd_present` in `parcels.py` — type/presence only, the value is
withheld since its semantics are unconfirmed), neither the JSON scan nor the
SSR page yielding usable data (`_warn_empty_data` in `api.py`), and an
HTTP 429 from either surface (`_warn_rate_limited` in `api.py` — the
`Retry-After` value only, never the tracking code). All fire once per HA
session and carry a copy-paste `issues/new?template=unrecognised_status.yml`
link.

## Running tests

```
python -m pytest tests/ --cov=custom_components.speedx
```

Coverage must stay **above 95%** (silver `test-coverage` rule). Run before
committing. A code change updates the README + this file + `docs/` in the same
commit.
