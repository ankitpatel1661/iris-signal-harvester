# `config/` — the knowledge the pipeline runs on

Domain knowledge lives here rather than in Python, so it can be changed without touching code.
Every run records a `config_hash`, so any output can be traced to the configuration that produced it.

| File | What it controls |
|---|---|
| `taxonomy.yaml` | The eight infrastructure-trigger signal types, their German terms, weights and negative-context windows. This is the file a domain expert would edit. |
| `document_types.yaml` | Controlled vocabulary for `paperType`. An unknown value becomes `unmapped` and is counted in the quality report rather than silently passed through. |
| `gazetteer.json`, `gazetteer_corpus.json` | AGS → municipality polygon and representative point, for the lowest geographic tier. |
| `bodies.yaml` | The source registry: which systems exist, what was observed about them, and their declared licence (frequently none). |
| `thresholds.yaml` | Completeness and rejection thresholds that drive the PASS / WARN / FAIL verdict and the exit code. |
