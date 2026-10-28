# `tests/` — the test suite

639 tests run with no network and no database. 705 run when `IRIS_DB_DSN` points at PostGIS;
the database tests skip cleanly without one, so a reviewer gets a green run either way.

| Path | What it covers |
|---|---|
| `fixtures/cassettes/` | Thirteen named scenarios — pagination, 429, 500, malformed JSON, empty page, duplicates across pages, missing publication date, the real `-0001-11-30` sentinel, out-of-domain coordinates, latitude/longitude in a projected field, zone-33 coordinates, soft-200 error page. |
| `fixtures/cassettes/corpus/` | The generated 761-paper corpus `make demo` replays. |
| `test_pipeline.py` | Includes `test_end_to_end_harvest_twice_is_idempotent` — the test the brief's re-run criterion turns on. |
| `test_sink.py` | Migrations, SRID enforcement, the dead-letter path, insert/update accounting. |
| `test_geo.py` | The validation gate, including real German border towns that must pass and foreign ones that must not. |
| `test_architecture.py` | Parses the import graph so the layering claim cannot silently regress. |

HTTP is mocked with `respx`. No test reaches the real network.
