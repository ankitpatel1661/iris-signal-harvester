"""Quality report and source verification. Entirely offline.

The quality tests assert the two properties that make the report trustworthy rather
than decorative: the funnel balances (so nothing disappears unaccounted for), and
"below threshold" is never counted as a rejection (so a real rejection spike cannot
hide inside a normal non-signal rate).

The verification tests drive the probe through an ``httpx.MockTransport``, so they
also pin the two rules that matter legally and ethically: no credential is ever sent,
and a refusal stops further probing of that host instead of being retried.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from iris_signal_harvester.domain.models import (
    GeoConfidence,
    GeoMethod,
    PublishedAtOrigin,
    RejectCode,
    RejectedRecord,
)
from iris_signal_harvester.reporting.quality import (
    BodyHealth,
    Observations,
    RunSummary,
    Thresholds,
    Verdict,
    build_report,
    write_report,
)
from iris_signal_harvester.reporting.verification import (
    SourceVerifier,
    write_verification,
)
from iris_signal_harvester.sink.postgis import LoadCounts

REPO = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
RUN = uuid.UUID("22222222-2222-2222-2222-222222222222")
THRESHOLDS = Thresholds.load(REPO / "config" / "thresholds.yaml")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class FakeSignal:
    """The attribute surface ``Observations.observe_signal`` reads."""

    def __init__(self, **overrides: object) -> None:
        self.title = "Bebauungsplan Nr. 1: Batteriespeicher"
        self.source_url = "https://oparl.example.org/paper/1"
        self.content_hash = "a" * 64
        self.signal_type = "storage"
        self.relevance_score = 0.9
        self.published_at: datetime | None = NOW
        self.published_at_origin = PublishedAtOrigin.PAYLOAD_FIELD
        self.easting: float | None = 691611.0
        self.northing: float | None = 5334758.0
        self.geo_method = GeoMethod.SOURCE_COORDINATES
        self.geo_confidence = GeoConfidence.HIGH
        self.ags: str | None = "09162000"
        self.municipality_name: str | None = "München"
        self.summary: str | None = None
        self.document_type: str | None = "Beschlussvorlage"
        self.source_licence: str | None = None
        for key, value in overrides.items():
            setattr(self, key, value)


def reject(code: RejectCode, record_id: str) -> RejectedRecord:
    return RejectedRecord(
        harvest_run_id=RUN,
        source_system="fixture",
        source_body_id="body/1",
        source_record_id=record_id,
        reason_code=code,
        reason_detail=f"{code.value} on {record_id}",
        raw_payload={"reference": record_id},
    )


def summary(**overrides: object) -> RunSummary:
    base: dict[str, object] = {
        "run_id": RUN,
        "source_system": "fixture",
        "started_at": NOW,
        "finished_at": NOW,
        "status": "succeeded",
        "counts": LoadCounts(3, 0, 0),
        "bodies_total": 1,
        "taxonomy_version": "2026.10.1",
    }
    base.update(overrides)
    return RunSummary(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# the funnel
# ---------------------------------------------------------------------------


def test_the_funnel_is_subtractive_and_balances() -> None:
    obs = Observations(fetched=10, pages_fetched=2)
    obs.observe_reject(reject(RejectCode.MISSING_REQUIRED_FIELD, "M-1"), from_adapter=True)
    obs.observe_reject(reject(RejectCode.GEO_OUT_OF_CRS_DOMAIN, "G-1"))
    obs.observe_below_threshold()
    obs.observe_below_threshold()
    obs.observe_below_threshold()
    obs.observe_below_threshold()
    obs.observe_duplicate()
    obs.observe_duplicate()
    for _ in range(2):
        obs.observe_signal(FakeSignal())  # type: ignore[arg-type]

    report = build_report(summary(counts=LoadCounts(2, 0, 0)), obs, (), THRESHOLDS)
    assert report.residual == 0
    # Each stage removes records and the remainder is carried forward.
    removed = {step.label: step.removed for step in report.funnel}
    assert removed["malformed by adapter"] == 1
    assert removed["rejected by normaliser"] == 1
    assert removed["below taxonomy threshold"] == 4
    assert removed["intra-run duplicates"] == 2
    assert report.funnel[-2].remaining == 2  # staged
    assert "balances" in report.to_markdown()


def test_a_funnel_that_does_not_balance_is_a_fail() -> None:
    """If the accounting is wrong, no other number in the report can be trusted."""
    obs = Observations(fetched=10, staged=1)
    report = build_report(summary(), obs, (), THRESHOLDS)
    assert report.residual == 9
    assert report.verdict is Verdict.FAIL
    assert any(f.rule == "funnel_balances" for f in report.findings)
    assert "DOES NOT BALANCE" in report.to_markdown()


def test_below_threshold_is_not_reported_as_a_rejection() -> None:
    """Non-signals are the expected majority; they must not inflate the reject rate."""
    obs = Observations(fetched=100)
    for _ in range(99):
        obs.observe_below_threshold()
    obs.observe_signal(FakeSignal())  # type: ignore[arg-type]

    report = build_report(summary(counts=LoadCounts(1, 0, 0)), obs, (), THRESHOLDS)
    assert obs.dead_lettered == 0
    assert not report.rejections
    assert report.verdict is Verdict.PASS
    body = report.to_markdown()
    assert "No records were dead-lettered." in body
    assert "below taxonomy threshold | 99" in body


def test_a_rejection_spike_is_visible_even_when_most_records_are_non_signals() -> None:
    obs = Observations(fetched=100)
    for _ in range(80):
        obs.observe_below_threshold()
    for n in range(15):
        obs.observe_reject(reject(RejectCode.DATE_OUT_OF_RANGE, f"D-{n}"))
    for _ in range(5):
        obs.observe_signal(FakeSignal())  # type: ignore[arg-type]
    report = build_report(summary(counts=LoadCounts(5, 0, 0)), obs, (), THRESHOLDS)
    assert report.verdict is Verdict.FAIL
    assert any(f.rule == "reject_rate_max" for f in report.findings)


# ---------------------------------------------------------------------------
# histogram, completeness, gate
# ---------------------------------------------------------------------------


def test_every_rejection_reason_carries_one_real_example() -> None:
    obs = Observations(fetched=3)
    obs.observe_reject(reject(RejectCode.DATE_UNPARSEABLE, "FIRST"))
    obs.observe_reject(reject(RejectCode.DATE_UNPARSEABLE, "SECOND"))
    obs.observe_reject(reject(RejectCode.GEO_NON_FINITE, "THIRD"))
    histogram = {r.reason_code: r for r in obs.histogram()}
    assert histogram["date_unparseable"].count == 2
    assert histogram["date_unparseable"].example_record_id == "FIRST"
    assert histogram["date_unparseable"].example_body_id == "body/1"
    assert histogram["geo_non_finite"].example_record_id == "THIRD"
    # Ordered by frequency, so the spike is the first row a reader sees.
    assert obs.histogram()[0].reason_code == "date_unparseable"


def test_a_structural_null_is_a_fail_and_a_soft_null_is_a_warn() -> None:
    obs = Observations(fetched=2)
    obs.observe_signal(FakeSignal(signal_type=None, published_at=None))  # type: ignore[arg-type]
    obs.observe_signal(FakeSignal(published_at=None))  # type: ignore[arg-type]
    report = build_report(summary(counts=LoadCounts(2, 0, 0)), obs, (), THRESHOLDS)
    rules = {f.rule: f.level for f in report.findings}
    assert rules["null_rate_max[signal_type]"] is Verdict.FAIL
    assert rules["null_rate_max[published_at]"] is Verdict.WARN
    assert report.verdict is Verdict.FAIL


def test_the_gate_names_the_degraded_bodies() -> None:
    obs = Observations(fetched=1)
    obs.observe_signal(FakeSignal())  # type: ignore[arg-type]
    bodies = (
        BodyHealth(body_id="body/1", name="Gutstadt", fetched=1, emitted=1),
        BodyHealth(
            body_id="body/2",
            name="Kaputtstadt",
            degraded=True,
            stop_reason="circuit_open",
            error="3 consecutive failures",
        ),
    )
    report = build_report(summary(bodies_total=2), obs, bodies, THRESHOLDS)
    finding = next(f for f in report.findings if f.rule == "degraded_body_rate_max")
    assert finding.bodies == ("Kaputtstadt (body/2)",)
    assert "Kaputtstadt" in report.to_markdown()
    assert "watermarks were not advanced" in finding.detail


def test_strict_turns_a_warn_into_a_failure_and_a_nonzero_exit() -> None:
    obs = Observations(fetched=2)
    obs.observe_signal(FakeSignal(published_at=None))  # type: ignore[arg-type]
    obs.observe_signal(FakeSignal(published_at=None))  # type: ignore[arg-type]
    lenient = build_report(summary(counts=LoadCounts(2, 0, 0)), obs, (), THRESHOLDS)
    assert lenient.verdict is Verdict.WARN
    assert lenient.exit_code == 0

    strict = build_report(summary(counts=LoadCounts(2, 0, 0), strict=True), obs, (), THRESHOLDS)
    assert strict.verdict is Verdict.FAIL
    assert strict.exit_code == 1


def test_strict_fails_on_any_rejection_at_all() -> None:
    obs = Observations(fetched=100)
    obs.observe_reject(reject(RejectCode.DATE_UNPARSEABLE, "D-1"))
    for _ in range(99):
        obs.observe_signal(FakeSignal())  # type: ignore[arg-type]
    report = build_report(summary(counts=LoadCounts(99, 0, 0), strict=True), obs, (), THRESHOLDS)
    assert report.verdict is Verdict.FAIL
    assert any(f.rule == "strict.any_rejection_fails" for f in report.findings)


def test_fetching_records_and_staging_none_is_reported() -> None:
    obs = Observations(fetched=4)
    for n in range(4):
        obs.observe_reject(reject(RejectCode.GEO_NON_FINITE, f"X-{n}"))
    report = build_report(summary(counts=LoadCounts(0, 0, 0)), obs, (), THRESHOLDS)
    assert report.verdict is Verdict.FAIL
    assert any(f.rule == "require_staged_when_fetched" for f in report.findings)


def test_both_artefacts_are_written_and_the_json_carries_the_verdict(
    tmp_path: Path,
) -> None:
    obs = Observations(fetched=1)
    obs.observe_signal(FakeSignal())  # type: ignore[arg-type]
    report = build_report(summary(counts=LoadCounts(1, 0, 0)), obs, (), THRESHOLDS)
    markdown, payload = write_report(report, tmp_path)
    assert markdown.name == f"quality_{RUN}.md"
    assert (tmp_path / "quality_latest.json").is_file()
    import json

    data = json.loads(payload.read_text(encoding="utf-8"))
    assert data["verdict"] == "PASS"
    assert data["funnel"]["balances"] is True
    assert data["run"]["run_id"] == str(RUN)
    assert data["exit_code"] == 0


def test_thresholds_refuse_unknown_keys(tmp_path: Path) -> None:
    from iris_signal_harvester.errors import ConfigurationError

    bad = tmp_path / "thresholds.yaml"
    bad.write_text("version: '1'\nnot_a_key: 3\n", encoding="utf-8")
    with pytest.raises(ConfigurationError):
        Thresholds.load(bad)


def test_the_committed_thresholds_mark_the_structural_fields() -> None:
    assert THRESHOLDS.is_structural("title")
    assert THRESHOLDS.is_structural("signal_type")
    assert not THRESHOLDS.is_structural("published_at")
    assert THRESHOLDS.limit_for("geom") == 0.95


# ---------------------------------------------------------------------------
# source verification
# ---------------------------------------------------------------------------

OPARL_SYSTEM = {
    "id": "https://oparl.example.org/oparl/v1.0",
    "type": "https://schema.oparl.org/1.0/System",
    "oparlVersion": "https://schema.oparl.org/1.0/",
    "body": "https://oparl.example.org/oparl/v1.0/list/body",
}


def handler(routes: dict[str, httpx.Response]) -> httpx.MockTransport:
    seen: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert "authorization" not in {k.lower() for k in request.headers}
        return routes.get(str(request.url), httpx.Response(404, text="not found"))

    transport = httpx.MockTransport(respond)
    transport.seen = seen  # type: ignore[attr-defined]
    return transport


def verifier(transport: httpx.MockTransport, resolve: dict[str, list[str]]) -> SourceVerifier:
    import socket

    def resolver(host: str) -> list[str]:
        if host not in resolve:
            raise socket.gaierror(8, "nodename nor servname provided, or not known")
        return resolve[host]

    return SourceVerifier(transport=transport, resolver=resolver, now=NOW)


async def test_the_generated_document_matches_the_committed_artefact_structure(
    tmp_path: Path,
) -> None:
    """The evidence artefact's shape is part of the deliverable, so it is pinned."""
    transport = handler(
        {
            "https://oparl.example.org/oparl/v1.0": httpx.Response(
                200, json=OPARL_SYSTEM, headers={"content-type": "application/json"}
            ),
            "https://oparl.example.org/robots.txt": httpx.Response(
                200, text="User-agent: *\nDisallow:\n"
            ),
        }
    )
    report = await verifier(transport, {"oparl.org": ["157.90.134.52"]}).probe(
        briefed_url="https://ratsdokumente.de/api/search",
        control_hosts=("oparl.org",),
        candidate_urls=("https://oparl.example.org/oparl/v1.0",),
    )
    generated = write_verification(report, tmp_path / "SOURCE_VERIFICATION.md")

    def headings(text: str) -> list[str]:
        return re.findall(r"(?m)^## .+$", text)

    committed = (REPO / "SOURCE_VERIFICATION.md").read_text(encoding="utf-8")
    produced = headings(generated.read_text(encoding="utf-8"))
    # The committed file may carry hand-written appendices (e.g. a live capture) after
    # the generated sections; the generated structure must be an unchanged prefix of it.
    assert headings(committed)[: len(produced)] == produced
    text = generated.read_text(encoding="utf-8")
    assert "No authentication attempted" in text
    assert "does not resolve" in text
    assert "**Branch B**" in text
    assert "JSON object keys: body, id, oparlVersion, type" in text


async def test_nxdomain_on_the_briefed_host_is_stated_as_such() -> None:
    transport = handler({})
    report = await verifier(transport, {"oparl.org": ["1.2.3.4"]}).probe(
        briefed_url="https://ratsdokumente.de/api/search",
        control_hosts=("oparl.org",),
        candidate_urls=(),
    )
    assert report.briefed_exists is False
    assert report.controls_healthy is True
    assert report.as_dict()["briefed"]["exists"] is False


async def test_a_refusal_stops_probing_that_host_and_is_never_retried() -> None:
    """403 is terminal. One request, no credential, no header variation, no retry."""
    transport = handler(
        {
            "https://walled.example.org/": httpx.Response(403, text="Forbidden"),
            "https://walled.example.org/robots.txt": httpx.Response(200, text="User-agent: *"),
        }
    )
    report = await verifier(transport, {"walled.example.org": ["5.6.7.8"]}).probe(
        briefed_url="https://walled.example.org/api/search",
        control_hosts=(),
        candidate_urls=(),
    )
    requests = [str(r.url) for r in transport.seen]  # type: ignore[attr-defined]
    # robots.txt is read BEFORE the first GET; the 403 then stops everything.
    assert requests == ["https://walled.example.org/robots.txt", "https://walled.example.org/"]
    assert report.refusals == ("walled.example.org",)
    assert "stopped all further probing" in report.to_markdown()


async def test_robots_txt_is_read_before_a_candidate_is_fetched() -> None:
    transport = handler(
        {"https://walled.example.org/api": httpx.Response(401, text="Unauthorized")}
    )
    report = await verifier(transport, {"ratsdokumente.de": ["9.9.9.9"]}).probe(
        briefed_url="https://ratsdokumente.de/api/search",
        control_hosts=(),
        candidate_urls=("https://walled.example.org/api",),
    )
    requests = [str(r.url) for r in transport.seen]  # type: ignore[attr-defined]
    assert requests.index("https://walled.example.org/robots.txt") < requests.index(
        "https://walled.example.org/api"
    )
    assert report.refusals == ("walled.example.org",)


async def test_a_robots_disallow_is_reported_as_a_restriction_to_respect() -> None:
    transport = handler(
        {
            "https://closed.example.org/oparl": httpx.Response(
                200, json={"id": "x"}, headers={"content-type": "application/json"}
            ),
            "https://closed.example.org/robots.txt": httpx.Response(
                200, text="User-agent: *\nDisallow: /\n"
            ),
        }
    )
    report = await verifier(transport, {"ratsdokumente.de": ["9.9.9.9"]}).probe(
        briefed_url="https://ratsdokumente.de/api/search",
        control_hosts=(),
        candidate_urls=("https://closed.example.org/oparl",),
    )
    assert report.candidate_robots[0].allowed is False
    assert "disallows automated access" in report.to_markdown()


async def test_a_soft_200_html_page_is_a_finding_not_a_json_endpoint() -> None:
    transport = handler(
        {
            "https://maintenance.example.org/oparl": httpx.Response(
                200, text="<html>Wartungsarbeiten</html>", headers={"content-type": "text/html"}
            ),
            "https://maintenance.example.org/robots.txt": httpx.Response(404, text=""),
        }
    )
    report = await verifier(transport, {"ratsdokumente.de": ["9.9.9.9"]}).probe(
        briefed_url="https://ratsdokumente.de/api/search",
        control_hosts=(),
        candidate_urls=("https://maintenance.example.org/oparl",),
    )
    assert report.candidates[0].json_shape == ()
    assert "soft-200" in (report.candidates[0].json_error or "")
    # A missing robots.txt is the absence of a rule, not a refusal.
    assert report.candidate_robots[0].allowed is True
    assert report.refusals == ()


async def test_unhealthy_controls_invalidate_the_conclusion() -> None:
    transport = handler({})
    report = await verifier(transport, {}).probe(
        briefed_url="https://ratsdokumente.de/api/search",
        control_hosts=("oparl.org",),
        candidate_urls=(),
    )
    assert report.controls_healthy is False
    assert "no conclusion may be drawn" in report.to_markdown()


# ---------------------------------------------------------------------------
# document types (the README claims the report counts unmapped ones)
# ---------------------------------------------------------------------------


def test_unmapped_document_types_are_counted_with_their_raw_values() -> None:
    obs = Observations(fetched=5)
    for raw in ("Mitteilungsvorlage", "Mitteilungsvorlage", "Sonderding"):
        obs.observe_signal(
            FakeSignal(document_type="unmapped", raw_payload={"paperType": f"  {raw} "})  # type: ignore[arg-type]
        )
    obs.observe_signal(FakeSignal(raw_payload={}))  # type: ignore[arg-type]  # a mapped one
    obs.observe_signal(
        FakeSignal(document_type="unmapped", raw_payload={}),  # type: ignore[arg-type]
        document_type_raw="Vorlage X",
    )
    report = build_report(summary(counts=LoadCounts(5, 0, 0)), obs, (), THRESHOLDS)

    dt = report.document_types
    assert (dt.typed, dt.unmapped) == (5, 4)
    assert dict(dt.top_unmapped_raw) == {"Mitteilungsvorlage": 2, "Sonderding": 1, "Vorlage X": 1}
    assert dt.top_unmapped_raw[0] == ("Mitteilungsvorlage", 2)  # most frequent first
    data = report.as_dict()["document_types"]
    assert data["unmapped"] == 4 and data["unmapped_rate"] == 0.8
    assert data["top_unmapped_raw"][0] == {"value": "Mitteilungsvorlage", "n": 2}
    markdown = report.to_markdown()
    assert "## 7b. Document types" in markdown
    assert "| `Mitteilungsvorlage` | 2 |" in markdown


def test_an_absent_document_type_is_not_counted_as_unmapped() -> None:
    obs = Observations(fetched=1)
    obs.observe_signal(FakeSignal(document_type=None))  # type: ignore[arg-type]
    report = build_report(summary(counts=LoadCounts(1, 0, 0)), obs, (), THRESHOLDS)
    assert (report.document_types.typed, report.document_types.unmapped) == (0, 0)


# ---------------------------------------------------------------------------
# a report rebuilt from stored rows may not regrade the run (final-review I-5)
# ---------------------------------------------------------------------------


def _rebuilt_obs(fetched: int = 4, staged: int = 4) -> Observations:
    obs = Observations(fetched=fetched)
    for _ in range(staged):
        obs.observe_signal(FakeSignal())  # type: ignore[arg-type]
    obs.not_recoverable = fetched - staged
    return obs


def _degraded_body() -> BodyHealth:
    return BodyHealth(body_id="body/1", stop_reason="server_error", degraded=True)


def test_a_rebuilt_report_of_a_degraded_run_does_not_flip_to_pass() -> None:
    """The reproduction: a server_error_500 run graded FAIL, then `report` said PASS."""
    rebuilt = summary(rebuilt=True, status="partial", counts=LoadCounts(0, 0, 0))
    report = build_report(rebuilt, _rebuilt_obs(4, 0), (_degraded_body(),), THRESHOLDS)
    assert report.verdict is Verdict.FAIL
    assert any(f.rule == "degraded_body_rate_max" for f in report.findings)


def test_a_rebuilt_report_of_a_failed_run_is_a_fail() -> None:
    report = build_report(
        summary(rebuilt=True, status="failed"),
        _rebuilt_obs(),
        (BodyHealth(body_id="b"),),
        THRESHOLDS,
    )
    assert report.verdict is Verdict.FAIL
    assert any(f.rule == "run_status" for f in report.findings)


def test_the_funnel_balance_is_never_claimed_by_a_rebuild() -> None:
    """Below-threshold and duplicate counts are not stored, so the rule cannot run."""
    obs = Observations(fetched=10)
    for _ in range(4):
        obs.observe_signal(FakeSignal())  # type: ignore[arg-type]
    # no not_recoverable bookkeeping at all: the funnel is out by 6
    report = build_report(
        summary(rebuilt=True, counts=LoadCounts(4, 0, 0)),
        obs,
        (BodyHealth(body_id="b"),),
        THRESHOLDS,
    )
    assert report.residual == 6
    assert not any(f.rule == "funnel_balances" for f in report.findings)
    assert report.verdict is Verdict.PASS  # complete population, nothing re-evaluable failed


def test_an_incomplete_population_is_unknown_never_pass() -> None:
    """A re-run owns no rows; if they cannot be found the report must say it cannot grade."""
    report = build_report(
        summary(rebuilt=True, population_complete=False, counts=LoadCounts(0, 0, 4)),
        _rebuilt_obs(4, 0),
        (BodyHealth(body_id="b"),),
        THRESHOLDS,
    )
    assert report.verdict is Verdict.UNKNOWN
    assert report.exit_code == 3
    assert [f.rule for f in report.findings] == ["cannot_grade"]
    # an empty population must not also trip the completeness rules it cannot evaluate
    assert not any(f.rule.startswith("null_rate_max") for f in report.findings)
    assert report.as_dict()["verdict"] == "UNKNOWN"


def test_an_unfinished_run_is_unknown() -> None:
    report = build_report(
        summary(rebuilt=True, status="running"),
        _rebuilt_obs(),
        (BodyHealth(body_id="b"),),
        THRESHOLDS,
    )
    assert report.verdict is Verdict.UNKNOWN


def test_a_fail_is_definitive_even_when_the_population_is_incomplete() -> None:
    report = build_report(
        summary(rebuilt=True, population_complete=False),
        _rebuilt_obs(4, 0),
        (_degraded_body(),),
        THRESHOLDS,
    )
    assert report.verdict is Verdict.FAIL


@pytest.mark.parametrize("stored", ["PASS", "WARN", "FAIL"])
def test_the_runs_own_stored_verdict_is_carried_not_recomputed(stored: str) -> None:
    report = build_report(
        summary(rebuilt=True, population_complete=False, carried_verdict=stored),
        _rebuilt_obs(4, 0),
        (BodyHealth(body_id="b"),),
        THRESHOLDS,
    )
    assert report.verdict.value == stored
    assert any(f.rule == "carried_verdict" for f in report.findings)


def test_strict_still_escalates_a_carried_warn() -> None:
    report = build_report(
        summary(rebuilt=True, strict=True, carried_verdict="WARN"),
        _rebuilt_obs(),
        (BodyHealth(body_id="b"),),
        THRESHOLDS,
    )
    assert report.verdict is Verdict.FAIL


def test_a_rebuilt_report_never_overwrites_the_runs_own_report(tmp_path: Path) -> None:
    original = build_report(
        summary(counts=LoadCounts(1, 0, 0)), _observations_for_one(), (), THRESHOLDS
    )
    own_md, _ = write_report(original, tmp_path)
    own_text = own_md.read_text(encoding="utf-8")

    rebuilt = build_report(
        summary(rebuilt=True, carried_verdict="PASS"),
        _rebuilt_obs(),
        (BodyHealth(body_id="b"),),
        THRESHOLDS,
    )
    md, js = write_report(rebuilt, tmp_path)
    assert md.name == f"quality_{RUN}.rebuilt.md" and js.name == f"quality_{RUN}.rebuilt.json"
    assert own_md.read_text(encoding="utf-8") == own_text
    assert "rebuilt from stored rows" in (tmp_path / "quality_latest.md").read_text(
        encoding="utf-8"
    )


def test_an_unknown_report_never_overwrites_latest(tmp_path: Path) -> None:
    original = build_report(
        summary(counts=LoadCounts(1, 0, 0)), _observations_for_one(), (), THRESHOLDS
    )
    write_report(original, tmp_path)
    latest = (tmp_path / "quality_latest.md").read_text(encoding="utf-8")

    unknown = build_report(
        summary(rebuilt=True, population_complete=False),
        _rebuilt_obs(4, 0),
        (BodyHealth(body_id="b"),),
        THRESHOLDS,
    )
    assert unknown.verdict is Verdict.UNKNOWN
    md, _ = write_report(unknown, tmp_path)
    assert md.name.endswith(".rebuilt.md") and md.is_file()
    assert (tmp_path / "quality_latest.md").read_text(encoding="utf-8") == latest
    assert "Verdict: UNKNOWN" in md.read_text(encoding="utf-8")


def _observations_for_one() -> Observations:
    obs = Observations(fetched=1)
    obs.observe_signal(FakeSignal())  # type: ignore[arg-type]
    return obs


# ---------------------------------------------------------------------------
# verification: redirects are reported, never followed (final-review F2)
# ---------------------------------------------------------------------------


async def test_a_redirect_is_reported_and_its_target_is_never_requested() -> None:
    transport = handler(
        {
            "https://oparl.example.org/robots.txt": httpx.Response(
                200, text="User-agent: *\nDisallow:\n"
            ),
            "https://oparl.example.org/oparl/v1.0": httpx.Response(
                302, headers={"location": "https://elsewhere.example.net/oparl/v1.0"}
            ),
            "https://elsewhere.example.net/oparl/v1.0": httpx.Response(200, json=OPARL_SYSTEM),
        }
    )
    report = await verifier(transport, {"oparl.org": ["157.90.134.52"]}).probe(
        briefed_url="https://ratsdokumente.de/api/search",
        control_hosts=("oparl.org",),
        candidate_urls=("https://oparl.example.org/oparl/v1.0",),
    )
    (candidate,) = report.candidates
    assert candidate.status == 302
    assert candidate.redirect_to == "https://elsewhere.example.net/oparl/v1.0"
    assert candidate.final_url == "https://oparl.example.org/oparl/v1.0"
    assert "redirect-not-followed:https://elsewhere.example.net/oparl/v1.0" in candidate.line()
    requested = {str(r.url) for r in transport.seen}  # type: ignore[attr-defined]
    assert not any("elsewhere.example.net" in url for url in requested)


async def test_a_redirecting_robots_txt_is_treated_as_unreadable_not_as_allow_all() -> None:
    transport = handler(
        {
            "https://oparl.example.org/robots.txt": httpx.Response(
                301, headers={"location": "https://www.oparl.example.org/robots.txt"}
            ),
            "https://oparl.example.org/oparl/v1.0": httpx.Response(200, json=OPARL_SYSTEM),
        }
    )
    report = await verifier(transport, {"oparl.org": ["157.90.134.52"]}).probe(
        briefed_url="https://ratsdokumente.de/api/search",
        control_hosts=("oparl.org",),
        candidate_urls=("https://oparl.example.org/oparl/v1.0",),
    )
    (candidate,) = report.candidates
    assert candidate.status is None
    assert candidate.error is not None and "robots.txt unreadable" in candidate.error
    assert "redirect not followed" in candidate.error
    requested = {str(r.url) for r in transport.seen}  # type: ignore[attr-defined]
    assert "https://oparl.example.org/oparl/v1.0" not in requested
