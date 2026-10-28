#!/usr/bin/env bash
# Print and assert the re-run delta for `make demo`.
#
# Reads the last two rows of iris_staging.harvest_run straight from the database, so the
# verdict does not depend on how the CLI formats its own output. Fails (exit 1) unless:
#   - there are two runs,
#   - pass 1 inserted at least one row (otherwise "0 on pass 2" would prove nothing),
#   - pass 2 inserted 0 and updated 0,
#   - no signal_ingest row had updated_at moved after pass 2 started.
set -euo pipefail

COMPOSE="${COMPOSE:-docker compose}"

psql_db() {
  # User and database come from the container's own environment, so this stays correct
  # whatever IRIS_POSTGRES_* resolved to. Local-socket auth inside the container needs no password.
  # shellcheck disable=SC2086
  $COMPOSE exec -T db sh -c 'exec psql -X -q -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 "$@"' sh "$@"
}

RUNS_SQL="
WITH r AS (
  SELECT id, started_at, status, records_fetched, inserted, updated, unchanged, rejected,
         row_number() OVER (ORDER BY started_at DESC) AS rn
  FROM iris_staging.harvest_run
)"

line() { printf '%s\n' "================================================================================"; }

line
echo " RE-RUN DELTA  (iris_staging.harvest_run, last two runs, oldest first)"
line
psql_db -c "$RUNS_SQL
SELECT CASE rn WHEN 2 THEN 'pass 1 (first load)' ELSE 'pass 2 (re-run)' END AS pass,
       left(id::text, 8) AS run, status, records_fetched AS fetched,
       inserted, updated, unchanged, rejected
FROM r WHERE rn <= 2 ORDER BY started_at;"

# machine-readable: n_runs|p1_inserted|p2_inserted|p2_updated|p2_started
# (captured first: a temporary IFS on `read` would leak into the process substitution)
summary=$(psql_db -tA -F'|' -c "$RUNS_SQL
SELECT (SELECT count(*) FROM r WHERE rn <= 2),
       coalesce((SELECT inserted FROM r WHERE rn = 2), -1),
       coalesce((SELECT inserted FROM r WHERE rn = 1), -1),
       coalesce((SELECT updated  FROM r WHERE rn = 1), -1),
       coalesce((SELECT started_at::text FROM r WHERE rn = 1), '');")
IFS='|' read -r n_runs p1_ins p2_ins p2_upd p2_started <<<"$summary"

total_rows=$(psql_db -tA -c "SELECT count(*) FROM iris_staging.signal_ingest;")
touched=$(psql_db -tA -c "SELECT count(*) FROM iris_staging.signal_ingest WHERE updated_at >= '${p2_started:-infinity}'::timestamptz;")
seen=$(psql_db -tA -c "SELECT count(*) FROM iris_staging.signal_ingest WHERE last_seen_at >= '${p2_started:-infinity}'::timestamptz;")

echo
echo " signal_ingest rows ............................. ${total_rows}"
echo " rows whose updated_at moved during pass 2 ...... ${touched}   (must be 0)"
echo " rows re-observed (last_seen_at) during pass 2 .. ${seen}   (liveness, expected = rows)"
echo

fail=0
[[ "$n_runs" == "2" ]]   || { echo " FAIL: expected 2 harvest runs, found ${n_runs}"; fail=1; }
[[ "$p1_ins" -gt 0 ]] 2>/dev/null || { echo " FAIL: pass 1 inserted ${p1_ins} rows; the demo would prove nothing"; fail=1; }
[[ "$p2_ins" == "0" ]]   || { echo " FAIL: pass 2 inserted ${p2_ins} rows (expected 0)"; fail=1; }
[[ "$p2_upd" == "0" ]]   || { echo " FAIL: pass 2 updated ${p2_upd} rows (expected 0)"; fail=1; }
[[ "$touched" == "0" ]]  || { echo " FAIL: ${touched} rows had updated_at moved in pass 2 (expected 0)"; fail=1; }

line
if [[ "$fail" == "0" ]]; then
  echo " IDEMPOTENT: pass 2 -> inserted=0 updated=0 (pass 1 inserted ${p1_ins}); ${total_rows} rows, none rewritten"
else
  echo " NOT IDEMPOTENT: see FAIL lines above"
fi
line
exit "$fail"
