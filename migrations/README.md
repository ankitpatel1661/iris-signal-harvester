# `migrations/` — schema

Plain SQL, applied by `iris_signal_harvester.sink.migrate`. No Alembic: the dependency surface stays
small so `make demo` works from a clean checkout.

The runner records a checksum per applied file and refuses to continue if an already-applied file was
edited, so schema history is append-only.

| File | What it adds |
|---|---|
| `001_initial.sql` | The `iris_staging` schema: `signal_ingest` with its natural key and geo-consistency constraint, the dead-letter table, the run manifest and the watermark table. |
| `002_reject_dedup.sql` | Makes a repeat rejection advance `seen_count` instead of inserting another row, so the dead-letter table does not grow on every run. |
