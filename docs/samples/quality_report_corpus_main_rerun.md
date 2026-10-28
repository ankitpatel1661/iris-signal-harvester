# Harvest quality report — PASS

| | |
|---|---|
| Run id | `c16f6580-b8ca-42bf-9025-ce19f41ef1ec` |
| Source system | `fixture` |
| Started (UTC) | 2026-10-28T09:08:20.221007+00:00 |
| Duration | 1.75 s |
| Run status | `success` |
| Mode | load |
| Incremental since | (full sweep) |
| Record limit | (none) |
| Bodies swept | 17 |
| Taxonomy version | `2026.10.1` |
| Thresholds version | `2026.1` |
| Config hash | `eff15a5332f933ac` |

## 1. Stage funnel (subtractive)

Every fetched record leaves the pipeline at exactly one step, so the steps
sum back to the total. `below threshold` is **not** a rejection: the record
parsed correctly and is simply not an infrastructure signal.

| stage | records out | remaining | disposition |
|---|---:|---:|---|
| fetched from source | — | 780 | — |
| malformed by adapter | 3 | 777 | dead letter (signal_ingest_reject) |
| rejected by normaliser | 10 | 767 | dead letter (signal_ingest_reject) |
| below taxonomy threshold | 486 | 281 | not a signal — no row written |
| intra-run duplicates | 16 | 265 | collapsed — same record seen twice |
| staged for load | 0 | 265 | reached the table |
| load outcome | 0 | 265 | 0 inserted / 0 updated / 265 unchanged |

Loaded: **0 inserted**, **0 updated**, **265 unchanged** (in-batch duplicates collapsed by the sink: 0).

Funnel residual: **0** (balances).

Pages fetched: 48.

## 2. Re-run delta

| | inserted | updated | unchanged |
|---|---:|---:|---:|
| previous run | 265 | 0 | 0 |
| this run | 0 | 0 | 265 |

Second pass over identical input is **idempotent**.

## 3. Rejection histogram

One real example id per reason, so investigating a spike is one query.

| reason_code | n | example record | body | detail |
|---|---:|---|---|---|
| `missing_required_field` | 4 | `https://oparl.example.org/oparl/v1.0/paper/25452` | `https://oparl.example.org/oparl/v1.0/body/3` | no usable title: none of name, title, subject carries text in the payload |
| `geo_latlon_in_projected_field` | 3 | `https://oparl.example.org/oparl/v1.0/paper/61196` | `https://oparl.example.org/oparl/v1.0/body/7` | (x=13.7373, y=51.0504) looks like longitude/latitude (or a zero placeholder) written into a projected-coordinate field; a real EPSG:25832 easting is a six-digit |
| `geo_out_of_crs_domain` | 3 | `https://oparl.example.org/oparl/v1.0/paper/9907` | `https://oparl.example.org/oparl/v1.0/body/1` | (x=-45210.5, y=5300000.0) is outside the EPSG:25832 domain E(265000.0, 935000.0) N(5200000.0, 6120000.0) |
| `date_out_of_range` | 1 | `https://oparl.example.org/oparl/v1.0/paper/48896` | `https://oparl.example.org/oparl/v1.0/body/5` | year 1850 in '1850-03-01' is before min_plausible_year=1990 |
| `date_unparseable` | 1 | `https://oparl.example.org/oparl/v1.0/paper/91118` | `https://oparl.example.org/oparl/v1.0/body/11` | cannot parse '31.02.2026': day is out of range for month |
| `schema_validation_failed` | 1 | `(none)` | `https://oparl.example.org/oparl/v1.0/body/3` | adapter could not build a RawDocument from this record (schema_validation_failed) |

## 4. Field completeness vs thresholds

Null rate is measured over staged records. A structural field (limit 0.0)
breaching its limit is a FAIL; any other breach is a WARN.

| field | present | null | null rate | limit | verdict |
|---|---:|---:|---:|---:|---|
| `title` | 265 | 0 | 0.0% | 0.00 | ok |
| `source_url` | 265 | 0 | 0.0% | 0.00 | ok |
| `content_hash` | 265 | 0 | 0.0% | 0.00 | ok |
| `signal_type` | 265 | 0 | 0.0% | 0.00 | ok |
| `relevance_score` | 265 | 0 | 0.0% | 0.00 | ok |
| `published_at` | 223 | 42 | 15.8% | 0.65 | ok |
| `geom` | 242 | 23 | 8.7% | 0.95 | ok |
| `ags` | 250 | 15 | 5.7% | 0.30 | ok |
| `municipality_name` | 242 | 23 | 8.7% | 0.30 | ok |
| `summary` | 0 | 265 | 100.0% | 1.00 | ok |
| `document_type` | 258 | 7 | 2.6% | 0.25 | ok |
| `source_licence` | 122 | 143 | 54.0% | 1.00 | ok |

## 5. Date provenance

A high `unknown` share is expected on OParl: `Paper.date` is frequently null
in real municipal data, which is why the provenance column exists.

| published_at_origin | n | pct |
|---|---:|---:|
| `payload_field` | 223 | 84.2% |
| `unknown` | 42 | 15.8% |

## 6. Geo confidence and method

`unresolved` with `none` confidence is an honest answer, not a failure. The
verified live OParl endpoint carries no `location` on `Paper` at all, so
tiers 4 and 5 dominate there.

| geo_confidence | n | pct |
|---|---:|---:|
| `high` | 17 | 6.4% |
| `low` | 225 | 84.9% |
| `none` | 23 | 8.7% |

| geo_method | n | pct |
|---|---:|---:|
| `source_coordinates` | 17 | 6.4% |
| `municipality_centroid` | 225 | 84.9% |
| `unresolved` | 23 | 8.7% |

## 7. Signal mix

| signal_type | n | mean relevance |
|---|---:|---:|
| `land_use_plan` | 70 | 0.684 |
| `pv_open_space` | 39 | 0.891 |
| `transport_civils` | 34 | 0.765 |
| `grid_connection` | 33 | 0.795 |
| `digital_infra` | 29 | 0.824 |
| `heat_planning` | 25 | 0.832 |
| `wind_energy` | 20 | 0.870 |
| `storage` | 15 | 0.873 |

## 7b. Document types

11 of 258 staged records with a document type carried one that `config/document_types.yaml` does not map (4.3%); they are stored as `unmapped` and the raw value is kept in `raw_payload`.

| raw value | n |
|---|---:|
| `Einladung` | 4 |
| `Petition` | 2 |
| `Stellungnahme der Fachbehörde` | 2 |
| `Tagesordnung` | 2 |
| `Bürgerbegehren` | 1 |

## 8. Source health

| body | licence | ags | pages | fetched | emitted | below | rejected | stop reason | watermark | state |
|---|---|---|---:|---:|---:|---:|---:|---|---|---|
| München (synthetisch) (https://oparl.example.org/oparl/v1.0/body/1) | (undeclared) | 09162000 | 3 | 68 | 21 | 44 | 1 | `exhausted` | `unchanged` | **ok** |
| Berlin (synthetisch) (https://oparl.example.org/oparl/v1.0/body/2) | https://www.govdata.de/dl-de/by-2-0 | 11000000 | 3 | 68 | 24 | 42 | 0 | `exhausted` | `unchanged` | **ok** |
| Hamburg (synthetisch) (https://oparl.example.org/oparl/v1.0/body/3) | https://www.govdata.de/dl-de/by-2-0 | 02000000 | 3 | 60 | 17 | 41 | 2 | `exhausted` | `unchanged` | **ok** |
| Köln (synthetisch) (https://oparl.example.org/oparl/v1.0/body/4) | (undeclared) | 05315000 | 3 | 61 | 25 | 33 | 1 | `exhausted` | `unchanged` | **ok** |
| Frankfurt am Main (synthetisch) (https://oparl.example.org/oparl/v1.0/body/5) | https://creativecommons.org/licenses/by/4.0/ | 06412000 | 3 | 54 | 20 | 31 | 1 | `exhausted` | `unchanged` | **ok** |
| Stuttgart (synthetisch) (https://oparl.example.org/oparl/v1.0/body/6) | (undeclared) | 08111000 | 3 | 53 | 13 | 39 | 1 | `exhausted` | `unchanged` | **ok** |
| Dresden (synthetisch) (https://oparl.example.org/oparl/v1.0/body/7) | https://creativecommons.org/licenses/by/4.0/ | 14612000 | 3 | 54 | 18 | 32 | 2 | `exhausted` | `unchanged` | **ok** |
| Leipzig (synthetisch) (https://oparl.example.org/oparl/v1.0/body/8) | (undeclared) | 14713000 | 3 | 52 | 14 | 37 | 1 | `exhausted` | `unchanged` | **ok** |
| Düsseldorf (synthetisch) (https://oparl.example.org/oparl/v1.0/body/9) | https://www.govdata.de/dl-de/by-2-0 | 05111000 | 3 | 46 | 14 | 30 | 0 | `exhausted` | `unchanged` | **ok** |
| Hannover (synthetisch) (https://oparl.example.org/oparl/v1.0/body/10) | (undeclared) | 03241001 | 3 | 46 | 16 | 28 | 0 | `exhausted` | `unchanged` | **ok** |
| Nürnberg (synthetisch) (https://oparl.example.org/oparl/v1.0/body/11) | https://creativecommons.org/licenses/by/4.0/ | 09564000 | 3 | 45 | 15 | 27 | 3 | `exhausted` | `unchanged` | **ok** |
| Bremen (synthetisch) (https://oparl.example.org/oparl/v1.0/body/12) | (undeclared) | 04011000 | 3 | 39 | 15 | 22 | 0 | `exhausted` | `unchanged` | **ok** |
| Essen (synthetisch) (https://oparl.example.org/oparl/v1.0/body/13) | (undeclared) | 05113000 | 3 | 37 | 16 | 21 | 0 | `exhausted` | `unchanged` | **ok** |
| Potsdam (synthetisch) (https://oparl.example.org/oparl/v1.0/body/14) | https://www.govdata.de/dl-de/by-2-0 | 12054000 | 2 | 30 | 14 | 15 | 1 | `exhausted` | `unchanged` | **ok** |
| Mainz (synthetisch) (https://oparl.example.org/oparl/v1.0/body/15) | (undeclared) | 07315000 | 2 | 30 | 8 | 22 | 0 | `exhausted` | `unchanged` | **ok** |
| Bezirksausschuss Beispielviertel (synthetisch) (https://oparl.example.org/oparl/v1.0/body/16) | (undeclared) | — | 3 | 22 | 9 | 13 | 0 | `exhausted` | `unchanged` | **ok** |
| Ortsbeirat Nord (synthetisch) (https://oparl.example.org/oparl/v1.0/body/17) | (undeclared) | — | 2 | 15 | 6 | 9 | 0 | `exhausted` | `unchanged` | **ok** |

| licence | records |
|---|---:|
| (undeclared) | 143 |
| https://www.govdata.de/dl-de/by-2-0 | 69 |
| https://creativecommons.org/licenses/by/4.0/ | 53 |

Most frequent resolution notes (sampled):

- (seen 225x) municipality centroid only: not the location the document refers to
- (seen 15x) no AGS supplied: municipality centroid tier unavailable; unresolved: no tier produced a trustworthy location
- (seen 8x) AGS 07315000 not in gazetteer: municipality containment check and centroid tier unavailable; unresolved: no tier produced a trustworthy location

## 9. Gate

**Verdict: PASS** — exit code 0.

No threshold was breached and no body was degraded.

