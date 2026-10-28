"""The sweep orchestrator: everything that must be written once rather than per source.

Why this file is where the hard parts live
------------------------------------------
``SourceAdapter`` is deliberately tiny — one method touches the network — so that
retry, rate limiting, pagination safety, deduplication, watermarking, validation and
loading are implemented and tested exactly once, here, instead of being re-invented
(and re-broken) per source. A new council system is a ~150-line adapter, not a new
copy of this logic.

Ordering guarantee: the run is registered in ``harvest_run`` **before** anything is
written. ``signal_ingest`` and ``signal_ingest_reject`` both carry a foreign key to
it, so loading first would fail the whole batch on the FK; and a run row that exists
only after a successful load would make a crashed run invisible. ``finish_run`` always
follows, with the final status and counts, including on failure.

The three pagination guard rails
--------------------------------
Municipal endpoints are not hostile, but they are frequently broken, and an unbounded
``while next: fetch(next)`` loop against a broken one is how a polite harvester turns
into an accidental denial-of-service. Each guard rail catches a different failure:

1. **Cursor loop** — a seen-cursor set. Catches ``next`` pointing back at a page we
   already requested (A → B → A). Cursors are only recorded once they have been
   *successfully* fetched, so retrying a failed page is not mistaken for a loop.
2. **Page fingerprint** — identical consecutive id sets. Catches a server that hands
   out fresh-looking cursors while ignoring them and re-serving page 1 forever. The
   fingerprint includes malformed records, so two different all-malformed pages do
   not both look like "the empty page".
3. **Zero yield** — ``settings.zero_yield_pages_before_stop`` consecutive pages whose
   ids were all seen already. Catches overlapping windows and unstable sort orders
   that make progress asymptotic rather than finite.

A guard rail firing means the window was *not* fully covered, so the body's watermark
is not advanced: the window is retried next run rather than silently skipped. The same
applies to a tripped circuit breaker, an access refusal and a transport failure. This
is the rule that keeps "incremental" from meaning "lossy".

Blast radius
------------
Bodies are swept with bounded concurrency (``settings.max_concurrent_bodies``) and
serially within a body — pages of one collection are inherently ordered, and
hammering one council in parallel is exactly the impoliteness the rate limiter
exists to prevent. Each body's sweep catches its own failures, so one failing
municipality degrades that municipality and the run finishes ``degraded``, not
``failed``.

Shared mutable state (the in-run seen-id set and the report accumulator) is mutated
only in stretches of code containing no ``await``, so under asyncio's cooperative
scheduling those updates are atomic without a lock.

Records are normalised once a body's pages have all been read, not page by page: a
record that appears on two pages of one run is resolved to the copy with the greater
parseable ``modified`` *before* it is normalised, so the version that is loaded is
the version the watermark reflects. Database calls are sync
(``psycopg``) and are therefore dispatched with ``asyncio.to_thread`` so one body's
load does not block another body's fetch.
"""

from __future__ import annotations

import asyncio
import random
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

import structlog
import yaml

from iris_signal_harvester.adapters.base import Cursor, Page, SourceAdapter
from iris_signal_harvester.config import Settings
from iris_signal_harvester.domain.models import (
    Body,
    CanonicalSignal,
    RawDocument,
    RejectCode,
    RejectedRecord,
)
from iris_signal_harvester.domain.normalise import Normaliser, Verdict
from iris_signal_harvester.errors import (
    AccessRestricted,
    ConfigurationError,
    HarvesterError,
    SourceUnavailable,
)
from iris_signal_harvester.http.resilience import CircuitBreaker, CircuitOpen, with_resilience
from iris_signal_harvester.reporting.quality import (
    BodyHealth,
    Observations,
    QualityReport,
    RunSummary,
    Thresholds,
    build_report,
)
from iris_signal_harvester.sink.postgis import LoadCounts, PostgisSink
from iris_signal_harvester.sink.watermark import (
    AdvanceOutcome,
    ModifiedTracker,
    WatermarkStore,
    max_modified,
)

__all__ = [
    "Pipeline",
    "RunStatus",
    "SourceRegistry",
    "StopReason",
    "SweepOptions",
    "SweepResult",
    "build_adapter",
]

log = structlog.get_logger(__name__)


class RunStatus(StrEnum):
    """Final status of a run.

    The *values* are the vocabulary ``harvest_run``'s ``ck_harvest_run_status`` CHECK
    accepts, and the same vocabulary the quality report and the CLI print. ``DEGRADED``
    is ``partial``: at least one body failed while the rest completed.
    """

    SUCCESS = "succeeded"
    DEGRADED = "partial"
    FAILED = "failed"


class StopReason(StrEnum):
    """Why a body's pagination loop ended. Only ``exhausted`` and ``not_modified``
    mean the window was fully covered; every other reason leaves the watermark alone."""

    EXHAUSTED = "exhausted"
    NOT_MODIFIED = "not_modified"
    CURSOR_LOOP = "cursor_loop"
    IDENTICAL_PAGE = "identical_page_fingerprint"
    ZERO_YIELD = "zero_yield"
    MAX_PAGES = "max_pages_per_body"
    RECORD_LIMIT = "record_limit_reached"
    CIRCUIT_OPEN = "circuit_open"
    ACCESS_RESTRICTED = "access_restricted"
    SOURCE_UNAVAILABLE = "source_unavailable"
    ERROR = "error"

    @property
    def clean(self) -> bool:
        return self in (StopReason.EXHAUSTED, StopReason.NOT_MODIFIED)


@dataclass(frozen=True)
class SweepOptions:
    """Everything the caller may vary per invocation."""

    since: datetime | None = None
    limit: int | None = None
    dry_run: bool = False
    strict: bool = False
    bodies: tuple[str, ...] = ()
    run_id: UUID | None = None
    now: datetime | None = None


@dataclass
class SweepResult:
    """What one sweep did, plus the finished quality report."""

    run_id: UUID
    source_system: str
    status: RunStatus
    counts: LoadCounts
    observations: Observations
    bodies: tuple[BodyHealth, ...]
    report: QualityReport
    dry_run: bool

    @property
    def exit_code(self) -> int:
        return self.report.exit_code


_Key = tuple[str, str, str]


@dataclass
class _Pending:
    """One not-yet-normalised record and its parseable ``modified`` (``None`` if unusable)."""

    document: RawDocument
    modified: datetime | None


@dataclass
class _BodyRun:
    """Mutable per-body bookkeeping, collapsed into a ``BodyHealth`` at the end."""

    body: Body
    pages: int = 0
    fetched: int = 0
    emitted: int = 0
    below_threshold: int = 0
    rejected: int = 0
    duplicates: int = 0
    stop: StopReason = StopReason.EXHAUSTED
    error: str | None = None
    watermark: str = "not_attempted"
    signals: list[CanonicalSignal] = field(default_factory=list)
    rejects: list[RejectedRecord] = field(default_factory=list)
    counts: LoadCounts = field(default_factory=lambda: LoadCounts(0, 0, 0))
    # Raw documents awaiting normalisation, keyed by natural key, in arrival order.
    pending: dict[_Key, _Pending] = field(default_factory=dict)
    finalised: bool = False
    persist_failed: bool = False
    # False when an explicit --since means this sweep cannot vouch for the whole window.
    may_advance: bool = True

    def health(self) -> BodyHealth:
        return BodyHealth(
            body_id=self.body.id,
            name=self.body.name,
            licence=self.body.licence,
            ags=self.body.ags,
            pages=self.pages,
            fetched=self.fetched,
            emitted=self.emitted,
            below_threshold=self.below_threshold,
            rejected=self.rejected,
            duplicates=self.duplicates,
            stop_reason=self.stop.value,
            degraded=not self.stop.clean,
            watermark=self.watermark,
            error=self.error,
        )


@dataclass
class _RunContext:
    """State shared by every body of one run."""

    run_id: UUID
    source_system: str
    options: SweepOptions
    observations: Observations
    seen: dict[_Key, _BodyRun] = field(default_factory=dict)
    refused_hosts: set[str] = field(default_factory=set)
    notes: list[str] = field(default_factory=list)

    @property
    def limit_reached(self) -> bool:
        limit = self.options.limit
        return limit is not None and self.observations.fetched >= limit


class Pipeline:
    """Wires adapter, normaliser, sink and watermark store into one sweep."""

    def __init__(
        self,
        settings: Settings,
        normaliser: Normaliser,
        thresholds: Thresholds,
        *,
        sink: PostgisSink | None = None,
        watermarks: WatermarkStore | None = None,
        dsn: str | None = None,
        breaker_threshold: int = 3,
        sleep: Any = None,
        rng: random.Random | None = None,
    ) -> None:
        self._settings = settings
        self._normaliser = normaliser
        self._thresholds = thresholds
        self._sink = sink
        self._watermarks = watermarks
        self._dsn = dsn
        self._breaker_threshold = breaker_threshold
        self._sleep = sleep if sleep is not None else asyncio.sleep
        self._rng = rng or random.Random()

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        normaliser: Normaliser | None = None,
        thresholds: Thresholds | None = None,
        gazetteer_path: Path | None = None,
        write: bool = True,
    ) -> Pipeline:
        """Build a pipeline. With ``write=False`` no database handle is created at all.

        ``write=False`` is what ``--dry-run`` uses: not a flag checked at the last
        moment before an INSERT, but a pipeline that has no sink to write to.
        """
        dsn = settings.require_dsn() if write else None
        return cls(
            settings,
            normaliser or Normaliser.from_settings(settings, gazetteer_path=gazetteer_path),
            thresholds or Thresholds.load(settings.thresholds_path),
            sink=PostgisSink(dsn, settings.db_schema) if dsn else None,
            watermarks=WatermarkStore(dsn, settings.db_schema) if dsn else None,
            dsn=dsn,
        )

    # -- the orchestrator ------------------------------------------------------

    async def sweep(
        self, adapter: SourceAdapter, options: SweepOptions | None = None
    ) -> SweepResult:
        """Sweep every body of ``adapter`` and return the run's outcome and report."""
        opts = options or SweepOptions()
        run_id = opts.run_id or uuid.uuid4()
        started_at = opts.now or datetime.now(UTC)
        ctx = _RunContext(
            run_id=run_id,
            source_system=adapter.source_system,
            options=opts,
            observations=Observations(),
        )
        writing = self._sink is not None and not opts.dry_run
        if opts.dry_run:
            ctx.notes.append(
                "Dry run: everything up to the sink ran, nothing was written and no watermark "
                "was advanced."
            )
        elif self._sink is None:
            ctx.notes.append(
                "No database handle was configured, so nothing was written. This is a "
                "reporting-only run."
            )

        capabilities = adapter.capabilities()
        if not capabilities.supports_modified_since and opts.since is not None:
            ctx.notes.append(
                "This source does not support modified_since, so --since could not be "
                "applied and a FULL sweep was performed instead."
            )

        # (1) Register the run BEFORE anything writes: both data tables have a
        #     foreign key to harvest_run.
        if writing and self._sink is not None:
            await asyncio.to_thread(
                self._sink.start_run,
                run_id,
                adapter.source_system,
                config_hash=self._settings.config_hash(),
                taxonomy_version=self._normaliser.taxonomy_version,
            )

        status = RunStatus.SUCCESS
        runs: list[_BodyRun] = []
        try:
            bodies = await self._discover(adapter, opts)
        except (AccessRestricted, SourceUnavailable, HarvesterError) as exc:
            # Discovery failing is a run-level failure: there is nothing to sweep.
            status = RunStatus.FAILED
            ctx.notes.append(f"body discovery failed: {type(exc).__name__}: {exc}")
            bodies = ()
        else:
            semaphore = asyncio.Semaphore(self._settings.max_concurrent_bodies)

            async def guarded(body: Body) -> _BodyRun:
                async with semaphore:
                    return await self._sweep_body(adapter, body, ctx)

            outcomes = await asyncio.gather(
                *(guarded(body) for body in bodies), return_exceptions=True
            )
            for body, outcome in zip(bodies, outcomes, strict=True):
                if isinstance(outcome, _BodyRun):
                    runs.append(outcome)
                    continue
                if not isinstance(outcome, Exception):
                    raise outcome  # cancellation and friends are not ours to swallow
                runs.append(_failed_run(body, outcome, ctx))
            if any(not r.stop.clean for r in runs):
                status = RunStatus.DEGRADED
            if runs and all(r.persist_failed for r in runs):
                status = RunStatus.FAILED
            capped = [r.body.name or r.body.id for r in runs if r.stop is StopReason.MAX_PAGES]
            if capped:
                shown = ", ".join(capped[:10]) + (", ..." if len(capped) > 10 else "")
                ctx.notes.append(
                    f"{len(capped)} body/bodies hit max_pages_per_body "
                    f"({self._settings.max_pages_per_body}) and could not finish: {shown}. Their "
                    "watermarks were held, so every run restarts at page 1 and a larger body "
                    "never completes its backfill. Raise IRIS_MAX_PAGES_PER_BODY for them."
                )

        counts = _total(r.counts for r in runs)
        finished_at = datetime.now(UTC) if opts.now is None else opts.now
        if writing and self._sink is not None:
            await asyncio.to_thread(
                self._sink.finish_run,
                run_id,
                status=status.value,
                records_fetched=ctx.observations.fetched,
                counts=counts,
                # adapter-malformed + normaliser-rejected + the sink's own dead letters
                # (``counts.rejected``, folded in by ``observe_sink_rejects``)
                rejected=ctx.observations.dead_lettered,
                notes={
                    "bodies": len(runs),
                    "degraded_bodies": [r.body.id for r in runs if not r.stop.clean],
                    "stop_reasons": {r.body.id: r.stop.value for r in runs},
                    "notes": ctx.notes,
                },
            )

        summary = RunSummary(
            run_id=run_id,
            source_system=adapter.source_system,
            started_at=started_at,
            finished_at=finished_at,
            status=status.value,
            counts=counts,
            dry_run=opts.dry_run or self._sink is None,
            strict=opts.strict or self._settings.strict,
            since=opts.since,
            limit=opts.limit,
            bodies_total=len(runs),
            config_hash=self._settings.config_hash(),
            taxonomy_version=self._normaliser.taxonomy_version,
            previous_counts=self._previous_counts(adapter.source_system, run_id)
            if writing
            else None,
            notes=tuple(ctx.notes),
        )
        health = tuple(r.health() for r in runs)
        report = build_report(summary, ctx.observations, health, self._thresholds)
        if writing and self._sink is not None:
            # Persist the verdict the run itself computed, so `iris-harvest report` and the
            # dashboard read it from the database instead of a gitignored reports/*.json.
            try:
                await asyncio.to_thread(
                    self._sink.record_verdict,
                    run_id,
                    report.verdict.value,
                    [{"level": f.level.value, "rule": f.rule} for f in report.findings],
                )
            except Exception as exc:
                # The sweep is done and its data is stored; losing the verdict degrades a
                # later `report` to UNKNOWN (never to a wrong grade), so it is not fatal.
                log.warning("verdict_not_persisted", run_id=str(run_id), error=str(exc))
        return SweepResult(
            run_id=run_id,
            source_system=adapter.source_system,
            status=status,
            counts=counts,
            observations=ctx.observations,
            bodies=health,
            report=report,
            dry_run=summary.dry_run,
        )

    async def discover(self, adapter: SourceAdapter) -> tuple[Body, ...]:
        """List the bodies a source exposes, without harvesting anything."""
        return await self._discover(adapter, SweepOptions())

    # -- per body --------------------------------------------------------------

    async def _discover(self, adapter: SourceAdapter, options: SweepOptions) -> tuple[Body, ...]:
        wanted = set(options.bodies)
        found: list[Body] = []
        async for body in adapter.discover_bodies():
            if not wanted or body.id in wanted or (body.name or "") in wanted:
                found.append(body)
        return tuple(found)

    async def _sweep_body(self, adapter: SourceAdapter, body: Body, ctx: _RunContext) -> _BodyRun:
        run = _BodyRun(body=body)
        if body.ags is not None and not _is_ags(body.ags):
            ctx.observations.bodies_with_bad_ags.add(body.id)
        tracker = ModifiedTracker(self._settings, now=ctx.options.now)
        breaker = CircuitBreaker(
            f"{adapter.source_system}:{body.id}", threshold=self._breaker_threshold
        )
        since, run.may_advance = await self._since_for(adapter, body, ctx.options)
        host = _host_of(body)

        cursor: Cursor | None = None
        seen_cursors: set[str] = set()
        previous_fingerprint: str | None = None
        zero_yield_streak = 0

        while True:
            if run.pages >= self._settings.max_pages_per_body:
                run.stop = StopReason.MAX_PAGES
                break
            if ctx.limit_reached:
                run.stop = StopReason.RECORD_LIMIT
                break
            if host is not None and host in ctx.refused_hosts:
                # A refusal is a property of the host, not of one body: do not knock again.
                run.stop = StopReason.ACCESS_RESTRICTED
                run.error = f"skipped: {host} already refused access during this run"
                break
            # Guard rail 1 — cursor loop.
            if cursor is not None and cursor.fingerprint() in seen_cursors:
                log.warning("cursor_loop_detected", body=body.id, cursor=cursor.fingerprint())
                run.stop = StopReason.CURSOR_LOOP
                break

            try:
                page = await self._fetch(adapter, body, cursor, since, breaker)
            except CircuitOpen as exc:
                run.stop, run.error = StopReason.CIRCUIT_OPEN, str(exc)
                break
            except AccessRestricted as exc:
                # Terminal by design: recorded as a finding, never retried or worked around.
                log.warning("access_restricted", body=body.id, error=str(exc))
                if host is not None:
                    ctx.refused_hosts.add(host)
                run.stop, run.error = StopReason.ACCESS_RESTRICTED, str(exc)
                break
            except SourceUnavailable as exc:
                # The HTTP client already exhausted its retries; the breaker counts this
                # as one failed operation and the same cursor is attempted again until
                # the breaker opens. The cursor is not in seen_cursors yet, so guard
                # rail 1 does not mistake a retry for a loop.
                run.error = str(exc)
                if breaker.is_open:
                    run.stop = StopReason.SOURCE_UNAVAILABLE
                    break
                continue
            except HarvesterError as exc:
                run.stop, run.error = StopReason.ERROR, f"{type(exc).__name__}: {exc}"
                break

            run.pages += 1
            ctx.observations.pages_fetched += 1
            if cursor is not None:
                seen_cursors.add(cursor.fingerprint())

            if page.not_modified:
                run.stop = StopReason.NOT_MODIFIED
                break

            # Guard rail 2 — the server is re-serving the same page.
            fingerprint = page.id_fingerprint()
            if previous_fingerprint is not None and fingerprint == previous_fingerprint:
                log.warning("identical_page_fingerprint", body=body.id, fingerprint=fingerprint)
                run.stop = StopReason.IDENTICAL_PAGE
                break
            previous_fingerprint = fingerprint

            new_ids, skipped = self._consume(page, body, ctx, run, tracker)

            if skipped:
                # The limit cut this page short, so records were left unread even when
                # there is no next page. That is not "exhausted".
                run.stop = StopReason.RECORD_LIMIT
                break

            if page.next_cursor is None:
                run.stop = StopReason.EXHAUSTED
                break

            # Guard rail 3 — pages that yield nothing new.
            if new_ids == 0:
                zero_yield_streak += 1
                if zero_yield_streak >= self._settings.zero_yield_pages_before_stop:
                    log.warning("zero_yield_stop", body=body.id, pages=zero_yield_streak)
                    run.stop = StopReason.ZERO_YIELD
                    break
            else:
                zero_yield_streak = 0
            cursor = page.next_cursor

        if not run.stop.clean:
            tracker.mark_unclean()
        try:
            self._finalise(run, ctx)
            await self._persist(run, tracker, ctx)
        except Exception as exc:
            # A sink (or normaliser) failure costs this body, never the run: finish_run
            # must still follow, and the other bodies' work must still be kept.
            log.error("body_persist_failed", body=body.id, error=str(exc), exc_info=True)
            run.stop, run.error = StopReason.ERROR, f"{type(exc).__name__}: {exc}"
            run.persist_failed = True
            run.watermark = "not advanced (persist failed)"
            tracker.mark_unclean()
            ctx.notes.append(f"body {body.id} failed to persist: {type(exc).__name__}: {exc}")
        return run

    def _consume(
        self,
        page: Page,
        body: Body,
        ctx: _RunContext,
        run: _BodyRun,
        tracker: ModifiedTracker,
    ) -> tuple[int, int]:
        """Collect one page's records. Returns ``(new keys, records left unread)``.

        Contains no ``await`` on purpose. That is what makes the shared seen-key map
        and the shared report accumulator safe to mutate without a lock while other
        bodies are in flight. Normalisation is deferred to ``_finalise``.
        """
        # Adapters cannot build a RejectedRecord (they have no run id), so they report
        # (reason_code, raw_payload) pairs and the pipeline dead-letters them here. The
        # rest of the page is still processed: one bad record never costs a page.
        for reason_code, raw in page.malformed:
            reject = _malformed_reject(reason_code, raw, ctx, body)
            run.rejects.append(reject)
            run.rejected += 1
            run.fetched += 1
            ctx.observations.fetched += 1
            ctx.observations.observe_reject(reject, from_adapter=True)
            tracker.observe(raw.get("modified"))

        new_keys = 0
        for index, document in enumerate(page.records):
            if ctx.limit_reached:
                return new_keys, len(page.records) - index
            run.fetched += 1
            ctx.observations.fetched += 1
            modified_raw = document.payload.get("modified")
            tracker.observe(modified_raw)
            modified = max_modified([modified_raw], self._settings, now=ctx.options.now)

            key: _Key = (document.source_system, document.source_body_id, document.source_record_id)
            owner = ctx.seen.get(key)
            if owner is None:
                ctx.seen[key] = run
                run.pending[key] = _Pending(document, modified)
                new_keys += 1
                continue
            # The same record on two pages of one run: an unstable cursor or an
            # overlapping window. Exactly one copy survives, and it is the one with
            # the greater parseable ``modified`` (the later page on a tie), so the
            # loaded row and the watermark describe the same version.
            run.duplicates += 1
            ctx.observations.observe_duplicate()
            held = owner.pending.get(key)
            if held is not None and _rank(modified) >= _rank(held.modified):
                owner.pending[key] = _Pending(document, modified)
        return new_keys, 0

    def _finalise(self, run: _BodyRun, ctx: _RunContext) -> None:
        """Normalise the surviving copies. Runs after the body's last page, no ``await``."""
        pending, run.pending = run.pending, {}
        run.finalised = True
        for item in pending.values():
            outcome = self._normaliser.normalise(
                item.document, run.body, ctx.run_id, now=ctx.options.now
            )
            ctx.observations.observe_notes(outcome.notes)
            if outcome.verdict is Verdict.EMITTED and outcome.signal is not None:
                run.signals.append(outcome.signal)
                run.emitted += 1
                ctx.observations.observe_signal(outcome.signal)
            elif outcome.verdict is Verdict.BELOW_THRESHOLD:
                run.below_threshold += 1
                ctx.observations.observe_below_threshold()
            elif outcome.reject is not None:
                run.rejects.append(outcome.reject)
                run.rejected += 1
                ctx.observations.observe_reject(outcome.reject)

    async def _fetch(
        self,
        adapter: SourceAdapter,
        body: Body,
        cursor: Cursor | None,
        since: datetime | None,
        breaker: CircuitBreaker,
    ) -> Page:
        """One page, with breaker accounting but no extra retries.

        Retries live in the HTTP client, which already honours ``Retry-After`` and
        bounds itself with ``settings.max_retries``. Retrying again here would
        multiply the request count against a council's server, so this call is
        ``max_retries=0``: ``with_resilience`` is used for its breaker contract only.
        """

        async def operation() -> Page:
            return await adapter.fetch_page(body, cursor, modified_since=since)

        return await with_resilience(
            operation,
            max_retries=0,
            sleep=self._sleep,
            rng=self._rng,
            breaker=breaker,
        )

    async def _since_for(
        self, adapter: SourceAdapter, body: Body, options: SweepOptions
    ) -> tuple[datetime | None, bool]:
        """``(modified_since, may_advance_watermark)`` for this body.

        The window is the explicit ``--since``, else the stored watermark minus the
        overlap. An explicit ``--since`` only vouches for ``[since, now)``: if the
        stored watermark is older than that (or absent) the gap ``[watermark, since)``
        was never read, and advancing over it would skip it forever. So an explicit
        ``--since`` may advance the watermark only when it reaches back past
        ``stored - overlap``.
        """
        if not adapter.capabilities().supports_modified_since:
            return None, True
        overlap = timedelta(hours=self._settings.overlap_hours)
        if options.since is not None:
            if self._watermarks is None or options.dry_run:
                return options.since, True  # nothing is persisted, so nothing can be skipped
            stored = await asyncio.to_thread(self._watermarks.read, adapter.source_system, body.id)
            return options.since, stored is not None and options.since <= stored - overlap
        if self._watermarks is None or options.dry_run:
            return None, True
        stored = await asyncio.to_thread(self._watermarks.read, adapter.source_system, body.id)
        if stored is None:
            return None, True
        # Municipal systems back-date edits; without the overlap those records are
        # lost permanently. Content hashing makes re-reading them free.
        return stored - overlap, True

    async def _persist(self, run: _BodyRun, tracker: ModifiedTracker, ctx: _RunContext) -> None:
        """Write this body's batch, then advance its watermark if the sweep was clean."""
        if self._sink is None or ctx.options.dry_run:
            run.watermark = "skipped (no write)"
            return
        sink = self._sink
        if run.rejects:
            await asyncio.to_thread(sink.write_rejects, run.rejects)
        if run.signals:
            run.counts = await asyncio.to_thread(sink.load, run.signals)
            if run.counts.rejected:
                # The sink's own guard dead-lettered these after ``observe_signal`` had
                # counted them as staged. Move them, or the funnel does not balance.
                ctx.observations.observe_sink_rejects(run.counts.rejected)
                run.emitted -= run.counts.rejected
                run.rejected += run.counts.rejected
        if self._watermarks is None:
            run.watermark = "no store"
            return
        if not run.may_advance:
            run.watermark = "held (explicit --since)"
            log.info("watermark_held_explicit_since", body=run.body.id)
            return
        outcome = await asyncio.to_thread(
            lambda: self._require(self._watermarks).advance(
                ctx.source_system,
                run.body.id,
                tracker.maximum,
                clean_sweep=tracker.clean,
                run_id=ctx.run_id,
                now=ctx.options.now,
            )
        )
        run.watermark = outcome.value
        if outcome is AdvanceOutcome.SKIPPED_UNCLEAN_SWEEP:
            # The whole point: a degraded body retries its window instead of skipping it.
            log.warning("watermark_held_back", body=run.body.id, stop_reason=run.stop.value)

    @staticmethod
    def _require(store: WatermarkStore | None) -> WatermarkStore:
        if store is None:  # pragma: no cover - guarded by the caller
            raise ConfigurationError("no watermark store configured")
        return store

    def _previous_counts(self, source_system: str, run_id: UUID) -> LoadCounts | None:
        """The most recent earlier run's counts, for the re-run delta section."""
        if self._dsn is None:
            return None
        from psycopg import sql

        from iris_signal_harvester.sink._common import connect, table

        query = sql.SQL(
            "SELECT inserted, updated, unchanged FROM {} "
            "WHERE source_system = %s AND id <> %s AND finished_at IS NOT NULL "
            "ORDER BY started_at DESC LIMIT 1"
        ).format(table(self._settings.db_schema, "harvest_run"))
        try:
            with connect(self._dsn) as conn, conn.cursor() as cur:
                cur.execute(query, (source_system, str(run_id)))
                row = cur.fetchone()
        except Exception as exc:  # pragma: no cover - reporting must never fail a run
            log.warning("previous_counts_unavailable", error=str(exc))
            return None
        if row is None:
            return None
        return LoadCounts(int(row[0]), int(row[1]), int(row[2]))


def _total(counts: Iterable[LoadCounts]) -> LoadCounts:
    inserted = updated = unchanged = duplicates = rejected = 0
    for c in counts:
        inserted += c.inserted
        updated += c.updated
        unchanged += c.unchanged
        duplicates += c.duplicates_in_batch
        rejected += c.rejected
    return LoadCounts(inserted, updated, unchanged, duplicates, rejected)


def _rank(moment: datetime | None) -> datetime:
    """Sort key for choosing between duplicate copies: unparseable loses to parseable."""
    return moment if moment is not None else datetime.min.replace(tzinfo=UTC)


def _host_of(body: Body) -> str | None:
    """The host a body's requests go to (its paper endpoint, else its own id)."""
    for candidate in (body.paper_endpoint, body.id):
        if candidate:
            host = urlsplit(candidate).hostname
            if host:
                return host.lower()
    return None


def _failed_run(body: Body, exc: Exception, ctx: _RunContext) -> _BodyRun:
    """A body whose sweep raised something nobody anticipated: degrade it, keep the run."""
    log.error("body_sweep_crashed", body=body.id, error=str(exc), exc_info=exc)
    ctx.notes.append(f"body {body.id} crashed: {type(exc).__name__}: {exc}")
    run = _BodyRun(body=body)
    run.stop, run.error = StopReason.ERROR, f"{type(exc).__name__}: {exc}"
    run.watermark = "not advanced (body crashed)"
    return run


def _is_ags(value: str) -> bool:
    candidate = value.strip()
    return len(candidate) == 8 and candidate.isdigit()


def _malformed_reject(
    reason_code: str, raw: Mapping[str, Any], ctx: _RunContext, body: Body
) -> RejectedRecord:
    """Convert a ``Page.malformed`` entry into a dead-letter row.

    The adapter knows the reason and the payload; the pipeline knows the run and the
    body. Neither can build the row alone, which is why the pair exists.
    """
    try:
        code = RejectCode(reason_code)
    except ValueError:
        code = RejectCode.SCHEMA_VALIDATION_FAILED
    # ``id`` is the identity of an OParl object; ``reference`` is an editable file number.
    reference = raw.get("id") or raw.get("reference")
    return RejectedRecord(
        harvest_run_id=ctx.run_id,
        source_system=ctx.source_system,
        source_body_id=body.id,
        source_record_id=str(reference) if isinstance(reference, str | int) else None,
        reason_detail=(f"adapter could not build a RawDocument from this record ({reason_code})"),
        reason_code=code,
        raw_payload=dict(raw),
    )


# ---------------------------------------------------------------------------
# source registry (config/bodies.yaml) and adapter construction
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceEntry:
    """One entry of ``config/bodies.yaml``'s ``sources`` map."""

    name: str
    adapter: str
    health: str = "unknown"
    licence: str = "unknown"
    verified_at: str | None = None
    system_url: str | None = None
    base_url: str | None = None
    scenario: str | None = None
    default: bool = False
    description: str = ""

    @property
    def summary(self) -> str:
        endpoint = self.system_url or self.base_url or self.scenario or "(none)"
        return (
            f"{self.name}: adapter={self.adapter} health={self.health} "
            f"licence={self.licence} endpoint={endpoint}"
        )


class SourceRegistry:
    """``config/bodies.yaml``, read once.

    The registry records each source's licence and health state so that choosing a
    source is a documented decision rather than a hard-coded URL. Bodies are not
    configured here: OParl bodies are discovered from the System object at runtime,
    because writing down 26 body ids that were never individually probed would be a
    guess.
    """

    def __init__(self, data: Mapping[str, Any]) -> None:
        raw_sources = data.get("sources")
        if not isinstance(raw_sources, Mapping) or not raw_sources:
            raise ConfigurationError("bodies.yaml defines no 'sources'")
        self.version = str(data.get("version", "unversioned"))
        self.default_source = str(data.get("default_source", ""))
        self.sources: dict[str, SourceEntry] = {}
        for name, entry in raw_sources.items():
            if not isinstance(entry, Mapping):
                raise ConfigurationError(f"bodies.yaml: source {name!r} is not a mapping")
            self.sources[str(name)] = SourceEntry(
                name=str(name),
                adapter=str(entry.get("adapter", name)),
                health=str(entry.get("health", "unknown")),
                licence=str(entry.get("licence", "unknown")),
                verified_at=_opt_str(entry.get("verified_at")),
                system_url=_opt_str(entry.get("system_url")),
                base_url=_opt_str(entry.get("base_url")),
                scenario=_opt_str(entry.get("scenario")),
                default=bool(entry.get("default", False)),
                description=str(entry.get("description", "")).strip(),
            )
        if self.default_source and self.default_source not in self.sources:
            raise ConfigurationError(
                f"bodies.yaml: default_source {self.default_source!r} is not a declared source"
            )

    @classmethod
    def load(cls, path: Path | str) -> SourceRegistry:
        source = Path(path)
        try:
            raw = yaml.safe_load(source.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise ConfigurationError(f"cannot read source registry {source}: {exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigurationError(f"source registry {source} must be a YAML mapping")
        return cls(raw)

    def entry(self, name: str) -> SourceEntry:
        try:
            return self.sources[name]
        except KeyError:
            raise ConfigurationError(
                f"unknown source {name!r}; declared sources: {', '.join(sorted(self.sources))}"
            ) from None


def _opt_str(value: object) -> str | None:
    return str(value) if isinstance(value, str) and value.strip() else None


def build_adapter(
    settings: Settings,
    *,
    registry: SourceRegistry | None = None,
    source: str | None = None,
    scenario: str | None = None,
    system_url: str | None = None,
) -> SourceAdapter:
    """Construct the adapter the registry describes for ``source``.

    Kept here rather than in the CLI so that tests exercise the same wiring the
    command line uses.
    """
    reg = registry or SourceRegistry.load(settings.bodies_path)
    name = source or settings.source
    entry = reg.entry(name)

    if entry.adapter == "fixture":
        from iris_signal_harvester.adapters.fixture import FixtureAdapter

        return FixtureAdapter(settings, scenario or entry.scenario or "happy_path")
    if entry.adapter == "oparl":
        from iris_signal_harvester.adapters.oparl import OParlAdapter

        url = system_url or entry.system_url
        if url is None:
            raise ConfigurationError(f"source {name!r} declares no system_url")
        return OParlAdapter.from_settings(settings, url)
    if entry.adapter == "ratsdokumente":
        from iris_signal_harvester.adapters.ratsdokumente import RatsdokumenteAdapter

        return RatsdokumenteAdapter.from_settings(
            settings, base_url=entry.base_url or "https://ratsdokumente.de"
        )
    raise ConfigurationError(f"source {name!r} names an unknown adapter {entry.adapter!r}")
