"""Sink, migrations and watermark.

The ``integration`` tests need a PostGIS database and skip cleanly without
``IRIS_DB_DSN``. They never touch the database that DSN names: a throwaway
database is created from it for the session and dropped afterwards, so a DSN
pointing at something precious cannot be truncated by a test.

The tests that matter most are ``test_second_identical_load_writes_nothing`` and
``test_changing_one_field_updates_exactly_one_row``: they are the idempotency
guarantee, asserted against the data rather than against the sink's own counters.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from iris_signal_harvester.config import Settings
from iris_signal_harvester.domain.hashing import content_hash
from iris_signal_harvester.domain.models import (
    CanonicalSignal,
    GeoConfidence,
    GeoMethod,
    Hit,
    PublishedAtOrigin,
    RejectCode,
    RejectedRecord,
)
from iris_signal_harvester.errors import ConfigurationError
from iris_signal_harvester.sink.migrate import apply_migrations, default_migrations_dir
from iris_signal_harvester.sink.postgis import LoadCounts, PostgisSink
from iris_signal_harvester.sink.watermark import (
    AdvanceOutcome,
    ModifiedTracker,
    WatermarkStore,
    is_plausible_watermark,
    max_modified,
)

REPO = Path(__file__).resolve().parents[1]
SENTINEL = "-0001-11-30T00:00:00+00:53"
NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
SETTINGS = Settings(_env_file=None)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def make_signal(record_id: str, run_id: uuid.UUID, **overrides: Any) -> CanonicalSignal:
    fields: dict[str, Any] = {
        "harvest_run_id": run_id,
        "source_system": "fixture",
        "source_body_id": "body-1",
        "source_record_id": record_id,
        "source_url": f"https://example.org/papers/{record_id}",
        "title": f"Beschluss {record_id}: Neubau Umspannwerk",
        "summary": "Aufstellungsbeschluss",
        "document_type": "Beschlussvorlage",
        "published_at": datetime(2026, 3, 1, 9, 30, tzinfo=UTC),
        "published_at_origin": PublishedAtOrigin.PAYLOAD_FIELD,
        "ags": "09162000",
        "municipality_name": "München",
        "bundesland": "Bayern",
        "easting": 691234.5,
        "northing": 5334567.25,
        "geo_method": GeoMethod.ADDRESS_GEOCODE,
        "geo_confidence": GeoConfidence.MEDIUM,
        "source_crs": "EPSG:25832",
        "signal_type": "grid_expansion",
        "matched_keywords": (
            Hit(
                term="Umspannwerk",
                surface="Umspannwerk",
                field="title",
                offset=20,
                weight=0.8,
                signal_type="grid_expansion",
            ),
        ),
        "relevance_score": 0.8,
        "taxonomy_version": "2026.1",
        "raw_payload": {"id": record_id, "name": "Neubau Umspannwerk"},
        "content_hash": "0" * 64,
    }
    fields.update(overrides)
    signal = CanonicalSignal(**fields)
    # A hostile value cannot be hashed either; the sink must cope with the record anyway.
    with contextlib.suppress(UnicodeEncodeError, ValueError):
        signal.content_hash = content_hash(signal.model_dump())
    return signal


def fixture_set(run_id: uuid.UUID, n: int = 5) -> list[CanonicalSignal]:
    signals = [make_signal(f"P-{i:03d}", run_id) for i in range(n)]
    # an honestly unresolved record: no date, no geometry
    signals.append(
        make_signal(
            "P-UNRESOLVED",
            run_id,
            published_at=None,
            published_at_origin=PublishedAtOrigin.UNKNOWN,
            easting=None,
            northing=None,
            geo_method=GeoMethod.UNRESOLVED,
            geo_confidence=GeoConfidence.NONE,
        )
    )
    return signals


def validation_queries() -> dict[str, str]:
    """Parse sql/validation_queries.sql into {'1': sql, '6b': sql, ...}."""
    text = (REPO / "sql" / "validation_queries.sql").read_text(encoding="utf-8")
    parts = re.split(r"(?m)^-- (\d+b?)\. ", text)
    queries: dict[str, str] = {}
    for key, body in zip(parts[1::2], parts[2::2], strict=True):
        statement = "\n".join(
            line for line in body.splitlines()[1:] if not line.lstrip().startswith("--")
        ).strip()
        queries[key] = statement
    return queries


# ---------------------------------------------------------------------------
# database fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def db_dsn() -> Iterator[str]:
    base = os.environ.get("IRIS_DB_DSN")
    if not base:
        pytest.skip("IRIS_DB_DSN is not set; skipping database tests")
    name = f"iris_test_{uuid.uuid4().hex[:12]}"
    try:
        with psycopg.connect(base, autocommit=True) as admin:
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    except psycopg.Error as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"cannot create a throwaway database: {exc}")
    dsn = make_conninfo(base, dbname=name)
    try:
        applied = apply_migrations(dsn)
        assert applied == ["001_initial", "002_reject_dedup"]
        yield dsn
    finally:
        with psycopg.connect(base, autocommit=True) as admin:
            admin.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
            )


@pytest.fixture
def sink(db_dsn: str) -> Iterator[PostgisSink]:
    with psycopg.connect(db_dsn, autocommit=True) as conn:
        conn.execute(
            "TRUNCATE iris_staging.signal_ingest, iris_staging.signal_ingest_reject, "
            "iris_staging.harvest_watermark, iris_staging.harvest_run CASCADE"
        )
    yield PostgisSink(db_dsn)


def new_run(sink: PostgisSink, source: str = "fixture") -> uuid.UUID:
    run_id = uuid.uuid4()
    sink.start_run(run_id, source, config_hash="c" * 64, taxonomy_version="2026.1")
    return run_id


def rows(dsn: str, query: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    with psycopg.connect(dsn) as conn:
        return conn.execute(query, params).fetchall()  # type: ignore[arg-type]


def snapshot(dsn: str) -> dict[str, tuple[datetime, datetime, datetime]]:
    result = rows(
        dsn,
        "SELECT source_record_id, first_seen_at, updated_at, last_seen_at "
        "FROM iris_staging.signal_ingest",
    )
    return {r[0]: (r[1], r[2], r[3]) for r in result}


# ---------------------------------------------------------------------------
# THE idempotency tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_first_load_inserts_everything(sink: PostgisSink, db_dsn: str) -> None:
    run = new_run(sink)
    counts = sink.load(fixture_set(run))
    assert counts == LoadCounts(inserted=6, updated=0, unchanged=0)
    assert rows(db_dsn, "SELECT count(*) FROM iris_staging.signal_ingest")[0][0] == 6


@pytest.mark.integration
def test_second_identical_load_writes_nothing(sink: PostgisSink, db_dsn: str) -> None:
    first_run = new_run(sink)
    signals = fixture_set(first_run)
    sink.load(signals)
    before = snapshot(db_dsn)

    second_run = new_run(sink)
    again = [s.model_copy(update={"harvest_run_id": second_run}) for s in signals]
    counts = sink.load(again)
    after = snapshot(db_dsn)

    assert counts.inserted == 0
    assert counts.updated == 0
    assert counts.unchanged == len(signals)
    assert len(after) == len(before) == len(signals)
    for key, (first_seen, updated_at, last_seen) in before.items():
        new_first_seen, new_updated_at, new_last_seen = after[key]
        assert new_first_seen == first_seen
        assert new_updated_at == updated_at, f"{key}: updated_at moved on an unchanged record"
        assert new_last_seen > last_seen, f"{key}: last_seen_at did not advance"
    # the row keeps pointing at the run that last WROTE it, not the run that re-saw it
    owners = {r[0] for r in rows(db_dsn, "SELECT harvest_run_id FROM iris_staging.signal_ingest")}
    assert owners == {first_run}


@pytest.mark.integration
def test_changing_one_field_updates_exactly_one_row(sink: PostgisSink, db_dsn: str) -> None:
    run1 = new_run(sink)
    signals = fixture_set(run1)
    sink.load(signals)
    before = snapshot(db_dsn)

    run2 = new_run(sink)
    changed = [s.model_copy(update={"harvest_run_id": run2}) for s in signals]
    changed[2] = make_signal("P-002", run2, title="Beschluss P-002: GEÄNDERTER Titel")
    counts = sink.load(changed)
    after = snapshot(db_dsn)

    assert (counts.inserted, counts.updated, counts.unchanged) == (0, 1, len(signals) - 1)
    for key in before:
        moved = after[key][1] != before[key][1]
        assert moved == (key == "P-002"), f"{key}: updated_at moved={moved}"
        assert after[key][2] > before[key][2], f"{key}: last_seen_at did not advance"
    title, owner = rows(
        db_dsn,
        "SELECT title, harvest_run_id FROM iris_staging.signal_ingest "
        "WHERE source_record_id = 'P-002'",
    )[0]
    assert "GEÄNDERTER" in title
    assert owner == run2


@pytest.mark.integration
def test_new_record_alongside_existing_counts_correctly(sink: PostgisSink) -> None:
    run1 = new_run(sink)
    base = fixture_set(run1, 3)
    sink.load(base)
    run2 = new_run(sink)
    counts = sink.load([*base, make_signal("P-NEW", run2)])
    assert (counts.inserted, counts.updated, counts.unchanged) == (1, 0, len(base))


@pytest.mark.integration
def test_geometry_change_is_detected_and_stored_in_25832(sink: PostgisSink, db_dsn: str) -> None:
    run = new_run(sink)
    sink.load([make_signal("G-1", run)])
    counts = sink.load([make_signal("G-1", run, easting=700000.0, northing=5340000.0)])
    assert (counts.inserted, counts.updated, counts.unchanged) == (0, 1, 0)
    srid, x, y = rows(
        db_dsn,
        "SELECT ST_SRID(geom), ST_X(geom), ST_Y(geom) FROM iris_staging.signal_ingest",
    )[0]
    assert (srid, x, y) == (25832, 700000.0, 5340000.0)


@pytest.mark.integration
def test_duplicate_keys_in_one_batch_are_collapsed_and_reported(
    sink: PostgisSink, db_dsn: str
) -> None:
    run = new_run(sink)
    first = make_signal("D-1", run, title="alt")
    last = make_signal("D-1", run, title="neu")
    counts = sink.load([first, last, make_signal("D-2", run)])
    assert (counts.inserted, counts.duplicates_in_batch) == (2, 1)
    assert rows(
        db_dsn, "SELECT title FROM iris_staging.signal_ingest WHERE source_record_id='D-1'"
    )[0] == ("neu",)


@pytest.mark.integration
def test_same_reference_in_two_bodies_is_two_rows_not_an_overwrite(
    sink: PostgisSink, db_dsn: str
) -> None:
    run = new_run(sink)
    a = make_signal("02-08/B00088", run, source_body_id="body-a", title="Stadt A")
    b = make_signal("02-08/B00088", run, source_body_id="body-b", title="Stadt B")
    counts = sink.load([a, b])
    assert (counts.inserted, counts.updated, counts.duplicates_in_batch) == (2, 0, 0)
    found = rows(
        db_dsn,
        "SELECT source_body_id, source_record_id, title FROM iris_staging.signal_ingest "
        "ORDER BY source_body_id",
    )
    assert found == [
        ("body-a", "02-08/B00088", "Stadt A"),
        ("body-b", "02-08/B00088", "Stadt B"),
    ]
    # reloading touches each row by its full key and still writes nothing
    again = sink.load([a, b])
    assert (again.inserted, again.updated, again.unchanged) == (0, 0, 2)


@pytest.mark.integration
def test_nul_bytes_do_not_fail_the_batch_and_are_removed(sink: PostgisSink, db_dsn: str) -> None:
    run = new_run(sink)
    dirty = make_signal(
        "NUL-1",
        run,
        title="Neu\u0000bau",
        raw_payload={"name": "x\u0000y", "nested": {"items": ["a\u0000b", {"k": "\u0000z"}]}},
    )
    counts = sink.load([dirty, make_signal("NUL-2", run)])
    assert counts.inserted == 2
    title, payload = rows(
        db_dsn,
        "SELECT title, raw_payload::text FROM iris_staging.signal_ingest "
        "WHERE source_record_id = 'NUL-1'",
    )[0]
    assert title == "Neubau"
    assert "\\u0000" not in payload
    assert '"name": "xy"' in payload
    assert '"a\u0000b"' not in payload
    assert '"ab"' in payload
    # a NUL in a dead-lettered payload must not kill the reject write either
    written = sink.write_rejects(
        [
            RejectedRecord(
                harvest_run_id=run,
                source_system="oparl",
                reason_code=RejectCode.MISSING_REQUIRED_FIELD,
                reason_detail="bad\u0000detail",
                raw_payload={"name": "q\u0000r"},
            )
        ]
    )
    assert written == 1


@pytest.mark.integration
def test_empty_load_is_a_noop(sink: PostgisSink) -> None:
    assert sink.load([]) == LoadCounts(0, 0, 0)


@pytest.mark.integration
def test_unresolved_signal_stores_null_geom_and_null_date(sink: PostgisSink, db_dsn: str) -> None:
    run = new_run(sink)
    sink.load(fixture_set(run))
    row = rows(
        db_dsn,
        "SELECT geom IS NULL, geo_method, geo_confidence, published_at, published_at_origin "
        "FROM iris_staging.signal_ingest WHERE source_record_id = 'P-UNRESOLVED'",
    )[0]
    assert row == (True, "unresolved", "none", None, "unknown")


# ---------------------------------------------------------------------------
# rejects
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_rejected_record_lands_with_its_reason_code(sink: PostgisSink, db_dsn: str) -> None:
    run = new_run(sink)
    written = sink.write_rejects(
        [
            RejectedRecord(
                harvest_run_id=run,
                source_system="oparl",
                source_body_id="b1",
                source_record_id="P-BAD",
                reason_code=RejectCode.GEO_OUTSIDE_NATIONAL_ENVELOPE,
                reason_detail="easting=1.0 northing=2.0 outside envelope",
                raw_payload={"id": "P-BAD", "location": {"x": 1}},
            ),
            RejectedRecord(
                harvest_run_id=run,
                source_system="oparl",
                reason_code=RejectCode.DATE_OUT_OF_RANGE,
                reason_detail=f"modified={SENTINEL}",
                raw_payload={"modified": SENTINEL},
            ),
        ]
    )
    assert written == 2
    found = rows(
        db_dsn,
        "SELECT reason_code, source_record_id, raw_payload->>'id' "
        "FROM iris_staging.signal_ingest_reject WHERE harvest_run_id = %s ORDER BY id",
        (run,),
    )
    assert found == [
        ("geo_outside_national_envelope", "P-BAD", "P-BAD"),
        ("date_out_of_range", None, None),
    ]
    # validation query 7 (histogram) runs against the real table
    q7 = validation_queries()["7"].replace(":'run_id'", "%s")
    histogram = rows(db_dsn, q7, (run,))
    assert {r[0]: r[1] for r in histogram} == {
        "geo_outside_national_envelope": 1,
        "date_out_of_range": 1,
    }


@pytest.mark.integration
def test_reject_table_refuses_free_text_reason(sink: PostgisSink, db_dsn: str) -> None:
    run = new_run(sink)
    with psycopg.connect(db_dsn) as conn, pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "INSERT INTO iris_staging.signal_ingest_reject "
            "(harvest_run_id, source_system, reason_code, reason_detail, raw_payload) "
            "VALUES (%s, 'x', 'it was bad', 'd', '{}')",
            (run,),
        )


def test_reject_check_constraint_mirrors_reject_code_enum() -> None:
    """The migration's CHECK list must not drift from ``RejectCode``."""
    ddl = (REPO / "migrations" / "001_initial.sql").read_text(encoding="utf-8")
    block = re.search(r"ck_reject_reason_code CHECK \(reason_code IN \((.*?)\)\)", ddl, re.S)
    assert block is not None
    in_ddl = set(re.findall(r"'([a-z_]+)'", block.group(1)))
    assert in_ddl == {code.value for code in RejectCode}


# ---------------------------------------------------------------------------
# schema invariants (constraints, not Python)
# ---------------------------------------------------------------------------


def _raw_insert(
    dsn: str,
    run: uuid.UUID,
    record_id: str,
    extra: str = "",
    values: str = "",
    *,
    body: str = "raw-body",
) -> None:
    columns = "harvest_run_id, source_system, source_body_id, source_record_id, source_url, title, "
    columns += "taxonomy_version, raw_payload, content_hash"
    placeholders = "%s, 'raw', %s, %s, 'https://x', 't', 'v', '{}', %s"
    if extra:
        columns += f", {extra}"
        placeholders += f", {values}"
    with psycopg.connect(dsn) as conn:
        conn.execute(
            f"INSERT INTO iris_staging.signal_ingest ({columns}) VALUES ({placeholders})",  # type: ignore[arg-type]
            (run, body, record_id, "a" * 64),
        )


@pytest.mark.integration
def test_srid_is_enforced_by_the_column_type(sink: PostgisSink, db_dsn: str) -> None:
    run = new_run(sink)
    with pytest.raises(psycopg.Error, match=r"(?i)srid"):
        _raw_insert(
            db_dsn,
            run,
            "wrong-srid",
            "geom, geo_method, geo_confidence",
            "ST_SetSRID(ST_MakePoint(8.0, 50.0), 4326), 'address_geocode', 'high'",
        )


@pytest.mark.integration
def test_geo_consistency_check_works_in_both_directions(sink: PostgisSink, db_dsn: str) -> None:
    run = new_run(sink)
    # a point with no confidence ("unlabelled point")
    with pytest.raises(psycopg.errors.CheckViolation, match="ck_geo_consistency"):
        _raw_insert(
            db_dsn,
            run,
            "unlabelled",
            "geom",
            "ST_SetSRID(ST_MakePoint(691234, 5334567), 25832)",
        )
    # confidence and method claimed with no geometry ("confident null")
    with pytest.raises(psycopg.errors.CheckViolation, match="ck_geo_consistency"):
        _raw_insert(
            db_dsn, run, "confident-null", "geo_method, geo_confidence", "'address_geocode', 'high'"
        )


@pytest.mark.integration
def test_published_origin_check(sink: PostgisSink, db_dsn: str) -> None:
    run = new_run(sink)
    with pytest.raises(psycopg.errors.CheckViolation, match="ck_published_origin"):
        _raw_insert(
            db_dsn,
            run,
            "date-without-provenance",
            "published_at, published_at_origin",
            "now(), 'unknown'",
        )
    with pytest.raises(psycopg.errors.CheckViolation, match="ck_published_origin"):
        _raw_insert(
            db_dsn,
            run,
            "provenance-without-date",
            "published_at_origin",
            "'payload_field'",
        )


@pytest.mark.integration
def test_natural_key_is_unique(sink: PostgisSink, db_dsn: str) -> None:
    run = new_run(sink)
    _raw_insert(db_dsn, run, "dup", body="b")
    with pytest.raises(psycopg.errors.UniqueViolation, match="uq_signal_natural_key"):
        _raw_insert(db_dsn, run, "dup", body="b")
    # the same reference in another body is a different paper, not a duplicate
    _raw_insert(db_dsn, run, "dup", body="other-body")


@pytest.mark.integration
def test_source_body_id_is_required_by_the_schema(sink: PostgisSink, db_dsn: str) -> None:
    run = new_run(sink)
    with (
        pytest.raises(psycopg.errors.NotNullViolation, match="source_body_id"),
        psycopg.connect(db_dsn) as conn,
    ):
        conn.execute(
            "INSERT INTO iris_staging.signal_ingest (harvest_run_id, source_system, "
            "source_record_id, source_url, title, taxonomy_version, raw_payload, "
            "content_hash) VALUES (%s, 'raw', 'x', 'https://x', 't', 'v', '{}', %s)",
            (run, "a" * 64),
        )


@pytest.mark.integration
def test_expected_indexes_exist(sink: PostgisSink, db_dsn: str) -> None:
    defs = " ".join(
        r[0]
        for r in rows(db_dsn, "SELECT indexdef FROM pg_indexes WHERE schemaname = 'iris_staging'")
    )
    assert "USING gist (geom)" in defs
    assert "USING gin (matched_keywords)" in defs
    assert "(published_at)" in defs
    assert "(signal_type, ags)" in defs
    assert "(content_hash)" in defs


@pytest.mark.integration
def test_validation_queries_hold_after_a_rerun(sink: PostgisSink, db_dsn: str) -> None:
    """Run the real sql/validation_queries.sql invariants against real loads."""
    run1 = new_run(sink)
    signals = fixture_set(run1)
    sink.load(signals)
    run2 = new_run(sink)
    sink.load([s.model_copy(update={"harvest_run_id": run2}) for s in signals])

    queries = validation_queries()
    for key in ("1", "2", "4", "6b", "10"):
        assert key in queries
    for key in ("1", "2", "3", "4", "10"):
        assert rows(db_dsn, queries[key]) == [], f"validation query {key} returned rows"
    # 6: nothing was written by the latest run; 6b: yet everything was re-seen
    assert rows(db_dsn, queries["6"]) == [(0,)]
    assert rows(db_dsn, queries["6b"]) == [(len(signals),)]


# ---------------------------------------------------------------------------
# migrations
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_migrations_are_idempotent_and_tamper_evident(db_dsn: str, tmp_path: Path) -> None:
    assert apply_migrations(db_dsn) == []  # already applied by the fixture
    copy = tmp_path / "migrations"
    copy.mkdir()
    for name in ("001_initial.sql", "002_reject_dedup.sql"):
        (copy / name).write_text(default_migrations_dir().joinpath(name).read_text())
    (copy / "001_initial.sql").write_text(
        default_migrations_dir().joinpath("001_initial.sql").read_text() + "\n-- edited\n"
    )
    with pytest.raises(ConfigurationError, match="modified after being applied"):
        apply_migrations(db_dsn, copy)


def test_migration_file_is_found() -> None:
    assert (default_migrations_dir() / "001_initial.sql").is_file()


def test_from_settings_without_dsn_fails_loudly() -> None:
    with pytest.raises(ConfigurationError):
        PostgisSink.from_settings(Settings(_env_file=None, db_dsn=None))  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# watermark: pure logic (runs everywhere)
# ---------------------------------------------------------------------------


def test_max_modified_ignores_the_sentinel() -> None:
    values: list[object] = [
        "2026-03-01T10:00:00+01:00",
        SENTINEL,
        "2026-04-01T10:00:00+02:00",
        "not a date",
        None,
        12345,
    ]
    best = max_modified(values, SETTINGS, now=NOW)
    assert best == datetime(2026, 4, 1, 8, 0, tzinfo=UTC)


def test_max_modified_of_only_garbage_is_none() -> None:
    assert max_modified([SENTINEL, "garbage", None], SETTINGS, now=NOW) is None


def test_far_future_modified_cannot_become_the_maximum() -> None:
    values: list[object] = ["2026-04-01T00:00:00+00:00", "2099-01-01T00:00:00+00:00"]
    assert max_modified(values, SETTINGS, now=NOW) == datetime(2026, 4, 1, tzinfo=UTC)


def test_plausibility_bounds_mirror_validation_query_10() -> None:
    assert not is_plausible_watermark(datetime(1989, 12, 31, tzinfo=UTC), now=NOW)
    assert is_plausible_watermark(datetime(1990, 1, 1, tzinfo=UTC), now=NOW)
    assert is_plausible_watermark(NOW + timedelta(days=399), now=NOW)
    assert not is_plausible_watermark(NOW + timedelta(days=401), now=NOW)
    assert not is_plausible_watermark(datetime(2026, 1, 1), now=NOW)  # naive


def test_tracker_counts_ignored_values_and_keeps_clean_flag() -> None:
    tracker = ModifiedTracker(settings=SETTINGS, now=NOW)
    for value in ("2026-02-01T00:00:00+00:00", SENTINEL, None, "2026-03-01T00:00:00+00:00"):
        tracker.observe(value)
    assert tracker.maximum == datetime(2026, 3, 1, tzinfo=UTC)
    assert (tracker.observed, tracker.ignored) == (3, 1)
    assert tracker.clean
    tracker.mark_unclean()
    assert not tracker.clean


# ---------------------------------------------------------------------------
# watermark: persistence
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_watermark_advances_only_on_a_clean_sweep(sink: PostgisSink, db_dsn: str) -> None:
    store = WatermarkStore(db_dsn)
    run = new_run(sink, "oparl")
    t1 = datetime(2026, 3, 1, tzinfo=UTC)

    unclean = store.advance("oparl", "b1", t1, clean_sweep=False, run_id=run, now=NOW)
    assert unclean is AdvanceOutcome.SKIPPED_UNCLEAN_SWEEP
    assert store.read("oparl", "b1") is None

    assert store.advance("oparl", "b1", t1, clean_sweep=True, run_id=run, now=NOW) is (
        AdvanceOutcome.ADVANCED
    )
    assert store.read("oparl", "b1") == t1


@pytest.mark.integration
def test_watermark_is_monotonic_and_skips_empty_candidates(sink: PostgisSink, db_dsn: str) -> None:
    store = WatermarkStore(db_dsn)
    t_new = datetime(2026, 5, 1, tzinfo=UTC)
    t_old = datetime(2026, 2, 1, tzinfo=UTC)
    store.advance("oparl", "b1", t_new, clean_sweep=True, now=NOW)
    again = store.advance("oparl", "b1", t_old, clean_sweep=True, now=NOW)
    assert again is AdvanceOutcome.UNCHANGED
    assert store.read("oparl", "b1") == t_new
    none = store.advance("oparl", "b1", None, clean_sweep=True, now=NOW)
    assert none is AdvanceOutcome.SKIPPED_NO_CANDIDATE


@pytest.mark.integration
def test_implausible_watermark_never_reaches_the_table(sink: PostgisSink, db_dsn: str) -> None:
    store = WatermarkStore(db_dsn)
    # what the sentinel would be if it ever parsed, and a far-future value
    for bad in (datetime(1, 1, 1, tzinfo=UTC), datetime(2099, 1, 1, tzinfo=UTC)):
        outcome = store.advance("oparl", "b1", bad, clean_sweep=True, now=NOW)
        assert outcome is AdvanceOutcome.SKIPPED_IMPLAUSIBLE
    assert store.read("oparl", "b1") is None
    # and the table itself refuses a pre-1990 value even if the Python guard were bypassed
    with psycopg.connect(db_dsn) as conn, pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "INSERT INTO iris_staging.harvest_watermark (source_system, body_id, "
            "last_modified_seen) VALUES ('oparl', 'poisoned', timestamptz '0001-01-01 00:00:00+00')"
        )


@pytest.mark.integration
def test_end_to_end_sentinel_never_poisons_watermark(sink: PostgisSink, db_dsn: str) -> None:
    store = WatermarkStore(db_dsn)
    tracker = ModifiedTracker(settings=SETTINGS, now=NOW)
    for value in ("2026-03-01T10:00:00+01:00", SENTINEL, "2026-03-02T10:00:00+01:00"):
        tracker.observe(value)
    outcome = store.advance("oparl", "b1", tracker.maximum, clean_sweep=tracker.clean, now=NOW)
    assert outcome is AdvanceOutcome.ADVANCED
    assert store.read("oparl", "b1") == datetime(2026, 3, 2, 9, 0, tzinfo=UTC)
    assert rows(db_dsn, validation_queries()["10"]) == []


# ---------------------------------------------------------------------------
# values PostgreSQL cannot store: NaN and lone surrogates (final-review I-1)
# ---------------------------------------------------------------------------

# Parsed exactly as ``httpx.Response.json()`` parses a body, which is the realistic source.
HOSTILE_JSON = (
    '{"nan": NaN, "inf": Infinity, "neg": -Infinity, "lone": "\\ud800", '
    '"nested": {"items": [1.5, NaN, "ok"]}, "\\udc00key": 1}'
)


def test_the_hostile_payload_really_is_what_json_parsing_yields() -> None:
    """Guard the premise: the stdlib parser (and so httpx) accepts both shapes."""
    parsed = json.loads(HOSTILE_JSON)
    assert parsed["nan"] != parsed["nan"]  # NaN
    assert parsed["lone"] == "\ud800"
    with pytest.raises(ValueError, match="Out of range float"):
        json.dumps(parsed, allow_nan=False)
    with pytest.raises(UnicodeEncodeError):
        parsed["lone"].encode("utf-8")


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"raw_payload": {"x": float("nan")}}, id="nan-in-payload"),
        pytest.param({"raw_payload": {"x": [1, {"y": float("inf")}]}}, id="inf-in-nested-payload"),
        pytest.param({"raw_payload": {"x": "a\ud800b"}}, id="surrogate-in-payload"),
        pytest.param({"raw_payload": {"k\udc00": 1}}, id="surrogate-in-payload-key"),
        pytest.param({"title": "Neubau \ud800"}, id="surrogate-in-title"),
        pytest.param({"summary": "\udfff"}, id="surrogate-in-summary"),
        pytest.param({"municipality_name": "M\ud83d"}, id="surrogate-in-municipality"),
        pytest.param({"easting": float("nan")}, id="nan-easting"),
    ],
)
@pytest.mark.integration
def test_one_unstorable_record_is_dead_lettered_and_does_not_kill_the_batch(
    sink: PostgisSink, db_dsn: str, overrides: dict[str, Any]
) -> None:
    run = new_run(sink)
    good = [make_signal(f"OK-{i}", run) for i in range(3)]
    bad = make_signal("BAD-1", run, **overrides)
    counts = sink.load([good[0], bad, good[1], good[2]])

    assert (counts.inserted, counts.updated, counts.unchanged) == (3, 0, 0)
    assert counts.rejected == 1
    assert counts.staged == 3  # the reject is not a staged row
    stored = rows(db_dsn, "SELECT source_record_id FROM iris_staging.signal_ingest ORDER BY 1")
    assert stored == [("OK-0",), ("OK-1",), ("OK-2",)]
    dead = rows(
        db_dsn,
        "SELECT reason_code, source_record_id, reason_detail, harvest_run_id "
        "FROM iris_staging.signal_ingest_reject",
    )
    assert len(dead) == 1
    code, record_id, detail, rejected_run = dead[0]
    assert (code, record_id, rejected_run) == ("schema_validation_failed", "BAD-1", run)
    assert detail.startswith("not storable in PostgreSQL: ")
    assert detail.isascii()  # describing a hostile key must not itself be hostile


@pytest.mark.integration
def test_a_batch_of_only_unstorable_records_still_writes_the_rejects(
    sink: PostgisSink, db_dsn: str
) -> None:
    run = new_run(sink)
    counts = sink.load([make_signal("BAD-1", run, title="x\ud800")])
    assert (counts.inserted, counts.updated, counts.unchanged, counts.rejected) == (0, 0, 0, 1)
    assert rows(db_dsn, "SELECT count(*) FROM iris_staging.signal_ingest") == [(0,)]
    assert rows(db_dsn, "SELECT count(*) FROM iris_staging.signal_ingest_reject") == [(1,)]


@pytest.mark.integration
def test_unstorable_record_rejected_on_every_run_is_one_row(sink: PostgisSink, db_dsn: str) -> None:
    """Composes the two fixes: the sink's own rejects are de-duplicated like any other."""
    for _ in range(3):
        run = new_run(sink)
        sink.load([make_signal("BAD-1", run, raw_payload={"x": float("nan")})])
    assert rows(
        db_dsn, "SELECT count(*), max(seen_count) FROM iris_staging.signal_ingest_reject"
    ) == [(1, 3)]


@pytest.mark.integration
def test_dead_letter_evidence_with_nan_or_surrogates_is_stored_not_dropped(
    sink: PostgisSink, db_dsn: str
) -> None:
    """A ``geo_non_finite`` reject carries the NaN that caused it; it must be storable."""
    run = new_run(sink)
    payload = json.loads(HOSTILE_JSON)
    written = sink.write_rejects(
        [
            RejectedRecord(
                harvest_run_id=run,
                source_system="oparl",
                source_body_id="b\ud800",
                source_record_id="P-NAN",
                reason_code=RejectCode.GEO_NON_FINITE,
                reason_detail="x=nan \udc00 y=\x00",
                raw_payload=payload,
            )
        ]
    )
    assert written == 1
    stored = rows(
        db_dsn,
        "SELECT raw_payload, reason_detail, source_body_id FROM iris_staging.signal_ingest_reject",
    )[0]
    raw, detail, body = stored
    assert raw["nan"] == "NaN"
    assert raw["inf"] == "Infinity"
    assert raw["neg"] == "-Infinity"
    assert raw["nested"]["items"] == [1.5, "NaN", "ok"]
    assert raw["lone"] == "�"
    assert detail == "x=nan � y="
    assert body == "b�"


@pytest.mark.integration
def test_finish_run_notes_with_hostile_values_do_not_fail(sink: PostgisSink, db_dsn: str) -> None:
    run = new_run(sink)
    sink.finish_run(
        run,
        status="partial",
        records_fetched=1,
        counts=LoadCounts(0, 0, 0),
        rejected=0,
        notes={"notes": ["bad \ud800", float("nan")], "stop_reasons": {"b\udc00": "exhausted"}},
    )
    (notes,) = rows(db_dsn, "SELECT notes FROM iris_staging.harvest_run WHERE id = %s", (run,))[0]
    assert notes["notes"] == ["bad �", "NaN"]


# ---------------------------------------------------------------------------
# the dead-letter table does not grow on every run (final-review I-10)
# ---------------------------------------------------------------------------


def a_reject(run: uuid.UUID, **overrides: Any) -> RejectedRecord:
    fields: dict[str, Any] = {
        "harvest_run_id": run,
        "source_system": "oparl",
        "source_body_id": "b1",
        "source_record_id": "P-BAD",
        "reason_code": RejectCode.GEO_OUTSIDE_NATIONAL_ENVELOPE,
        "reason_detail": "easting=1.0 northing=2.0 outside envelope",
        "raw_payload": {"id": "P-BAD", "location": {"x": 1}},
    }
    fields.update(overrides)
    return RejectedRecord(**fields)


@pytest.mark.integration
def test_the_same_bad_record_over_three_runs_is_one_row(sink: PostgisSink, db_dsn: str) -> None:
    """The reproduction from the review: 1 bad record, 3 runs, used to be 3 identical rows."""
    runs = [new_run(sink) for _ in range(3)]
    for run in runs:
        assert sink.write_rejects([a_reject(run)]) == 1

    found = rows(
        db_dsn,
        "SELECT count(*), max(seen_count), min(harvest_run_id::text), "
        "bool_and(last_seen_at > rejected_at) FROM iris_staging.signal_ingest_reject",
    )
    assert found == [(1, 3, str(runs[0]), True)]  # the row keeps the run that FIRST rejected it


@pytest.mark.integration
def test_changed_payload_or_changed_reason_is_a_new_rejection(
    sink: PostgisSink, db_dsn: str
) -> None:
    run = new_run(sink)
    sink.write_rejects([a_reject(run)])
    sink.write_rejects([a_reject(run, raw_payload={"id": "P-BAD", "location": {"x": 2}})])
    sink.write_rejects([a_reject(run, reason_code=RejectCode.DATE_OUT_OF_RANGE)])
    sink.write_rejects([a_reject(run, source_record_id="P-OTHER")])
    sink.write_rejects([a_reject(run, source_body_id="b2")])
    assert rows(
        db_dsn, "SELECT count(*), max(seen_count) FROM iris_staging.signal_ingest_reject"
    ) == [(5, 1)]


@pytest.mark.integration
def test_rejects_without_ids_are_deduplicated_too(sink: PostgisSink, db_dsn: str) -> None:
    """NULL ids must not defeat the unique key (a plain UNIQUE treats NULLs as distinct)."""
    for _ in range(2):
        run = new_run(sink)
        sink.write_rejects(
            [
                a_reject(
                    run,
                    source_body_id=None,
                    source_record_id=None,
                    reason_code=RejectCode.MISSING_REQUIRED_FIELD,
                    raw_payload={"modified": SENTINEL},
                )
            ]
        )
    assert rows(
        db_dsn, "SELECT count(*), max(seen_count) FROM iris_staging.signal_ingest_reject"
    ) == [(1, 2)]


@pytest.mark.integration
def test_repeats_inside_one_batch_count_as_sightings_and_do_not_crash(
    sink: PostgisSink, db_dsn: str
) -> None:
    run = new_run(sink)
    reordered = a_reject(run, raw_payload={"location": {"x": 1}, "id": "P-BAD"})
    assert sink.write_rejects([a_reject(run), reordered, a_reject(run)]) == 3
    assert rows(
        db_dsn, "SELECT count(*), max(seen_count) FROM iris_staging.signal_ingest_reject"
    ) == [(1, 3)]


@pytest.mark.integration
def test_rejects_are_still_scoped_for_the_first_sighting_histogram(
    sink: PostgisSink, db_dsn: str
) -> None:
    """Query 7 keeps working: it sees what a run FIRST rejected."""
    first, second = new_run(sink), new_run(sink)
    sink.write_rejects([a_reject(first), a_reject(first, source_record_id="P-2")])
    sink.write_rejects([a_reject(second), a_reject(second, source_record_id="P-3")])
    q7 = validation_queries()["7"].replace(":'run_id'", "%s")
    assert {r[0]: r[1] for r in rows(db_dsn, q7, (first,))} == {"geo_outside_national_envelope": 2}
    assert {r[0]: r[1] for r in rows(db_dsn, q7, (second,))} == {"geo_outside_national_envelope": 1}


@pytest.fixture
def scratch_dsn() -> Iterator[str]:
    """An empty throwaway database, for tests that control which migrations are applied."""
    base = os.environ.get("IRIS_DB_DSN")
    if not base:
        pytest.skip("IRIS_DB_DSN is not set; skipping database tests")
    name = f"iris_scratch_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(base, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        yield make_conninfo(base, dbname=name)
    finally:
        with psycopg.connect(base, autocommit=True) as admin:
            admin.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
            )


@pytest.mark.integration
def test_migration_002_collapses_duplicates_that_already_exist(
    scratch_dsn: str, tmp_path: Path
) -> None:
    """A database that already holds the 001-era duplicates upgrades cleanly."""
    only_001 = tmp_path / "m"
    only_001.mkdir()
    (only_001 / "001_initial.sql").write_text(
        default_migrations_dir().joinpath("001_initial.sql").read_text()
    )
    assert apply_migrations(scratch_dsn, only_001) == ["001_initial"]
    run = uuid.uuid4()
    with psycopg.connect(scratch_dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO iris_staging.harvest_run (id, source_system) VALUES (%s, 'oparl')", (run,)
        )
        insert = (
            "INSERT INTO iris_staging.signal_ingest_reject (harvest_run_id, source_system, "
            "source_body_id, source_record_id, reason_code, reason_detail, raw_payload, "
            "rejected_at) "
            "VALUES (%s, 'oparl', %s, %s, %s, 'd', %s::jsonb, now() - %s * interval '1 day')"
        )
        for age in (3, 2, 1):  # three identical sightings, one per "run"
            conn.execute(insert, (run, "b1", "P-BAD", "date_out_of_range", '{"a": 1, "b": 2}', age))
        conn.execute(insert, (run, "b1", "P-BAD", "date_out_of_range", '{"b": 2, "a": 1}', 0))
        conn.execute(insert, (run, "b1", "P-OTHER", "date_out_of_range", '{"a": 1}', 0))
        conn.execute(insert, (run, None, None, "date_out_of_range", '{"a": 1}', 0))
        conn.execute(insert, (run, None, None, "date_out_of_range", '{"a": 1}', 0))

    assert apply_migrations(scratch_dsn) == ["002_reject_dedup"]
    found = rows(
        scratch_dsn,
        "SELECT source_body_id, source_record_id, seen_count, rejected_at = last_seen_at "
        "FROM iris_staging.signal_ingest_reject ORDER BY source_record_id NULLS FIRST",
    )
    assert found == [(None, None, 2, False), ("b1", "P-BAD", 4, False), ("b1", "P-OTHER", 1, True)]
    assert apply_migrations(scratch_dsn) == []


@pytest.mark.integration
def test_record_verdict_is_merged_into_notes_without_losing_finish_run_keys(
    sink: PostgisSink, db_dsn: str
) -> None:
    run = new_run(sink)
    sink.finish_run(
        run,
        status="partial",
        records_fetched=3,
        counts=LoadCounts(1, 0, 0),
        rejected=0,
        notes={"degraded_bodies": ["b1"], "stop_reasons": {"b1": "server_error"}},
    )
    sink.record_verdict(run, "FAIL", [{"level": "FAIL", "rule": "degraded_body_rate_max"}])
    (notes,) = rows(db_dsn, "SELECT notes FROM iris_staging.harvest_run WHERE id = %s", (run,))[0]
    assert notes["degraded_bodies"] == ["b1"]
    assert notes["quality"]["verdict"] == "FAIL"
    assert notes["quality"]["findings"] == [{"level": "FAIL", "rule": "degraded_body_rate_max"}]
