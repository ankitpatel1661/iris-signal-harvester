# IRIS-SIG-23 — Implementation Plan

Spec authority: `deliverables/IRIS-SIG-23_Solution_Design_and_Implementation_Plan.pdf` (v1.1).
This file is the executable argument from that spec. Where the two disagree, the spec wins.

> **Status note (2026-10-28).** This is the original plan. Later review changed some behaviour, and
> where it differs from `README.md`, the README is authoritative. Statements corrected in this file
> are marked **Corrected**. Facts about the live OParl endpoint are now backed by the dated capture in
> `docs/evidence/`.

## Context

A federated harvester for German municipal council documents (*Ratsinformationssysteme*),
extracting infrastructure-trigger signals into `iris_staging.signal_ingest` on
PostgreSQL 16 / PostGIS 3.4.

**Verified source status (2026-10-27):** the briefed endpoint `https://ratsdokumente.de/api/search`
does not exist — NXDOMAIN, and DENIC reports the domain `Status: free`. **OParl** is the
justified alternative and the primary live source. A real OParl endpoint was probed and
confirmed: `https://www.muenchen-transparent.de/oparl/v1.0` (26 bodies, OParl **1.0**).

Findings from that live probe that bind the implementation (**Corrected** 2026-10-28 against the
committed capture in `docs/evidence/`; the first probe kept no raw response, so nothing from it that
could not be reproduced is stated here):

- Pagination is `links.next` plus `pagination: {elementsPerPage, totalPages}` → `object_link`.
- `modified_since` **is** honoured and narrows the result set. Observed 2026-10-28 on
  `https://www.muenchen-transparent.de/oparl/v1.0/body/1/list/paper`, first page:

  | `modified_since` | items on page 1 | `totalPages` |
  |---|---|---|
  | (none) | 100 | 15 |
  | 2020-01-01 | 100 | 8 |
  | 2026-01-01 | 80 | 1 |
  | 2026-06-01 | 45 | 1 |

  The committed JSON capture holds the unfiltered row and two other cut-offs (`2016-01-01` → 9 pages,
  `2025-01-01` → 3 pages); the other three rows are not in it (see `README.md`, "Data source").
- `Paper.date` is an **absent key** (0 of 100 papers on that page), not `null` — the date-provenance
  ladder is load-bearing, not a nicety.
- `Paper.modified` and `Paper.created` carry a corrupt sentinel, `-0001-11-30T00:00:00+00:53`, on
  **96 of 100** papers on that page — it is the norm there, not an edge case. It must never poison a
  watermark. (It is captured verbatim and ignored by the watermark parser; the record is not rejected
  for it.)
- `Body.license` is an **absent key** on all 26 bodies → `source_licence` must support "undeclared".
- `Paper` carries **no** `location` → geo tiers 4–5 will dominate.
- Only **1 of 26** bodies declares an `ags`, and that body's advertised paper URL answered HTTP 404 when
  requested once on 2026-10-28.
- The endpoint serves OParl **1.0**; adapters must accept 1.0 and 1.1 type URLs.

## Global Constraints

These bind every task. A reviewer must treat a violation as a defect.

1. **Never silently guess.** Any uncertain fact is represented as a field, not a default.
   An unverifiable date is `NULL` + `published_at_origin='unknown'`. An unresolvable
   location is `NULL` + `geo_method='unresolved'` + `geo_confidence='none'`.
2. **Never clamp, snap or round a failing coordinate.** Validation failure → `RecordRejected`
   with a `RejectCode`. Never a fallback point.
3. **Idempotency is a tested property.** Second run over identical input must produce
   0 inserts, 0 updates and no `updated_at` movement.
4. **No credentials in source.** Secrets come from the environment only. No default DSN.
5. **No access-restriction bypass.** 401/403/robots-disallow raises `AccessRestricted`,
   which is terminal. No retry, no header manipulation, no user-agent spoofing.
6. **The contract is `CanonicalSignal`.** Adapters produce it; the sink consumes it.
   Do not add fields to it without updating `migrations/` and the contract test.
7. **Tests run offline.** No test may require network access. HTTP is mocked with `respx`;
   database tests are marked `integration` and skip cleanly when no DSN is present.
8. **Python 3.11+**, `ruff` clean, `mypy --strict` clean on `src/`.
9. Reject reasons are `RejectCode` enum members, never free text.

## Existing foundation (already written — do not modify without a stated reason)

| File | Contract it fixes |
|---|---|
| `src/iris_signal_harvester/domain/models.py` | `CanonicalSignal`, `RawDocument`, `Body`, `Hit`, `MatchResult`, `GeoResolution`, `RejectedRecord`, and the `PublishedAtOrigin` / `GeoMethod` / `GeoConfidence` / `RejectCode` enums |
| `src/iris_signal_harvester/adapters/base.py` | `SourceAdapter` protocol, `Cursor`, `Capabilities`, `Page` |
| `src/iris_signal_harvester/errors.py` | `SourceUnavailable` (retryable) vs `AccessRestricted` (terminal), `RecordRejected`, `MalformedRecord`, `ConfigurationError` |
| `src/iris_signal_harvester/config.py` | `Settings`, `get_settings()`, `config_hash()` |

---

## Task 1 — Taxonomy matcher

**Files:** `src/iris_signal_harvester/domain/taxonomy.py`, `config/taxonomy.yaml`,
`tests/test_taxonomy.py`

Implement German-aware keyword matching producing explainable `MatchResult`.

- `fold(text)`: NFKC normalise, casefold, umlaut-fold (`ä→ae`, `ö→oe`, `ü→ue`, `ß→ss`).
  Applied symmetrically to corpus and taxonomy.
- `compile_term(term, compound)`: escape, treat `-` as `[-\s]?` (municipal prose writes
  `Freiflächen-PV`, `Freiflächen PV`, `FreiflächenPV`). When `compound=True`, allow a
  **bounded** suffix `[a-z]{0,20}` so `Solarpark` is found inside `Solarparkerweiterung`.
  This is a documented heuristic, not morphology — add a module docstring saying so.
- Negative context: a configurable token window around a hit suppresses it
  (`Ablehnung`, `Rückbau`, `Aufhebung`, `kein`, `Rücknahme`).
- Scoring: `score = min(max(hit.weight * field_weight) + breadth_bonus * (distinct_types - 1), cap)`.
- `Taxonomy.load(path)` reads YAML with a `version` string, surfaced as `taxonomy_version`.

`config/taxonomy.yaml` must define these 8 signal types with representative German terms:
`land_use_plan`, `wind_energy`, `pv_open_space`, `grid_connection`, `storage`,
`heat_planning`, `digital_infra`, `transport_civils`.
Field weights: `title: 1.0, summary: 0.6, body: 0.4`. `breadth_bonus: 0.05`, `cap: 1.0`,
`emit_min_score: 0.35`.

**Tests must include:** `Solarpark` found inside `Solarparkerweiterung`;
`Freiflächen-PV` found inside `Freiflächenphotovoltaikanlage`; umlaut folding both
directions; `Ablehnung des Solarparks` suppressed; title outranks summary; breadth bonus
arithmetic; a document matching nothing returns `MatchResult.empty()`; scores are
deterministic across runs.

## Task 2 — Dates and hashing

**Files:** `src/iris_signal_harvester/domain/dates.py`,
`src/iris_signal_harvester/domain/hashing.py`, `tests/test_dates.py`, `tests/test_hashing.py`

**Dates.** `resolve_published_at(payload_date, http_last_modified, meeting_date, settings)`
returns `(datetime | None, PublishedAtOrigin)` using the ladder: payload field →
HTTP header → meeting date → unknown. Parse German `TT.MM.JJJJ` and ISO-8601 variants.
Naive datetimes are localised with `settings.source_timezone`, then converted to UTC.

A date whose year is `< settings.min_plausible_year` or more than `settings.max_future_days`
in the future is **malformed**: raise `RecordRejected(RejectCode.DATE_OUT_OF_RANGE, ...)`.
A value that cannot be parsed at all raises `RejectCode.DATE_UNPARSEABLE`.
Provide `safe_parse_for_watermark(value)` returning `datetime | None` that swallows those
errors — a corrupt `modified` must never poison a watermark.

**Must be tested against the real sentinel** `-0001-11-30T00:00:00+00:53`.

**Hashing.** `content_hash(signal_fields)` = sha256 over canonical JSON (sorted keys, no
insignificant whitespace, NFKC-normalised strings, explicit nulls) of the *semantic*
projection only. The projection **excludes** volatile fields: `modified`, server
timestamps, request ids, `harvest_run_id`, `first_seen_at`, `last_seen_at`, `updated_at`.
Expose `SEMANTIC_FIELDS` and `HASH_VERSION = 1`.

**Tests must include:** key reordering produces the same hash; whitespace-only change in a
volatile field produces the same hash; a title change produces a different hash; the hash
is stable across processes (assert a literal expected digest for a fixed input).

## Task 3 — Geo resolution and validation

**Files:** `src/iris_signal_harvester/domain/geo/validate.py`,
`src/iris_signal_harvester/domain/geo/resolver.py`,
`src/iris_signal_harvester/domain/geo/gazetteer.py`, `tests/test_geo.py`

**`validate.py`** — `validate_25832(x, y, *, country, municipality, tol_m) -> tuple[float, float]`
running four checks in order, each raising `RecordRejected` with its own `RejectCode`:
1. finite → `GEO_NON_FINITE`
2. `abs(x) <= 180 and abs(y) <= 90` → `GEO_LATLON_IN_PROJECTED_FIELD` (also catches `(0,0)`)
3. outside `E_RANGE = (265_000.0, 935_000.0)` or `N_RANGE = (5_200_000.0, 6_120_000.0)`
   → `GEO_OUT_OF_CRS_DOMAIN`
4. outside the national envelope → `GEO_OUTSIDE_NATIONAL_ENVELOPE`;
   outside the claimed municipality (buffered by `tol_m`) → `GEO_OUTSIDE_CLAIMED_MUNICIPALITY`

Never clamp, never snap, never substitute a fallback point.

**`resolver.py`** — tiered chain returning `GeoResolution`:
tier 1 `source_coordinates` (high) → tier 2 `cadastre_parcel` (high; **not implementable
without an ALKIS/WFS endpoint — detect the parcel reference, skip with an explanatory
`note`, and do not pretend to resolve it**) → tier 3 `address_geocode` (medium; behind a
`Geocoder` Protocol so tests inject a stub and no network is needed) → tier 4
`municipality_centroid` via AGS (low) → tier 5 `unresolved` (none).
Reprojection uses `pyproj`; EPSG:25833 input is reprojected to 25832 and the original CRS
is retained in `GeoResolution.source_crs`.

**`gazetteer.py`** — `Gazetteer` Protocol mapping AGS → municipality polygon/point plus
name/kreis/bundesland, with a small `StaticGazetteer` loaded from a JSON file for the demo
(include a handful of real AGS codes, e.g. `09162000` München).

**Tests must include:** a lat/lon pair in a projected field is rejected; `(0,0)` rejected;
out-of-domain easting rejected; a valid Munich-area point passes; tier skipping records a
`note`; an unresolved result has `confidence=none` and `method=unresolved`; zone-33 input
is reprojected and `source_crs` retained.

## Task 4 — HTTP core and resilience

**Files:** `src/iris_signal_harvester/http/client.py`,
`src/iris_signal_harvester/http/ratelimit.py`,
`src/iris_signal_harvester/http/resilience.py`, `tests/test_http.py`

- `client.py`: `HarvesterClient` wrapping `httpx.AsyncClient` — descriptive UA from
  settings, timeouts, conditional GET (`ETag` / `If-Modified-Since`, 304 → `not_modified`),
  robots.txt fetch-and-cache per host with `Crawl-delay` honoured when `respect_robots`.
- `ratelimit.py`: per-host async token bucket at `rate_limit_rps`; shrinks for the rest of
  the run when a 429 is seen.
- `resilience.py`: `with_resilience(...)` using `tenacity` — retry on `SourceUnavailable`
  with exponential backoff + full jitter, bounded by `max_retries`, honouring
  `retry_after` exactly. **Never retries `AccessRestricted`.** Plus a per-body
  `CircuitBreaker` that opens after N consecutive failures.
- Status mapping: 401/403 → `AccessRestricted`; 429/5xx/timeouts → `SourceUnavailable`;
  a 200 whose content-type is not JSON → `SourceUnavailable` (soft-200 detection).

**Tests (respx, offline):** 429 then success honours `Retry-After`; 500 then success;
403 raises immediately with zero retries; soft-200 HTML is treated as failure not as an
empty page; 304 yields `not_modified`; token bucket spaces requests; breaker opens.

## Task 5 — Adapters: OParl, fixture, ratsdokumente

**Files:** `src/iris_signal_harvester/adapters/oparl.py`,
`src/iris_signal_harvester/adapters/fixture.py`,
`src/iris_signal_harvester/adapters/ratsdokumente.py`,
`tests/fixtures/cassettes/*.json`, `tests/test_adapters.py`

All three satisfy `SourceAdapter` and emit `RawDocument`.

- **`oparl.py`** — traverse System → `body` list → per-body `paper` list. Accept both
  `schema.oparl.org/1.0/` and `/1.1/` type URLs. Pagination via `links.next`
  (`object_link`). `modified_since` passed as a query parameter when supported. Map
  `id` → `source_record_id` and `source_url` (**Corrected:** the original plan keyed on `reference`,
  which is an editable file number; `reference` is now only a stored attribute), `name` → title,
  `paperType` → document_type (**Corrected:** mapped through `config/document_types.yaml`, unknown →
  `unmapped`),
  `body.license` → licence (may be `None`). Capture `date` and `modified` **raw**; do not
  parse here — Task 2 owns dates.
- **`fixture.py`** — replay committed JSON cassettes from `settings.fixture_dir`,
  returning the same `Page` objects. This is the demo path and must need no network.
- **`ratsdokumente.py`** — implement the briefed Pattern C REST contract
  (`/api/search`, offset/limit pagination). Its module docstring must state that the
  domain was verified non-existent on 2026-10-27 and that it is retained, fixture-tested,
  so it can be enabled unchanged if such a source is stood up.

**Cassettes required** (anonymised: example hosts, placeholder municipality names, no real
personal names): `happy_path`, `multi_page`, `rate_limited_429`, `server_error_500`,
`malformed_json`, `empty_page`, `duplicate_across_pages`, `missing_publication_date`,
`sentinel_modified_date`, `coordinate_out_of_domain`, `latlon_in_projected_field`,
`zone33_coordinates`, `soft_200_error_page`.

**Tests:** a contract test asserting each adapter is a `SourceAdapter` and every emitted
record validates as `RawDocument`; OParl pagination across pages; 1.0 and 1.1 type URLs
both accepted; fixture adapter is deterministic.

## Task 6 — PostGIS sink, migrations, watermark

**Files:** `src/iris_signal_harvester/sink/postgis.py`,
`src/iris_signal_harvester/sink/reject.py`, `src/iris_signal_harvester/sink/watermark.py`,
`migrations/001_initial.sql`, `tests/test_sink.py`

DDL per the spec: `iris_staging` schema; `signal_ingest` with
`geometry(Point, 25832)`, `UNIQUE (source_system, source_body_id, source_record_id)`, the
`ck_geo_consistency` CHECK, and GIST/GIN/btree indexes; plus `signal_ingest_reject`,
`harvest_run`, `harvest_watermark`. Use plain SQL migrations applied by a small runner —
no Alembic, to keep the dependency surface small.

`PostgisSink.load(signals)`:
1. `COPY` into an unlogged temp table
2. one `INSERT … ON CONFLICT (source_system, source_body_id, source_record_id) DO UPDATE … WHERE
   t.content_hash IS DISTINCT FROM EXCLUDED.content_hash RETURNING (xmax = 0) AS was_insert`
3. a separate cheap bulk `UPDATE` advancing `last_seen_at` for staged ids
4. return `LoadCounts(inserted, updated, unchanged)` derived from `RETURNING`

`watermark.py`: read/write `harvest_watermark`; advance **only on a clean body sweep**;
ignore unparseable `modified` values when computing the maximum.

**Tests marked `integration`**, skipped cleanly without `IRIS_DB_DSN`. Must include the
**idempotency test**: load a fixture set twice → second load reports
`inserted=0, updated=0`, row count unchanged, `updated_at` unchanged, `last_seen_at`
advanced. Plus: a changed field produces exactly one update; a rejected record lands in
`signal_ingest_reject`; SRID is enforced.

## Task 7 — Orchestrator, normaliser, CLI, reporting

**Files:** `src/iris_signal_harvester/pipeline.py`,
`src/iris_signal_harvester/domain/normalise.py`,
`src/iris_signal_harvester/reporting/quality.py`,
`src/iris_signal_harvester/reporting/verification.py`,
`src/iris_signal_harvester/cli.py`, `config/thresholds.yaml`, `config/bodies.yaml`,
`tests/test_pipeline.py`

- `normalise.py`: `RawDocument` → `CanonicalSignal`, calling dates, taxonomy, geo and
  hashing. Rejections become `RejectedRecord`, never exceptions that kill the sweep.
- `pipeline.py`: the `sweep()` orchestrator with the three pagination guard rails
  (cursor-loop detection via a seen-cursor set, page-id fingerprinting, zero-yield stop),
  bounded concurrency across bodies, serial within a body, per-body circuit breaker,
  and an in-run seen-id set collapsing intra-run duplicates.
- `quality.py`: build the report described in the spec — stage funnel (requested →
  fetched → parsed → matched → geo-resolved → loaded, with inserted/updated/unchanged/
  rejected), rejection histogram with one example id per reason, field completeness vs
  thresholds, date-provenance and geo-confidence distributions, signal mix, source health,
  re-run delta, and a PASS/WARN/FAIL verdict driving the exit code. Emit `.md` and `.json`.
- `verification.py`: generate `SOURCE_VERIFICATION.md` from a read-only probe
  (DNS, WHOIS where available, robots.txt, HTTP status, JSON shape). **Corrected:** `make verify` writes
  to the git-ignored `reports/` directory, not over the committed file; see `README.md`.
- `cli.py` (typer): `verify-source`, `discover`, `harvest` (`--since --limit --dry-run
  --strict`), `report`, `validate`.

**Tests:** guard rails each terminate a pathological sweep; intra-run duplicates collapse;
a degraded cassette drives a FAIL verdict and a non-zero exit; `--dry-run` writes a report
without touching the database.

## Task 8 — Packaging, demo, CI, docs

**Files:** `Makefile`, `docker-compose.yml`, `Dockerfile`, `.env.example`,
`.github/workflows/ci.yml`, `README.md`, `sql/validation_queries.sql`

- `docker-compose.yml`: `postgis/postgis:16-3.4` plus the harvester; migrations applied on
  start.
- `Makefile`: `up`, `verify`, `demo`, `test`, `harvest`, `report`, `lint`, `typecheck`.
  **`make demo` must run the harvest twice over fixtures and print the re-run delta
  showing 0 inserts and 0 updates on the second pass**, with no network access.
- `sql/validation_queries.sql`: the 8 queries from the spec's Appendix D.
- `.github/workflows/ci.yml`: ruff, mypy, pytest (unit offline + integration against a
  PostGIS service container), and a `gitleaks` secrets scan.
- `README.md` with exactly four top-level sections, in the brief's own words:
  **Data source**, **Mapping assumptions**, **How to run**, **Known limitations**.
  The data-source section must state the 2026-10-27 verification finding and link
  `SOURCE_VERIFICATION.md`. Known limitations must be candid — uneven OParl coverage,
  rural geocoding falling to municipality centroid, the compound matcher being a
  heuristic, PDF attachment text out of scope, cross-source duplicates not detected
  and not merged (**Corrected:** the original wording said "reported"; nothing reports them), and no recall/precision figures claimed because no labelled eval set exists.
