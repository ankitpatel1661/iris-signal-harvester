-- IRIS-SIG-23 — 001 initial schema
--
-- Plain SQL, applied by iris_signal_harvester.sink.migrate (no Alembic).
-- Every statement is idempotent so the file is safe to re-run.
--
-- Design notes that matter to a reviewer:
--
--   * updated_at   moves ONLY when content changed (content_hash differs).
--   * last_seen_at moves on EVERY run that re-observes the record.
--     Keeping the two apart is what lets validation queries 6 and 6b both be
--     meaningful: "nothing was written" and "everything was still seen".
--   * Uncertainty is a column, never a default: published_at_origin sits beside
--     published_at, geo_method / geo_confidence sit beside geom.

CREATE SCHEMA IF NOT EXISTS iris_staging;

CREATE EXTENSION IF NOT EXISTS postgis;

-- ---------------------------------------------------------------------------
-- harvest_run — one row per orchestrator invocation
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS iris_staging.harvest_run (
    id               uuid PRIMARY KEY,
    source_system    text        NOT NULL,
    started_at       timestamptz NOT NULL DEFAULT now(),
    finished_at      timestamptz,
    status           text        NOT NULL DEFAULT 'running',
    config_hash      text,
    taxonomy_version text,
    records_fetched  integer     NOT NULL DEFAULT 0,
    inserted         integer     NOT NULL DEFAULT 0,
    updated          integer     NOT NULL DEFAULT 0,
    unchanged        integer     NOT NULL DEFAULT 0,
    rejected         integer     NOT NULL DEFAULT 0,
    notes            jsonb       NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT ck_harvest_run_status
        CHECK (status IN ('running', 'succeeded', 'partial', 'failed')),
    CONSTRAINT ck_harvest_run_counts
        CHECK (records_fetched >= 0 AND inserted >= 0 AND updated >= 0
               AND unchanged >= 0 AND rejected >= 0)
);

CREATE INDEX IF NOT EXISTS ix_harvest_run_started_at
    ON iris_staging.harvest_run (started_at DESC);

-- ---------------------------------------------------------------------------
-- signal_ingest — the canonical signal table
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS iris_staging.signal_ingest (
    id                  bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,

    -- run that last WROTE this row (insert or content change); not the last run to see it
    harvest_run_id      uuid        NOT NULL REFERENCES iris_staging.harvest_run (id),

    -- provenance
    source_system       text        NOT NULL,
    source_body_id      text        NOT NULL,
    source_record_id    text        NOT NULL,
    source_url          text        NOT NULL,
    source_licence      text,

    -- content
    title               text        NOT NULL,
    summary             text,
    document_type       text,

    -- date + provenance
    published_at        timestamptz,
    published_at_origin text        NOT NULL DEFAULT 'unknown',

    -- geography
    ags                 char(8),
    ars                 varchar(12),
    municipality_name   text,
    kreis               text,
    bundesland          text,
    geom                geometry(Point, 25832),
    geo_method          text        NOT NULL DEFAULT 'unresolved',
    geo_confidence      text        NOT NULL DEFAULT 'none',
    source_crs          text,

    -- signal
    signal_type         text,
    matched_keywords    jsonb       NOT NULL DEFAULT '[]'::jsonb,
    relevance_score     double precision,
    taxonomy_version    text        NOT NULL,

    -- integrity
    raw_payload         jsonb       NOT NULL,
    content_hash        char(64)    NOT NULL,
    hash_version        integer     NOT NULL DEFAULT 1,

    -- lifecycle
    first_seen_at       timestamptz NOT NULL DEFAULT now(),
    last_seen_at        timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),

    -- OParl references are unique only within a body, so the body is part of the key
    CONSTRAINT uq_signal_natural_key UNIQUE (source_system, source_body_id, source_record_id),

    CONSTRAINT ck_published_origin CHECK (
        published_at_origin IN
            ('payload_field', 'http_header', 'derived_from_meeting', 'unknown')
        -- a date and its provenance agree: no date <=> origin 'unknown'
        AND ((published_at IS NULL) = (published_at_origin = 'unknown'))
    ),

    CONSTRAINT ck_geo_method CHECK (
        geo_method IN ('source_coordinates', 'cadastre_parcel', 'address_geocode',
                       'municipality_centroid', 'unresolved')
    ),

    CONSTRAINT ck_geo_confidence_values CHECK (
        geo_confidence IN ('high', 'medium', 'low', 'none')
    ),

    -- geometry presence, confidence and method must agree in BOTH directions:
    -- no confident nulls, and no unlabelled points.
    CONSTRAINT ck_geo_consistency CHECK (
        (geom IS NULL     AND geo_confidence = 'none' AND geo_method = 'unresolved')
        OR
        (geom IS NOT NULL AND geo_confidence <> 'none' AND geo_method <> 'unresolved')
    ),

    CONSTRAINT ck_relevance_range CHECK (
        relevance_score IS NULL OR (relevance_score >= 0 AND relevance_score <= 1)
    )
);

CREATE INDEX IF NOT EXISTS ix_signal_ingest_geom
    ON iris_staging.signal_ingest USING gist (geom);

CREATE INDEX IF NOT EXISTS ix_signal_ingest_matched_keywords
    ON iris_staging.signal_ingest USING gin (matched_keywords);

CREATE INDEX IF NOT EXISTS ix_signal_ingest_published_at
    ON iris_staging.signal_ingest (published_at);

CREATE INDEX IF NOT EXISTS ix_signal_ingest_type_ags
    ON iris_staging.signal_ingest (signal_type, ags);

CREATE INDEX IF NOT EXISTS ix_signal_ingest_content_hash
    ON iris_staging.signal_ingest (content_hash);

-- last_seen_at is deliberately NOT indexed: an indexed column would defeat HOT
-- updates, and last_seen_at is rewritten for every re-observed row on every run.

-- ---------------------------------------------------------------------------
-- signal_ingest_reject — dead-letter table (rows, not log lines)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS iris_staging.signal_ingest_reject (
    id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    harvest_run_id   uuid        NOT NULL REFERENCES iris_staging.harvest_run (id),
    source_system    text        NOT NULL,
    source_body_id   text,
    source_record_id text,
    -- must mirror domain.models.RejectCode; tests/test_sink.py fails on drift
    reason_code      text        NOT NULL,
    reason_detail    text        NOT NULL,
    raw_payload      jsonb       NOT NULL,
    rejected_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_reject_reason_code CHECK (reason_code IN (
        'malformed_json',
        'schema_validation_failed',
        'missing_required_field',
        'invalid_source_url',
        'date_out_of_range',
        'date_unparseable',
        'geo_non_finite',
        'geo_latlon_in_projected_field',
        'geo_out_of_crs_domain',
        'geo_outside_national_envelope',
        'geo_outside_claimed_municipality',
        'geo_reprojection_failed'
    ))
);

CREATE INDEX IF NOT EXISTS ix_signal_reject_run_reason
    ON iris_staging.signal_ingest_reject (harvest_run_id, reason_code);

-- ---------------------------------------------------------------------------
-- harvest_watermark — per (source, body) incremental cursor
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS iris_staging.harvest_watermark (
    source_system      text        NOT NULL,
    body_id            text        NOT NULL,
    last_modified_seen timestamptz,
    last_run_id        uuid        REFERENCES iris_staging.harvest_run (id),
    updated_at         timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (source_system, body_id),
    -- belt and braces for validation query 10 (the upper bound depends on now(),
    -- which a CHECK cannot use; watermark.py enforces it).
    CONSTRAINT ck_watermark_not_sentinel
        CHECK (last_modified_seen IS NULL OR last_modified_seen >= timestamptz '1990-01-01 00:00:00+00')
);
