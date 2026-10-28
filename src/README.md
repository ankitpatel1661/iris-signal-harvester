# `src/` — the harvester

The Python package. Layered so that the domain core has no outward dependencies; this is
enforced by `tests/test_architecture.py`, which parses the import graph and fails if anything
under `domain/` imports `adapters`, `http`, `sink`, `reporting` or a third-party I/O library.

| Path | Responsibility |
|---|---|
| `adapters/` | One per source. `base.py` is the port: `fetch_page` is the only method that touches the network, which is what lets the whole pipeline run offline from recorded cassettes. `oparl.py` is the live source, `ratsdokumente.py` the briefed contract (its endpoint does not exist — see `SOURCE_VERIFICATION.md`), `fixture.py` replays cassettes. |
| `domain/` | Pure logic: `taxonomy.py` (German-aware matching), `dates.py` (the publication-date provenance ladder), `geo/` (tiered resolution to EPSG:25832 and the validation gate), `hashing.py` (change detection), `normalise.py`, `models.py`. |
| `http/` | Transport: rate limiting, retry policy, and the retryable-vs-terminal distinction. A 401/403 is terminal and is never retried. |
| `sink/` | PostGIS load. The idempotency guarantee lives in `postgis.py`. |
| `reporting/` | The quality report, the validation runner, and the source-verification probe. |
| `pipeline.py`, `cli.py` | The sweep orchestrator and the command-line entry points. |
