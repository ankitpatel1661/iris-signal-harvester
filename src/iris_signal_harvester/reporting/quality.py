"""The quality report: the artefact that makes a run's claims checkable.

Three design rules shape this module.

**1. The funnel is subtractive.** Every record that was fetched and did not reach the
table leaves the pipeline at exactly one named step, and the steps sum back to the
total:

    fetched
      - malformed        (dead-lettered by the adapter: no usable identifier)
      - rejected         (dead-lettered by the normaliser: a validation gate)
      - below threshold  (parsed fine, is not an infrastructure signal)
      - duplicates       (the same record seen twice inside one run)
      = staged           = inserted + updated + unchanged

The report prints the residual of that identity. A non-zero residual means records
went missing unaccounted for, and it is a FAIL — because if the accounting does not
balance, no other number in the report can be trusted either.

**2. "Below threshold" is never reported as a rejection.** They are different events.
A below-threshold record is the *expected* outcome for most council documents; a
rejection is a defect in the data or in us. Summing them would let a genuine
rejection spike hide inside a normal below-threshold rate, which is the single most
likely way for this harvester to fail silently.

**3. A finding names the bodies responsible.** A WARN that says "reject rate 4%" sends
a reader to grep. A WARN that says "reject rate 4%; worst: body/7 (31%), body/3 (12%)"
sends them to one query. Every rejection reason likewise carries one real example
record id, so investigating a spike starts from an actual document.

The verdict drives the CLI's exit code, so this file and ``config/thresholds.yaml``
together are the project's quality gate rather than a description of it.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import UUID

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from iris_signal_harvester.domain.models import (
    CanonicalSignal,
    GeoConfidence,
    GeoMethod,
    PublishedAtOrigin,
    RejectCode,
    RejectedRecord,
)
from iris_signal_harvester.domain.normalise import UNMAPPED_DOCUMENT_TYPE
from iris_signal_harvester.errors import ConfigurationError
from iris_signal_harvester.sink.postgis import LoadCounts

__all__ = [
    "COMPLETENESS_FIELDS",
    "BodyHealth",
    "DocumentTypeSummary",
    "FieldCompleteness",
    "Finding",
    "FunnelStep",
    "Observations",
    "QualityReport",
    "RejectionStat",
    "RunSummary",
    "Thresholds",
    "Verdict",
    "build_report",
    "write_report",
]

# Fields whose null rate is reported. ``geom`` is the geometry pair, not a column on
# ``CanonicalSignal``; it is counted as present when both coordinates are present.
COMPLETENESS_FIELDS: tuple[str, ...] = (
    "title",
    "source_url",
    "content_hash",
    "signal_type",
    "relevance_score",
    "published_at",
    "geom",
    "ags",
    "municipality_name",
    "summary",
    "document_type",
    "source_licence",
)

_MAX_GEO_NOTES = 12


class Verdict(StrEnum):
    """The gate's outcomes. PASS / WARN / FAIL are ordered worst-last for ``max()``.

    ``UNKNOWN`` is not a grade. It is what a report rebuilt from stored rows says when
    the stored data cannot support a verdict, so that it can never be mistaken for PASS.
    """

    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"

    @property
    def rank(self) -> int:
        return {"PASS": 0, "WARN": 1, "FAIL": 2, "UNKNOWN": 3}[self.value]


# ---------------------------------------------------------------------------
# thresholds
# ---------------------------------------------------------------------------


class _FailRules(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reject_rate_max: float = Field(default=0.10, ge=0.0, le=1.0)
    degraded_body_rate_max: float = Field(default=0.25, ge=0.0, le=1.0)
    structural_fields_must_hold: bool = True
    require_staged_when_fetched: bool = True
    require_funnel_balances: bool = True


class _WarnRules(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reject_rate_max: float = Field(default=0.02, ge=0.0, le=1.0)
    degraded_body_rate_max: float = Field(default=0.0, ge=0.0, le=1.0)
    unknown_date_rate_max: float = Field(default=0.50, ge=0.0, le=1.0)
    unresolved_geo_rate_max: float = Field(default=0.80, ge=0.0, le=1.0)
    null_rate_breach_is_warn: bool = True


class _Rules(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fail: _FailRules = _FailRules()
    warn: _WarnRules = _WarnRules()


class _StrictRules(BaseModel):
    model_config = ConfigDict(extra="forbid")

    any_rejection_fails: bool = True
    escalate_warn_to_fail: bool = True


class Thresholds(BaseModel):
    """``config/thresholds.yaml``, validated. Unknown keys are refused, not ignored."""

    model_config = ConfigDict(extra="forbid")

    version: str = "unversioned"
    null_rate_max: dict[str, float] = Field(default_factory=dict)
    rules: _Rules = _Rules()
    strict: _StrictRules = _StrictRules()
    exit_codes: dict[str, int] = Field(default_factory=lambda: {"pass": 0, "warn": 0, "fail": 1})

    @classmethod
    def load(cls, path: Path | str) -> Thresholds:
        source = Path(path)
        try:
            raw = yaml.safe_load(source.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise ConfigurationError(f"cannot read thresholds {source}: {exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigurationError(f"thresholds {source} must be a YAML mapping")
        try:
            return cls.model_validate(raw)
        except ValidationError as exc:
            raise ConfigurationError(f"invalid thresholds in {source}: {exc}") from exc

    def limit_for(self, field_name: str) -> float | None:
        return self.null_rate_max.get(field_name)

    def is_structural(self, field_name: str) -> bool:
        """A field whose allowed null rate is exactly zero: it must never be null."""
        return self.null_rate_max.get(field_name) == 0.0


# ---------------------------------------------------------------------------
# observations — filled as the sweep runs, never re-derived afterwards
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RejectionStat:
    """One row of the rejection histogram, with a real example to start from."""

    reason_code: str
    count: int
    example_record_id: str | None
    example_body_id: str | None
    example_detail: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "reason_code": self.reason_code,
            "count": self.count,
            "example_record_id": self.example_record_id,
            "example_body_id": self.example_body_id,
            "example_detail": self.example_detail,
        }


@dataclass
class Observations:
    """Running aggregates for one sweep.

    The pipeline feeds this as it goes, so the report never needs the signals
    themselves held in memory or re-read from the database. That is what lets a run
    over hundreds of councils produce a full report at constant memory.
    """

    fetched: int = 0
    pages_fetched: int = 0
    malformed: int = 0
    rejected: int = 0
    below_threshold: int = 0
    duplicates_in_run: int = 0
    staged: int = 0
    present_counts: Counter[str] = field(default_factory=Counter)
    date_origin: Counter[str] = field(default_factory=Counter)
    geo_method: Counter[str] = field(default_factory=Counter)
    geo_confidence: Counter[str] = field(default_factory=Counter)
    signal_mix: Counter[str] = field(default_factory=Counter)
    score_sum: dict[str, float] = field(default_factory=dict)
    licences: Counter[str] = field(default_factory=Counter)
    rejections: Counter[str] = field(default_factory=Counter)
    rejection_examples: dict[str, tuple[str | None, str | None, str]] = field(default_factory=dict)
    geo_notes: Counter[str] = field(default_factory=Counter)
    bodies_with_bad_ags: set[str] = field(default_factory=set)
    unmapped_document_types: int = 0
    """Staged records whose raw document type was outside ``config/document_types.yaml``."""
    unmapped_raw: Counter[str] = field(default_factory=Counter)
    """The raw values behind ``unmapped_document_types``, for the vocabulary's next edit."""
    not_recoverable: int = 0
    """Rebuilt reports only: fetched records whose fate was not stored (see ``_funnel``)."""

    # -- feeders -------------------------------------------------------------

    def observe_signal(
        self, signal: CanonicalSignal, *, document_type_raw: str | None = None
    ) -> None:
        """Count one record that reached the staging batch.

        ``document_type_raw`` is ``Normalised.document_type_raw``. When the caller does not
        have it, the raw value is read back from the payload, so the unmapped count is
        right either way and only the quality of the "top raw values" list depends on it.
        """
        self.staged += 1
        if signal.document_type == UNMAPPED_DOCUMENT_TYPE:
            self.observe_unmapped_document_type(
                document_type_raw or raw_document_type(signal.raw_payload)
            )
        for name in COMPLETENESS_FIELDS:
            if self._present(signal, name):
                self.present_counts[name] += 1
        self.date_origin[signal.published_at_origin.value] += 1
        self.geo_method[signal.geo_method.value] += 1
        self.geo_confidence[signal.geo_confidence.value] += 1
        kind = signal.signal_type or "(none)"
        self.signal_mix[kind] += 1
        self.score_sum[kind] = self.score_sum.get(kind, 0.0) + (signal.relevance_score or 0.0)
        self.licences[signal.source_licence or "(undeclared)"] += 1

    def observe_unmapped_document_type(self, raw: str | None) -> None:
        """Count one staged record whose document type the vocabulary did not map."""
        self.unmapped_document_types += 1
        self.unmapped_raw[raw if raw else "(raw value unavailable)"] += 1

    def observe_reject(self, reject: RejectedRecord, *, from_adapter: bool = False) -> None:
        """Count one dead-lettered record and keep the first one as the example."""
        if from_adapter:
            self.malformed += 1
        else:
            self.rejected += 1
        code = reject.reason_code.value
        self.rejections[code] += 1
        self.rejection_examples.setdefault(
            code, (reject.source_record_id, reject.source_body_id, reject.reason_detail)
        )

    def observe_sink_rejects(self, count: int) -> None:
        """Move ``count`` signals from *staged* to *rejected* after the sink refused them.

        The sink's own guard dead-letters a signal PostgreSQL cannot store
        (``schema_validation_failed``). By then ``observe_signal`` has counted it as
        staged, so without this the funnel would not balance. Completeness counts keep
        the refused signals (they cannot be un-observed), which is why the readers clamp
        ``present`` to ``staged``.
        """
        if count <= 0:
            return
        moved = min(count, self.staged)
        self.staged -= moved
        self.rejected += moved
        code = RejectCode.SCHEMA_VALIDATION_FAILED.value
        self.rejections[code] += moved
        self.rejection_examples.setdefault(
            code, (None, None, "refused by the sink: not storable in PostgreSQL")
        )

    def observe_below_threshold(self) -> None:
        self.below_threshold += 1

    def observe_duplicate(self) -> None:
        self.duplicates_in_run += 1

    def observe_notes(self, notes: Iterable[str]) -> None:
        for note in notes:
            if len(self.geo_notes) < _MAX_GEO_NOTES or note in self.geo_notes:
                self.geo_notes[note] += 1

    # -- derived -------------------------------------------------------------

    @property
    def dead_lettered(self) -> int:
        """Rejections of both kinds: adapter-malformed plus normaliser-rejected."""
        return self.malformed + self.rejected

    def null_rate(self, field_name: str) -> float:
        if self.staged == 0:
            return 0.0
        return max(self.staged - self.present_counts.get(field_name, 0), 0) / self.staged

    def share(self, counter: Counter[str], key: str) -> float:
        total = sum(counter.values())
        return counter.get(key, 0) / total if total else 0.0

    def histogram(self) -> tuple[RejectionStat, ...]:
        stats = []
        for code, count in self.rejections.items():
            record_id, body_id, detail = self.rejection_examples.get(code, (None, None, ""))
            stats.append(
                RejectionStat(
                    reason_code=code,
                    count=count,
                    example_record_id=record_id,
                    example_body_id=body_id,
                    example_detail=detail or None,
                )
            )
        return tuple(sorted(stats, key=lambda s: (-s.count, s.reason_code)))

    @staticmethod
    def _present(signal: CanonicalSignal, name: str) -> bool:
        if name == "geom":
            return signal.easting is not None and signal.northing is not None
        return getattr(signal, name, None) is not None


_DOCUMENT_TYPE_KEYS = ("paperType", "document_type", "documentType")  # mirrors normalise._TYPE_KEYS
_MAX_TOP_RAW = 10


def raw_document_type(payload: Mapping[str, Any]) -> str | None:
    """The raw document type of a payload, whitespace-normalised, as the normaliser read it."""
    for key in _DOCUMENT_TYPE_KEYS:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())
    return None


# ---------------------------------------------------------------------------
# report value types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BodyHealth:
    """Per-municipality outcome. One failing body degrades itself, not the run."""

    body_id: str
    name: str | None = None
    licence: str | None = None
    ags: str | None = None
    pages: int = 0
    fetched: int = 0
    emitted: int = 0
    below_threshold: int = 0
    rejected: int = 0
    duplicates: int = 0
    stop_reason: str = "exhausted"
    degraded: bool = False
    watermark: str = "not_attempted"
    error: str | None = None

    @property
    def reject_rate(self) -> float:
        return self.rejected / self.fetched if self.fetched else 0.0

    @property
    def label(self) -> str:
        return f"{self.name} ({self.body_id})" if self.name else self.body_id

    def as_dict(self) -> dict[str, Any]:
        return {
            "body_id": self.body_id,
            "name": self.name,
            "licence": self.licence,
            "ags": self.ags,
            "pages": self.pages,
            "fetched": self.fetched,
            "emitted": self.emitted,
            "below_threshold": self.below_threshold,
            "rejected": self.rejected,
            "duplicates": self.duplicates,
            "stop_reason": self.stop_reason,
            "degraded": self.degraded,
            "watermark": self.watermark,
            "error": self.error,
            "reject_rate": round(self.reject_rate, 4),
        }


@dataclass(frozen=True)
class RunSummary:
    """Everything about the run that is not a measurement."""

    run_id: UUID
    source_system: str
    started_at: datetime
    finished_at: datetime
    status: str
    """The ``harvest_run.status`` vocabulary: ``succeeded`` / ``partial`` / ``failed``."""
    counts: LoadCounts
    dry_run: bool = False
    strict: bool = False
    since: datetime | None = None
    limit: int | None = None
    bodies_total: int = 0
    config_hash: str | None = None
    taxonomy_version: str | None = None
    previous_counts: LoadCounts | None = None
    notes: tuple[str, ...] = ()
    rebuilt: bool = False
    """True when rebuilt from stored rows rather than measured by the run itself."""
    population_complete: bool = True
    """Rebuilt only: the rows found for the run match what the run reported loading."""
    carried_verdict: str | None = None
    """Rebuilt only: the verdict the run itself computed and stored, if it did."""

    @property
    def duration_s(self) -> float:
        return max((self.finished_at - self.started_at).total_seconds(), 0.0)

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": str(self.run_id),
            "source_system": self.source_system,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
            "duration_s": round(self.duration_s, 3),
            "status": self.status,
            "dry_run": self.dry_run,
            "strict": self.strict,
            "since": self.since.isoformat() if self.since else None,
            "limit": self.limit,
            "bodies_total": self.bodies_total,
            "config_hash": self.config_hash,
            "taxonomy_version": self.taxonomy_version,
            "inserted": self.counts.inserted,
            "updated": self.counts.updated,
            "unchanged": self.counts.unchanged,
            "rebuilt": self.rebuilt,
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class FunnelStep:
    """One subtractive stage. ``removed`` records left here, ``remaining`` continued."""

    label: str
    removed: int
    remaining: int
    disposition: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "removed": self.removed,
            "remaining": self.remaining,
            "disposition": self.disposition,
        }


@dataclass(frozen=True)
class FieldCompleteness:
    field_name: str
    present: int
    staged: int
    null_rate: float
    limit: float | None
    structural: bool

    @property
    def breached(self) -> bool:
        return self.limit is not None and self.null_rate > self.limit + 1e-9

    def as_dict(self) -> dict[str, Any]:
        return {
            "field": self.field_name,
            "present": self.present,
            "null": self.staged - self.present,
            "null_rate": round(self.null_rate, 4),
            "limit": self.limit,
            "structural": self.structural,
            "breached": self.breached,
        }


@dataclass(frozen=True)
class Finding:
    """One triggered rule, naming what is wrong and which bodies caused it."""

    level: Verdict
    rule: str
    detail: str
    bodies: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "level": self.level.value,
            "rule": self.rule,
            "detail": self.detail,
            "bodies": list(self.bodies),
        }


@dataclass(frozen=True)
class DocumentTypeSummary:
    """How much of the document-type vocabulary the data actually fell outside of."""

    typed: int = 0
    unmapped: int = 0
    top_unmapped_raw: tuple[tuple[str, int], ...] = ()

    @property
    def unmapped_rate(self) -> float:
        return self.unmapped / self.typed if self.typed else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "typed": self.typed,
            "unmapped": self.unmapped,
            "unmapped_rate": round(self.unmapped_rate, 4),
            "top_unmapped_raw": [{"value": v, "n": n} for v, n in self.top_unmapped_raw],
        }


@dataclass(frozen=True)
class QualityReport:
    """The finished report. ``to_markdown`` and ``to_dict`` are the two artefacts."""

    summary: RunSummary
    thresholds_version: str
    funnel: tuple[FunnelStep, ...]
    residual: int
    completeness: tuple[FieldCompleteness, ...]
    rejections: tuple[RejectionStat, ...]
    date_origin: tuple[tuple[str, int, float], ...]
    geo_confidence: tuple[tuple[str, int, float], ...]
    geo_method: tuple[tuple[str, int, float], ...]
    signal_mix: tuple[tuple[str, int, float], ...]
    licences: tuple[tuple[str, int], ...]
    bodies: tuple[BodyHealth, ...]
    findings: tuple[Finding, ...]
    verdict: Verdict
    observations: Observations
    exit_codes: Mapping[str, int] = field(default_factory=lambda: {"pass": 0, "warn": 0, "fail": 1})
    document_types: DocumentTypeSummary = field(default_factory=DocumentTypeSummary)

    # -- the gate ------------------------------------------------------------

    @property
    def exit_code(self) -> int:
        """Exit code for the CLI. WARN is 0 normally and 1 under ``--strict``."""
        if self.verdict is Verdict.UNKNOWN:
            # Distinct from 0 (passed) and 1 (failed): "could not be graded" must stop a gate.
            return self.exit_codes.get("unknown", 3)
        if self.verdict is Verdict.FAIL:
            return self.exit_codes.get("fail", 1)
        if self.verdict is Verdict.WARN:
            return 1 if self.summary.strict else self.exit_codes.get("warn", 0)
        return self.exit_codes.get("pass", 0)

    # -- artefacts -----------------------------------------------------------

    def as_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict.value,
            "exit_code": self.exit_code,
            "thresholds_version": self.thresholds_version,
            "run": self.summary.as_dict(),
            "funnel": {
                "fetched": self.observations.fetched,
                "pages_fetched": self.observations.pages_fetched,
                "steps": [s.as_dict() for s in self.funnel],
                "residual": self.residual,
                "balances": self.residual == 0,
            },
            "load": {
                "inserted": self.summary.counts.inserted,
                "updated": self.summary.counts.updated,
                "unchanged": self.summary.counts.unchanged,
                "duplicates_in_batch": self.summary.counts.duplicates_in_batch,
            },
            "rerun_delta": self._delta_dict(),
            "document_types": self.document_types.as_dict(),
            "completeness": [c.as_dict() for c in self.completeness],
            "rejections": [r.as_dict() for r in self.rejections],
            "distributions": {
                "published_at_origin": [
                    {"key": k, "n": n, "pct": round(p * 100, 1)} for k, n, p in self.date_origin
                ],
                "geo_confidence": [
                    {"key": k, "n": n, "pct": round(p * 100, 1)} for k, n, p in self.geo_confidence
                ],
                "geo_method": [
                    {"key": k, "n": n, "pct": round(p * 100, 1)} for k, n, p in self.geo_method
                ],
                "signal_mix": [
                    {"signal_type": k, "n": n, "mean_score": round(p, 3)}
                    for k, n, p in self.signal_mix
                ],
                "licences": [{"licence": k, "n": n} for k, n in self.licences],
            },
            "bodies": [b.as_dict() for b in self.bodies],
            "findings": [f.as_dict() for f in self.findings],
            "geo_notes": [
                {"note": note, "n": n} for note, n in self.observations.geo_notes.most_common()
            ],
        }

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), indent=2, ensure_ascii=False, sort_keys=False)

    def _delta_dict(self) -> dict[str, Any]:
        previous = self.summary.previous_counts
        counts = self.summary.counts
        return {
            "this_run": {
                "inserted": counts.inserted,
                "updated": counts.updated,
                "unchanged": counts.unchanged,
            },
            "previous_run": (
                None
                if previous is None
                else {
                    "inserted": previous.inserted,
                    "updated": previous.updated,
                    "unchanged": previous.unchanged,
                }
            ),
            "idempotent": (
                None
                if previous is None
                else counts.inserted == 0 and counts.updated == 0 and counts.unchanged > 0
            ),
        }

    def to_markdown(self) -> str:
        out: list[str] = []
        add = out.append
        s = self.summary
        add(f"# Harvest quality report — {self.verdict.value}")
        add("")
        add("| | |")
        add("|---|---|")
        add(f"| Run id | `{s.run_id}` |")
        add(f"| Source system | `{s.source_system}` |")
        add(f"| Started (UTC) | {s.started_at.isoformat()} |")
        add(f"| Duration | {s.duration_s:.2f} s |")
        add(f"| Run status | `{s.status}` |")
        add(
            f"| Mode | {'dry-run (no database writes)' if s.dry_run else 'load'}"
            f"{', strict' if s.strict else ''} |"
        )
        add(f"| Incremental since | {s.since.isoformat() if s.since else '(full sweep)'} |")
        add(f"| Record limit | {s.limit if s.limit is not None else '(none)'} |")
        add(f"| Bodies swept | {s.bodies_total} |")
        add(f"| Taxonomy version | `{s.taxonomy_version or 'unknown'}` |")
        add(f"| Thresholds version | `{self.thresholds_version}` |")
        add(f"| Config hash | `{(s.config_hash or '')[:16]}` |")
        if s.rebuilt:
            add("| Source of this report | rebuilt from stored rows, not measured by the run |")
        add("")
        for note in s.notes:
            add(f"> {note}")
        if s.notes:
            add("")

        add("## 1. Stage funnel (subtractive)")
        add("")
        add("Every fetched record leaves the pipeline at exactly one step, so the steps")
        add("sum back to the total. `below threshold` is **not** a rejection: the record")
        add("parsed correctly and is simply not an infrastructure signal.")
        add("")
        add("| stage | records out | remaining | disposition |")
        add("|---|---:|---:|---|")
        add(f"| fetched from source | — | {self.observations.fetched} | — |")
        for step in self.funnel:
            add(f"| {step.label} | {step.removed} | {step.remaining} | {step.disposition} |")
        add("")
        if s.dry_run:
            add("No sink ran (dry run), so `inserted`/`updated`/`unchanged` are all zero by")
            add("construction and the staged count is what *would* have been written.")
        else:
            add(
                f"Loaded: **{s.counts.inserted} inserted**, **{s.counts.updated} updated**, "
                f"**{s.counts.unchanged} unchanged**"
                f" (in-batch duplicates collapsed by the sink: {s.counts.duplicates_in_batch})."
            )
        add("")
        add(
            f"Funnel residual: **{self.residual}** "
            f"({'balances' if self.residual == 0 else 'DOES NOT BALANCE — see findings'})."
        )
        add("")
        add(f"Pages fetched: {self.observations.pages_fetched}.")
        add("")

        add("## 2. Re-run delta")
        add("")
        delta = self._delta_dict()
        previous = delta["previous_run"]
        if previous is None:
            add("No previous run counts were supplied, so no delta is claimed.")
            add("Idempotency is asserted by running the same harvest twice and comparing")
            add("this section's `inserted` and `updated` against zero.")
        else:
            add("| | inserted | updated | unchanged |")
            add("|---|---:|---:|---:|")
            add(
                f"| previous run | {previous['inserted']} | {previous['updated']} "
                f"| {previous['unchanged']} |"
            )
            add(f"| this run | {s.counts.inserted} | {s.counts.updated} | {s.counts.unchanged} |")
            add("")
            verdict = "idempotent" if delta["idempotent"] else "NOT idempotent"
            add(f"Second pass over identical input is **{verdict}**.")
        add("")

        add("## 3. Rejection histogram")
        add("")
        if not self.rejections:
            add("No records were dead-lettered.")
        else:
            add("One real example id per reason, so investigating a spike is one query.")
            add("")
            add("| reason_code | n | example record | body | detail |")
            add("|---|---:|---|---|---|")
            for r in self.rejections:
                detail = (r.example_detail or "").replace("|", "\\|")
                add(
                    f"| `{r.reason_code}` | {r.count} | `{r.example_record_id or '(none)'}` "
                    f"| `{r.example_body_id or '(none)'}` | {detail[:160]} |"
                )
        add("")

        add("## 4. Field completeness vs thresholds")
        add("")
        add("Null rate is measured over staged records. A structural field (limit 0.0)")
        add("breaching its limit is a FAIL; any other breach is a WARN.")
        add("")
        add("| field | present | null | null rate | limit | verdict |")
        add("|---|---:|---:|---:|---:|---|")
        for c in self.completeness:
            limit = "—" if c.limit is None else f"{c.limit:.2f}"
            state = "ok" if not c.breached else ("FAIL" if c.structural else "WARN")
            add(
                f"| `{c.field_name}` | {c.present} | {c.staged - c.present} "
                f"| {c.null_rate * 100:.1f}% | {limit} | {state} |"
            )
        add("")

        add("## 5. Date provenance")
        add("")
        add("A high `unknown` share is expected on OParl: `Paper.date` is frequently null")
        add("in real municipal data, which is why the provenance column exists.")
        add("")
        self._distribution(add, self.date_origin, "published_at_origin")

        add("## 6. Geo confidence and method")
        add("")
        add("`unresolved` with `none` confidence is an honest answer, not a failure. The")
        add("verified live OParl endpoint carries no `location` on `Paper` at all, so")
        add("tiers 4 and 5 dominate there.")
        add("")
        self._distribution(add, self.geo_confidence, "geo_confidence")
        self._distribution(add, self.geo_method, "geo_method")

        add("## 7. Signal mix")
        add("")
        add("| signal_type | n | mean relevance |")
        add("|---|---:|---:|")
        for key, n, mean in self.signal_mix:
            add(f"| `{key}` | {n} | {mean:.3f} |")
        if not self.signal_mix:
            add("| (none) | 0 | — |")
        add("")

        add("## 7b. Document types")
        add("")
        dt = self.document_types
        add(
            f"{dt.unmapped} of {dt.typed} staged records with a document type carried one that "
            f"`config/document_types.yaml` does not map ({dt.unmapped_rate * 100:.1f}%); they are "
            f"stored as `{UNMAPPED_DOCUMENT_TYPE}` and the raw value is kept in `raw_payload`."
        )
        add("")
        if dt.top_unmapped_raw:
            add("| raw value | n |")
            add("|---|---:|")
            for value, n in dt.top_unmapped_raw:
                add(f"| `{value.replace('|', chr(92) + '|')}` | {n} |")
            add("")

        add("## 8. Source health")
        add("")
        add(
            "| body | licence | ags | pages | fetched | emitted | below | rejected "
            "| stop reason | watermark | state |"
        )
        add("|---|---|---|---:|---:|---:|---:|---:|---|---|---|")
        for b in self.bodies:
            state = "degraded" if b.degraded else "ok"
            add(
                f"| {b.label} | {b.licence or '(undeclared)'} | {b.ags or '—'} | {b.pages} "
                f"| {b.fetched} | {b.emitted} | {b.below_threshold} | {b.rejected} "
                f"| `{b.stop_reason}` | `{b.watermark}` | **{state}** |"
            )
        if not self.bodies:
            add("| (none discovered) | — | — | 0 | 0 | 0 | 0 | 0 | — | — | — |")
        add("")
        add("| licence | records |")
        add("|---|---:|")
        for licence, n in self.licences:
            add(f"| {licence} | {n} |")
        add("")
        if self.observations.geo_notes:
            add("Most frequent resolution notes (sampled):")
            add("")
            for note, n in self.observations.geo_notes.most_common(_MAX_GEO_NOTES):
                add(f"- (seen {n}x) {note}")
            add("")

        add("## 9. Gate")
        add("")
        add(f"**Verdict: {self.verdict.value}** — exit code {self.exit_code}.")
        add("")
        if not self.findings:
            add("No threshold was breached and no body was degraded.")
        else:
            add("| level | rule | detail | bodies responsible |")
            add("|---|---|---|---|")
            for f in self.findings:
                bodies = ", ".join(f"`{b}`" for b in f.bodies) if f.bodies else "—"
                add(f"| **{f.level.value}** | `{f.rule}` | {f.detail} | {bodies} |")
        add("")
        return "\n".join(out) + "\n"

    @staticmethod
    def _distribution(add: Any, rows: Sequence[tuple[str, int, float]], label: str) -> None:
        add(f"| {label} | n | pct |")
        add("|---|---:|---:|")
        for key, n, pct in rows:
            add(f"| `{key}` | {n} | {pct * 100:.1f}% |")
        if not rows:
            add("| (no staged records) | 0 | — |")
        add("")


# ---------------------------------------------------------------------------
# building
# ---------------------------------------------------------------------------


def _distribution(
    counter: Counter[str], order: Sequence[str] = ()
) -> tuple[tuple[str, int, float], ...]:
    total = sum(counter.values())
    keys = [k for k in order if k in counter] + sorted(set(counter) - set(order))
    return tuple((k, counter[k], counter[k] / total if total else 0.0) for k in keys)


def _funnel(
    obs: Observations, counts: LoadCounts, *, dry_run: bool
) -> tuple[tuple[FunnelStep, ...], int]:
    remaining = obs.fetched
    steps: list[FunnelStep] = []

    def step(label: str, removed: int, disposition: str) -> None:
        nonlocal remaining
        remaining -= removed
        steps.append(FunnelStep(label, removed, remaining, disposition))

    step("malformed by adapter", obs.malformed, "dead letter (signal_ingest_reject)")
    step("rejected by normaliser", obs.rejected, "dead letter (signal_ingest_reject)")
    step("below taxonomy threshold", obs.below_threshold, "not a signal — no row written")
    step("intra-run duplicates", obs.duplicates_in_run, "collapsed — same record seen twice")
    if obs.not_recoverable:
        step(
            "not recoverable from storage",
            obs.not_recoverable,
            "rebuilt report: below-threshold and in-run duplicates are never stored",
        )
    steps.append(
        FunnelStep(
            "staged for load" if not dry_run else "staged (dry run: not written)",
            0,
            obs.staged,
            "reached the table" if not dry_run else "would have reached the table",
        )
    )
    residual = remaining - obs.staged
    if not dry_run:
        written = counts.inserted + counts.updated + counts.unchanged
        steps.append(
            FunnelStep(
                "load outcome",
                0,
                written,
                f"{counts.inserted} inserted / {counts.updated} updated "
                f"/ {counts.unchanged} unchanged",
            )
        )
    return tuple(steps), residual


def _completeness(obs: Observations, thresholds: Thresholds) -> tuple[FieldCompleteness, ...]:
    return tuple(
        FieldCompleteness(
            field_name=name,
            present=min(obs.present_counts.get(name, 0), obs.staged),
            staged=obs.staged,
            null_rate=obs.null_rate(name),
            limit=thresholds.limit_for(name),
            structural=thresholds.is_structural(name),
        )
        for name in COMPLETENESS_FIELDS
    )


def _worst_bodies(bodies: Sequence[BodyHealth], key: Any, limit: int = 5) -> tuple[str, ...]:
    ranked = sorted((b for b in bodies if key(b)), key=lambda b: -b.reject_rate)
    return tuple(b.label for b in ranked[:limit])


def build_report(
    summary: RunSummary,
    observations: Observations,
    bodies: Sequence[BodyHealth],
    thresholds: Thresholds,
) -> QualityReport:
    """Measure, then grade. The grading rules all live in ``thresholds``."""
    obs = observations
    funnel, residual = _funnel(obs, summary.counts, dry_run=summary.dry_run)
    completeness = _completeness(obs, thresholds)
    findings: list[Finding] = []

    fail = thresholds.rules.fail
    warn = thresholds.rules.warn
    reject_rate = obs.dead_lettered / obs.fetched if obs.fetched else 0.0
    degraded = [b for b in bodies if b.degraded]
    degraded_rate = len(degraded) / len(bodies) if bodies else 0.0

    # --- FAIL conditions ----------------------------------------------------
    if fail.require_funnel_balances and residual != 0:
        findings.append(
            Finding(
                Verdict.FAIL,
                "funnel_balances",
                f"the subtractive funnel does not balance: residual {residual} records are "
                "unaccounted for, so no other number in this report can be trusted",
            )
        )
    if reject_rate > fail.reject_rate_max:
        findings.append(
            Finding(
                Verdict.FAIL,
                "reject_rate_max",
                f"{obs.dead_lettered} of {obs.fetched} fetched records were dead-lettered "
                f"({reject_rate * 100:.1f}% > {fail.reject_rate_max * 100:.1f}%)",
                _worst_bodies(bodies, lambda b: b.rejected > 0),
            )
        )
    if degraded_rate > fail.degraded_body_rate_max:
        findings.append(
            Finding(
                Verdict.FAIL,
                "degraded_body_rate_max",
                f"{len(degraded)} of {len(bodies)} bodies ended degraded "
                f"({degraded_rate * 100:.1f}% > {fail.degraded_body_rate_max * 100:.1f}%); "
                "their watermarks were not advanced, so those windows will be retried "
                "rather than skipped",
                tuple(b.label for b in degraded),
            )
        )
    if fail.structural_fields_must_hold:
        for c in completeness:
            if c.breached and c.structural:
                findings.append(
                    Finding(
                        Verdict.FAIL,
                        f"null_rate_max[{c.field_name}]",
                        f"structural field `{c.field_name}` is null in "
                        f"{c.staged - c.present} of {c.staged} staged records",
                    )
                )
    if fail.require_staged_when_fetched and obs.fetched > 0 and obs.staged == 0:
        level = Verdict.FAIL
        detail = (
            f"{obs.fetched} records were fetched and none reached the table "
            f"({obs.below_threshold} below threshold, {obs.dead_lettered} dead-lettered)"
        )
        # A sweep that fetched only non-signals is a legitimate, reportable outcome;
        # a sweep that fetched only rejects is not.
        if obs.dead_lettered == 0:
            level, detail = Verdict.WARN, detail + " — all were simply not signals"
        findings.append(Finding(level, "require_staged_when_fetched", detail))

    # --- WARN conditions ----------------------------------------------------
    if warn.reject_rate_max < reject_rate <= fail.reject_rate_max:
        findings.append(
            Finding(
                Verdict.WARN,
                "reject_rate_max",
                f"dead-letter rate {reject_rate * 100:.1f}% exceeds the warning level "
                f"{warn.reject_rate_max * 100:.1f}%",
                _worst_bodies(bodies, lambda b: b.rejected > 0),
            )
        )
    if degraded and degraded_rate <= fail.degraded_body_rate_max:
        findings.append(
            Finding(
                Verdict.WARN,
                "degraded_body_rate_max",
                f"{len(degraded)} body/bodies degraded; their watermarks were not advanced, "
                "so the window will be retried rather than skipped",
                tuple(b.label for b in degraded),
            )
        )
    unknown_dates = obs.share(obs.date_origin, PublishedAtOrigin.UNKNOWN.value)
    if unknown_dates > warn.unknown_date_rate_max:
        findings.append(
            Finding(
                Verdict.WARN,
                "unknown_date_rate_max",
                f"{unknown_dates * 100:.1f}% of staged records have no verifiable publication "
                f"date (limit {warn.unknown_date_rate_max * 100:.1f}%)",
            )
        )
    unresolved_geo = obs.share(obs.geo_method, GeoMethod.UNRESOLVED.value)
    if unresolved_geo > warn.unresolved_geo_rate_max:
        findings.append(
            Finding(
                Verdict.WARN,
                "unresolved_geo_rate_max",
                f"{unresolved_geo * 100:.1f}% of staged records are geographically unresolved "
                f"(limit {warn.unresolved_geo_rate_max * 100:.1f}%)",
            )
        )
    if warn.null_rate_breach_is_warn:
        for c in completeness:
            if c.breached and not c.structural:
                findings.append(
                    Finding(
                        Verdict.WARN,
                        f"null_rate_max[{c.field_name}]",
                        f"`{c.field_name}` null rate {c.null_rate * 100:.1f}% exceeds its "
                        f"limit {(c.limit or 0) * 100:.1f}%",
                    )
                )
    for body in bodies:
        if body.error:
            findings.append(
                Finding(
                    Verdict.WARN,
                    "body_error",
                    f"{body.stop_reason}: {body.error}",
                    (body.label,),
                )
            )
    for body_id in sorted(obs.bodies_with_bad_ags):
        findings.append(
            Finding(
                Verdict.WARN,
                "malformed_ags",
                "declared AGS is not 8 digits; dropped to NULL, so the municipality-centroid "
                "geo tier is unavailable for this body",
                (body_id,),
            )
        )

    # --- strict escalation ---------------------------------------------------
    if summary.strict and thresholds.strict.any_rejection_fails and obs.dead_lettered > 0:
        findings.append(
            Finding(
                Verdict.FAIL,
                "strict.any_rejection_fails",
                f"--strict: {obs.dead_lettered} record-level rejection(s) fail the run",
                _worst_bodies(bodies, lambda b: b.rejected > 0),
            )
        )
    if summary.status == "failed":
        findings.append(
            Finding(Verdict.FAIL, "run_status", "the run itself did not complete successfully")
        )

    if summary.rebuilt:
        findings = [f for f in findings if _verifiable_after_rebuild(f.rule, summary)]
    verdict = Verdict.PASS
    for finding in findings:
        if finding.level.rank > verdict.rank:
            verdict = finding.level
    if summary.rebuilt:
        verdict, findings = _rebuilt_verdict(summary, verdict, findings)
    if verdict is Verdict.WARN and summary.strict and thresholds.strict.escalate_warn_to_fail:
        verdict = Verdict.FAIL
        findings = [
            *findings,
            Finding(
                Verdict.FAIL,
                "strict.escalate_warn_to_fail",
                "--strict: a WARN is a failure in CI mode",
            ),
        ]

    return QualityReport(
        summary=summary,
        thresholds_version=thresholds.version,
        funnel=funnel,
        residual=residual,
        completeness=completeness,
        rejections=obs.histogram(),
        date_origin=_distribution(obs.date_origin, [o.value for o in PublishedAtOrigin]),
        geo_confidence=_distribution(obs.geo_confidence, [c.value for c in GeoConfidence]),
        geo_method=_distribution(obs.geo_method, [m.value for m in GeoMethod]),
        signal_mix=tuple(
            (kind, n, obs.score_sum.get(kind, 0.0) / n)
            for kind, n in sorted(obs.signal_mix.items(), key=lambda kv: (-kv[1], kv[0]))
        ),
        licences=tuple(sorted(obs.licences.items(), key=lambda kv: (-kv[1], kv[0]))),
        bodies=tuple(bodies),
        findings=tuple(findings),
        verdict=verdict,
        observations=obs,
        exit_codes=dict(thresholds.exit_codes),
        document_types=DocumentTypeSummary(
            typed=obs.present_counts.get("document_type", 0),
            unmapped=obs.unmapped_document_types,
            top_unmapped_raw=tuple(
                sorted(obs.unmapped_raw.items(), key=lambda kv: (-kv[1], kv[0]))[:_MAX_TOP_RAW]
            ),
        ),
    )


# Rules a report rebuilt from stored rows cannot evaluate. The run measured these in
# memory as it went, and what the database keeps does not let them be recomputed.
_NEVER_REBUILDABLE = ("funnel_balances", "body_error", "malformed_ags")
# Rules that need the run's full population, so they hold only when it was recovered whole.
_NEEDS_FULL_POPULATION = (
    "null_rate_max[",
    "unknown_date_rate_max",
    "unresolved_geo_rate_max",
    "require_staged_when_fetched",
)


def _verifiable_after_rebuild(rule: str, summary: RunSummary) -> bool:
    if rule in _NEVER_REBUILDABLE:
        return False
    return summary.population_complete or not rule.startswith(_NEEDS_FULL_POPULATION)


def _rebuilt_verdict(
    summary: RunSummary, computed: Verdict, findings: list[Finding]
) -> tuple[Verdict, list[Finding]]:
    """Decide what a report rebuilt from stored rows may claim.

    1. If the run stored the verdict it computed, that verdict is authoritative: the
       rebuild measures a subset of what the run did and must not regrade it.
    2. Otherwise a FAIL found among the rules that can be re-evaluated is definitive.
    3. Otherwise the stored data does not prove a pass, and the honest answer is UNKNOWN.
       A rebuild is allowed to say PASS or WARN only if the run's whole population was
       recovered AND the run finished.
    """
    carried = _parse_verdict(summary.carried_verdict)
    if carried is not None and carried is not Verdict.UNKNOWN:
        return carried, [
            *findings,
            Finding(
                carried,
                "carried_verdict",
                f"verdict {carried.value} was computed and stored by the run itself; this "
                "rebuild re-evaluates only a subset of the rules and does not override it",
            ),
        ]
    if computed is Verdict.FAIL:
        return computed, findings
    reasons = []
    if not summary.population_complete:
        reasons.append("the rows found for this run do not match what it reported loading")
    if summary.status not in ("succeeded", "partial", "failed"):
        reasons.append(f"the run never finished (status {summary.status!r})")
    if reasons:
        return Verdict.UNKNOWN, [
            *findings,
            Finding(
                Verdict.UNKNOWN,
                "cannot_grade",
                "the stored data cannot support a verdict: " + "; ".join(reasons),
            ),
        ]
    return computed, findings


def _parse_verdict(value: str | None) -> Verdict | None:
    try:
        return Verdict(value) if value is not None else None
    except ValueError:
        return None


def write_report(report: QualityReport, directory: Path | str) -> tuple[Path, Path]:
    """Write ``<dir>/quality_<run_id>.{md,json}`` plus a ``latest`` pair. Returns both paths.

    A report rebuilt from stored rows is a lesser artefact than the one the run wrote
    itself, so it never overwrites that one: it goes to ``quality_<run_id>.rebuilt.*``.
    An ``UNKNOWN`` report is not a grade at all and is never written as ``latest``.
    """
    target = Path(directory)
    target.mkdir(parents=True, exist_ok=True)
    rebuilt = report.summary.rebuilt
    stem = f"quality_{report.summary.run_id}" + (".rebuilt" if rebuilt else "")
    markdown = target / f"{stem}.md"
    payload = target / f"{stem}.json"
    markdown.write_text(report.to_markdown(), encoding="utf-8")
    payload.write_text(report.to_json(), encoding="utf-8")
    if report.verdict is Verdict.UNKNOWN:
        return markdown, payload
    # A stable name for `make demo` and CI to pick up without knowing the run id.
    (target / "quality_latest.md").write_text(report.to_markdown(), encoding="utf-8")
    (target / "quality_latest.json").write_text(report.to_json(), encoding="utf-8")
    return markdown, payload


# ---------------------------------------------------------------------------
# rebuilding a report from the database (`iris-harvest report --run-id`)
# ---------------------------------------------------------------------------


def observations_from_db(
    dsn: str, schema: str, run_id: UUID
) -> tuple[RunSummary, Observations, tuple[BodyHealth, ...]]:
    """Rebuild a report's inputs from stored data, for a run that happened earlier.

    What can and cannot be recovered, stated plainly because the alternative is a
    report that quietly means something different from the one the run itself wrote
    (final-review I-5: a FAILED run regraded PASS, an idempotent re-run graded PASS on
    zero rows):

    * ``harvest_run`` holds what the run *observed*: records fetched, inserted, updated,
      unchanged, rejected, its status, and in ``notes`` the degraded bodies and every
      body's stop reason. All of that is read back, so degradation and failure survive.
    * ``signal_ingest.harvest_run_id`` is the run that last *wrote* a row, so an
      idempotent re-run owns no rows. The population is therefore every row of the same
      source whose ``last_seen_at`` falls inside the run's own start/finish window:
      ``last_seen_at`` moves on every re-observation. That is checked against the count
      the run reported loading; if they differ (a later run re-saw the rows, or runs
      overlapped) the population is flagged incomplete and the report will not grade.
    * The run's own verdict, when it recorded one (``PostgisSink.record_verdict``), is
      carried through and is authoritative.
    * Not stored, so not invented: below-threshold and in-run duplicate counts (the
      funnel cannot be re-balanced), per-body page counts, body errors, AGS warnings.
    """
    from psycopg import sql

    from iris_signal_harvester.sink._common import connect, table

    signals = table(schema, "signal_ingest")
    rejects = table(schema, "signal_ingest_reject")
    runs = table(schema, "harvest_run")

    with connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            sql.SQL(
                "SELECT source_system, started_at, finished_at, status, config_hash, "
                "taxonomy_version, records_fetched, inserted, updated, unchanged, rejected, "
                "notes FROM {} WHERE id = %s"
            ).format(runs),
            (str(run_id),),
        )
        row = cur.fetchone()
        if row is None:
            raise ConfigurationError(f"no harvest_run with id {run_id}")
        (
            source_system,
            started_at,
            finished_at,
            stored_status,
            config_hash,
            taxonomy_version,
            records_fetched,
            inserted,
            updated,
            unchanged,
            rejected,
            raw_notes,
        ) = row
        stored_notes: dict[str, Any] = raw_notes if isinstance(raw_notes, dict) else {}
        window_end = finished_at or datetime.now(UTC)

        obs = Observations(fetched=int(records_fetched), rejected=int(rejected))
        cur.execute(
            sql.SQL(
                "SELECT title, source_url, content_hash, signal_type, relevance_score, "
                "published_at, geom, ags, municipality_name, summary, document_type, "
                "source_licence, published_at_origin, geo_method, geo_confidence, "
                "source_body_id FROM {} "
                "WHERE source_system = %s AND last_seen_at >= %s AND last_seen_at <= %s"
            ).format(signals),
            (source_system, started_at, window_end),
        )
        per_body: Counter[str] = Counter()
        for record in cur.fetchall():
            obs.staged += 1
            for name, value in zip(COMPLETENESS_FIELDS, record[:12], strict=True):
                if value is not None:
                    obs.present_counts[name] += 1
            obs.date_origin[str(record[12])] += 1
            obs.geo_method[str(record[13])] += 1
            obs.geo_confidence[str(record[14])] += 1
            kind = str(record[3]) if record[3] is not None else "(none)"
            obs.signal_mix[kind] += 1
            obs.score_sum[kind] = obs.score_sum.get(kind, 0.0) + float(record[4] or 0.0)
            obs.licences[str(record[11]) if record[11] is not None else "(undeclared)"] += 1
            per_body[str(record[15])] += 1

        cur.execute(
            sql.SQL(
                "SELECT raw_payload FROM {} WHERE source_system = %s AND document_type = %s "
                "AND last_seen_at >= %s AND last_seen_at <= %s"
            ).format(signals),
            (source_system, UNMAPPED_DOCUMENT_TYPE, started_at, window_end),
        )
        for (payload,) in cur.fetchall():
            obs.observe_unmapped_document_type(
                raw_document_type(payload) if isinstance(payload, dict) else None
            )

        # Rejections are stored once however many runs re-saw them, so the histogram is
        # of the distinct rejections this run saw; the headline count is the stored one.
        cur.execute(
            sql.SQL(
                "SELECT reason_code, count(*), min(source_record_id), min(source_body_id), "
                "min(reason_detail) FROM {} WHERE source_system = %s "
                "AND last_seen_at >= %s AND last_seen_at <= %s GROUP BY 1"
            ).format(rejects),
            (source_system, started_at, window_end),
        )
        for code, count, example_id, example_body, detail in cur.fetchall():
            obs.rejections[str(code)] += int(count)
            obs.rejection_examples.setdefault(
                str(code), (example_id, example_body, str(detail or ""))
            )
        cur.execute(
            sql.SQL(
                "SELECT source_body_id, count(*) FROM {} WHERE source_system = %s "
                "AND last_seen_at >= %s AND last_seen_at <= %s GROUP BY 1"
            ).format(rejects),
            (source_system, started_at, window_end),
        )
        rejects_per_body: Counter[str] = Counter()
        for body_id, count in cur.fetchall():
            rejects_per_body[str(body_id)] += int(count)

    stop_reasons: dict[str, str] = {
        str(k): str(v) for k, v in (stored_notes.get("stop_reasons") or {}).items()
    }
    degraded_ids = {str(b) for b in (stored_notes.get("degraded_bodies") or [])}
    bodies = tuple(
        BodyHealth(
            body_id=body_id,
            emitted=per_body.get(body_id, 0),
            rejected=rejects_per_body.get(body_id, 0),
            fetched=per_body.get(body_id, 0) + rejects_per_body.get(body_id, 0),
            stop_reason=stop_reasons.get(body_id, "(not stored)"),
            degraded=body_id in degraded_ids,
            watermark="(not re-read)",
        )
        for body_id in sorted(set(per_body) | set(rejects_per_body) | set(stop_reasons))
    )

    reported_staged = int(inserted) + int(updated) + int(unchanged)
    population_complete = obs.staged == reported_staged and bool(stop_reasons)
    obs.not_recoverable = max(obs.fetched - obs.rejected - obs.staged, 0)
    quality = stored_notes.get("quality")
    carried = (
        str(quality["verdict"]) if isinstance(quality, dict) and "verdict" in quality else None
    )
    status = str(stored_status)  # the database's own vocabulary, unchanged

    notes = [
        "Rebuilt from stored rows and the run's stored notes. "
        f"The run reported {int(records_fetched)} fetched, "
        f"{reported_staged} loaded ({int(inserted)} inserted / {int(updated)} updated / "
        f"{int(unchanged)} unchanged) and {int(rejected)} rejected; {obs.staged} row(s) were "
        "found that this run wrote or re-observed. "
        + (
            "The two agree, so the distributions below describe the run's whole population."
            if obs.staged == reported_staged
            else "They DISAGREE: a later run re-observed some of these rows or runs "
            "overlapped, so the distributions below are partial and are not graded."
        ),
        "Not stored, so not re-evaluated here: the funnel balance (below-threshold and "
        "in-run duplicate counts), body errors and AGS warnings.",
    ]
    if not stop_reasons:
        notes.append(
            "This run stored no per-body stop reasons, so whether any body degraded is unknown."
        )
    if carried is not None:
        notes.append(f"The run itself computed and stored the verdict {carried}.")
    summary = RunSummary(
        run_id=run_id,
        source_system=str(source_system),
        started_at=started_at or datetime.now(UTC),
        finished_at=window_end,
        status=status,
        counts=LoadCounts(int(inserted), int(updated), int(unchanged)),
        bodies_total=int(stored_notes.get("bodies") or len(bodies)),
        config_hash=config_hash,
        taxonomy_version=taxonomy_version,
        notes=tuple(notes),
        rebuilt=True,
        population_complete=population_complete,
        carried_verdict=carried,
    )
    return summary, obs, bodies
