# IRIS signal harvester - dashboard

Streamlit window onto the harvest output. It has two data sources behind one interface
(`ui/data/service.py`), chosen automatically:

| Mode | When | Data | Banner |
|---|---|---|---|
| **Database** | `IRIS_DB_DSN` is set **and** the database is reachable | live `iris_staging`, read-only | none |
| **Sample** | `IRIS_DB_DSN` unset, or set but unreachable | the committed runs in `docs/samples/` | persistent, not dismissible |

No environment variable is required. With none set the app starts in sample mode and makes no
network call and no database connection.

## Run mode 1 - database (read-only)

It only runs `SELECT`; the session is also opened `default_transaction_read_only=on`, so a
write would be refused by the server too. This path is unchanged.

```bash
.venv/bin/python -m pip install -r ui/requirements.txt
export IRIS_DB_DSN=postgresql://iris:iris_local@127.0.0.1:5433/iris   # same variable as the harvester
make dashboard            # or: .venv/bin/python -m streamlit run ui/app.py
```

Optional environment: `IRIS_DB_SCHEMA` (default `iris_staging`), `IRIS_REPORTS_DIR`
(default `./reports`, where the PASS/WARN/FAIL verdict is read from).

If `IRIS_DB_DSN` is set but the database cannot be reached, the app falls back to the sample
data and the banner says why (the DSN itself is never shown). If the database is reachable but
the schema is missing, you get the usual explanatory error state, not a silent fallback.

## Run mode 2 - sample data (no database, no Docker)

```bash
.venv/bin/python -m pip install -r ui/requirements.txt     # or: -r requirements.txt
unset IRIS_DB_DSN
.venv/bin/python -m streamlit run streamlit_app.py
```

(`make dashboard` insists on `IRIS_DB_DSN`; use the command above for the demo.)

The sample source (`ui/data/samples.py`) reads `docs/samples/`: the staged-row CSVs, the reject
CSVs and the `quality_report_*.json` files of a real pipeline run over the **generated**
761-paper synthetic corpus. The sidebar has a **Sample run** selector:

- `corpus_main, first load` - verdict PASS, 265 staged rows, 13 dead-lettered.
- `corpus_main, re-run` - same corpus loaded again: nothing inserted or updated, so the re-run
  delta panel shows the idempotency.
- `corpus_degraded` - deliberately faulty input: verdict **FAIL**, 124 staged rows, 131
  dead-lettered across 10 reason codes. The rejection histogram and the FAIL verdict only exist
  in this run.

**Honesty in sample mode.** A banner at the top of every render (also in the empty and error
states) says: this is a GENERATED synthetic corpus, not real municipal data; it is a snapshot of
a committed run; no live database is connected. The header and sidebar repeat it. What the
extracts do not contain is shown as unknown, never invented: `first_seen_at` / `last_seen_at`
are not exported (shown "unknown"), `id` is the row position in the CSV, and the reject table
shows `seen_count` because the extracts have no `rejected_at`. NULL stays "unknown", rows with
no geometry are counted and never placed, and opacity/size still follow `geo_confidence`.
WGS84 coordinates are computed from the exported EPSG:25832 columns (inverse UTM, within
1e-10 degrees of PROJ).

## Explaining the domain in place

The page assumes no knowledge of German municipal open data. Every term carries a hover
tooltip (the (i) next to a KPI, filter, chart heading or table column), and a collapsed
**What the terms mean** expander near the top collects them as tables (AGS, signal, dead letter,
quality gate, EPSG:25832, the date-origin and geometry-method vocabularies, the eight signal
types with their German terms, the funnel stages and the rejection reason codes).

All of that wording lives in one module, `ui/help_text.py`, so the tooltips, chart hovers and
the glossary cannot drift apart. It only says what a term *means*; it never states or changes
a number. Category lists run best to worst, top to bottom, everywhere (summary, charts,
cross-table); the funnel is numbered and reads top to bottom, with a table beside it. The
re-run delta compares two runs in one database: in sample mode pick `corpus_main, re-run`.

## Deploy on Streamlit Community Cloud

1. Push the repository (including `docs/samples/`) to GitHub.
2. Go to <https://share.streamlit.io>, sign in, **Create app** -> **Deploy a public app from
   GitHub**.
3. Repository: your fork/copy of `iris-signal-harvester`. Branch: the branch to deploy
   (e.g. `main`). **Main file path: `streamlit_app.py`** (the default name, so nothing to
   change).
4. **Advanced settings**: leave secrets and environment variables **empty** (do not set
   `IRIS_DB_DSN`; there is no database on Community Cloud). Python 3.11 or newer.
5. Deploy. Cloud installs the root `requirements.txt` (the UI dependency set; the package's own
   dependencies stay in `pyproject.toml`) and applies `.streamlit/config.toml` (theme matching
   `ui/theme.py`). The app opens in sample mode with the banner.

Keep `requirements.txt` and `ui/requirements.txt` identical when you change either.

## Layout

Mirrors the platform UI: `app.py` entry point (and root `streamlit_app.py`, which just runs
it), `config.py` (settings) / `theme.py` (colours) / `help_text.py` (definitions) / `assets/styles.css`, `components/` (header,
sample_banner, glossary, kpi_section, sidebar, signal_table, provenance_panel, quality_panel,
`map/pydeck/`), and `data/` (`connection.py` + `queries.py` for PostgreSQL, `samples.py` for the
files, `service.py` choosing between them).

Notes on honesty: NULL is shown as "unknown", rows without geometry are counted ("N signals
have no resolvable location") and never placed, and in database mode the verdict reads "NOT
RECORDED" unless a `reports/quality_<run_id>.json` exists for the latest run (the verdict is
not stored in PostgreSQL).
