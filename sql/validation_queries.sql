-- IRIS-SIG-23 — validation queries
--
-- These let a reviewer verify the acceptance criteria independently, without reading
-- any Python. Queries marked "MUST return zero rows" are invariants: the `validate`
-- CLI command runs them and exits non-zero on any violation.
--
-- Three kinds of query, so a green run can be read for what it actually proves:
--
--   * Queries 1, 2, 4 and 10 are DEFENCE IN DEPTH. The schema already makes a violation
--     impossible (uq_signal_natural_key, geometry(Point, 25832), ck_geo_consistency,
--     ck_watermark_not_sentinel); they would catch a dropped or weakened constraint,
--     not bad data. Query 3 is a COARSE bounding check, not the national outline.
--   * Queries 11, 12 and 15 are DATA-QUALITY checks: the schema cannot prevent what they
--     look for, so they can genuinely fail. They say "ZERO ROWS EXPECTED" rather than
--     "MUST return zero rows" only so that older tooling that keys on the latter does
--     not mistake them for structural invariants; `validate` runs and enforces both.
--   * Everything else is situational awareness and is only printed.
--
-- `validate` also FAILS when it finds fewer checks than the expected set below, or an
-- empty signal_ingest, so a truncated file or an empty database cannot pass vacuously.
--
-- Usage:  psql "$IRIS_DB_DSN" -v run_id="'<uuid>'" -f sql/validation_queries.sql
--     or: iris-harvest validate


-- 1. No duplicates on the natural key.  MUST return zero rows.
--    Defence in depth: uq_signal_natural_key makes this structurally impossible.
SELECT source_system, source_body_id, source_record_id, count(*) AS n
FROM iris_staging.signal_ingest
GROUP BY 1, 2, 3
HAVING count(*) > 1;


-- 2. Every geometry is stored in EPSG:25832.  MUST return zero rows.
--    Defence in depth: the column type geometry(Point, 25832) enforces this.
SELECT id, ST_SRID(geom) AS actual_srid
FROM iris_staging.signal_ingest
WHERE geom IS NOT NULL
  AND ST_SRID(geom) <> 25832;


-- 3. Every geometry lies inside the EPSG:25832 coordinate domain.  MUST return zero rows.
--    COARSE BOUNDING CHECK, not the national outline: it is the exact rectangle of the
--    Python CRS-domain gate (domain/geo/validate.py E_RANGE x N_RANGE; a test pins the two
--    together). The tighter national-envelope polygon is enforced in Python before a
--    record is written; a point can pass this query and still be outside Germany.
SELECT id, ST_AsText(geom) AS wkt
FROM iris_staging.signal_ingest
WHERE geom IS NOT NULL
  AND NOT (ST_X(geom) BETWEEN 265000 AND 935000
       AND ST_Y(geom) BETWEEN 5200000 AND 6120000);


-- 4. Confidence labelling is consistent with geometry presence.  MUST return zero rows.
--    No confident nulls, no unlabelled points. Defence in depth: ck_geo_consistency.
SELECT id, geo_method, geo_confidence, (geom IS NULL) AS geom_is_null
FROM iris_staging.signal_ingest
WHERE (geom IS NULL) <> (geo_confidence = 'none')
   OR (geom IS NULL) <> (geo_method = 'unresolved');


-- 5. Date provenance distribution — how much do we actually know?
--    Not an invariant; a situational-awareness query. A high `unknown` share is expected
--    on OParl sources because Paper.date is frequently null in real municipal data.
SELECT published_at_origin,
       count(*)                                                   AS n,
       round(100.0 * count(*) / sum(count(*)) OVER (), 1)          AS pct
FROM iris_staging.signal_ingest
GROUP BY 1
ORDER BY 2 DESC;


-- 6. Idempotency: after a second identical run, MUST return zero.
--    Nothing may have been written by the most recent run if the input did not change.
SELECT count(*) AS changed_by_latest_run
FROM iris_staging.signal_ingest
WHERE updated_at >= (SELECT started_at
                     FROM iris_staging.harvest_run
                     ORDER BY started_at DESC
                     LIMIT 1);


-- 6b. ...while liveness tracking still advanced. MUST return > 0 on a re-run.
--     Separating last_seen_at from updated_at is what makes 6 and 6b both meaningful.
SELECT count(*) AS reseen_by_latest_run
FROM iris_staging.signal_ingest
WHERE last_seen_at >= (SELECT started_at
                       FROM iris_staging.harvest_run
                       ORDER BY started_at DESC
                       LIMIT 1);


-- 7. Rejection histogram with one worked example per reason.
--    Every rejection is a row, not a log line, so it can be counted and replayed.
SELECT reason_code,
       count(*)                 AS n,
       min(source_record_id)    AS example_record_id
FROM iris_staging.signal_ingest_reject
WHERE harvest_run_id = :'run_id'::uuid
GROUP BY 1
ORDER BY 2 DESC;


-- 8. Signal + geography sanity: top municipalities by high-confidence trigger.
--    Note the geo_confidence filter — scoring on municipality centroids would be scoring
--    the location of a town hall, which is why the confidence column exists.
SELECT bundesland, signal_type, count(*) AS n
FROM iris_staging.signal_ingest
WHERE geo_confidence IN ('high', 'medium')
  AND relevance_score >= 0.5
GROUP BY 1, 2
ORDER BY n DESC
LIMIT 25;


-- 9. Licence coverage — which bodies have not declared one?
--    Real OParl endpoints frequently return Body.license = null, so `unknown` is a
--    visible, actionable state rather than an assumption of permissiveness.
SELECT COALESCE(source_licence, '(undeclared)') AS licence,
       count(DISTINCT source_body_id)           AS bodies,
       count(*)                                 AS rows_affected
FROM iris_staging.signal_ingest
GROUP BY 1
ORDER BY 3 DESC;


-- 10. Watermark health — a corrupt `modified` must never have poisoned a watermark.
--     MUST return zero rows. Live OParl data contains '-0001-11-30T00:00:00+00:53'.
--     Defence in depth for the lower bound (ck_watermark_not_sentinel); the upper bound
--     depends on now(), which a CHECK cannot use, so only this query and watermark.py
--     guard it.
SELECT source_system, body_id, last_modified_seen
FROM iris_staging.harvest_watermark
WHERE last_modified_seen IS NOT NULL
  AND (last_modified_seen < timestamptz '1990-01-01'
       OR last_modified_seen > now() + interval '400 days');


-- 11. Data quality: one source_url under several natural keys.  ZERO ROWS EXPECTED.
--     The same dereferenceable object stored twice means the natural key is not a stable
--     identity: a renumbered paper (Paper.reference is an editable file number) or two
--     bodies exposing one URL. uq_signal_natural_key cannot see this; the data can.
SELECT source_url,
       count(*)                                              AS n_keys,
       min(source_record_id)                                 AS example_record_id,
       array_agg(DISTINCT source_body_id ORDER BY source_body_id) AS bodies
FROM iris_staging.signal_ingest
GROUP BY source_url
HAVING count(*) > 1;


-- 12. Data quality: administrative-key shape.  ZERO ROWS EXPECTED.
--     AGS is 8 digits and ARS is 12; the normaliser drops a misshapen value to NULL, so a
--     non-NULL value that is not digits means something wrote around it. A bad AGS also
--     silently disables the municipality-centroid geo tier.
SELECT id, ags, ars
FROM iris_staging.signal_ingest
WHERE (ags IS NOT NULL AND ags !~ '^[0-9]{8}$')
   OR (ars IS NOT NULL AND ars !~ '^[0-9]{12}$');


-- 13. Staleness: rows not re-observed by any of the last 3 succeeded runs.  Informational.
--     Not an invariant: an incremental sweep legitimately never re-fetches an unchanged
--     old record, and deleted source objects are not reconciled. A count that grows run
--     after run on a FULL sweep is the signal. Empty until a source has 3 succeeded runs.
WITH ranked AS (
    SELECT source_system, started_at,
           row_number() OVER (PARTITION BY source_system ORDER BY started_at DESC) AS rn
    FROM iris_staging.harvest_run
    WHERE status = 'succeeded'
)
SELECT s.source_system,
       count(*)                AS stale_rows,
       min(s.last_seen_at)     AS oldest_last_seen_at
FROM iris_staging.signal_ingest s
JOIN ranked r ON r.source_system = s.source_system AND r.rn = 3
WHERE s.last_seen_at < r.started_at
GROUP BY 1;


-- 14. Rejection-rate trend over the last 10 finished runs.  Informational.
--     Counts come from harvest_run (what each run observed), not from the reject table,
--     which holds each distinct rejection once however many runs re-saw it.
SELECT id AS run_id, started_at, status, records_fetched, rejected,
       round(100.0 * rejected / NULLIF(records_fetched, 0), 1) AS reject_pct
FROM iris_staging.harvest_run
WHERE finished_at IS NOT NULL
ORDER BY started_at DESC
LIMIT 10;


-- 15. Data quality: no run left unfinished.  ZERO ROWS EXPECTED.
--     A run that is still 'running' a day later crashed before finish_run (or was killed);
--     its watermark was not advanced and its counts were never recorded.
SELECT id, source_system, started_at
FROM iris_staging.harvest_run
WHERE status = 'running'
  AND started_at < now() - interval '1 day';


-- 16. Document types the vocabulary did not map.  Informational.
--     config/document_types.yaml maps raw OParl paperType values; anything else is stored
--     as 'unmapped'. The quality report lists the raw values; this is the table-wide count.
SELECT count(*) FILTER (WHERE document_type = 'unmapped')     AS unmapped,
       count(*) FILTER (WHERE document_type IS NOT NULL)      AS typed,
       count(*)                                                AS total
FROM iris_staging.signal_ingest;
