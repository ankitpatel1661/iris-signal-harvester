"""Run ``sql/validation_queries.sql`` and refuse to pass vacuously.

Why this module exists (final-review I-14). The command-line ``validate`` used to:

* silently skip any query it could not run (one containing ``:'run_id'``),
* silently skip any query whose marker comment had been edited away, and
* exit 0 on ``0 invariant(s) checked, 0 violated``.

So a truncated file, a renamed marker or an empty database all looked like a clean bill
of health. Here "checked" is compared with an explicit *expected* set, and the run FAILS
when anything expected is missing, when a statement errors, or when there is no data to
validate at all.

Three kinds of check are enforced:

``invariant``       marked ``MUST return zero rows``. Mostly defence in depth: the schema
                    already forbids a violation (see the header of the SQL file).
``data_quality``    marked ``ZERO ROWS EXPECTED``. The schema cannot prevent these.
``idempotency``     queries ``6`` / ``6b``, only with ``expect_idempotent``.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from psycopg import sql

from iris_signal_harvester.sink._common import connect, table

__all__ = [
    "EXPECTED_DATA_QUALITY",
    "EXPECTED_INVARIANTS",
    "Check",
    "CheckKind",
    "Query",
    "ValidationOutcome",
    "parse_validation_sql",
    "run_validation",
]

# The checks a healthy validation file must contain. Changing the file's checks means
# changing these in the same commit, which is the point: it is a reviewable act.
EXPECTED_INVARIANTS: frozenset[str] = frozenset({"1", "2", "3", "4", "10"})
EXPECTED_DATA_QUALITY: frozenset[str] = frozenset({"11", "12", "15"})

_QUERY_SPLIT = re.compile(r"(?m)^-- (\d+b?)\. ")
_INVARIANT_MARKER = "MUST return zero rows"
_DATA_QUALITY_MARKER = "ZERO ROWS EXPECTED"
_RUN_ID_PARAMETER = ":'run_id'"


class CheckKind(StrEnum):
    INVARIANT = "invariant"
    DATA_QUALITY = "data_quality"
    IDEMPOTENCY = "idempotency"


@dataclass(frozen=True)
class Query:
    key: str
    title: str
    comment: str
    statement: str

    @property
    def kind(self) -> CheckKind | None:
        """What kind of check this is, or ``None`` for an informational query."""
        text = f"{self.title}\n{self.comment}"
        if _INVARIANT_MARKER in text:
            return CheckKind.INVARIANT
        if _DATA_QUALITY_MARKER in text:
            return CheckKind.DATA_QUALITY
        return None

    @property
    def needs_run_id(self) -> bool:
        return _RUN_ID_PARAMETER in self.statement


@dataclass(frozen=True)
class Check:
    key: str
    title: str
    kind: CheckKind
    violation: str | None

    @property
    def ok(self) -> bool:
        return self.violation is None


@dataclass
class ValidationOutcome:
    """Everything ``validate`` needs to print, and the single answer to "did it pass"."""

    checks: list[Check] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    """Reasons the run cannot be trusted, independent of any query's own result."""

    @property
    def checked(self) -> int:
        return len(self.checks)

    @property
    def violations(self) -> int:
        return sum(1 for c in self.checks if not c.ok)

    @property
    def ok(self) -> bool:
        return not self.problems and self.violations == 0

    @property
    def exit_code(self) -> int:
        return 0 if self.ok else 1

    def summary(self) -> str:
        tail = "" if not self.problems else f"; {len(self.problems)} problem(s)"
        return f"{self.checked} check(s) run, {self.violations} violated{tail}"


def parse_validation_sql(path: Path) -> list[Query]:
    """Split the file into its numbered queries (same grammar as the original CLI)."""
    text = path.read_text(encoding="utf-8")
    parts = _QUERY_SPLIT.split(text)
    parsed: list[Query] = []
    for key, chunk in zip(parts[1::2], parts[2::2], strict=True):
        lines = chunk.splitlines()
        title = lines[0].strip() if lines else ""
        comment = "\n".join(line for line in lines if line.lstrip().startswith("--"))
        statement = "\n".join(
            line for line in lines[1:] if not line.lstrip().startswith("--")
        ).strip()
        parsed.append(Query(key, title, comment, statement))
    return parsed


def _violation(key: str, rows: list[Any], kind: CheckKind) -> str | None:
    """``None`` when the query passed, else a description of the violation."""
    if key == "6":
        count = int(rows[0][0]) if rows else 0
        return None if count == 0 else f"{count} row(s) were written by the most recent run"
    if key == "6b":
        count = int(rows[0][0]) if rows else 0
        return None if count > 0 else "the most recent run re-observed no rows at all"
    if rows:
        label = "violate the invariant" if kind is CheckKind.INVARIANT else "fail the check"
        return f"{len(rows)} row(s) {label}, e.g. {rows[0]}"
    return None


def run_validation(
    dsn: str,
    path: Path,
    *,
    expect_idempotent: bool = False,
    expected_invariants: Collection[str] = EXPECTED_INVARIANTS,
    expected_data_quality: Collection[str] = EXPECTED_DATA_QUALITY,
    require_data: bool = True,
    on_check: Callable[[Check], None] | None = None,
    schema: str = "iris_staging",
) -> ValidationOutcome:
    """Run every expected check; fail if any is missing, skipped, errored or violated.

    ``on_check`` is called as each check finishes, so a CLI can print progressively.
    ``require_data`` makes an empty ``signal_ingest`` a failure: invariants over zero
    rows hold trivially and prove nothing.
    """
    outcome = ValidationOutcome()
    queries = parse_validation_sql(path)

    seen_invariants = {q.key for q in queries if q.kind is CheckKind.INVARIANT}
    seen_quality = {q.key for q in queries if q.kind is CheckKind.DATA_QUALITY}
    for label, expected, seen in (
        ("invariant", expected_invariants, seen_invariants),
        ("data-quality check", expected_data_quality, seen_quality),
    ):
        missing = sorted(set(expected) - seen, key=lambda k: (len(k), k))
        if missing:
            outcome.problems.append(
                f"expected {len(set(expected))} {label}(s) in {path.name} but found "
                f"{len(seen & set(expected))}; missing: {', '.join(missing)}"
            )

    with connect(dsn) as conn, conn.cursor() as cur:
        if require_data:
            cur.execute(sql.SQL("SELECT count(*) FROM {}").format(table(schema, "signal_ingest")))
            row = cur.fetchone()
            if row is None or int(row[0]) == 0:
                outcome.problems.append(
                    "signal_ingest is empty: every invariant holds trivially over zero rows, "
                    "so there is nothing to validate (run a harvest first)"
                )
        for query in queries:
            kind = query.kind
            if kind is None and expect_idempotent and query.key in ("6", "6b"):
                kind = CheckKind.IDEMPOTENCY
            if kind is None:
                continue
            if query.needs_run_id:
                # Never skip silently: a check that cannot run is a problem, not a pass.
                outcome.problems.append(
                    f"query {query.key} is a check but takes :'run_id', which validate "
                    "cannot supply; it was not run"
                )
                continue
            if not query.statement:
                outcome.problems.append(f"query {query.key} is a check with an empty statement")
                continue
            try:
                cur.execute(query.statement)
                rows = cur.fetchall()
            except Exception as exc:
                conn.rollback()
                outcome.problems.append(f"query {query.key} raised {type(exc).__name__}: {exc}")
                continue
            check = Check(query.key, query.title, kind, _violation(query.key, rows, kind))
            outcome.checks.append(check)
            if on_check is not None:
                on_check(check)

    if expect_idempotent:
        ran = {c.key for c in outcome.checks if c.kind is CheckKind.IDEMPOTENCY}
        if ran != {"6", "6b"}:
            outcome.problems.append(
                "--expect-idempotent was given but queries 6 and 6b did not both run"
            )
    return outcome
