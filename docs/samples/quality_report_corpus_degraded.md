# Harvest quality report — FAIL

| | |
|---|---|
| Run id | `56f728c4-bd71-489f-abda-f92d3f1db931` |
| Source system | `fixture` |
| Started (UTC) | 2026-10-28T09:08:24.348597+00:00 |
| Duration | 1.71 s |
| Run status | `degraded` |
| Mode | load |
| Incremental since | (full sweep) |
| Record limit | (none) |
| Bodies swept | 17 |
| Taxonomy version | `2026.10.1` |
| Thresholds version | `2026.1` |
| Config hash | `8a8d71515f8e0946` |

## 1. Stage funnel (subtractive)

Every fetched record leaves the pipeline at exactly one step, so the steps
sum back to the total. `below threshold` is **not** a rejection: the record
parsed correctly and is simply not an infrastructure signal.

| stage | records out | remaining | disposition |
|---|---:|---:|---|
| fetched from source | — | 707 | — |
| malformed by adapter | 3 | 704 | dead letter (signal_ingest_reject) |
| rejected by normaliser | 128 | 576 | dead letter (signal_ingest_reject) |
| below taxonomy threshold | 438 | 138 | not a signal — no row written |
| intra-run duplicates | 14 | 124 | collapsed — same record seen twice |
| staged for load | 0 | 124 | reached the table |
| load outcome | 0 | 124 | 124 inserted / 0 updated / 0 unchanged |

Loaded: **124 inserted**, **0 updated**, **0 unchanged** (in-batch duplicates collapsed by the sink: 0).

Funnel residual: **0** (balances).

Pages fetched: 43.

## 2. Re-run delta

No previous run counts were supplied, so no delta is claimed.
Idempotency is asserted by running the same harvest twice and comparing
this section's `inserted` and `updated` against zero.

## 3. Rejection histogram

One real example id per reason, so investigating a spike is one query.

| reason_code | n | example record | body | detail |
|---|---:|---|---|---|
| `geo_out_of_crs_domain` | 37 | `https://oparl.example.org/oparl/v1.0/paper/40836` | `https://oparl.example.org/oparl/v1.0/body/4` | (x=-45210.5, y=6000000.0) is outside the EPSG:25832 domain E(265000.0, 935000.0) N(5200000.0, 6120000.0) |
| `geo_latlon_in_projected_field` | 34 | `https://oparl.example.org/oparl/v1.0/paper/43421` | `https://oparl.example.org/oparl/v1.0/body/4` | (x=6.9603, y=50.9375) looks like longitude/latitude (or a zero placeholder) written into a projected-coordinate field; a real EPSG:25832 easting is a six-digit  |
| `date_unparseable` | 15 | `https://oparl.example.org/oparl/v1.0/paper/35188` | `https://oparl.example.org/oparl/v1.0/body/4` | cannot parse '31.02.2026': day is out of range for month |
| `geo_reprojection_failed` | 13 | `https://oparl.example.org/oparl/v1.0/paper/36924` | `https://oparl.example.org/oparl/v1.0/body/4` | unsupported source CRS 'EPSG:31468' (supported: EPSG:25832, EPSG:25833) |
| `geo_non_finite` | 10 | `https://oparl.example.org/oparl/v1.0/paper/37483` | `https://oparl.example.org/oparl/v1.0/body/4` | coordinate is not finite: x=nan, y=5644856.0 |
| `date_out_of_range` | 9 | `https://oparl.example.org/oparl/v1.0/paper/14928` | `https://oparl.example.org/oparl/v1.0/body/1` | year 1899 in '1899-12-31' is before min_plausible_year=1990 |
| `geo_outside_claimed_municipality` | 6 | `https://oparl.example.org/oparl/v1.0/paper/35389` | `https://oparl.example.org/oparl/v1.0/body/4` | (x=556689.0, y=5554856.0) is outside the claimed municipality (tolerance 2000.0 m) |
| `missing_required_field` | 4 | `https://oparl.example.org/oparl/v1.0/paper/25452` | `https://oparl.example.org/oparl/v1.0/body/3` | no usable title: none of name, title, subject carries text in the payload |
| `geo_outside_national_envelope` | 2 | `https://oparl.example.org/oparl/v1.0/paper/7732` | `https://oparl.example.org/oparl/v1.0/body/1` | (x=891611.0, y=5244758.0) is outside the national envelope |
| `schema_validation_failed` | 1 | `(none)` | `https://oparl.example.org/oparl/v1.0/body/3` | adapter could not build a RawDocument from this record (schema_validation_failed) |

## 4. Field completeness vs thresholds

Null rate is measured over staged records. A structural field (limit 0.0)
breaching its limit is a FAIL; any other breach is a WARN.

| field | present | null | null rate | limit | verdict |
|---|---:|---:|---:|---:|---|
| `title` | 124 | 0 | 0.0% | 0.00 | ok |
| `source_url` | 124 | 0 | 0.0% | 0.00 | ok |
| `content_hash` | 124 | 0 | 0.0% | 0.00 | ok |
| `signal_type` | 124 | 0 | 0.0% | 0.00 | ok |
| `relevance_score` | 124 | 0 | 0.0% | 0.00 | ok |
| `published_at` | 100 | 24 | 19.4% | 0.65 | ok |
| `geom` | 101 | 23 | 18.5% | 0.95 | ok |
| `ags` | 109 | 15 | 12.1% | 0.30 | ok |
| `municipality_name` | 101 | 23 | 18.5% | 0.30 | ok |
| `summary` | 0 | 124 | 100.0% | 1.00 | ok |
| `document_type` | 120 | 4 | 3.2% | 0.25 | ok |
| `source_licence` | 52 | 72 | 58.1% | 1.00 | ok |

## 5. Date provenance

A high `unknown` share is expected on OParl: `Paper.date` is frequently null
in real municipal data, which is why the provenance column exists.

| published_at_origin | n | pct |
|---|---:|---:|
| `payload_field` | 100 | 80.6% |
| `unknown` | 24 | 19.4% |

## 6. Geo confidence and method

`unresolved` with `none` confidence is an honest answer, not a failure. The
verified live OParl endpoint carries no `location` on `Paper` at all, so
tiers 4 and 5 dominate there.

| geo_confidence | n | pct |
|---|---:|---:|
| `high` | 15 | 12.1% |
| `low` | 86 | 69.4% |
| `none` | 23 | 18.5% |

| geo_method | n | pct |
|---|---:|---:|
| `source_coordinates` | 15 | 12.1% |
| `municipality_centroid` | 86 | 69.4% |
| `unresolved` | 23 | 18.5% |

## 7. Signal mix

| signal_type | n | mean relevance |
|---|---:|---:|
| `land_use_plan` | 33 | 0.676 |
| `pv_open_space` | 21 | 0.905 |
| `transport_civils` | 19 | 0.771 |
| `grid_connection` | 14 | 0.789 |
| `heat_planning` | 11 | 0.859 |
| `digital_infra` | 9 | 0.789 |
| `wind_energy` | 9 | 0.878 |
| `storage` | 8 | 0.875 |

## 7b. Document types

5 of 120 staged records with a document type carried one that `config/document_types.yaml` does not map (4.2%); they are stored as `unmapped` and the raw value is kept in `raw_payload`.

| raw value | n |
|---|---:|
| `Bürgerbegehren` | 1 |
| `Einladung` | 1 |
| `Petition` | 1 |
| `Stellungnahme der Fachbehörde` | 1 |
| `Tagesordnung` | 1 |

## 8. Source health

| body | licence | ags | pages | fetched | emitted | below | rejected | stop reason | watermark | state |
|---|---|---|---:|---:|---:|---:|---:|---|---|---|
| München (synthetisch) (https://oparl.example.org/oparl/v1.0/body/1) | (undeclared) | 09162000 | 3 | 68 | 2 | 44 | 20 | `exhausted` | `advanced` | **ok** |
| Berlin (synthetisch) (https://oparl.example.org/oparl/v1.0/body/2) | https://www.govdata.de/dl-de/by-2-0 | 11000000 | 3 | 68 | 11 | 42 | 13 | `exhausted` | `advanced` | **ok** |
| Hamburg (synthetisch) (https://oparl.example.org/oparl/v1.0/body/3) | https://www.govdata.de/dl-de/by-2-0 | 02000000 | 3 | 60 | 8 | 41 | 11 | `exhausted` | `advanced` | **ok** |
| Köln (synthetisch) (https://oparl.example.org/oparl/v1.0/body/4) | (undeclared) | 05315000 | 3 | 61 | 14 | 33 | 12 | `exhausted` | `advanced` | **ok** |
| Frankfurt am Main (synthetisch) (https://oparl.example.org/oparl/v1.0/body/5) | https://creativecommons.org/licenses/by/4.0/ | 06412000 | 3 | 54 | 8 | 31 | 13 | `exhausted` | `advanced` | **ok** |
| Stuttgart (synthetisch) (https://oparl.example.org/oparl/v1.0/body/6) | (undeclared) | 08111000 | 1 | 26 | 2 | 21 | 3 | `source_unavailable` | `skipped_unclean_sweep` | **degraded** |
| Dresden (synthetisch) (https://oparl.example.org/oparl/v1.0/body/7) | https://creativecommons.org/licenses/by/4.0/ | 14612000 | 3 | 54 | 14 | 32 | 6 | `exhausted` | `advanced` | **ok** |
| Leipzig (synthetisch) (https://oparl.example.org/oparl/v1.0/body/8) | (undeclared) | 14713000 | 3 | 52 | 10 | 37 | 5 | `exhausted` | `advanced` | **ok** |
| Düsseldorf (synthetisch) (https://oparl.example.org/oparl/v1.0/body/9) | https://www.govdata.de/dl-de/by-2-0 | 05111000 | 0 | 0 | 0 | 0 | 0 | `source_unavailable` | `skipped_unclean_sweep` | **degraded** |
| Hannover (synthetisch) (https://oparl.example.org/oparl/v1.0/body/10) | (undeclared) | 03241001 | 3 | 46 | 7 | 28 | 9 | `exhausted` | `advanced` | **ok** |
| Nürnberg (synthetisch) (https://oparl.example.org/oparl/v1.0/body/11) | https://creativecommons.org/licenses/by/4.0/ | 09564000 | 3 | 45 | 6 | 27 | 12 | `exhausted` | `advanced` | **ok** |
| Bremen (synthetisch) (https://oparl.example.org/oparl/v1.0/body/12) | (undeclared) | 04011000 | 3 | 39 | 7 | 22 | 8 | `exhausted` | `advanced` | **ok** |
| Essen (synthetisch) (https://oparl.example.org/oparl/v1.0/body/13) | (undeclared) | 05113000 | 3 | 37 | 7 | 21 | 9 | `exhausted` | `advanced` | **ok** |
| Potsdam (synthetisch) (https://oparl.example.org/oparl/v1.0/body/14) | https://www.govdata.de/dl-de/by-2-0 | 12054000 | 2 | 30 | 5 | 15 | 10 | `exhausted` | `advanced` | **ok** |
| Mainz (synthetisch) (https://oparl.example.org/oparl/v1.0/body/15) | (undeclared) | 07315000 | 2 | 30 | 8 | 22 | 0 | `exhausted` | `advanced` | **ok** |
| Bezirksausschuss Beispielviertel (synthetisch) (https://oparl.example.org/oparl/v1.0/body/16) | (undeclared) | — | 3 | 22 | 9 | 13 | 0 | `exhausted` | `advanced` | **ok** |
| Ortsbeirat Nord (synthetisch) (https://oparl.example.org/oparl/v1.0/body/17) | (undeclared) | — | 2 | 15 | 6 | 9 | 0 | `exhausted` | `advanced` | **ok** |

| licence | records |
|---|---:|
| (undeclared) | 72 |
| https://creativecommons.org/licenses/by/4.0/ | 28 |
| https://www.govdata.de/dl-de/by-2-0 | 24 |

Most frequent resolution notes (sampled):

- (seen 86x) municipality centroid only: not the location the document refers to
- (seen 15x) no AGS supplied: municipality centroid tier unavailable; unresolved: no tier produced a trustworthy location
- (seen 8x) AGS 07315000 not in gazetteer: municipality containment check and centroid tier unavailable; unresolved: no tier produced a trustworthy location

## 9. Gate

**Verdict: FAIL** — exit code 1.

| level | rule | detail | bodies responsible |
|---|---|---|---|
| **FAIL** | `reject_rate_max` | 131 of 707 fetched records were dead-lettered (18.5% > 10.0%) | `Potsdam (synthetisch) (https://oparl.example.org/oparl/v1.0/body/14)`, `München (synthetisch) (https://oparl.example.org/oparl/v1.0/body/1)`, `Nürnberg (synthetisch) (https://oparl.example.org/oparl/v1.0/body/11)`, `Essen (synthetisch) (https://oparl.example.org/oparl/v1.0/body/13)`, `Frankfurt am Main (synthetisch) (https://oparl.example.org/oparl/v1.0/body/5)` |
| **WARN** | `degraded_body_rate_max` | 2 body/bodies degraded; their watermarks were not advanced, so the window will be retried rather than skipped | `Stuttgart (synthetisch) (https://oparl.example.org/oparl/v1.0/body/6)`, `Düsseldorf (synthetisch) (https://oparl.example.org/oparl/v1.0/body/9)` |
| **WARN** | `body_error` | source_unavailable: HTTP 200 from https://oparl.example.org/oparl/v1.0/body/6/list/paper?id=55716 with unparseable JSON body: Unterminated string starting at: line 1 column 8604 (char 8603) | `Stuttgart (synthetisch) (https://oparl.example.org/oparl/v1.0/body/6)` |
| **WARN** | `body_error` | source_unavailable: HTTP 500 from https://oparl.example.org/oparl/v1.0/body/9/list/paper | `Düsseldorf (synthetisch) (https://oparl.example.org/oparl/v1.0/body/9)` |

