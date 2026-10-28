"""The sweep orchestrator: guard rails, deduplication, dead-lettering, idempotency.

The guard-rail tests are the ones that matter most. Each drives a *separate*
pathological server through the real ``Pipeline`` and asserts that the sweep
terminates for the specific reason that failure mode calls for — and that the body's
watermark is held back, because a sweep that stopped early did not cover its window
and must retry it rather than skip it.

Everything here is offline except the tests marked ``integration``, which need a
PostGIS database and skip cleanly without ``IRIS_DB_DSN``. They create and drop their
own throwaway database, so the DSN's own database is never touched.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import make_conninfo

from iris_signal_harvester.adapters.base import Capabilities, Cursor, Page
from iris_signal_harvester.adapters.fixture import FixtureAdapter
from iris_signal_harvester.config import Settings
from iris_signal_harvester.domain.models import Body, RawDocument, RejectCode
from iris_signal_harvester.domain.normalise import Normaliser
from iris_signal_harvester.errors import AccessRestricted, SourceUnavailable
from iris_signal_harvester.pipeline import (
    Pipeline,
    RunStatus,
    SourceRegistry,
    StopReason,
    SweepOptions,
    build_adapter,
)
from iris_signal_harvester.reporting.quality import Thresholds, Verdict
from iris_signal_harvester.sink.migrate import apply_migrations
from iris_signal_harvester.sink.postgis import LoadCounts, PostgisSink
from iris_signal_harvester.sink.watermark import AdvanceOutcome, WatermarkStore

REPO = Path(__file__).resolve().parents[1]
CASSETTES = REPO / "tests" / "fixtures" / "cassettes"
NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)

BODY = Body(
    id="https://oparl.example.org/oparl/v1.0/body/1",
    name="Musterstadt",
    ags="09162000",
    paper_endpoint="https://oparl.example.org/oparl/v1.0/body/1/list/paper",
)


def settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "taxonomy_path": REPO / "config" / "taxonomy.yaml",
        "thresholds_path": REPO / "config" / "thresholds.yaml",
        "bodies_path": REPO / "config" / "bodies.yaml",
        "fixture_dir": CASSETTES,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[call-arg]


@pytest.fixture(scope="module")
def normaliser() -> Normaliser:
    return Normaliser.from_settings(settings(), gazetteer_path=REPO / "config" / "gazetteer.json")


def pipeline(
    normaliser: Normaliser,
    *,
    sink: Any = None,
    watermarks: Any = None,
    dsn: str | None = None,
    **overrides: Any,
) -> Pipeline:
    conf = settings(**overrides)
    return Pipeline(
        conf,
        normaliser,
        Thresholds.load(conf.thresholds_path),
        sink=sink,
        watermarks=watermarks,
        dsn=dsn,
        breaker_threshold=2,
        sleep=_no_sleep,
    )


async def _no_sleep(_seconds: float) -> None:
    """Offline tests never wait for a backoff."""


# ---------------------------------------------------------------------------
# stub adapters: one per pathology
# ---------------------------------------------------------------------------


def paper(n: int, *, title: str | None = None) -> RawDocument:
    reference = f"01-20/B{n:05d}"
    return RawDocument(
        source_system="stub",
        source_body_id=BODY.id,
        source_record_id=reference,
        source_url=f"https://oparl.example.org/oparl/v1.0/paper/{n}",
        payload={
            "id": f"https://oparl.example.org/oparl/v1.0/paper/{n}",
            "reference": reference,
            "name": title or f"Bebauungsplan Nr. {n}: Batteriespeicher im Gewerbegebiet",
            "paperType": "Beschlussvorlage",
            "date": "2026-05-12",
            "modified": "2026-06-01T09:00:00+02:00",
        },
    )


@dataclass
class StubAdapter:
    """An adapter whose pages are scripted. ``source_system`` is deliberately its own."""

    pages: Sequence[Page] = ()
    repeat_last: bool = True
    bodies: Sequence[Body] = (BODY,)
    source_system = "stub"
    calls: list[tuple[str | None, datetime | None]] = field(default_factory=list)
    raises: BaseException | None = None
    supports_modified_since: bool = True

    def capabilities(self) -> Capabilities:
        return Capabilities(
            pagination="object_link",
            supports_modified_since=self.supports_modified_since,
            supports_conditional_get=False,
            max_page_size=100,
        )

    async def discover_bodies(self) -> AsyncIterator[Body]:
        for body in self.bodies:
            yield body

    async def fetch_page(
        self,
        body: Body,
        cursor: Cursor | None,
        *,
        modified_since: datetime | None = None,
    ) -> Page:
        self.calls.append((cursor.token if cursor else None, modified_since))
        if self.raises is not None:
            raise self.raises
        index = len(self.calls) - 1
        if index < len(self.pages):
            return self.pages[index]
        if self.repeat_last and self.pages:
            return self.pages[-1]
        return Page(records=())

    async def aclose(self) -> None:
        return None


@dataclass
class RecordingSink:
    """Records the order of sink calls, so FK ordering can be asserted offline."""

    order: list[str] = field(default_factory=list)
    loaded: list[Any] = field(default_factory=list)
    rejects: list[Any] = field(default_factory=list)

    def start_run(self, run_id: uuid.UUID, source_system: str, **kwargs: Any) -> None:
        self.order.append("start_run")

    def load(self, signals: Any) -> LoadCounts:
        self.order.append("load")
        batch = list(signals)
        self.loaded.extend(batch)
        return LoadCounts(len(batch), 0, 0)

    def write_rejects(self, rejects: Any) -> int:
        self.order.append("write_rejects")
        batch = list(rejects)
        self.rejects.extend(batch)
        return len(batch)

    def finish_run(self, run_id: uuid.UUID, **kwargs: Any) -> None:
        self.order.append("finish_run")
        self.finished = kwargs

    def record_verdict(self, run_id: uuid.UUID, verdict: str, findings: Any = ()) -> None:
        self.order.append("record_verdict")
        self.verdict = (verdict, list(findings))


# ---------------------------------------------------------------------------
# guard rail 1 — cursor loop
# ---------------------------------------------------------------------------


async def test_cursor_loop_terminates_the_sweep(normaliser: Normaliser) -> None:
    """``next`` points back at a page we already fetched: A -> B -> A -> ..."""
    loop = Cursor(token="https://oparl.example.org/page/A")
    adapter = StubAdapter(
        pages=[
            Page(records=(paper(1),), next_cursor=loop),
            Page(records=(paper(2),), next_cursor=loop),
        ]
    )
    result = await pipeline(normaliser).sweep(adapter, SweepOptions(now=NOW))
    assert result.bodies[0].stop_reason == StopReason.CURSOR_LOOP.value
    assert result.bodies[0].degraded is True
    # Terminated: two pages, not five hundred.
    assert len(adapter.calls) == 2
    assert result.status is RunStatus.DEGRADED


async def test_a_retried_page_is_not_mistaken_for_a_cursor_loop(
    normaliser: Normaliser,
) -> None:
    """A failed fetch must be retryable: a cursor only counts once it was served."""
    adapter = StubAdapter(raises=SourceUnavailable("500 from the council"))
    result = await pipeline(normaliser).sweep(adapter, SweepOptions(now=NOW))
    assert result.bodies[0].stop_reason in (
        StopReason.SOURCE_UNAVAILABLE.value,
        StopReason.CIRCUIT_OPEN.value,
    )
    # The breaker, not the cursor-loop detector, is what stopped it.
    assert len(adapter.calls) == 2


# ---------------------------------------------------------------------------
# guard rail 2 — page fingerprinting
# ---------------------------------------------------------------------------


async def test_identical_page_fingerprint_terminates_the_sweep(
    normaliser: Normaliser,
) -> None:
    """Fresh-looking cursors, same page every time: the server ignores the cursor."""
    same = (paper(1), paper(2))
    adapter = StubAdapter(
        pages=[
            Page(records=same, next_cursor=Cursor(token="https://example.org/p/1")),
            Page(records=same, next_cursor=Cursor(token="https://example.org/p/2")),
            Page(records=same, next_cursor=Cursor(token="https://example.org/p/3")),
        ]
    )
    result = await pipeline(normaliser).sweep(adapter, SweepOptions(now=NOW))
    assert result.bodies[0].stop_reason == StopReason.IDENTICAL_PAGE.value
    assert len(adapter.calls) == 2


def test_an_all_malformed_page_does_not_fingerprint_as_empty() -> None:
    """Fix for the bug this guard rail depended on.

    Two different all-malformed pages used to hash identically (to the empty page),
    which would falsely trip the "server is ignoring the cursor" detector.
    """
    empty = Page(records=())
    one = Page(records=(), malformed=(("missing_required_field", {"name": "A"}),))
    two = Page(records=(), malformed=(("missing_required_field", {"name": "B"}),))
    assert one.id_fingerprint() != empty.id_fingerprint()
    assert one.id_fingerprint() != two.id_fingerprint()
    # Stable across key ordering and across calls.
    reordered = Page(records=(), malformed=(("missing_required_field", {"name": "A"}),))
    assert one.id_fingerprint() == reordered.id_fingerprint()


# ---------------------------------------------------------------------------
# guard rail 3 — zero yield
# ---------------------------------------------------------------------------


async def test_zero_yield_pages_terminate_the_sweep(normaliser: Normaliser) -> None:
    """Different pages, different cursors, nothing new on any of them."""
    adapter = StubAdapter(
        pages=[
            Page(
                records=(paper(1), paper(2)),
                next_cursor=Cursor(token="https://example.org/p/1"),
            ),
            Page(records=(paper(1),), next_cursor=Cursor(token="https://example.org/p/2")),
            Page(records=(paper(2),), next_cursor=Cursor(token="https://example.org/p/3")),
            Page(records=(paper(1),), next_cursor=Cursor(token="https://example.org/p/4")),
        ]
    )
    result = await pipeline(normaliser, zero_yield_pages_before_stop=2).sweep(
        adapter, SweepOptions(now=NOW)
    )
    body = result.bodies[0]
    assert body.stop_reason == StopReason.ZERO_YIELD.value
    assert len(adapter.calls) == 3  # stopped on the second barren page
    assert body.duplicates == 2


async def test_max_pages_per_body_is_a_hard_stop(normaliser: Normaliser) -> None:
    """The last line of defence: a server that is novel on every page still ends."""
    adapter = StubAdapter(
        pages=[
            Page(records=(paper(n),), next_cursor=Cursor(token=f"https://example.org/p/{n}"))
            for n in range(50)
        ],
        repeat_last=False,
    )
    result = await pipeline(normaliser, max_pages_per_body=3).sweep(adapter, SweepOptions(now=NOW))
    assert result.bodies[0].stop_reason == StopReason.MAX_PAGES.value
    assert len(adapter.calls) == 3


# ---------------------------------------------------------------------------
# deduplication, dead-lettering, blast radius
# ---------------------------------------------------------------------------


async def test_intra_run_duplicates_collapse(normaliser: Normaliser) -> None:
    adapter = StubAdapter(
        pages=[
            Page(
                records=(paper(1), paper(2)),
                next_cursor=Cursor(token="https://example.org/p/2"),
            ),
            Page(records=(paper(2), paper(3))),
        ],
        repeat_last=False,
    )
    result = await pipeline(normaliser).sweep(adapter, SweepOptions(now=NOW))
    obs = result.observations
    assert obs.fetched == 4
    assert obs.duplicates_in_run == 1
    assert obs.staged == 3
    assert result.report.residual == 0


async def test_a_malformed_record_is_dead_lettered_and_its_page_completes(
    normaliser: Normaliser,
) -> None:
    """Adapters emit (reason_code, payload); the pipeline attaches run and body."""
    adapter = StubAdapter(
        pages=[
            Page(
                records=(paper(1), paper(2)),
                malformed=(("missing_required_field", {"name": "Solarpark ohne Kennung"}),),
            )
        ],
        repeat_last=False,
    )
    sink = RecordingSink()
    result = await pipeline(normaliser, sink=sink).sweep(adapter, SweepOptions(now=NOW))

    assert result.observations.malformed == 1
    assert result.observations.staged == 2  # the rest of the page survived
    assert len(sink.rejects) == 1
    reject = sink.rejects[0]
    assert reject.harvest_run_id == result.run_id
    assert reject.source_body_id == BODY.id
    assert reject.source_system == "stub"
    assert reject.reason_code is RejectCode.MISSING_REQUIRED_FIELD
    assert reject.raw_payload == {"name": "Solarpark ohne Kennung"}
    # Counted as a rejection, never as "below threshold".
    histogram = {r.reason_code: r for r in result.report.rejections}
    assert histogram["missing_required_field"].count == 1
    assert result.observations.below_threshold == 0


async def test_an_unknown_reason_code_still_lands_in_the_dead_letter(
    normaliser: Normaliser,
) -> None:
    adapter = StubAdapter(
        pages=[Page(records=(), malformed=(("not_a_real_code", {"x": 1}),))],
        repeat_last=False,
    )
    sink = RecordingSink()
    await pipeline(normaliser, sink=sink).sweep(adapter, SweepOptions(now=NOW))
    assert sink.rejects[0].reason_code is RejectCode.SCHEMA_VALIDATION_FAILED


async def test_the_run_is_registered_before_anything_is_written(
    normaliser: Normaliser,
) -> None:
    """``signal_ingest`` and the reject table both have a FK to ``harvest_run``."""
    adapter = StubAdapter(
        pages=[
            Page(
                records=(paper(1),),
                malformed=(("missing_required_field", {"name": "x"}),),
            )
        ],
        repeat_last=False,
    )
    sink = RecordingSink()
    await pipeline(normaliser, sink=sink).sweep(adapter, SweepOptions(now=NOW))
    assert sink.order[0] == "start_run"
    assert sink.order[-2:] == ["finish_run", "record_verdict"]
    assert sink.order.index("start_run") < sink.order.index("load")
    assert sink.order.index("start_run") < sink.order.index("write_rejects")


async def test_the_runs_own_verdict_is_persisted_after_the_report_is_built(
    normaliser: Normaliser,
) -> None:
    """W2a: ``report`` and the dashboard read this from the database, not from a file."""
    sink = RecordingSink()
    adapter = StubAdapter(pages=[Page(records=(paper(1),))], repeat_last=False)
    result = await pipeline(normaliser, sink=sink).sweep(adapter, SweepOptions(now=NOW))
    verdict, findings = sink.verdict
    assert verdict == result.report.verdict.value
    assert findings == [{"level": f.level.value, "rule": f.rule} for f in result.report.findings]


async def test_a_dry_run_persists_no_verdict(normaliser: Normaliser) -> None:
    sink = RecordingSink()
    adapter = StubAdapter(pages=[Page(records=(paper(1),))], repeat_last=False)
    await pipeline(normaliser, sink=sink).sweep(adapter, SweepOptions(now=NOW, dry_run=True))
    assert "record_verdict" not in sink.order


async def test_a_failure_to_store_the_verdict_does_not_lose_the_sweep(
    normaliser: Normaliser,
) -> None:
    class VerdictlessSink(RecordingSink):
        def record_verdict(self, run_id: uuid.UUID, verdict: str, findings: Any = ()) -> None:
            raise RuntimeError("connection reset")

    adapter = StubAdapter(pages=[Page(records=(paper(1),))], repeat_last=False)
    result = await pipeline(normaliser, sink=VerdictlessSink()).sweep(
        adapter, SweepOptions(now=NOW)
    )
    assert result.counts.inserted == 1


@dataclass
class RejectingSink(RecordingSink):
    """A sink whose own guard dead-letters the first ``n`` signals of every batch."""

    refuse: int = 1

    def load(self, signals: Any) -> LoadCounts:
        self.order.append("load")
        batch = list(signals)
        self.loaded.extend(batch)
        return LoadCounts(len(batch) - self.refuse, 0, 0, rejected=self.refuse)


async def test_sink_rejects_reach_the_funnel_and_the_run_row(normaliser: Normaliser) -> None:
    """W2b: ``LoadCounts.rejected`` is neither dropped by ``_total`` nor by ``finish_run``."""
    sink = RejectingSink(refuse=1)
    adapter = StubAdapter(pages=[Page(records=(paper(1), paper(2), paper(3)))], repeat_last=False)
    result = await pipeline(normaliser, sink=sink).sweep(adapter, SweepOptions(now=NOW))
    assert result.counts.rejected == 1
    assert result.counts.inserted == 2
    obs = result.observations
    assert (obs.fetched, obs.rejected, obs.staged) == (3, 1, 2)
    assert result.report.residual == 0, "the funnel must balance with the sink's rejects in it"
    assert sink.finished["rejected"] == 1
    assert sink.finished["counts"].rejected == 1
    assert result.bodies[0].rejected == 1 and result.bodies[0].emitted == 2


async def test_a_nan_or_surrogate_record_is_dead_lettered_not_an_unexpected_exception(
    normaliser: Normaliser,
) -> None:
    """W3 end to end: the normaliser rejects before hashing, so the sweep carries on."""
    nan_doc = paper(1)
    nan_doc.payload["score"] = float("nan")
    surrogate_doc = paper(2)
    surrogate_doc.payload["description"] = "broken \ud800 text"
    sink = RecordingSink()
    adapter = StubAdapter(
        pages=[Page(records=(nan_doc, surrogate_doc, paper(3)))], repeat_last=False
    )
    result = await pipeline(normaliser, sink=sink).sweep(adapter, SweepOptions(now=NOW))
    assert result.status is RunStatus.SUCCESS
    assert (result.observations.rejected, result.observations.staged) == (2, 1)
    assert {r.reason_code.value for r in sink.rejects} == {"schema_validation_failed"}
    assert result.report.residual == 0


async def test_one_failing_municipality_degrades_only_itself(
    normaliser: Normaliser,
) -> None:
    healthy = Body(id="body/ok", name="Gutstadt", ags="09162000", paper_endpoint="https://x/ok")
    broken = Body(id="body/bad", name="Kaputtstadt", ags="11000000", paper_endpoint="https://x/b")

    class Mixed(StubAdapter):
        async def fetch_page(
            self,
            body: Body,
            cursor: Cursor | None,
            *,
            modified_since: datetime | None = None,
        ) -> Page:
            self.calls.append((cursor.token if cursor else None, modified_since))
            if body.id == "body/bad":
                raise SourceUnavailable("the council's server is down")
            return Page(records=(paper(1), paper(2)))

    result = await pipeline(normaliser).sweep(
        Mixed(bodies=(healthy, broken)), SweepOptions(now=NOW)
    )
    health = {b.body_id: b for b in result.bodies}
    assert health["body/ok"].degraded is False
    assert health["body/ok"].emitted == 2
    assert health["body/bad"].degraded is True
    # The run is degraded, not failed: the healthy municipality's data is kept.
    assert result.status is RunStatus.DEGRADED
    assert result.observations.staged == 2


async def test_an_access_refusal_stops_that_body_without_retrying(
    normaliser: Normaliser,
) -> None:
    adapter = StubAdapter(raises=AccessRestricted("403 Forbidden", status=403))
    result = await pipeline(normaliser).sweep(adapter, SweepOptions(now=NOW))
    assert result.bodies[0].stop_reason == StopReason.ACCESS_RESTRICTED.value
    assert len(adapter.calls) == 1  # exactly one attempt, ever


async def test_discovery_failure_fails_the_run_rather_than_reporting_success(
    normaliser: Normaliser,
) -> None:
    class NoDiscovery(StubAdapter):
        async def discover_bodies(self) -> AsyncIterator[Body]:
            raise SourceUnavailable("System object unreachable")
            yield BODY  # pragma: no cover

    result = await pipeline(normaliser).sweep(NoDiscovery(), SweepOptions(now=NOW))
    assert result.status is RunStatus.FAILED
    assert result.report.verdict is Verdict.FAIL
    assert result.exit_code == 1


async def test_a_record_limit_stops_the_sweep_and_holds_the_watermark(
    normaliser: Normaliser,
) -> None:
    adapter = StubAdapter(
        pages=[
            Page(records=(paper(n), paper(n + 50)), next_cursor=Cursor(token=f"p/{n}"))
            for n in range(1, 10)
        ],
        repeat_last=False,
    )
    result = await pipeline(normaliser).sweep(adapter, SweepOptions(now=NOW, limit=3))
    assert result.observations.fetched == 3
    assert result.bodies[0].stop_reason == StopReason.RECORD_LIMIT.value
    assert result.bodies[0].degraded is True


async def test_bodies_can_be_restricted_by_id(normaliser: Normaliser) -> None:
    a = Body(id="body/a", paper_endpoint="https://x/a")
    b = Body(id="body/b", paper_endpoint="https://x/b")
    adapter = StubAdapter(pages=[Page(records=(paper(1),))], bodies=(a, b))
    result = await pipeline(normaliser).sweep(adapter, SweepOptions(now=NOW, bodies=("body/b",)))
    assert [body.body_id for body in result.bodies] == ["body/b"]


async def test_a_source_without_modified_since_support_sweeps_fully(
    normaliser: Normaliser,
) -> None:
    adapter = StubAdapter(
        pages=[Page(records=(paper(1),))], supports_modified_since=False, repeat_last=False
    )
    result = await pipeline(normaliser).sweep(
        adapter, SweepOptions(now=NOW, since=NOW - timedelta(days=1))
    )
    assert adapter.calls == [(None, None)]
    assert any("FULL sweep" in note for note in result.report.summary.notes)


# ---------------------------------------------------------------------------
# dry run
# ---------------------------------------------------------------------------


async def test_dry_run_reports_without_a_database(normaliser: Normaliser, tmp_path: Path) -> None:
    from iris_signal_harvester.reporting.quality import write_report

    adapter = FixtureAdapter(settings(), "happy_path")
    try:
        result = await pipeline(normaliser).sweep(adapter, SweepOptions(dry_run=True, now=NOW))
    finally:
        await adapter.aclose()
    markdown, payload = write_report(result.report, tmp_path)

    assert result.observations.staged == 4
    assert result.counts == LoadCounts(0, 0, 0)
    assert result.dry_run is True
    assert markdown.read_text(encoding="utf-8").startswith("# Harvest quality report")
    assert payload.is_file()
    assert "dry-run" in markdown.read_text(encoding="utf-8")
    assert all(b.watermark == "skipped (no write)" for b in result.bodies)


async def test_a_degraded_cassette_fails_the_verdict_with_a_nonzero_exit(
    normaliser: Normaliser,
) -> None:
    """A coordinate outside the CRS domain is rejected, never clamped — and that
    rejection rate fails the gate."""
    adapter = FixtureAdapter(settings(), "coordinate_out_of_domain")
    try:
        result = await pipeline(normaliser).sweep(adapter, SweepOptions(dry_run=True, now=NOW))
    finally:
        await adapter.aclose()
    assert result.report.verdict is Verdict.FAIL
    assert result.exit_code == 1
    codes = {r.reason_code for r in result.report.rejections}
    assert "geo_out_of_crs_domain" in codes
    example = next(r for r in result.report.rejections if r.reason_code == "geo_out_of_crs_domain")
    assert example.example_record_id  # one real id per reason
    assert result.report.residual == 0


async def test_the_gate_names_the_bodies_responsible(normaliser: Normaliser) -> None:
    adapter = StubAdapter(raises=SourceUnavailable("down"))
    result = await pipeline(normaliser).sweep(adapter, SweepOptions(now=NOW))
    degraded = [f for f in result.report.findings if f.rule == "degraded_body_rate_max"]
    assert degraded
    assert any("Musterstadt" in body for body in degraded[0].bodies)


# ---------------------------------------------------------------------------
# registry wiring
# ---------------------------------------------------------------------------


def test_the_registry_declares_the_verified_live_endpoint_and_the_fixture_default() -> None:
    registry = SourceRegistry.load(REPO / "config" / "bodies.yaml")
    assert registry.default_source == "fixture"
    assert registry.entry("fixture").default is True
    oparl = registry.entry("oparl")
    assert oparl.system_url == "https://www.muenchen-transparent.de/oparl/v1.0"
    assert oparl.licence == "undeclared"
    assert oparl.health == "verified_live"
    assert registry.entry("ratsdokumente").health == "nonexistent"


def test_build_adapter_follows_the_registry() -> None:
    adapter = build_adapter(settings(), source="fixture")
    assert adapter.source_system == "fixture"
    assert isinstance(adapter, FixtureAdapter)


def test_an_unknown_source_is_a_configuration_error() -> None:
    from iris_signal_harvester.errors import ConfigurationError

    with pytest.raises(ConfigurationError, match="unknown source"):
        build_adapter(settings(), source="not-a-source")


# ---------------------------------------------------------------------------
# review fixes: sink failure, limit on the last page, --since, duplicates, hosts
# ---------------------------------------------------------------------------


def dated(n: int, modified: str, *, title: str | None = None, body: Body = BODY) -> RawDocument:
    """A paper whose ``modified`` (and optionally title) is chosen by the test."""
    base = paper(n, title=title)
    return base.model_copy(
        update={"source_body_id": body.id, "payload": {**base.payload, "modified": modified}}
    )


@dataclass
class FakeWatermarks:
    """An in-memory watermark store that records what the pipeline asked of it."""

    stored: datetime | None = None
    advances: list[tuple[datetime | None, bool]] = field(default_factory=list)

    def read(self, source_system: str, body_id: str) -> datetime | None:
        return self.stored

    def advance(
        self,
        source_system: str,
        body_id: str,
        candidate: datetime | None,
        *,
        clean_sweep: bool,
        **_: Any,
    ) -> AdvanceOutcome:
        self.advances.append((candidate, clean_sweep))
        return AdvanceOutcome.ADVANCED if clean_sweep else AdvanceOutcome.SKIPPED_UNCLEAN_SWEEP


@dataclass
class PerBodyAdapter(StubAdapter):
    """Serves each body its own scripted page, whatever order the requests arrive in."""

    by_body: dict[str, Page] = field(default_factory=dict)

    async def fetch_page(
        self, body: Body, cursor: Cursor | None, *, modified_since: datetime | None = None
    ) -> Page:
        self.calls.append((cursor.token if cursor else None, modified_since))
        return self.by_body[body.id]


class FailingSink(RecordingSink):
    """A sink whose ``load`` raises for the bodies named in ``fail_for``."""

    def __init__(self, fail_for: set[str]) -> None:
        super().__init__()
        self.fail_for = fail_for

    def load(self, signals: Any) -> LoadCounts:
        batch = list(signals)
        if any(s.source_body_id in self.fail_for for s in batch):
            self.order.append("load_failed")
            raise RuntimeError("InvalidTextRepresentation: NaN is not valid JSON")
        return super().load(batch)


async def test_a_sink_failure_still_finishes_the_run_and_keeps_the_other_bodies(
    normaliser: Normaliser,
) -> None:
    """I-1: one body's failed load must not leave the run ``running`` forever."""
    good = Body(id="body/good", paper_endpoint="https://good.example/p")
    bad = Body(id="body/bad", paper_endpoint="https://bad.example/p")
    adapter = PerBodyAdapter(
        bodies=(good, bad),
        by_body={
            good.id: Page(records=(dated(1, "2026-06-01T09:00:00+02:00", body=good),)),
            bad.id: Page(records=(dated(2, "2026-06-01T09:00:00+02:00", body=bad),)),
        },
    )
    sink = FailingSink({bad.id})
    marks = FakeWatermarks()
    result = await pipeline(normaliser, sink=sink, watermarks=marks).sweep(
        adapter, SweepOptions(now=NOW)
    )
    assert sink.order[-2:] == ["finish_run", "record_verdict"]
    assert sink.finished["status"] == RunStatus.DEGRADED.value
    assert result.status is RunStatus.DEGRADED
    assert [s.source_body_id for s in sink.loaded] == [good.id]
    health = {b.body_id: b for b in result.bodies}
    assert health[bad.id].stop_reason == StopReason.ERROR.value
    assert "RuntimeError" in (health[bad.id].error or "")
    assert health[good.id].degraded is False
    # Only the healthy body's watermark may move.
    assert len(marks.advances) == 1


async def test_a_run_whose_every_body_fails_to_persist_is_failed_not_running(
    normaliser: Normaliser,
) -> None:
    sink = FailingSink({BODY.id})
    adapter = StubAdapter(pages=[Page(records=(paper(1),))], repeat_last=False)
    result = await pipeline(normaliser, sink=sink).sweep(adapter, SweepOptions(now=NOW))
    assert "finish_run" in sink.order
    assert sink.finished["status"] == RunStatus.FAILED.value
    assert result.status is RunStatus.FAILED
    assert result.exit_code == 1


async def test_a_limit_hit_mid_way_through_the_last_page_is_not_exhausted(
    normaliser: Normaliser,
) -> None:
    """I-2: the unread third record must keep the watermark where it is."""
    adapter = StubAdapter(
        pages=[
            Page(
                records=(
                    dated(1, "2026-10-01T09:00:00+02:00"),
                    dated(2, "2026-10-02T09:00:00+02:00"),
                    dated(3, "2026-01-01T09:00:00+01:00"),
                )
            )
        ],
        repeat_last=False,
    )
    marks = FakeWatermarks()
    result = await pipeline(normaliser, sink=RecordingSink(), watermarks=marks).sweep(
        adapter, SweepOptions(now=NOW, limit=2)
    )
    assert result.bodies[0].stop_reason == StopReason.RECORD_LIMIT.value
    assert result.bodies[0].degraded is True
    assert [clean for _, clean in marks.advances] == [False]


async def test_a_limit_that_consumes_the_last_page_exactly_is_still_exhausted(
    normaliser: Normaliser,
) -> None:
    adapter = StubAdapter(
        pages=[
            Page(
                records=(
                    dated(1, "2026-10-01T09:00:00+02:00"),
                    dated(2, "2026-10-02T09:00:00+02:00"),
                )
            )
        ],
        repeat_last=False,
    )
    result = await pipeline(normaliser).sweep(adapter, SweepOptions(now=NOW, limit=2))
    assert result.bodies[0].stop_reason == StopReason.EXHAUSTED.value


@pytest.mark.parametrize(
    ("stored", "since", "advances"),
    [
        (None, datetime(2026, 10, 1, tzinfo=UTC), False),  # fresh DB: the backfill never ran
        (datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 10, 1, tzinfo=UTC), False),  # gap
        (datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 9, 1, tzinfo=UTC), True),  # covered
    ],
)
async def test_an_explicit_since_only_advances_a_watermark_it_fully_covers(
    normaliser: Normaliser, stored: datetime | None, since: datetime, advances: bool
) -> None:
    """I-3: `--since` must not skip the window between the stored watermark and `since`."""
    adapter = StubAdapter(
        pages=[Page(records=(dated(1, "2026-10-05T09:00:00+02:00"),))], repeat_last=False
    )
    marks = FakeWatermarks(stored=stored)
    result = await pipeline(normaliser, sink=RecordingSink(), watermarks=marks).sweep(
        adapter, SweepOptions(now=NOW, since=since)
    )
    assert bool(marks.advances) is advances
    if not advances:
        assert result.bodies[0].watermark == "held (explicit --since)"
        assert result.bodies[0].degraded is False  # a held watermark is not a failure
    assert adapter.calls[0][1] == since


async def test_a_default_run_still_advances_its_watermark(normaliser: Normaliser) -> None:
    adapter = StubAdapter(
        pages=[Page(records=(dated(1, "2026-10-05T09:00:00+02:00"),))], repeat_last=False
    )
    marks = FakeWatermarks(stored=datetime(2026, 10, 1, tzinfo=UTC))
    await pipeline(normaliser, sink=RecordingSink(), watermarks=marks).sweep(
        adapter, SweepOptions(now=NOW)
    )
    assert [clean for _, clean in marks.advances] == [True]


async def test_bodies_that_hit_the_page_cap_are_named_in_a_finding(
    normaliser: Normaliser,
) -> None:
    """I-4: a body that can never finish its backfill must be visible, not silent."""
    adapter = StubAdapter(
        pages=[
            Page(records=(paper(n),), next_cursor=Cursor(token=f"https://example.org/p/{n}"))
            for n in range(50)
        ],
        repeat_last=False,
    )
    result = await pipeline(normaliser, max_pages_per_body=2).sweep(adapter, SweepOptions(now=NOW))
    notes = " ".join(result.report.summary.notes)
    assert "Musterstadt" in notes
    assert "max_pages_per_body" in notes


async def test_the_duplicate_with_the_greater_modified_wins(normaliser: Normaliser) -> None:
    """I-6: the loaded title and the watermark must come from the same copy."""
    old = dated(7, "2026-10-01T09:00:00+02:00", title="Batteriespeicher ALT im Gewerbegebiet")
    new = dated(7, "2026-10-20T09:00:00+02:00", title="Batteriespeicher NEU im Gewerbegebiet")
    adapter = StubAdapter(
        pages=[
            Page(records=(old, paper(101)), next_cursor=Cursor(token="https://example.org/p/2")),
            Page(records=(new, paper(102))),
        ],
        repeat_last=False,
    )
    sink = RecordingSink()
    marks = FakeWatermarks()
    result = await pipeline(normaliser, sink=sink, watermarks=marks).sweep(
        adapter, SweepOptions(now=NOW)
    )
    titles = [
        s.title for s in sink.loaded if "Batteriespeicher" in s.title and "Nr." not in s.title
    ]
    assert titles == ["Batteriespeicher NEU im Gewerbegebiet"]
    assert marks.advances[0][0] == datetime(2026, 10, 20, 7, 0, tzinfo=UTC)
    assert result.observations.duplicates_in_run == 1
    assert result.report.residual == 0


async def test_a_staler_later_duplicate_does_not_replace_the_newer_copy(
    normaliser: Normaliser,
) -> None:
    newer = dated(7, "2026-10-20T09:00:00+02:00", title="Batteriespeicher NEU im Gewerbegebiet")
    stale = dated(7, "2026-10-01T09:00:00+02:00", title="Batteriespeicher ALT im Gewerbegebiet")
    unparseable = dated(7, "not-a-date", title="Batteriespeicher XXX im Gewerbegebiet")
    adapter = StubAdapter(
        pages=[
            Page(records=(newer, paper(101)), next_cursor=Cursor(token="https://example.org/p/2")),
            Page(records=(stale, paper(102)), next_cursor=Cursor(token="https://example.org/p/3")),
            Page(records=(unparseable, paper(103))),
        ],
        repeat_last=False,
    )
    sink = RecordingSink()
    result = await pipeline(normaliser, sink=sink).sweep(adapter, SweepOptions(now=NOW))
    titles = [
        s.title for s in sink.loaded if "Batteriespeicher" in s.title and "Nr." not in s.title
    ]
    assert titles == ["Batteriespeicher NEU im Gewerbegebiet"]
    assert result.observations.duplicates_in_run == 2
    assert result.report.residual == 0


async def test_an_access_refusal_is_remembered_per_host(normaliser: Normaliser) -> None:
    """I-13: other bodies on a refused host are skipped without a request of their own."""
    a = Body(id="body/a", paper_endpoint="https://refuses.example/oparl/a/papers")
    b = Body(id="body/b", paper_endpoint="https://refuses.example/oparl/b/papers")
    c = Body(id="body/c", paper_endpoint="https://elsewhere.example/oparl/c/papers")
    adapter = StubAdapter(raises=AccessRestricted("403 Forbidden", status=403), bodies=(a, b, c))
    result = await pipeline(normaliser, max_concurrent_bodies=1).sweep(
        adapter, SweepOptions(now=NOW)
    )
    assert len(adapter.calls) == 2  # a, and the unrelated host c; never b
    health = {h.body_id: h for h in result.bodies}
    assert health["body/b"].stop_reason == StopReason.ACCESS_RESTRICTED.value
    assert "already refused" in (health["body/b"].error or "")


async def test_a_malformed_reject_is_keyed_by_id_not_reference(normaliser: Normaliser) -> None:
    sink = RecordingSink()
    adapter = StubAdapter(
        pages=[
            Page(
                records=(),
                malformed=(
                    (
                        "missing_required_field",
                        {"id": "https://x.example/paper/9", "reference": "AZ 1/2"},
                    ),
                ),
            )
        ],
        repeat_last=False,
    )
    await pipeline(normaliser, sink=sink).sweep(adapter, SweepOptions(now=NOW))
    assert sink.rejects[0].source_record_id == "https://x.example/paper/9"


# ---------------------------------------------------------------------------
# integration: the whole pipeline against PostGIS
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def harvest_dsn() -> Iterator[str]:
    base = os.environ.get("IRIS_DB_DSN")
    if not base:
        pytest.skip("IRIS_DB_DSN is not set; skipping database tests")
    name = f"iris_pipeline_{uuid.uuid4().hex[:12]}"
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


@pytest.fixture
def clean_db(harvest_dsn: str) -> Iterator[str]:
    with psycopg.connect(harvest_dsn, autocommit=True) as conn:
        conn.execute(
            "TRUNCATE iris_staging.signal_ingest, iris_staging.signal_ingest_reject, "
            "iris_staging.harvest_watermark, iris_staging.harvest_run CASCADE"
        )
    yield harvest_dsn


def rows(dsn: str, query: str) -> list[tuple[Any, ...]]:
    with psycopg.connect(dsn) as conn:
        return conn.execute(query).fetchall()  # type: ignore[arg-type]


async def _harvest(dsn: str, normaliser: Normaliser, scenario: str = "happy_path") -> Any:
    """One full harvest through the real sink, as the CLI would run it."""
    harvester = pipeline(
        normaliser,
        sink=PostgisSink(dsn),
        watermarks=WatermarkStore(dsn),
        dsn=dsn,
    )
    adapter = FixtureAdapter(settings(), scenario)
    try:
        return await harvester.sweep(adapter, SweepOptions())
    finally:
        await adapter.aclose()


@pytest.mark.integration
async def test_end_to_end_harvest_twice_is_idempotent(
    clean_db: str, normaliser: Normaliser
) -> None:
    """THE acceptance property: a second pass over identical input writes nothing.

    Asserted against the data as well as the counters — ``updated_at`` must not move
    while ``last_seen_at`` must, because liveness tracking and mutation tracking are
    separate columns on purpose.
    """
    first = await _harvest(clean_db, normaliser)
    assert first.counts.inserted == 4
    assert first.counts.updated == 0
    assert first.status is RunStatus.SUCCESS

    before = rows(
        clean_db,
        "SELECT source_record_id, updated_at, last_seen_at FROM iris_staging.signal_ingest "
        "ORDER BY source_record_id",
    )

    second = await _harvest(clean_db, normaliser)
    assert second.counts.inserted == 0
    assert second.counts.updated == 0
    assert second.counts.unchanged == 4
    assert second.report.verdict is not Verdict.FAIL

    after = rows(
        clean_db,
        "SELECT source_record_id, updated_at, last_seen_at FROM iris_staging.signal_ingest "
        "ORDER BY source_record_id",
    )
    assert [r[0] for r in before] == [r[0] for r in after]
    assert [r[1] for r in before] == [r[1] for r in after]  # updated_at frozen
    assert all(a[2] >= b[2] for a, b in zip(after, before, strict=True))  # last_seen_at moved

    # The re-run delta the report prints is the same claim.
    delta = second.report.as_dict()["rerun_delta"]
    assert delta["previous_run"]["inserted"] == 4
    assert delta["idempotent"] is True


@pytest.mark.integration
async def test_the_run_row_exists_and_is_finished_with_counts(
    clean_db: str, normaliser: Normaliser
) -> None:
    result = await _harvest(clean_db, normaliser)
    run = rows(
        clean_db,
        "SELECT status, records_fetched, inserted, updated, unchanged, rejected, "
        "finished_at IS NOT NULL FROM iris_staging.harvest_run",
    )
    assert len(run) == 1
    status, fetched, inserted, updated, unchanged, rejected, finished = run[0]
    assert status == "succeeded"
    assert fetched == 4
    assert (inserted, updated, unchanged, rejected) == (4, 0, 0, 0)
    assert finished is True
    assert result.run_id is not None


@pytest.mark.integration
async def test_rejects_land_in_the_dead_letter_with_a_valid_run_reference(
    clean_db: str, normaliser: Normaliser
) -> None:
    """Proves the FK ordering: a reject row cannot exist unless start_run ran first."""
    result = await _harvest(clean_db, normaliser, "coordinate_out_of_domain")
    stored = rows(
        clean_db,
        "SELECT r.reason_code, r.source_body_id, r.harvest_run_id = h.id "
        "FROM iris_staging.signal_ingest_reject r JOIN iris_staging.harvest_run h "
        "ON h.id = r.harvest_run_id",
    )
    assert len(stored) == 1
    assert stored[0][0] == "geo_out_of_crs_domain"
    assert stored[0][2] is True
    assert result.report.verdict is Verdict.FAIL


@pytest.mark.integration
async def test_a_clean_sweep_advances_the_watermark(clean_db: str, normaliser: Normaliser) -> None:
    result = await _harvest(clean_db, normaliser)
    assert all(b.watermark == AdvanceOutcome.ADVANCED.value for b in result.bodies)
    stored = rows(
        clean_db,
        "SELECT body_id, last_modified_seen FROM iris_staging.harvest_watermark ORDER BY body_id",
    )
    assert len(stored) == 2
    assert all(value is not None for _, value in stored)


@pytest.mark.integration
async def test_a_degraded_body_does_not_advance_its_watermark(
    clean_db: str, normaliser: Normaliser
) -> None:
    """The window has to be retried, not skipped. This is the lossy-incremental guard."""
    harvester = pipeline(
        normaliser,
        sink=PostgisSink(clean_db),
        watermarks=WatermarkStore(clean_db),
        dsn=clean_db,
    )
    adapter = StubAdapter(raises=SourceUnavailable("the council's server is down"))
    result = await harvester.sweep(adapter, SweepOptions())
    assert result.bodies[0].watermark == AdvanceOutcome.SKIPPED_UNCLEAN_SWEEP.value
    assert rows(clean_db, "SELECT * FROM iris_staging.harvest_watermark") == []
    finished = rows(clean_db, "SELECT status FROM iris_staging.harvest_run")
    assert finished == [("partial",)]


@pytest.mark.integration
async def test_the_sentinel_modified_value_never_reaches_the_watermark(
    clean_db: str, normaliser: Normaliser
) -> None:
    await _harvest(clean_db, normaliser, "sentinel_modified_date")
    stored = rows(clean_db, "SELECT last_modified_seen FROM iris_staging.harvest_watermark")
    assert stored
    for (value,) in stored:
        assert value is None or value > datetime(1990, 1, 1, tzinfo=UTC)


@pytest.mark.integration
async def test_report_can_be_rebuilt_from_the_database(
    clean_db: str, normaliser: Normaliser
) -> None:
    from iris_signal_harvester.reporting.quality import build_report, observations_from_db

    result = await _harvest(clean_db, normaliser)
    summary, observations, bodies = observations_from_db(clean_db, "iris_staging", result.run_id)
    rebuilt = build_report(
        summary, observations, bodies, Thresholds.load(REPO / "config" / "thresholds.yaml")
    )
    assert summary.counts.inserted == 4
    assert observations.staged == 4
    assert len(bodies) == 2
    assert rebuilt.verdict in (Verdict.PASS, Verdict.WARN)
