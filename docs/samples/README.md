# Sample output, quality reports and the synthetic corpus

> **THE DATA BEHIND EVERY FILE IN THIS DIRECTORY IS SYNTHETIC.**
> The input corpus is **generated** by [`scripts/generate_corpus.py`](../../scripts/generate_corpus.py) from a
> fixed seed and is **reproducible**. It is **modelled on the structure** of real OParl responses that were
> verified on 2026-10-27/28 (see [`../evidence/`](../evidence/README.md)). It is **NOT real municipal data**:
> every paper title, id, reference number, date and coordinate is invented. Only the 15 AGS codes (real 8-digit
> municipality keys) are real, because the geo tier-4 lookup and the containment check need real keys to be
> exercised honestly. Municipality names in the extracts come from the gazetteer, not from any source. Do not
> read anything here as a statement about what a real council decided, published or planned.

These files close review finding C-3 (the brief asks for "sample anonymised output, validation queries, and a
concise error/quality report", and `reports/` is git-ignored). They are produced by actually running the pipeline
against a PostGIS 16 / 3.4 container, not written by hand.

## The corpus

| | |
|---|---|
| Bodies | 17 (15 declare a real AGS across 11 Bundesländer; 2 declare none, as 25 of 26 bodies do on the probed endpoint) |
| Distinct papers | 761, served as 780 items (16 duplicates across pages + 3 malformed items) on 48 paper pages (10-25 per page, 2-3 pages per body) |
| Seed | `20261028` (`--seed` overrides it; the same seed gives byte-identical cassettes) |
| Cassettes | `tests/fixtures/cassettes/corpus/corpus_main.json` and `corpus_degraded.json` (~0.7 MB each) |
| Format | the existing cassette format, replayed by `FixtureAdapter` through the real OParl adapter and HTTP client. Each file carries a `_meta` block saying it is synthetic. |

They are in a sub-directory on purpose: `FixtureAdapter.available_scenarios` globs only the top level, and a
test pins the 13 hand-written scenarios.

**Mix (generator intent, `corpus_labels.json`).** 266 papers (35%) are written to match a signal type; the rest is
ordinary council business (budgets, schools, playgrounds, committees: 421), plus deliberate edge cases: 26
"near misses" that are energy- or infrastructure-flavoured but use no taxonomy term, 17 whose only taxonomy term
sits in `keyword` (field weight 0.4, stays below 0.35), 25 with a negation term (`Ablehnung`, `Aufhebung`,
`Rückbau`, `Verzicht`), and 6 "lexical traps" (`Straßenausbaubeitragssatzung`, `Radwegebenutzungspflicht`) that a
compound-prefix matcher will wrongly accept.

**Imperfections injected into `corpus_main`:** 61 explicit `"date": null`, 46 with no `date` key at all, 23 dates
in German `TT.MM.JJJJ`, 6 `modified` values equal to the real sentinel `-0001-11-30T00:00:00+00:53`, 3 lat/lon
pairs in a projected field (one is `0, 0`), 3 finite coordinates outside the EPSG:25832 domain, 2 EPSG:25833
(zone 33) coordinates, 15 valid EPSG:25832 coordinates, 1 unparseable and 1 out-of-range date, 2 papers without
a title, 3 malformed items (a bare string, two objects without `id`), 16 papers repeated on the next page, and
bodies with `"license": null` (10 of 17; the rest carry a CC-BY-4.0 or dl-de/by-2-0 URL, invented for the test).
Titles use the vocabulary of `config/taxonomy.yaml` and German council phrasing (`Aufstellungsbeschluss`,
`Bebauungsplan Nr. NN`, `Teilfortschreibung Flächennutzungsplan`, `Beschlussvorlage`, `Niederschrift`, ...).

Where the corpus differs from what was observed live (so nobody mistakes it for a replica):

- On the probed endpoint 96 of the first 100 papers carry the sentinel in `modified` and `created`, and none carries
  `date` or `location`. Here the sentinel is rare and most papers are dated. A faithful replica would have been
  unusable for exercising the date and geo tiers; the live proportions are in `docs/evidence/`.
- 15 papers carry a valid `location` and 2 a zone-33 one. The probed endpoint serves none; other OParl servers do,
  and tier 1 has to be exercised.
- The gazetteer used for these runs, `corpus_gazetteer.json`, is `config/gazetteer.json` plus five more
  cities (Hannover, Nürnberg, Bremen, Essen, Potsdam) with crude 8 km 12-gon polygons. Mainz (`07315000`) is
  deliberately absent so the "AGS declared, not in the gazetteer" path shows up (its papers are `unresolved`).

## Files

| File | What it is |
|---|---|
| `signal_ingest_corpus_main.csv` | **The sample extract:** all 265 rows that `signal_ingest` received from the main run, 24 columns (no `raw_payload`, no run ids or timestamps), geometry as `x_epsg25832`/`y_epsg25832`. Regenerating it is byte-reproducible. |
| `signal_ingest_corpus_main_examples.json` | 15 of those rows with their untouched `raw_payload`: one per signal type plus a zone-33 row, a tier-1 row, an unknown-date row, an unresolved-geo row, a null-licence row, an `unmapped` and a null document type. |
| `quality_report_corpus_main.md` / `.json` | **The quality report** of the first load. Verdict **PASS**; 13 of 780 dead-lettered (1.7%). |
| `quality_report_corpus_main_rerun.md` / `.json` | The same corpus loaded a second time: `inserted=0 updated=0 unchanged=265` (idempotency), verdict PASS. |
| `signal_ingest_reject_corpus_main.csv` | The 13 dead-lettered records of that run, with reason code and detail. |
| `quality_report_corpus_degraded.md` / `.json` | **The report from the deliberately degraded run.** Verdict **FAIL**, non-empty rejection histogram of 10 reason codes (see below). This is the evidence for acceptance criterion 3. |
| `signal_ingest_corpus_degraded.csv`, `signal_ingest_reject_corpus_degraded.csv` | What that run staged (124 rows) and dead-lettered (131 records). |
| `run_output_corpus_main.txt`, `run_output_corpus_degraded.txt` | Console output of each harvest (funnel, verdict, exit code), the migrations applied, and the output of `iris-harvest validate` (`sql/validation_queries.sql`: 5 invariants, 0 violated, in both runs). |
| `corpus_gazetteer.json` | The gazetteer used (see above). |
| `corpus_labels.json` | The generator's intent per paper: category, intended signal type, injected imperfections. **Not a gold standard**: the same hand wrote titles and labels. |

### Acceptance criterion 3: errors visible, invalid coordinates not silently accepted

`corpus_degraded` is the same papers as `corpus_main` with faults injected into roughly half of the signal papers, one body
answering HTTP 500 and one page of truncated JSON. The report's rejection histogram is:

| reason_code | n |
|---|---:|
| `geo_out_of_crs_domain` | 37 |
| `geo_latlon_in_projected_field` | 34 |
| `date_unparseable` | 15 |
| `geo_reprojection_failed` (unsupported CRS `EPSG:31468`) | 13 |
| `geo_non_finite` (`NaN` easting) | 10 |
| `date_out_of_range` | 9 |
| `geo_outside_claimed_municipality` | 6 |
| `missing_required_field` | 4 |
| `geo_outside_national_envelope` | 2 |
| `schema_validation_failed` | 1 |

The verdict is `FAIL` (`reject_rate_max`: 131 of 707 fetched, 18.5% > 10%), the two failed bodies are named with
their stop reason and their watermarks were not advanced, `harvest` exits 1, and none of the 131 rejected records is
in the staged CSV. The validation queries still report 0 violations, which is the point: the bad coordinates never
reached the table.

### What the main run did with the generator's intent

This compares the matcher's decision with the labels. It checks that the corpus and the matcher agree; it is **not**
a precision or recall figure, and none is claimed (the labels are not independent).

| generator intent | staged | rejected | below threshold |
|---|---:|---:|---:|
| signal papers (266) | 256 | 10 (the injected geo/date/title faults) | 0 |
| ordinary (421), near miss (26), keyword only (17) | 0 | 0 | 464 |
| negation term (25) | 3 | 0 | 22 |
| lexical trap (6) | 6 | 0 | 0 |

Two honest observations. Three `Ablehnung des Antrags auf Errichtung eines Solarparks ...` titles were staged because the
negation is five tokens from the hit and the window is four (README, "negation window"). And the six lexical traps
are false positives by construction: the matcher cannot tell `Straßenausbaubeitragssatzung` from road construction.
Geo outcome of the 265 staged rows: 225 municipality centroid (`low`), 17 source coordinates (`high`, two of them
reprojected from EPSG:25833, `source_crs` keeps the original), 23 unresolved (the two bodies without AGS and Mainz).
Date outcome: 223 `payload_field`, 42 `unknown`.

## Regenerating

```bash
# 1. the cassettes, gazetteer and labels (deterministic; needs only the project venv)
.venv/bin/python scripts/generate_corpus.py

# 2. a PostGIS server, then every other file in this directory
docker run -d --rm --name iris-corpus-pg -e POSTGRES_PASSWORD=postgres -p 55437:5432 postgis/postgis:16-3.4
.venv/bin/python scripts/make_samples.py --admin-dsn postgresql://postgres:postgres@localhost:55437/postgres
docker rm -f iris-corpus-pg
```

`make_samples.py` recreates two databases (`iris_samples_main`, `iris_samples_degraded`; it drops them first, so
point it at a throwaway server), applies the migrations, runs `iris-harvest harvest --source fixture --scenario
corpus_main|corpus_degraded --gazetteer docs/samples/corpus_gazetteer.json` with `IRIS_FIXTURE_DIR` set to the corpus
directory, runs `iris-harvest validate`, and exports the extracts. The CSV and JSON extracts regenerate
byte-for-byte; the `.md`/`.json` reports differ in run id, timestamps, duration, the config hash and which example record is shown per reason code (bodies are swept concurrently).

To replay by hand:

```bash
IRIS_FIXTURE_DIR=tests/fixtures/cassettes/corpus IRIS_DB_DSN=postgresql://... \
  .venv/bin/iris-harvest harvest --source fixture --scenario corpus_main \
  --gazetteer docs/samples/corpus_gazetteer.json
```

Generated against migrations `001_initial` and `002_reject_dedup`.

**Snapshot note.** These files are a snapshot, not regenerated by CI. `run_output_*.txt` shows `iris-harvest
validate` checking 5 invariants (queries 1, 2, 3, 4, 10). `validate` has since been extended to run the
data-quality checks 11, 12 and 15 as well and to fail when an expected check is missing or the table is empty;
the sample outputs were not re-run after that change, so they do not show those checks.
The data is synthetic throughout (see the statement at the top): nothing here describes a real council.
