-- IRIS-SIG-23 — 002 de-duplicate the dead-letter table
--
-- Problem (final-review I-10): the incremental overlap window re-fetches a bad record
-- on every run, so the same rejection was inserted once per run. Three runs over one bad
-- record gave three identical rows, and the table grew without bound.
--
-- Fix: a rejection is identified by WHAT was rejected and WHY:
--
--     (source_system, source_body_id, source_record_id, reason_code, payload_hash)
--
-- A repeat sighting bumps last_seen_at / seen_count instead of inserting a row.
-- rejected_at keeps meaning "first rejected" and harvest_run_id the run that first
-- rejected it; changing the payload (or the reason) is a new rejection, as it should be.
--
-- payload_hash is computed by the database from the jsonb text form, which is canonical
-- (key order and whitespace are normalised), so Python never has to reproduce it. A
-- BEFORE INSERT trigger fills it: a generated column is not possible because
-- jsonb -> text is only STABLE, not IMMUTABLE, and a trigger (unlike a DEFAULT) can read
-- raw_payload. The trigger fires before the unique-index arbiter and before NOT NULL.
-- Idempotent: safe to re-run.

ALTER TABLE iris_staging.signal_ingest_reject
    ADD COLUMN IF NOT EXISTS payload_hash text,
    ADD COLUMN IF NOT EXISTS last_seen_at timestamptz NOT NULL DEFAULT now(),
    ADD COLUMN IF NOT EXISTS seen_count   integer     NOT NULL DEFAULT 1;

UPDATE iris_staging.signal_ingest_reject
SET payload_hash = encode(sha256(convert_to(raw_payload::text, 'UTF8')), 'hex'),
    last_seen_at = rejected_at  -- a pre-migration row was seen exactly when it was rejected
WHERE payload_hash IS NULL;

CREATE OR REPLACE FUNCTION iris_staging.reject_set_payload_hash() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    NEW.payload_hash := encode(sha256(convert_to(NEW.raw_payload::text, 'UTF8')), 'hex');
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS trg_reject_payload_hash ON iris_staging.signal_ingest_reject;
CREATE TRIGGER trg_reject_payload_hash
    BEFORE INSERT ON iris_staging.signal_ingest_reject
    FOR EACH ROW EXECUTE FUNCTION iris_staging.reject_set_payload_hash();

ALTER TABLE iris_staging.signal_ingest_reject
    ALTER COLUMN payload_hash SET NOT NULL;

-- Collapse duplicates that already exist: keep the oldest row, remember how often it
-- was seen and when it was last seen, drop the rest.
WITH grp AS (
    SELECT id,
           min(id)          OVER w AS keep_id,
           count(*)         OVER w AS n,
           max(rejected_at) OVER w AS last_at
    FROM iris_staging.signal_ingest_reject
    WINDOW w AS (PARTITION BY source_system,
                              COALESCE(source_body_id, ''),
                              COALESCE(source_record_id, ''),
                              reason_code,
                              payload_hash)
)
UPDATE iris_staging.signal_ingest_reject r
SET seen_count = g.n, last_seen_at = g.last_at
FROM grp g
WHERE r.id = g.id AND g.id = g.keep_id AND g.n > 1;

DELETE FROM iris_staging.signal_ingest_reject r
USING (
    SELECT id, min(id) OVER (PARTITION BY source_system,
                                          COALESCE(source_body_id, ''),
                                          COALESCE(source_record_id, ''),
                                          reason_code,
                                          payload_hash) AS keep_id
    FROM iris_staging.signal_ingest_reject
) d
WHERE r.id = d.id AND d.id <> d.keep_id;

-- body / record id are nullable (a record can be rejected before it has an id), and a
-- plain UNIQUE treats NULLs as distinct, so the NULLs are folded to '' in the index.
CREATE UNIQUE INDEX IF NOT EXISTS uq_signal_reject_dedup
    ON iris_staging.signal_ingest_reject (
        source_system,
        COALESCE(source_body_id, ''),
        COALESCE(source_record_id, ''),
        reason_code,
        payload_hash
    );
