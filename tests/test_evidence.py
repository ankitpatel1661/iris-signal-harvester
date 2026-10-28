"""The committed live-probe evidence backs the access-rule claims made in the documentation."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

EVIDENCE = Path(__file__).resolve().parents[1] / "docs" / "evidence"
ACCESS = EVIDENCE / "muenchen-transparent_access_rules.json"

# Every path the OParl adapter requests on the probed host (see docs/evidence/README.md).
READ_PATHS = [
    "/oparl/v1.0",
    "/oparl/v1.0/list/body",
    "/oparl/v1.0/list/paper",
    "/oparl/v1.0/body/1/list/paper",
    "/oparl/v1.0/body/1/list/paper?id=1301812",
    "/oparl/v1.0/body/1/list/paper?modified_since=2026-01-01T00:00:00%2B00:00",
]


def _robots_pattern(rule: str) -> re.Pattern[str]:
    """A robots.txt path rule with ``*`` wildcards and ``$`` end anchor, as a regex."""
    anchored = rule.endswith("$")
    body = rule[:-1] if anchored else rule
    regex = "".join(".*" if ch == "*" else re.escape(ch) for ch in body)
    return re.compile(regex + ("$" if anchored else ""))


def test_robots_txt_is_in_evidence_and_has_the_dav_rule() -> None:
    robots = json.loads(ACCESS.read_text(encoding="utf-8"))["robots_txt"]
    assert robots["http_status"] == 200
    assert "Disallow: /*/dav/" in robots["directives"]


@pytest.mark.parametrize("path", READ_PATHS)
def test_no_disallow_rule_covers_a_path_the_adapter_reads(path: str) -> None:
    directives = json.loads(ACCESS.read_text(encoding="utf-8"))["robots_txt"]["directives"]
    rules = [d.split(":", 1)[1].strip() for d in directives if d.lower().startswith("disallow:")]
    assert rules, "the capture should carry at least one Disallow rule"
    for rule in rules:
        assert _robots_pattern(rule).match(path) is None, f"{rule!r} covers {path!r}"


def test_the_dav_rule_does_match_a_dav_path() -> None:
    """Guards the matcher itself: the rule must bite where it is meant to."""
    assert _robots_pattern("/*/dav/").match("/oparl/dav/x") is not None


def test_body_zero_paper_url_404_is_in_evidence() -> None:
    entry = json.loads(ACCESS.read_text(encoding="utf-8"))["advertised_endpoint_that_404s"]
    assert entry["body"].endswith("/oparl/v1.0/body/0")
    assert entry["advertised_paper_url"].endswith("/oparl/v1.0/list/paper")
    assert entry["http_status"] == 404


def test_no_evidence_file_records_a_literal_contact_address() -> None:
    """The operator address is redacted everywhere; the placeholder is not passed off as sent."""
    for path in sorted(EVIDENCE.glob("*.json")):
        text = path.read_text(encoding="utf-8")
        assert "example.org" not in text, path.name
        assert not re.search(r"mailto:(?!\[redacted\])", text), path.name
