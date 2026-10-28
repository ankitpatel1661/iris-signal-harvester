"""``iris-harvest`` — the operator interface.

Five commands, each answering one question a reviewer or an operator actually asks:

===============  =============================================================
command          question it answers
===============  =============================================================
`verify-source`  Does the briefed source exist, and may we read it?
`discover`       Which councils does this source expose, under what licence?
`harvest`        Run the sweep. `--dry-run` runs everything up to the sink.
`report`         Rebuild the quality report for a run that already happened.
`validate`       Do the stored rows satisfy the invariants in `sql/`?
===============  =============================================================

Exit codes come from the quality verdict, not from "did Python raise": PASS and WARN
exit 0 (WARN exits 1 under ``--strict``), FAIL exits 1, and a configuration or startup
problem exits 2. A harvester whose exit code says "fine" while its report says FAIL
would be worse than useless in CI, which is why the gate and the exit code are the
same decision.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID

import typer

from iris_signal_harvester.config import Settings
from iris_signal_harvester.domain.models import Body
from iris_signal_harvester.domain.normalise import Normaliser
from iris_signal_harvester.errors import ConfigurationError, HarvesterError
from iris_signal_harvester.pipeline import (
    Pipeline,
    SourceRegistry,
    SweepOptions,
    SweepResult,
    build_adapter,
)
from iris_signal_harvester.reporting.quality import (
    Thresholds,
    build_report,
    observations_from_db,
    write_report,
)
from iris_signal_harvester.reporting.validation import Check, run_validation
from iris_signal_harvester.reporting.verification import (
    BRIEFED_URL,
    CANDIDATE_URLS,
    CONTROL_HOSTS,
    SourceVerifier,
    whois_via_cli,
    write_verification,
)

__all__ = ["app"]

EXIT_CONFIG = 2

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Federated harvester for German municipal council documents (IRIS-SIG-23).",
)


# ---------------------------------------------------------------------------
# shared plumbing
# ---------------------------------------------------------------------------


def _settings(**overrides: Any) -> Settings:
    """Settings from the environment, with explicit CLI overrides applied on top."""
    base = Settings()
    clean = {k: v for k, v in overrides.items() if v is not None}
    return base.model_copy(update=clean) if clean else base


def _fail(message: str, code: int = EXIT_CONFIG) -> None:
    typer.secho(message, fg=typer.colors.RED, err=True)
    raise typer.Exit(code)


def _aware(moment: datetime | None) -> datetime | None:
    if moment is None:
        return None
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment


def _dsn(settings: Settings) -> str:
    try:
        return settings.require_dsn()
    except ConfigurationError as exc:
        _fail(str(exc))
        raise  # pragma: no cover - _fail always raises


# ---------------------------------------------------------------------------
# verify-source
# ---------------------------------------------------------------------------


@app.command("verify-source")
def verify_source(
    output: Annotated[
        Path | None,
        typer.Option(
            "--output",
            "-o",
            help="Where to write the document. Defaults to "
            "<report_dir>/SOURCE_VERIFICATION.md so the committed evidence artefact is "
            "never overwritten by accident.",
        ),
    ] = None,
    briefed_url: Annotated[str, typer.Option(help="The briefed endpoint to verify.")] = BRIEFED_URL,
    candidate: Annotated[
        list[str] | None,
        typer.Option("--candidate", help="Alternative endpoint to probe. Repeatable."),
    ] = None,
    whois: Annotated[
        bool, typer.Option(help="Include registry (whois) evidence where available.")
    ] = True,
) -> None:
    """Probe the briefed source read-only and write SOURCE_VERIFICATION.md.

    Never authenticates and never retries around a refusal: a 401, 403 or
    robots.txt disallow is recorded and stops all further probing of that host.
    """
    settings = _settings()
    verifier = SourceVerifier(
        user_agent=settings.user_agent, whois=whois_via_cli if whois else None
    )
    try:
        report = asyncio.run(
            verifier.probe(
                briefed_url=briefed_url,
                candidate_urls=tuple(candidate or ()) or CANDIDATE_URLS,
                control_hosts=CONTROL_HOSTS,
            )
        )
    except HarvesterError as exc:  # pragma: no cover - probe is deliberately tolerant
        _fail(f"probe failed: {exc}")
        return
    target = output or (settings.report_dir / "SOURCE_VERIFICATION.md")
    written = write_verification(report, target)
    typer.echo(
        f"briefed host {report.briefed_host}: "
        f"{'resolves' if report.briefed_exists else 'does NOT resolve'}"
    )
    typer.echo(f"controls healthy: {report.controls_healthy}")
    typer.echo(f"live candidates: {len(report.live_candidates)} of {len(report.candidates)}")
    if report.refusals:
        typer.echo(f"access refused by (not retried): {', '.join(report.refusals)}")
    typer.echo(f"wrote {written}")


# ---------------------------------------------------------------------------
# discover
# ---------------------------------------------------------------------------


@app.command()
def discover(
    source: Annotated[str | None, typer.Option(help="Source name from config/bodies.yaml.")] = None,
    scenario: Annotated[
        str | None, typer.Option(help="Cassette name for the fixture source.")
    ] = None,
    system_url: Annotated[str | None, typer.Option(help="Override the OParl System URL.")] = None,
) -> None:
    """List the councils a source exposes, with their declared licence."""
    settings = _settings(source=source)
    try:
        registry = SourceRegistry.load(settings.bodies_path)
        entry = registry.entry(source or settings.source)
        adapter = build_adapter(
            settings,
            registry=registry,
            source=source,
            scenario=scenario,
            system_url=system_url,
        )
    except ConfigurationError as exc:
        _fail(str(exc))
        return

    typer.echo(f"registry {settings.bodies_path} (version {registry.version})")
    typer.echo(entry.summary)
    if entry.health == "nonexistent":
        typer.secho(
            "this source was verified NON-EXISTENT; see SOURCE_VERIFICATION.md",
            fg=typer.colors.YELLOW,
        )

    async def run() -> tuple[Body, ...]:
        try:
            pipeline = Pipeline.from_settings(settings, write=False)
            return await pipeline.discover(adapter)
        finally:
            await adapter.aclose()

    try:
        bodies = asyncio.run(run())
    except HarvesterError as exc:
        _fail(f"discovery failed: {type(exc).__name__}: {exc}", code=1)
        return
    typer.echo(f"{len(bodies)} body/bodies")
    for body in bodies:
        typer.echo(
            f"  {body.id}  name={body.name or '(none)'}  ags={body.ags or '(none)'}  "
            f"licence={body.licence or '(undeclared)'}"
        )


# ---------------------------------------------------------------------------
# harvest
# ---------------------------------------------------------------------------


@app.command()
def harvest(
    since: Annotated[
        datetime | None,
        typer.Option(
            formats=["%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"],
            help="Only records modified at or after this instant. Default: the body's "
            "watermark minus the configured overlap.",
        ),
    ] = None,
    limit: Annotated[int | None, typer.Option(help="Stop after this many fetched records.")] = None,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Run everything up to the sink and write a report; no database.",
        ),
    ] = False,
    strict: Annotated[
        bool,
        typer.Option("--strict", help="CI mode: any record-level rejection fails the run."),
    ] = False,
    source: Annotated[str | None, typer.Option(help="Source name from config/bodies.yaml.")] = None,
    scenario: Annotated[
        str | None, typer.Option(help="Cassette name for the fixture source.")
    ] = None,
    system_url: Annotated[str | None, typer.Option(help="Override the OParl System URL.")] = None,
    body: Annotated[
        list[str] | None,
        typer.Option("--body", help="Restrict to these body ids. Repeatable."),
    ] = None,
    report_dir: Annotated[
        Path | None, typer.Option(help="Where to write the quality report.")
    ] = None,
    gazetteer: Annotated[
        Path | None, typer.Option(help="Gazetteer JSON for the geo tier-4 lookup.")
    ] = None,
) -> None:
    """Sweep a source, load the signals and write the quality report.

    The exit code is the report's verdict: PASS/WARN 0, FAIL 1 (WARN is 1 under
    ``--strict``). Configuration problems exit 2.
    """
    settings = _settings(source=source, strict=strict or None, report_dir=report_dir)
    if not dry_run:
        _dsn(settings)
    try:
        normaliser = Normaliser.from_settings(settings, gazetteer_path=gazetteer)
        thresholds = Thresholds.load(settings.thresholds_path)
        adapter = build_adapter(settings, source=source, scenario=scenario, system_url=system_url)
        pipeline = Pipeline.from_settings(
            settings, normaliser=normaliser, thresholds=thresholds, write=not dry_run
        )
    except ConfigurationError as exc:
        _fail(str(exc))
        return

    options = SweepOptions(
        since=_aware(since),
        limit=limit,
        dry_run=dry_run,
        strict=strict,
        bodies=tuple(body or ()),
    )

    async def run() -> SweepResult:
        try:
            return await pipeline.sweep(adapter, options)
        finally:
            await adapter.aclose()

    try:
        result = asyncio.run(run())
    except HarvesterError as exc:
        _fail(f"harvest failed before any body was swept: {type(exc).__name__}: {exc}", code=1)
        return

    markdown, payload = write_report(result.report, settings.report_dir)
    _echo_result(result, markdown, payload)
    raise typer.Exit(result.exit_code)


def _echo_result(result: SweepResult, markdown: Path, payload: Path) -> None:
    obs = result.observations
    colour = {
        "PASS": typer.colors.GREEN,
        "WARN": typer.colors.YELLOW,
        "FAIL": typer.colors.RED,
    }[result.report.verdict.value]
    typer.echo(f"run        {result.run_id}")
    typer.echo(f"status     {result.status.value}")
    typer.echo(
        f"funnel     fetched={obs.fetched} malformed={obs.malformed} rejected={obs.rejected} "
        f"below_threshold={obs.below_threshold} duplicates={obs.duplicates_in_run} "
        f"staged={obs.staged}"
    )
    typer.echo(
        f"load       inserted={result.counts.inserted} updated={result.counts.updated} "
        f"unchanged={result.counts.unchanged}"
        + ("  (dry run: nothing written)" if result.dry_run else "")
    )
    for finding in result.report.findings:
        bodies = f" [{', '.join(finding.bodies)}]" if finding.bodies else ""
        typer.echo(f"  {finding.level.value}: {finding.rule} — {finding.detail}{bodies}")
    typer.secho(f"verdict    {result.report.verdict.value}", fg=colour, bold=True)
    typer.echo(f"report     {markdown}")
    typer.echo(f"           {payload}")


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


@app.command()
def report(
    run_id: Annotated[
        str | None,
        typer.Option(
            "--run-id", help="Run to report on. Defaults to the most recent finished run."
        ),
    ] = None,
    report_dir: Annotated[Path | None, typer.Option(help="Where to write the report.")] = None,
    strict: Annotated[bool, typer.Option("--strict", help="Escalate a WARN to FAIL.")] = False,
) -> None:
    """Rebuild the quality report for a stored run from the database."""
    settings = _settings(report_dir=report_dir, strict=strict or None)
    dsn = _dsn(settings)
    try:
        identifier = UUID(run_id) if run_id else _latest_run(dsn, settings.db_schema)
        summary, observations, bodies = observations_from_db(dsn, settings.db_schema, identifier)
        thresholds = Thresholds.load(settings.thresholds_path)
    except ConfigurationError as exc:
        _fail(str(exc))
        return
    except ValueError as exc:
        _fail(f"--run-id is not a UUID: {exc}")
        return

    rebuilt = build_report(
        summary if not strict else _with_strict(summary), observations, bodies, thresholds
    )
    markdown, payload = write_report(rebuilt, settings.report_dir)
    typer.echo(f"run      {identifier}")
    typer.echo(
        f"load     inserted={summary.counts.inserted} updated={summary.counts.updated} "
        f"unchanged={summary.counts.unchanged}"
    )
    typer.echo(f"verdict  {rebuilt.verdict.value}")
    typer.echo(f"report   {markdown}")
    typer.echo(f"         {payload}")
    raise typer.Exit(rebuilt.exit_code)


def _with_strict(summary: Any) -> Any:
    from dataclasses import replace

    return replace(summary, strict=True)


def _latest_run(dsn: str, schema: str) -> UUID:
    from psycopg import sql

    from iris_signal_harvester.sink._common import connect, table

    query = sql.SQL(
        "SELECT id FROM {} WHERE finished_at IS NOT NULL ORDER BY started_at DESC LIMIT 1"
    ).format(table(schema, "harvest_run"))
    with connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(query)
        row = cur.fetchone()
    if row is None:
        raise ConfigurationError("no finished harvest_run exists yet; run `iris-harvest harvest`")
    value = row[0]
    return value if isinstance(value, uuid.UUID) else UUID(str(value))


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


@app.command()
def validate(
    sql_path: Annotated[
        Path | None,
        typer.Option(
            "--sql", help="Validation query file. Defaults to sql/validation_queries.sql."
        ),
    ] = None,
    expect_idempotent: Annotated[
        bool,
        typer.Option(
            "--expect-idempotent",
            help="Also assert queries 6 and 6b: the most recent run changed nothing but "
            "did re-observe rows. Only meaningful straight after a second identical "
            "harvest.",
        ),
    ] = False,
) -> None:
    """Run the checks in ``sql/validation_queries.sql`` against the database.

    Delegates to ``reporting.validation.run_validation``, which refuses to pass
    vacuously: it fails when an expected check is missing from the file, when a check
    cannot run or raises, and when ``signal_ingest`` is empty. Informational queries are
    not run. Any problem or violation exits 1.
    """
    settings = _settings()
    dsn = _dsn(settings)
    path = sql_path or Path("sql/validation_queries.sql")
    if not path.is_file():
        _fail(f"no validation query file at {path}")
        return

    def show(check: Check) -> None:
        if check.ok:
            typer.secho(f"  ok    {check.key:>3}. {check.title}", fg=typer.colors.GREEN)
        else:
            typer.secho(
                f"  FAIL  {check.key:>3}. {check.title}: {check.violation}", fg=typer.colors.RED
            )

    outcome = run_validation(
        dsn,
        path,
        expect_idempotent=expect_idempotent,
        on_check=show,
        schema=settings.db_schema,
    )
    for problem in outcome.problems:
        typer.secho(f"  PROBLEM  {problem}", fg=typer.colors.RED)
    typer.echo(outcome.summary())
    raise typer.Exit(outcome.exit_code)


if __name__ == "__main__":  # pragma: no cover
    app()
