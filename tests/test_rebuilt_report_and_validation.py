"""``iris-harvest report`` / ``validate`` against a real database.

Covers final-review I-5 (a rebuilt report regraded a failed run as PASS and graded an
idempotent re-run on zero rows) and I-14 (validation that could pass vacuously, and
queries that were guaranteed by the DDL). The database fixtures and signal builders are
shared with ``test_sink.py``; the tests skip cleanly without ``IRIS_DB_DSN``.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import psycopg
import pytest

from iris_signal_harvester.domain.geo.validate import E_RANGE, N_RANGE
from iris_signal_harvester.reporting.quality import (
    Thresholds,
    Verdict,
    build_report,
    observations_from_db,
)
from iris_signal_harvester.reporting.validation import (
    EXPECTED_DATA_QUALITY,
    EXPECTED_INVARIANTS,
    CheckKind,
    parse_validation_sql,
    run_validation,
)
from iris_signal_harvester.sink.postgis import LoadCounts, PostgisSink
from tests.test_sink import REPO, fixture_set, make_signal, new_run, rows

SQL_FILE = REPO / "sql" / "validation_queries.sql"
THRESHOLDS = Thresholds.load(REPO / "config" / "thresholds.yaml")


def finish(
    sink: PostgisSink,
    run: uuid.UUID,
    counts: LoadCounts,
    *,
    status: str = "succeeded",
    fetched: int | None = None,
    rejected: int = 0,
    stops: dict[str, str] | None = None,
    degraded: tuple[str, ...] = (),
) -> None:
    sink.finish_run(
        run,
        status=status,
        records_fetched=counts.staged if fetched is None else fetched,
        counts=counts,
        rejected=rejected,
        notes={
            "bodies": len(stops or {}),
            "degraded_bodies": list(degraded),
            "stop_reasons": stops if stops is not None else {"body-1": "exhausted"},
        },
    )


def rebuilt(dsn: str, run: uuid.UUID, **summary_overrides: Any) -> Any:
    summary, obs, bodies = observations_from_db(dsn, "iris_staging", run)
    if summary_overrides:
        from dataclasses import replace

        summary = replace(summary, **summary_overrides)
    return build_report(summary, obs, bodies, THRESHOLDS)


# ---------------------------------------------------------------------------
# I-5: report
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_a_degraded_run_is_not_regraded_pass_by_report(sink: PostgisSink, db_dsn: str) -> None:
    """Reproduction: a server_error run graded FAIL, then `report` printed PASS."""
    run = new_run(sink)
    finish(
        sink,
        run,
        LoadCounts(0, 0, 0),
        status="partial",
        fetched=0,
        stops={"body-1": "server_error"},
        degraded=("body-1",),
    )
    report = rebuilt(db_dsn, run)
    assert report.verdict is Verdict.FAIL
    assert [b.degraded for b in report.bodies] == [True]
    assert report.bodies[0].stop_reason == "server_error"
    assert any(f.rule == "degraded_body_rate_max" for f in report.findings)


@pytest.mark.integration
def test_a_failed_run_stays_failed(sink: PostgisSink, db_dsn: str) -> None:
    run = new_run(sink)
    finish(sink, run, LoadCounts(0, 0, 0), status="failed", fetched=0)
    assert rebuilt(db_dsn, run).verdict is Verdict.FAIL


@pytest.mark.integration
def test_an_idempotent_rerun_is_graded_on_what_it_saw_not_on_zero_rows(
    sink: PostgisSink, db_dsn: str
) -> None:
    """Reproduction: after two happy runs `report` said "fetched 0, staged 0" and PASS."""
    first = new_run(sink)
    signals = fixture_set(first, 4)
    counts1 = sink.load(signals)
    n = len(signals)
    finish(sink, first, counts1)
    second = new_run(sink)
    counts2 = sink.load([s.model_copy(update={"harvest_run_id": second}) for s in signals])
    finish(sink, second, counts2)
    assert (counts2.inserted, counts2.updated, counts2.unchanged) == (0, 0, n)
    # the premise of the bug: the re-run owns no rows
    assert rows(
        db_dsn,
        "SELECT count(*) FROM iris_staging.signal_ingest WHERE harvest_run_id = %s",
        (second,),
    ) == [(0,)]

    report = rebuilt(db_dsn, second)
    assert report.observations.fetched == n
    assert report.observations.staged == n  # re-observed rows, found through last_seen_at
    assert report.summary.population_complete is True
    assert report.verdict is Verdict.PASS
    assert report.summary.rebuilt is True
    assert "Rebuilt from stored rows" in report.to_markdown()


@pytest.mark.integration
def test_an_older_run_whose_rows_were_reseen_since_cannot_be_graded(
    sink: PostgisSink, db_dsn: str
) -> None:
    first = new_run(sink)
    signals = fixture_set(first, 4)
    finish(sink, first, sink.load(signals))
    second = new_run(sink)
    finish(
        sink, second, sink.load([s.model_copy(update={"harvest_run_id": second}) for s in signals])
    )

    report = rebuilt(db_dsn, first)
    assert report.summary.population_complete is False
    assert report.observations.staged == 0 and report.observations.fetched == len(signals)
    assert report.verdict is Verdict.UNKNOWN
    assert report.exit_code == 3


@pytest.mark.integration
def test_a_run_that_stored_no_stop_reasons_is_unknown_because_degradation_is_unknowable(
    sink: PostgisSink, db_dsn: str
) -> None:
    run = new_run(sink)
    counts = sink.load(fixture_set(run, 2))
    sink.finish_run(run, status="succeeded", records_fetched=3, counts=counts, rejected=0, notes={})
    assert rebuilt(db_dsn, run).verdict is Verdict.UNKNOWN


@pytest.mark.integration
def test_the_verdict_the_run_recorded_is_carried(sink: PostgisSink, db_dsn: str) -> None:
    run = new_run(sink)
    counts = sink.load(fixture_set(run, 3))
    finish(sink, run, counts)
    sink.record_verdict(run, "WARN", [{"level": "WARN", "rule": "unknown_date_rate_max"}])
    report = rebuilt(db_dsn, run)
    assert report.verdict is Verdict.WARN
    assert report.summary.carried_verdict == "WARN"


@pytest.mark.integration
def test_unmapped_document_types_survive_the_rebuild(sink: PostgisSink, db_dsn: str) -> None:
    run = new_run(sink)
    signals = [
        make_signal(
            f"U-{i}",
            run,
            document_type="unmapped",
            raw_payload={"paperType": f" Sonder{i % 2} "},
        )
        for i in range(3)
    ]
    signals.append(make_signal("M-0", run))
    finish(sink, run, sink.load(signals))
    report = rebuilt(db_dsn, run)
    assert (report.document_types.typed, report.document_types.unmapped) == (4, 3)
    assert dict(report.document_types.top_unmapped_raw) == {"Sonder0": 2, "Sonder1": 1}


# ---------------------------------------------------------------------------
# I-14: validation cannot pass vacuously
# ---------------------------------------------------------------------------


def test_the_shipped_file_contains_every_expected_check() -> None:
    queries = parse_validation_sql(SQL_FILE)
    by_kind: dict[CheckKind | None, set[str]] = {}
    for q in queries:
        by_kind.setdefault(q.kind, set()).add(q.key)
    assert by_kind[CheckKind.INVARIANT] == set(EXPECTED_INVARIANTS) == {"1", "2", "3", "4", "10"}
    assert by_kind[CheckKind.DATA_QUALITY] == set(EXPECTED_DATA_QUALITY) == {"11", "12", "15"}
    for q in queries:
        if q.kind is not None:
            assert q.statement and not q.needs_run_id, f"check {q.key} could not be run"


def test_query_3_is_pinned_to_the_python_crs_domain() -> None:
    """The SQL rectangle and the Python gate it mirrors cannot drift apart silently."""
    statement = {q.key: q.statement for q in parse_validation_sql(SQL_FILE)}["3"]
    assert f"BETWEEN {E_RANGE[0]:.0f} AND {E_RANGE[1]:.0f}" in statement
    assert f"BETWEEN {N_RANGE[0]:.0f} AND {N_RANGE[1]:.0f}" in statement
    header = SQL_FILE.read_text(encoding="utf-8")
    assert "COARSE BOUNDING CHECK" in header


@pytest.mark.integration
def test_validation_passes_on_clean_data_and_reports_how_many_checks_ran(
    sink: PostgisSink, db_dsn: str
) -> None:
    run = new_run(sink)
    sink.load(fixture_set(run))
    outcome = run_validation(db_dsn, SQL_FILE)
    assert outcome.ok, outcome.problems + [c.violation or "" for c in outcome.checks]
    assert outcome.checked == len(EXPECTED_INVARIANTS) + len(EXPECTED_DATA_QUALITY)
    assert outcome.violations == 0


@pytest.mark.integration
def test_an_empty_database_does_not_pass(sink: PostgisSink, db_dsn: str) -> None:
    outcome = run_validation(db_dsn, SQL_FILE)
    assert not outcome.ok and outcome.exit_code == 1
    assert any("signal_ingest is empty" in p for p in outcome.problems)
    assert run_validation(db_dsn, SQL_FILE, require_data=False).ok


@pytest.mark.integration
def test_a_file_with_no_checks_fails_instead_of_checking_zero_invariants(
    sink: PostgisSink, db_dsn: str, tmp_path: Path
) -> None:
    run = new_run(sink)
    sink.load(fixture_set(run))
    empty = tmp_path / "empty.sql"
    empty.write_text("-- nothing here\n", encoding="utf-8")
    outcome = run_validation(db_dsn, empty)
    assert outcome.checked == 0 and not outcome.ok
    assert any("expected 5 invariant(s)" in p and "found 0" in p for p in outcome.problems)
    assert any("expected 3 data-quality check(s)" in p for p in outcome.problems)


@pytest.mark.integration
def test_a_deleted_marker_is_detected_not_skipped(
    sink: PostgisSink, db_dsn: str, tmp_path: Path
) -> None:
    run = new_run(sink)
    sink.load(fixture_set(run))
    edited = tmp_path / "edited.sql"
    edited.write_text(
        SQL_FILE.read_text(encoding="utf-8").replace(
            "-- 2. Every geometry is stored in EPSG:25832.  MUST return zero rows.",
            "-- 2. Every geometry is stored in EPSG:25832.",
        ),
        encoding="utf-8",
    )
    outcome = run_validation(db_dsn, edited)
    assert not outcome.ok
    assert any("missing: 2" in p for p in outcome.problems)


@pytest.mark.integration
def test_a_check_that_cannot_run_is_a_problem_not_a_pass(
    sink: PostgisSink, db_dsn: str, tmp_path: Path
) -> None:
    run = new_run(sink)
    sink.load(fixture_set(run))
    broken = tmp_path / "broken.sql"
    broken.write_text(
        SQL_FILE.read_text(encoding="utf-8").replace(
            "FROM iris_staging.signal_ingest\nGROUP BY 1, 2, 3",
            "FROM iris_staging.no_such_table\nGROUP BY 1, 2, 3",
        ),
        encoding="utf-8",
    )
    outcome = run_validation(db_dsn, broken)
    assert not outcome.ok
    assert any(p.startswith("query 1 raised") for p in outcome.problems)


@pytest.mark.integration
def test_expect_idempotent_runs_queries_6_and_6b(sink: PostgisSink, db_dsn: str) -> None:
    first = new_run(sink)
    signals = fixture_set(first)
    sink.load(signals)
    second = new_run(sink)
    sink.load([s.model_copy(update={"harvest_run_id": second}) for s in signals])
    ok = run_validation(db_dsn, SQL_FILE, expect_idempotent=True)
    assert ok.ok and {c.key for c in ok.checks if c.kind is CheckKind.IDEMPOTENCY} == {"6", "6b"}

    third = new_run(sink)
    changed = [make_signal(f"P-{i:03d}", third, title="a changed title") for i in range(5)]
    sink.load(changed)
    bad = run_validation(db_dsn, SQL_FILE, expect_idempotent=True)
    assert not bad.ok
    assert any(c.key == "6" and c.violation for c in bad.checks)


def _insert_raw_signal(dsn: str, run: uuid.UUID, record: str, **columns: Any) -> None:
    values: dict[str, Any] = {
        "harvest_run_id": run,
        "source_system": "fixture",
        "source_body_id": "body-1",
        "source_record_id": record,
        "source_url": f"https://example.org/papers/{record}",
        "title": "t",
        "taxonomy_version": "v",
        "raw_payload": "{}",
        "content_hash": "a" * 64,
    }
    values.update(columns)
    names = ", ".join(values)
    marks = ", ".join(["%s"] * len(values))
    with psycopg.connect(dsn) as conn:
        conn.execute(
            f"INSERT INTO iris_staging.signal_ingest ({names}) VALUES ({marks})",
            tuple(values.values()),
        )


@pytest.mark.integration
def test_the_data_quality_checks_can_actually_fail(sink: PostgisSink, db_dsn: str) -> None:
    """Unlike queries 1, 2, 4 and 10, the schema cannot stop these."""
    run = new_run(sink)
    sink.load(fixture_set(run, 2))
    assert run_validation(db_dsn, SQL_FILE).ok

    # 11: the same object under two natural keys (a renumbered paper)
    _insert_raw_signal(db_dsn, run, "P-RENUMBERED", source_url="https://example.org/papers/P-000")
    # 12: misshapen administrative keys
    _insert_raw_signal(db_dsn, run, "P-BAD-AGS", ags="0916200X")
    _insert_raw_signal(db_dsn, run, "P-BAD-ARS", ars="123")
    # 15: a run that never finished
    with psycopg.connect(db_dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO iris_staging.harvest_run (id, source_system, started_at) "
            "VALUES (%s, 'fixture', now() - interval '3 days')",
            (uuid.uuid4(),),
        )

    outcome = run_validation(db_dsn, SQL_FILE)
    failed = {c.key for c in outcome.checks if not c.ok}
    assert failed == {"11", "12", "15"}
    assert {c.kind for c in outcome.checks if not c.ok} == {CheckKind.DATA_QUALITY}
    assert outcome.exit_code == 1


@pytest.mark.integration
def test_query_3_flags_points_outside_the_crs_domain_and_not_inside_it(
    sink: PostgisSink, db_dsn: str
) -> None:
    run = new_run(sink)
    sink.load(
        [
            # inside the Python domain but outside the old 280000..930000 rectangle: the old
            # query flagged valid data here
            make_signal("EDGE", run, easting=270_000.0, northing=5_300_000.0),
            make_signal("OK", run),
        ]
    )
    assert run_validation(db_dsn, SQL_FILE).ok
    far = make_signal("FAR", run, easting=100_000.0, northing=5_300_000.0)
    sink.load([far])
    outcome = run_validation(db_dsn, SQL_FILE)
    assert {c.key for c in outcome.checks if not c.ok} == {"3"}


@pytest.mark.integration
def test_the_new_informational_queries_run(sink: PostgisSink, db_dsn: str) -> None:
    run = new_run(sink)
    sink.load(fixture_set(run, 2))
    finish(sink, run, LoadCounts(2, 0, 0), rejected=1)
    queries = {q.key: q.statement for q in parse_validation_sql(SQL_FILE)}
    for key in ("13", "14", "16"):
        rows(db_dsn, queries[key])  # must not raise
    (unmapped, typed, total) = rows(db_dsn, queries["16"])[0]
    assert (unmapped, total) == (0, 3) and typed == 3
    ((_, _, status, fetched, rejected, pct),) = rows(db_dsn, queries["14"])
    assert (status, rejected) == ("succeeded", 1) and fetched == 2 and float(pct) == 50.0
