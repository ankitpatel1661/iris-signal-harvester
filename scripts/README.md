# `scripts/`

| Script | What it does |
|---|---|
| `demo_delta.sh` | The gate `make demo` depends on. It fails if the first pass inserted nothing (a zero on the second pass would then prove nothing), if the second pass inserted or updated anything, or if any row's `updated_at` moved. |
| `generate_corpus.py` | Builds the committed synthetic corpus. Seeded and deterministic: running it twice produces byte-identical output. |
| `make_samples.py` | Regenerates everything under `docs/samples/` by running the pipeline against PostGIS. It drops and recreates its own databases, so point it at a throwaway server. |
| `keep_awake.py` | Opens the public dashboard in a headless browser and holds a session, so Streamlit Community Cloud does not put it to sleep. Run every two hours by `.github/workflows/keep-dashboard-awake.yml`. A plain HTTP ping does not work: Streamlit counts websocket sessions, and a sleeping app answers 502 to curl while waking fine in a browser. |
