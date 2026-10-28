"""The ``iris-harvest`` command line. Offline except where marked ``integration``.

The property under test throughout is that the *exit code is the verdict*: a
harvester whose process status says "fine" while its report says FAIL is worse than
one that simply crashes, because CI would believe it.

Every test clears ``IRIS_DB_DSN`` from the environment unless it means to use a
database, so a developer with a DSN exported cannot accidentally make the offline
suite touch PostgreSQL.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo
from typer.testing import CliRunner

from iris_signal_harvester.cli import app
from iris_signal_harvester.reporting.validation import CheckKind, parse_validation_sql
from iris_signal_harvester.sink.migrate import apply_migrations

REPO = Path(__file__).resolve().parents[1]
runner = CliRunner()


@pytest.fixture(autouse=True)
def _in_repo_without_a_dsn(monkeypatch: pytest.MonkeyPatch) -> None:
    """Config paths in ``Settings`` are repo-relative, so run from the repo root."""
    monkeypatch.chdir(REPO)
    monkeypatch.delenv("IRIS_DB_DSN", raising=False)


def env(report_dir: Path, **extra: str) -> dict[str, str]:
    return {"IRIS_REPORT_DIR": str(report_dir), **extra}


# ---------------------------------------------------------------------------
# harvest --dry-run
# ---------------------------------------------------------------------------


def test_dry_run_writes_a_report_without_a_database(tmp_path: Path) -> None:
    result = runner.invoke(app, ["harvest", "--dry-run"], env=env(tmp_path))
    assert result.exit_code == 0, result.output
    assert "verdict    PASS" in result.output
    markdown = tmp_path / "quality_latest.md"
    payload = tmp_path / "quality_latest.json"
    assert markdown.is_file() and payload.is_file()

    data = json.loads(payload.read_text(encoding="utf-8"))
    assert data["run"]["dry_run"] is True
    assert data["load"] == {
        "inserted": 0,
        "updated": 0,
        "unchanged": 0,
        "duplicates_in_batch": 0,
    }
    assert data["funnel"]["steps"][-1]["remaining"] == 4
    assert data["funnel"]["balances"] is True


def test_harvest_without_a_dsn_exits_two_before_touching_anything(tmp_path: Path) -> None:
    result = runner.invoke(app, ["harvest"], env=env(tmp_path))
    assert result.exit_code == 2
    assert "IRIS_DB_DSN is not set" in result.output
    assert not list(tmp_path.iterdir())


def test_a_degraded_cassette_exits_nonzero(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["harvest", "--dry-run", "--scenario", "coordinate_out_of_domain"],
        env=env(tmp_path),
    )
    assert result.exit_code == 1
    assert "FAIL" in result.output
    assert "geo_out_of_crs_domain" in (tmp_path / "quality_latest.md").read_text(encoding="utf-8")


def test_a_warn_exits_zero_but_fails_under_strict(tmp_path: Path) -> None:
    lenient = runner.invoke(
        app,
        ["harvest", "--dry-run", "--scenario", "missing_publication_date"],
        env=env(tmp_path),
    )
    assert lenient.exit_code == 0
    assert "verdict    WARN" in lenient.output

    strict = runner.invoke(
        app,
        ["harvest", "--dry-run", "--strict", "--scenario", "missing_publication_date"],
        env=env(tmp_path),
    )
    assert strict.exit_code == 1
    assert "verdict    FAIL" in strict.output


def test_an_unavailable_source_is_degraded_and_reported(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["harvest", "--dry-run", "--scenario", "server_error_500"],
        env=env(tmp_path),
    )
    assert result.exit_code == 1
    assert "status     partial" in result.output
    assert "source_unavailable" in (tmp_path / "quality_latest.md").read_text(encoding="utf-8")


def test_limit_and_body_filters_reach_the_sweep(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "harvest",
            "--dry-run",
            "--limit",
            "1",
            "--body",
            "https://oparl.example.org/oparl/v1.0/body/1",
        ],
        env=env(tmp_path),
    )
    assert result.exit_code in (0, 1)
    data = json.loads((tmp_path / "quality_latest.json").read_text(encoding="utf-8"))
    assert data["run"]["limit"] == 1
    assert data["run"]["bodies_total"] == 1
    assert data["funnel"]["fetched"] == 1


def test_since_is_parsed_and_recorded(tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["harvest", "--dry-run", "--since", "2026-01-01"], env=env(tmp_path)
    )
    assert result.exit_code == 0
    data = json.loads((tmp_path / "quality_latest.json").read_text(encoding="utf-8"))
    assert data["run"]["since"].startswith("2026-01-01T00:00:00")


def test_an_unknown_scenario_is_a_configuration_error(tmp_path: Path) -> None:
    result = runner.invoke(app, ["harvest", "--dry-run", "--scenario", "nope"], env=env(tmp_path))
    assert result.exit_code == 2
    assert "no cassette" in result.output


# ---------------------------------------------------------------------------
# discover
# ---------------------------------------------------------------------------


def test_discover_lists_bodies_with_their_licence(tmp_path: Path) -> None:
    result = runner.invoke(app, ["discover"], env=env(tmp_path))
    assert result.exit_code == 0
    assert "2 body/bodies" in result.output
    assert "licence=https://creativecommons.org/licenses/by/4.0/" in result.output
    assert "licence=(undeclared)" in result.output


def test_discover_warns_that_the_briefed_source_does_not_exist(tmp_path: Path) -> None:
    result = runner.invoke(app, ["discover", "--source", "ratsdokumente"], env=env(tmp_path))
    assert "verified NON-EXISTENT" in result.output


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


def test_validate_without_a_dsn_exits_two(tmp_path: Path) -> None:
    result = runner.invoke(app, ["validate"], env=env(tmp_path))
    assert result.exit_code == 2


def test_the_invariant_queries_are_the_ones_the_sql_file_marks_as_such() -> None:
    parsed = parse_validation_sql(REPO / "sql" / "validation_queries.sql")
    keys = {q.key for q in parsed}
    assert {"1", "2", "3", "4", "5", "6", "6b", "7", "8", "9", "10"} <= keys
    invariants = {q.key for q in parsed if q.kind is CheckKind.INVARIANT}
    # Exactly the structural invariants: duplicates, SRID, envelope, geo labelling,
    # watermark sanity. Situational queries (distributions, histograms) are excluded.
    assert invariants == {"1", "2", "3", "4", "10"}
    # The data-quality checks the schema cannot enforce: duplicate source_url, ags/ars
    # shape, runs stuck in `running`.
    assert {q.key for q in parsed if q.kind is CheckKind.DATA_QUALITY} == {"11", "12", "15"}
    # Query 7 is parameterised by run id; validation reports that rather than skipping it.
    statements = {q.key: q.statement for q in parsed}
    assert ":'run_id'" in statements["7"]


def test_verify_source_help_states_that_it_never_authenticates() -> None:
    result = runner.invoke(app, ["verify-source", "--help"])
    assert result.exit_code == 0
    assert "Never authenticates" in result.output.replace("\n", " ")


def test_verify_source_sends_the_configured_user_agent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """F1: a live probe must identify itself with the configured contact, not a placeholder."""
    from iris_signal_harvester import cli

    seen: dict[str, object] = {}

    class Spy(Exception):
        pass

    class SpyVerifier:
        def __init__(self, **kwargs: object) -> None:
            seen.update(kwargs)
            raise Spy

    monkeypatch.setattr(cli, "SourceVerifier", SpyVerifier)
    agent = "ProjectIris-Harvester/0.1 (+mailto:ops@iris-test.invalid)"
    result = runner.invoke(
        app,
        ["verify-source", "--output", str(tmp_path / "v.md")],
        env=env(tmp_path, IRIS_USER_AGENT=agent),
    )
    assert isinstance(result.exception, Spy)
    assert seen["user_agent"] == agent


# ---------------------------------------------------------------------------
# integration: harvest -> validate -> report, twice
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def cli_dsn() -> Iterator[str]:
    base = os.environ.get("IRIS_DB_DSN")
    if not base:
        pytest.skip("IRIS_DB_DSN is not set; skipping database tests")
    name = f"iris_cli_{uuid.uuid4().hex[:12]}"
    try:
        with psycopg.connect(base, autocommit=True) as admin:
            admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    except psycopg.Error as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"cannot create a throwaway database: {exc}")
    dsn = make_conninfo(base, dbname=name)
    try:
        apply_migrations(dsn)
        yield dsn
    finally:
        with psycopg.connect(base, autocommit=True) as admin:
            admin.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
            )


@pytest.mark.integration
def test_harvest_twice_then_validate_and_report(cli_dsn: str, tmp_path: Path) -> None:
    """The demo claim, made through the CLI exactly as `make demo` does."""
    with psycopg.connect(cli_dsn, autocommit=True) as conn:
        conn.execute(
            "TRUNCATE iris_staging.signal_ingest, iris_staging.signal_ingest_reject, "
            "iris_staging.harvest_watermark, iris_staging.harvest_run CASCADE"
        )
    environment = env(tmp_path, IRIS_DB_DSN=cli_dsn)

    first = runner.invoke(app, ["harvest"], env=environment)
    assert first.exit_code == 0, first.output
    assert "inserted=4" in first.output

    second = runner.invoke(app, ["harvest"], env=environment)
    assert second.exit_code == 0, second.output
    assert "inserted=0 updated=0 unchanged=4" in second.output

    # The invariants hold, including idempotency queries 6 and 6b.
    validated = runner.invoke(app, ["validate", "--expect-idempotent"], env=environment)
    assert validated.exit_code == 0, validated.output
    assert "0 violated" in validated.output
    # Not vacuous: 5 invariants + 3 data-quality checks (11, 12, 15) + idempotency 6 and 6b.
    assert "10 check(s) run, 0 violated" in validated.output
    for key in ("11", "12", "15"):
        assert re.search(rf"\bok\s+{key}\.", validated.output), key

    rebuilt = runner.invoke(app, ["report"], env=environment)
    assert rebuilt.exit_code == 0, rebuilt.output
    assert "inserted=0 updated=0 unchanged=4" in rebuilt.output
    text = (tmp_path / "quality_latest.md").read_text(encoding="utf-8")
    assert "Rebuilt from stored rows" in text

    # The rebuilt report must carry the real numbers, not pass on emptiness (final-review
    # I-5): the idempotent re-run fetched 4 records and re-observed 4 stored rows.
    (rebuilt_json,) = tmp_path.glob("quality_*.rebuilt.json")
    data = json.loads(rebuilt_json.read_text(encoding="utf-8"))
    assert data["run"]["rebuilt"] is True
    assert data["run"]["status"] == "succeeded"
    assert data["verdict"] == "PASS"
    assert data["funnel"]["fetched"] == 4
    staged = next(step for step in data["funnel"]["steps"] if step["label"] == "staged for load")
    assert staged["remaining"] == 4
    assert data["funnel"]["balances"] is True
    assert (data["load"]["inserted"], data["load"]["updated"], data["load"]["unchanged"]) == (
        0,
        0,
        4,
    )

    # W2: the run's own verdict was persisted by the pipeline, not left in a report file.
    with psycopg.connect(cli_dsn) as conn:
        rows = conn.execute(
            "SELECT status, notes->'quality'->>'verdict' FROM iris_staging.harvest_run "
            "ORDER BY started_at"
        ).fetchall()
    assert rows == [("succeeded", "PASS"), ("succeeded", "PASS")]


@pytest.mark.integration
def test_validate_reports_a_violation_as_a_nonzero_exit(cli_dsn: str, tmp_path: Path) -> None:
    """Query 6 only holds straight after an identical re-run; one harvest breaks it."""
    with psycopg.connect(cli_dsn, autocommit=True) as conn:
        conn.execute(
            "TRUNCATE iris_staging.signal_ingest, iris_staging.signal_ingest_reject, "
            "iris_staging.harvest_watermark, iris_staging.harvest_run CASCADE"
        )
    environment = env(tmp_path, IRIS_DB_DSN=cli_dsn)
    assert runner.invoke(app, ["harvest"], env=environment).exit_code == 0

    structural = runner.invoke(app, ["validate"], env=environment)
    assert structural.exit_code == 0, structural.output

    idempotency = runner.invoke(app, ["validate", "--expect-idempotent"], env=environment)
    assert idempotency.exit_code == 1
    assert "written by the most recent run" in idempotency.output
