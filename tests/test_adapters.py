"""Adapters: OParl, fixture replay, ratsdokumente. Every test is offline.

The real ``OParlAdapter`` is exercised through the real ``HarvesterClient`` with HTTP
mocked by ``respx``; the cassettes are replayed into respx by ``CassetteReplay``.
The ``FixtureAdapter`` is exercised separately because it owns its own transport.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from iris_signal_harvester.adapters.base import Cursor, Page, SourceAdapter
from iris_signal_harvester.adapters.fixture import CassetteReplay, FixtureAdapter, load_cassette
from iris_signal_harvester.adapters.oparl import OParlAdapter, UnsupportedOParlVersion
from iris_signal_harvester.adapters.ratsdokumente import RatsdokumenteAdapter
from iris_signal_harvester.config import Settings
from iris_signal_harvester.domain.models import Body, RawDocument
from iris_signal_harvester.errors import (
    AccessRestricted,
    ConfigurationError,
    SourceUnavailable,
)
from iris_signal_harvester.http.client import HarvesterClient, UnexpectedResponse

CASSETTES = Path(__file__).parent / "fixtures" / "cassettes"
SYSTEM = "https://oparl.example.org/oparl/v1.0"
PAPERS_1 = f"{SYSTEM}/body/1/list/paper"
JSON = {"content-type": "application/json"}

EXPECTED_SCENARIOS = {
    "happy_path",
    "multi_page",
    "rate_limited_429",
    "server_error_500",
    "malformed_json",
    "empty_page",
    "duplicate_across_pages",
    "missing_publication_date",
    "sentinel_modified_date",
    "coordinate_out_of_domain",
    "latlon_in_projected_field",
    "zone33_coordinates",
    "soft_200_error_page",
}
SUCCEEDING = sorted(
    EXPECTED_SCENARIOS - {"server_error_500", "malformed_json", "soft_200_error_page"}
)


# --- helpers -------------------------------------------------------------------


async def _no_sleep(_seconds: float) -> None:
    return None


def make_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "respect_robots": False,
        "rate_limit_rps": 50.0,
        "max_retries": 2,
        "fixture_dir": CASSETTES,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def make_client(**overrides: object) -> HarvesterClient:
    return HarvesterClient(make_settings(**overrides), sleep=_no_sleep, rng=random.Random(1))


def cassette_replay(scenario: str) -> CassetteReplay:
    return CassetteReplay(load_cassette(CASSETTES / f"{scenario}.json")["interactions"])


def oparl_over_cassette(
    router: respx.MockRouter, scenario: str
) -> tuple[OParlAdapter, CassetteReplay]:
    replay = cassette_replay(scenario)
    router.route().mock(side_effect=replay.handler)
    return OParlAdapter(make_client(), SYSTEM), replay


async def bodies_of(adapter: OParlAdapter | RatsdokumenteAdapter | FixtureAdapter) -> list[Body]:
    return [b async for b in adapter.discover_bodies()]


async def sweep_body(adapter: SourceAdapter, body: Body, *, limit: int = 20) -> list[Page]:
    pages: list[Page] = []
    cursor: Cursor | None = None
    for _ in range(limit):
        page = await adapter.fetch_page(body, cursor)
        pages.append(page)
        if page.next_cursor is None:
            return pages
        cursor = page.next_cursor
    raise AssertionError("sweep did not terminate")


async def sweep_all(adapter: SourceAdapter) -> list[RawDocument]:
    records: list[RawDocument] = []
    async for body in adapter.discover_bodies():
        for page in await sweep_body(adapter, body):
            records.extend(page.records)
    return records


def oparl_system(version: str = "1.0") -> dict[str, Any]:
    t = f"https://schema.oparl.org/{version}/"
    return {
        "id": SYSTEM,
        "type": f"{t}System",
        "oparlVersion": t,
        "body": f"{SYSTEM}/list/body",
    }


def oparl_body(version: str = "1.0", license_: str | None = None) -> dict[str, Any]:
    return {
        "id": f"{SYSTEM}/body/1",
        "type": f"https://schema.oparl.org/{version}/Body",
        "name": "Musterstadt",
        "ags": "09162000",
        "rgs": "091620000000",
        "paper": PAPERS_1,
        "license": license_,
    }


def oparl_paper(version: str = "1.0", n: int = 1, **extra: Any) -> dict[str, Any]:
    paper: dict[str, Any] = {
        "id": f"{SYSTEM}/paper/{n}",
        "type": f"https://schema.oparl.org/{version}/Paper",
        "reference": f"01-20/B{n:05d}",
        "name": f"Beispielvorlage {n}",
        "date": None,
        "modified": "2026-06-01T09:00:00+02:00",
    }
    paper.update(extra)
    return paper


def mount_oparl(
    router: respx.MockRouter,
    *,
    version: str = "1.0",
    papers: list[dict[str, Any]] | None = None,
    next_link: str | None = None,
) -> None:
    router.get(SYSTEM).respond(200, json=oparl_system(version), headers=JSON)
    router.get(f"{SYSTEM}/list/body").respond(
        200, json={"data": [oparl_body(version)], "links": {}}, headers=JSON
    )
    links = {"next": next_link} if next_link else {}
    router.get(PAPERS_1).respond(
        200,
        json={"data": papers if papers is not None else [oparl_paper(version)], "links": links},
        headers=JSON,
    )


# --- the cassettes themselves --------------------------------------------------


def test_all_thirteen_scenarios_are_committed() -> None:
    assert set(FixtureAdapter.available_scenarios(CASSETTES)) == EXPECTED_SCENARIOS


@pytest.mark.parametrize("scenario", sorted(EXPECTED_SCENARIOS))
def test_cassette_is_well_formed_and_anonymised(scenario: str) -> None:
    doc = load_cassette(CASSETTES / f"{scenario}.json")
    assert doc["scenario"] == scenario
    text = (CASSETTES / f"{scenario}.json").read_text(encoding="utf-8")
    assert "example.org" in text
    # No real municipal hosts leak into a cassette.
    assert "muenchen" not in text.lower()
    assert "ratsdokumente.de" not in text


# --- the contract: every adapter satisfies SourceAdapter and emits RawDocument -


def _ratsdokumente_payload(offset: int, count: int) -> dict[str, Any]:
    return {
        "total": 3,
        "offset": offset,
        "limit": 2,
        "results": [
            {
                "id": f"rd-{offset + i}",
                "title": f"Beispieltitel {offset + i}",
                "url": f"https://ratsdokumente.example.org/doc/{offset + i}",
                "document_type": "Beschlussvorlage",
                "published": "2026-05-12",
                "modified": "2026-06-01T09:00:00+02:00",
                "municipality": {"ags": "09162000", "name": "Musterstadt"},
            }
            for i in range(count)
        ],
    }


def mount_ratsdokumente(router: respx.MockRouter) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params["offset"])
        return httpx.Response(
            200, json=_ratsdokumente_payload(offset, 2 if offset == 0 else 1), headers=JSON
        )

    router.get("https://ratsdokumente.example.org/api/search").mock(side_effect=handler)


def build(kind: str, router: respx.MockRouter) -> SourceAdapter:
    if kind == "oparl":
        return oparl_over_cassette(router, "happy_path")[0]
    if kind == "fixture":
        return FixtureAdapter(make_settings(), "happy_path")
    mount_ratsdokumente(router)
    return RatsdokumenteAdapter(
        make_client(), base_url="https://ratsdokumente.example.org", page_size=2
    )


@pytest.mark.parametrize("kind", ["oparl", "fixture", "ratsdokumente"])
async def test_adapter_satisfies_source_adapter_and_emits_valid_raw_documents(kind: str) -> None:
    with respx.mock(assert_all_called=False) as router:
        adapter = build(kind, router)
        assert isinstance(adapter, SourceAdapter)
        assert (
            adapter.source_system
            == {
                "oparl": "oparl",
                "fixture": "fixture",
                "ratsdokumente": "ratsdokumente",
            }[kind]
        )
        caps = adapter.capabilities()
        assert caps.max_page_size > 0
        records = await sweep_all(adapter)
        await adapter.aclose()

    assert records
    for record in records:
        assert isinstance(record, RawDocument)
        revalidated = RawDocument.model_validate(record.model_dump())
        assert revalidated == record
        assert record.source_system == adapter.source_system
        assert record.source_record_id.strip()
        assert record.source_url.startswith(("http://", "https://"))


def test_capabilities_are_declared_not_assumed() -> None:
    oparl = OParlAdapter(make_client(), SYSTEM).capabilities()
    assert oparl.pagination == "object_link"
    assert oparl.supports_modified_since is True
    assert set(oparl.oparl_versions) == {"1.0", "1.1"}

    rats = RatsdokumenteAdapter(make_client()).capabilities()
    assert rats.pagination == "offset"
    assert rats.supports_modified_since is False  # unverified contract: not claimed


# --- OParl: mapping ------------------------------------------------------------


async def test_oparl_maps_paper_fields_and_licences() -> None:
    with respx.mock(assert_all_called=False) as router:
        adapter, _ = oparl_over_cassette(router, "happy_path")
        bodies = await bodies_of(adapter)
        assert [b.name for b in bodies] == ["Musterstadt", "Beispielstadt"]
        licensed, unlicensed = bodies
        assert licensed.licence == "https://creativecommons.org/licenses/by/4.0/"
        assert unlicensed.licence is None  # license: null stays None, never inferred
        assert licensed.ags == "09162000"
        assert licensed.ars == "091620000000"  # OParl 'rgs'
        assert licensed.paper_endpoint == "https://oparl.example.org/oparl/v1.0/body/1/list/paper"

        page = (await sweep_body(adapter, licensed))[0]
        other = (await sweep_body(adapter, unlicensed))[0]

    first = page.records[0]
    assert first.source_record_id == first.payload["id"] == first.source_url
    assert first.payload["reference"]  # the file number is kept as an attribute
    assert first.source_url == first.payload["id"]
    assert first.source_body_id == licensed.id
    assert first.licence == licensed.licence
    assert first.payload["name"].startswith("Antrag")
    assert first.payload["paperType"] == "BA-Antrag"
    assert all(r.licence is None for r in other.records)


async def test_record_id_is_paper_id_and_reference_is_only_an_attribute() -> None:
    papers = [
        oparl_paper(n=7, reference=None),
        oparl_paper(n=8, reference="   "),
        oparl_paper(n=9),
        {k: v for k, v in oparl_paper(n=10).items() if k != "reference"},
    ]
    with respx.mock(assert_all_called=False) as router:
        mount_oparl(router, papers=papers)
        records = await sweep_all(OParlAdapter(make_client(), SYSTEM))
    assert [r.source_record_id for r in records] == [f"{SYSTEM}/paper/{n}" for n in (7, 8, 9, 10)]
    # The file number survives untouched in the payload, but is not the identity.
    assert records[2].payload["reference"] == "01-20/B00009"


async def test_shared_reference_does_not_collapse_distinct_papers() -> None:
    """Two papers with the same Aktenzeichen are two records; one renumbered paper is one."""
    papers = [
        oparl_paper(n=1, reference="DS 1/2026"),
        oparl_paper(n=2, reference="DS 1/2026"),
    ]
    renumbered = [oparl_paper(n=1, reference="DS 99/2026")]
    with respx.mock(assert_all_called=False) as router:
        mount_oparl(router, papers=papers)
        shared = await sweep_all(OParlAdapter(make_client(), SYSTEM))
    with respx.mock(assert_all_called=False) as router:
        mount_oparl(router, papers=renumbered)
        after = await sweep_all(OParlAdapter(make_client(), SYSTEM))
    assert len({r.source_record_id for r in shared}) == 2
    assert after[0].source_record_id == shared[0].source_record_id


async def test_paper_without_any_identity_is_reported_not_raised_and_not_lost() -> None:
    bad = {"name": "no id at all", "reference": "DS 1/2026"}
    with respx.mock(assert_all_called=False) as router:
        mount_oparl(router, papers=[bad])
        adapter = OParlAdapter(make_client(), SYSTEM)
        (body,) = await bodies_of(adapter)
        (page,) = await sweep_body(adapter, body)
    assert tuple(page.records) == ()
    assert list(page.malformed) == [("missing_required_field", bad)]


async def test_one_unusable_record_does_not_discard_the_rest_of_its_page() -> None:
    bad = {"name": "no id at all", "reference": None}
    good = [oparl_paper(n=n) for n in (1, 2, 3, 4)]
    papers = [good[0], good[1], bad, good[2], good[3]]
    with respx.mock(assert_all_called=False) as router:
        mount_oparl(router, papers=papers)
        adapter = OParlAdapter(make_client(), SYSTEM)
        (body,) = await bodies_of(adapter)
        (page,) = await sweep_body(adapter, body)
    assert [r.source_record_id for r in page.records] == [
        f"{SYSTEM}/paper/{n}" for n in (1, 2, 3, 4)
    ]
    assert len(page.malformed) == 1
    reason, payload = page.malformed[0]
    assert reason == "missing_required_field"
    assert payload == bad


async def test_non_object_collection_item_is_reported_as_malformed() -> None:
    with respx.mock(assert_all_called=False) as router:
        mount_oparl(router, papers=[oparl_paper(n=1), "junk", oparl_paper(n=2)])  # type: ignore[list-item]
        adapter = OParlAdapter(make_client(), SYSTEM)
        (body,) = await bodies_of(adapter)
        (page,) = await sweep_body(adapter, body)
    assert len(page.records) == 2
    assert list(page.malformed) == [("schema_validation_failed", {"value": "junk"})]


async def test_page_level_parse_failure_still_raises_loudly() -> None:
    with respx.mock(assert_all_called=False) as router:
        adapter, _ = oparl_over_cassette(router, "malformed_json")
        body = (await bodies_of(adapter))[0]
        with pytest.raises(SourceUnavailable):
            await adapter.fetch_page(body, None)


# --- OParl: dates are captured raw ---------------------------------------------


async def test_null_and_absent_dates_are_preserved_not_defaulted() -> None:
    with respx.mock(assert_all_called=False) as router:
        adapter, _ = oparl_over_cassette(router, "missing_publication_date")
        records = await sweep_all(adapter)
    assert len(records) == 3
    assert records[0].payload["date"] is None
    assert records[1].payload["date"] is None
    assert "date" not in records[2].payload
    assert all(isinstance(r.payload["modified"], str) for r in records)


async def test_sentinel_modified_is_captured_verbatim_not_parsed_or_dropped() -> None:
    with respx.mock(assert_all_called=False) as router:
        adapter, _ = oparl_over_cassette(router, "sentinel_modified_date")
        records = await sweep_all(adapter)
    assert records[0].payload["modified"] == "-0001-11-30T00:00:00+00:53"
    assert records[1].payload["modified"] == "-0001-11-30T00:00:00+00:53"
    assert records[2].payload["modified"] == "2026-06-01T09:00:00+02:00"
    # Raw strings all the way down: this module never produced a datetime.
    assert not any(isinstance(r.payload["modified"], datetime) for r in records)
    assert all(r.http_last_modified is None for r in records)


@pytest.mark.parametrize(
    "scenario", ["coordinate_out_of_domain", "latlon_in_projected_field", "zone33_coordinates"]
)
async def test_source_coordinates_are_passed_through_unvalidated(scenario: str) -> None:
    """Geometry is judged by the geo resolver; the adapter must not clamp, snap or drop."""
    raw = load_cassette(CASSETTES / f"{scenario}.json")["interactions"]
    recorded = [
        item["location"]
        for ix in raw
        for item in ix["response"].get("json", {}).get("data", [])
        if isinstance(item, dict) and "location" in item
    ]
    assert recorded
    with respx.mock(assert_all_called=False) as router:
        adapter, _ = oparl_over_cassette(router, scenario)
        records = await sweep_all(adapter)
    assert [r.payload["location"] for r in records if "location" in r.payload] == recorded


# --- OParl: pagination ---------------------------------------------------------


async def test_pagination_walks_every_page_via_links_next() -> None:
    with respx.mock(assert_all_called=False) as router:
        adapter, replay = oparl_over_cassette(router, "multi_page")
        body = (await bodies_of(adapter))[0]
        pages = await sweep_body(adapter, body)

    assert [len(p.records) for p in pages] == [2, 2, 1]
    assert [p.next_cursor for p in pages[:2]] == [
        Cursor(token=f"{PAPERS_1}?id=1001"),
        Cursor(token=f"{PAPERS_1}?id=1003"),
    ]
    assert pages[-1].next_cursor is None
    ids = [r.source_record_id for p in pages for r in p.records]
    assert len(ids) == len(set(ids)) == 5
    paper_requests = [u for u in replay.requests if "/list/paper" in u]
    assert paper_requests == [PAPERS_1, f"{PAPERS_1}?id=1001", f"{PAPERS_1}?id=1003"]


async def test_first_page_uses_body_paper_endpoint_and_cursor_token_is_followed_verbatim() -> None:
    with respx.mock(assert_all_called=False) as router:
        # Registered first: respx matches in order and a param-less route would shadow it.
        router.get(f"{PAPERS_1}?id=42").respond(
            200, json={"data": [oparl_paper(n=2)], "links": {}}, headers=JSON
        )
        mount_oparl(router, next_link=f"{PAPERS_1}?id=42")
        adapter = OParlAdapter(make_client(), SYSTEM)
        body = (await bodies_of(adapter))[0]
        first = await adapter.fetch_page(body, None)
        assert first.next_cursor == Cursor(token=f"{PAPERS_1}?id=42")
        second = await adapter.fetch_page(body, first.next_cursor)
    assert [r.source_record_id for r in second.records] == [f"{SYSTEM}/paper/2"]
    assert second.next_cursor is None


async def test_next_link_on_another_host_is_refused() -> None:
    with respx.mock(assert_all_called=False) as router:
        mount_oparl(router)
        adapter = OParlAdapter(make_client(), SYSTEM)
        body = (await bodies_of(adapter))[0]
        with pytest.raises(UnexpectedResponse):
            await adapter.fetch_page(body, Cursor(token="https://elsewhere.example.net/p?id=1"))


async def test_body_list_is_paginated_too() -> None:
    other = dict(oparl_body(), id=f"{SYSTEM}/body/2", name="Beispielstadt")
    with respx.mock(assert_all_called=False) as router:
        # Registered first: respx matches in order and a param-less route would shadow it.
        router.get(f"{SYSTEM}/list/body?id=1").respond(
            200, json={"data": [other], "links": {}}, headers=JSON
        )
        router.get(SYSTEM).respond(200, json=oparl_system(), headers=JSON)
        router.get(f"{SYSTEM}/list/body").respond(
            200,
            json={"data": [oparl_body()], "links": {"next": f"{SYSTEM}/list/body?id=1"}},
            headers=JSON,
        )
        bodies = await bodies_of(OParlAdapter(make_client(), SYSTEM))
    assert [b.name for b in bodies] == ["Musterstadt", "Beispielstadt"]


async def test_empty_page_is_a_legitimate_end_not_an_error() -> None:
    with respx.mock(assert_all_called=False) as router:
        adapter, _ = oparl_over_cassette(router, "empty_page")
        body = (await bodies_of(adapter))[0]
        pages = await sweep_body(adapter, body)
    assert len(pages) == 1
    assert tuple(pages[0].records) == ()
    assert pages[0].next_cursor is None


async def test_duplicates_across_pages_are_emitted_for_the_orchestrator_to_collapse() -> None:
    with respx.mock(assert_all_called=False) as router:
        adapter, _ = oparl_over_cassette(router, "duplicate_across_pages")
        body = (await bodies_of(adapter))[0]
        pages = await sweep_body(adapter, body)
    ids = [r.source_record_id for p in pages for r in p.records]
    assert len(ids) == 4
    assert len(set(ids)) == 3
    assert pages[0].id_fingerprint() != pages[1].id_fingerprint()


# --- OParl: versions -----------------------------------------------------------


@pytest.mark.parametrize("version", ["1.0", "1.1"])
async def test_both_oparl_type_url_versions_are_accepted(version: str) -> None:
    with respx.mock(assert_all_called=False) as router:
        mount_oparl(router, version=version)
        records = await sweep_all(OParlAdapter(make_client(), SYSTEM))
    assert len(records) == 1
    assert records[0].payload["type"] == f"https://schema.oparl.org/{version}/Paper"


async def test_mixed_version_payload_is_accepted() -> None:
    with respx.mock(assert_all_called=False) as router:
        mount_oparl(router, version="1.1", papers=[oparl_paper("1.0", 1), oparl_paper("1.1", 2)])
        records = await sweep_all(OParlAdapter(make_client(), SYSTEM))
    assert len(records) == 2


async def test_multi_page_cassette_serves_oparl_1_1_end_to_end() -> None:
    doc = load_cassette(CASSETTES / "multi_page.json")
    assert doc["interactions"][0]["response"]["json"]["type"].endswith("/1.1/System")
    with respx.mock(assert_all_called=False) as router:
        adapter, _ = oparl_over_cassette(router, "multi_page")
        assert len(await sweep_all(adapter)) == 5


@pytest.mark.parametrize("bad_type", ["https://schema.oparl.org/2.0/Paper", "urn:other:Paper"])
async def test_unsupported_paper_type_is_refused_not_guessed_at(bad_type: str) -> None:
    with respx.mock(assert_all_called=False) as router:
        mount_oparl(router, papers=[oparl_paper(type=bad_type)])
        with pytest.raises(UnsupportedOParlVersion):
            await sweep_all(OParlAdapter(make_client(), SYSTEM))


async def test_unsupported_system_version_is_refused() -> None:
    with respx.mock(assert_all_called=False) as router:
        router.get(SYSTEM).respond(200, json=oparl_system("2.0"), headers=JSON)
        with pytest.raises(UnsupportedOParlVersion):
            await bodies_of(OParlAdapter(make_client(), SYSTEM))


async def test_system_without_type_is_refused() -> None:
    with respx.mock(assert_all_called=False) as router:
        router.get(SYSTEM).respond(200, json={"body": f"{SYSTEM}/list/body"}, headers=JSON)
        with pytest.raises(UnsupportedOParlVersion):
            await bodies_of(OParlAdapter(make_client(), SYSTEM))


# --- OParl: modified_since -----------------------------------------------------


async def test_modified_since_is_sent_on_first_page_and_on_next_links() -> None:
    since = datetime(2026, 9, 1, 12, 30, tzinfo=timezone(timedelta(hours=2)))
    seen: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        nxt = f"{PAPERS_1}?id=5" if "id" not in request.url.params else None
        links = {"next": nxt} if nxt else {}
        return httpx.Response(200, json={"data": [], "links": links}, headers=JSON)

    with respx.mock(assert_all_called=False) as router:
        router.get(SYSTEM).respond(200, json=oparl_system(), headers=JSON)
        router.get(f"{SYSTEM}/list/body").respond(
            200, json={"data": [oparl_body()], "links": {}}, headers=JSON
        )
        router.get(PAPERS_1).mock(side_effect=handler)
        adapter = OParlAdapter(make_client(), SYSTEM)
        body = (await bodies_of(adapter))[0]
        first = await adapter.fetch_page(body, None, modified_since=since)
        await adapter.fetch_page(body, first.next_cursor, modified_since=since)

    assert len(seen) == 2
    # normalised to UTC; the +02:00 offset is not lost, it is converted
    assert all(u.params["modified_since"] == "2026-09-01T10:30:00+00:00" for u in seen)
    assert seen[1].params["id"] == "5"


async def test_no_modified_since_parameter_when_not_requested() -> None:
    with respx.mock(assert_all_called=False) as router:
        mount_oparl(router)
        route = router.routes[-1]
        await sweep_all(OParlAdapter(make_client(), SYSTEM))
        assert "modified_since" not in route.calls.last.request.url.params


async def test_naive_modified_since_is_rejected_rather_than_assumed_utc() -> None:
    with respx.mock(assert_all_called=False) as router:
        mount_oparl(router)
        adapter = OParlAdapter(make_client(), SYSTEM)
        body = (await bodies_of(adapter))[0]
        with pytest.raises(ValueError, match="timezone-aware"):
            await adapter.fetch_page(body, None, modified_since=datetime(2026, 9, 1))


# --- OParl: failure modes ------------------------------------------------------


async def test_rate_limited_then_recovers() -> None:
    with respx.mock(assert_all_called=False) as router:
        adapter, replay = oparl_over_cassette(router, "rate_limited_429")
        records = await sweep_all(adapter)
    assert len(records) == 2
    assert sum("/list/paper" in u for u in replay.requests) == 2


@pytest.mark.parametrize("scenario", ["server_error_500", "malformed_json", "soft_200_error_page"])
async def test_failing_sources_raise_and_never_look_like_an_empty_page(scenario: str) -> None:
    with respx.mock(assert_all_called=False) as router:
        adapter, _ = oparl_over_cassette(router, scenario)
        body = (await bodies_of(adapter))[0]
        with pytest.raises(SourceUnavailable):
            await adapter.fetch_page(body, None)


async def test_access_restricted_is_terminal_and_not_retried() -> None:
    with respx.mock(assert_all_called=False) as router:
        router.get(SYSTEM).respond(200, json=oparl_system(), headers=JSON)
        router.get(f"{SYSTEM}/list/body").respond(
            200, json={"data": [oparl_body()], "links": {}}, headers=JSON
        )
        denied = router.get(PAPERS_1).respond(403)
        adapter = OParlAdapter(make_client(), SYSTEM)
        body = (await bodies_of(adapter))[0]
        with pytest.raises(AccessRestricted):
            await adapter.fetch_page(body, None)
    assert denied.call_count == 1


async def test_non_collection_json_is_an_error_not_an_empty_page() -> None:
    with respx.mock(assert_all_called=False) as router:
        router.get(SYSTEM).respond(200, json=oparl_system(), headers=JSON)
        router.get(f"{SYSTEM}/list/body").respond(
            200, json={"data": [oparl_body()], "links": {}}, headers=JSON
        )
        router.get(PAPERS_1).respond(200, json={"error": "maintenance"}, headers=JSON)
        adapter = OParlAdapter(make_client(), SYSTEM)
        body = (await bodies_of(adapter))[0]
        with pytest.raises(SourceUnavailable):
            await adapter.fetch_page(body, None)


async def test_body_without_paper_endpoint_is_a_configuration_error() -> None:
    adapter = OParlAdapter(make_client(), SYSTEM)
    with pytest.raises(ConfigurationError):
        await adapter.fetch_page(Body(id="x"), None)


# --- fixture adapter -----------------------------------------------------------


async def _snapshot(scenario: str) -> list[str]:
    adapter = FixtureAdapter(make_settings(), scenario)
    try:
        return [r.model_dump_json() for r in await sweep_all(adapter)]
    finally:
        await adapter.aclose()


@pytest.mark.parametrize("scenario", SUCCEEDING)
async def test_fixture_adapter_is_deterministic_across_runs(scenario: str) -> None:
    first = await _snapshot(scenario)
    second = await _snapshot(scenario)
    assert first == second
    assert first or scenario == "empty_page"


async def test_fixture_adapter_replay_state_is_per_instance() -> None:
    """Consuming a [429, 200] cassette in one adapter does not use it up for the next."""
    for _ in range(3):
        assert len(await _snapshot("rate_limited_429")) == 2


@pytest.mark.parametrize("scenario", ["server_error_500", "malformed_json", "soft_200_error_page"])
async def test_fixture_adapter_reproduces_failures_through_the_production_client(
    scenario: str,
) -> None:
    adapter = FixtureAdapter(make_settings(), scenario)
    body = (await bodies_of(adapter))[0]
    with pytest.raises(SourceUnavailable):
        await adapter.fetch_page(body, None)
    await adapter.aclose()


async def test_fixture_adapter_emits_fixture_source_system_and_same_pages_as_oparl() -> None:
    with respx.mock(assert_all_called=False) as router:
        oparl, _ = oparl_over_cassette(router, "multi_page")
        live_shape = await sweep_all(oparl)
    replayed = await sweep_all(FixtureAdapter(make_settings(), "multi_page"))
    assert {r.source_system for r in replayed} == {"fixture"}
    assert [r.model_copy(update={"source_system": "oparl"}) for r in replayed] == live_shape


async def test_fixture_adapter_needs_no_network() -> None:
    with respx.mock(assert_all_called=False) as router:
        records = await sweep_all(FixtureAdapter(make_settings(), "happy_path"))
        assert len(records) == 4
        assert router.calls.call_count == 0


async def test_fixture_adapter_reads_settings_fixture_dir_and_defaults_to_happy_path() -> None:
    adapter = FixtureAdapter(make_settings(fixture_dir=CASSETTES))
    assert adapter.scenario == "happy_path"
    assert len(await sweep_all(adapter)) == 4


def test_unknown_scenario_is_a_configuration_error_naming_the_alternatives() -> None:
    with pytest.raises(ConfigurationError, match="happy_path"):
        FixtureAdapter(make_settings(), "does_not_exist")


def test_corrupt_cassette_is_a_configuration_error(tmp_path: Path) -> None:
    (tmp_path / "bad.json").write_text("{not json", encoding="utf-8")
    (tmp_path / "noversion.json").write_text('{"interactions": []}', encoding="utf-8")
    for name in ("bad", "noversion"):
        with pytest.raises(ConfigurationError):
            FixtureAdapter(make_settings(), name, fixture_dir=tmp_path)


async def test_unrecorded_request_is_a_404_not_silent_success(tmp_path: Path) -> None:
    (tmp_path / "tiny.json").write_text(
        '{"cassette_version": 1, "system_url": "https://oparl.example.org/oparl/v1.0",'
        ' "interactions": [{"request": {"method": "GET", "url": "https://oparl.example.org/x"},'
        ' "response": {"status": 200, "json": {}}}]}',
        encoding="utf-8",
    )
    adapter = FixtureAdapter(make_settings(), "tiny", fixture_dir=tmp_path)
    with pytest.raises(UnexpectedResponse):
        await bodies_of(adapter)


# --- ratsdokumente (Pattern C; the briefed source does not exist) ---------------


def test_ratsdokumente_docstring_records_that_the_source_does_not_exist() -> None:
    import iris_signal_harvester.adapters.ratsdokumente as module

    doc = module.__doc__ or ""
    assert "2026-10-27" in doc
    assert "NXDOMAIN" in doc
    assert "Status: free" in doc
    assert "NOT presented as verified" in " ".join(doc.split())


async def test_ratsdokumente_offset_pagination_follows_total() -> None:
    requests: list[httpx.QueryParams] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.params)
        offset = int(request.url.params["offset"])
        return httpx.Response(
            200, json=_ratsdokumente_payload(offset, 2 if offset == 0 else 1), headers=JSON
        )

    with respx.mock(assert_all_called=False) as router:
        router.get("https://ratsdokumente.example.org/api/search").mock(side_effect=handler)
        adapter = RatsdokumenteAdapter(
            make_client(), base_url="https://ratsdokumente.example.org/", page_size=2, query="solar"
        )
        (body,) = await bodies_of(adapter)
        pages = await sweep_body(adapter, body)

    assert [len(p.records) for p in pages] == [2, 1]
    assert pages[0].next_cursor == Cursor(offset=2)
    assert pages[1].next_cursor is None
    assert [(p["offset"], p["limit"], p["q"]) for p in requests] == [
        ("0", "2", "solar"),
        ("2", "2", "solar"),
    ]
    assert "modified_since" not in requests[0]
    first = pages[0].records[0]
    assert (first.source_record_id, first.source_body_id) == ("rd-0", "ratsdokumente")
    assert first.payload["municipality"]["ags"] == "09162000"


async def test_ratsdokumente_without_total_stops_on_a_short_page() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        offset = int(request.url.params["offset"])
        doc = _ratsdokumente_payload(offset, 2 if offset == 0 else 1)
        del doc["total"]
        return httpx.Response(200, json=doc, headers=JSON)

    with respx.mock(assert_all_called=False) as router:
        router.get("https://ratsdokumente.example.org/api/search").mock(side_effect=handler)
        adapter = RatsdokumenteAdapter(
            make_client(), base_url="https://ratsdokumente.example.org", page_size=2
        )
        (body,) = await bodies_of(adapter)
        pages = await sweep_body(adapter, body)
    assert [len(p.records) for p in pages] == [2, 1]


async def test_ratsdokumente_empty_results_end_the_sweep_even_if_total_lies() -> None:
    with respx.mock(assert_all_called=False) as router:
        router.get("https://ratsdokumente.example.org/api/search").respond(
            200, json={"total": 99, "results": []}, headers=JSON
        )
        adapter = RatsdokumenteAdapter(make_client(), base_url="https://ratsdokumente.example.org")
        (body,) = await bodies_of(adapter)
        page = await adapter.fetch_page(body, Cursor(offset=40))
    assert tuple(page.records) == ()
    assert page.next_cursor is None


async def test_ratsdokumente_modified_since_only_when_declared_supported() -> None:
    since = datetime(2026, 9, 1, tzinfo=UTC)
    with respx.mock(assert_all_called=False) as router:
        route = router.get("https://ratsdokumente.example.org/api/search").respond(
            200, json={"total": 0, "results": []}, headers=JSON
        )
        off = RatsdokumenteAdapter(make_client(), base_url="https://ratsdokumente.example.org")
        on = RatsdokumenteAdapter(
            make_client(),
            base_url="https://ratsdokumente.example.org",
            supports_modified_since=True,
        )
        assert on.capabilities().supports_modified_since is True
        (body,) = await bodies_of(off)
        await off.fetch_page(body, None, modified_since=since)
        assert "modified_since" not in route.calls.last.request.url.params
        await on.fetch_page(body, None, modified_since=since)
        assert route.calls.last.request.url.params["modified_since"] == "2026-09-01T00:00:00+00:00"


@pytest.mark.parametrize(
    ("bad", "reason"),
    [
        ({"title": "no id", "url": "https://x.example.org/1"}, "missing_required_field"),
        ({"id": "1", "title": "no url"}, "missing_required_field"),
        ("not an object", "schema_validation_failed"),
    ],
)
async def test_ratsdokumente_malformed_record_is_reported_and_the_page_continues(
    bad: Any, reason: str
) -> None:
    good = {"id": "ok-1", "title": "fine", "url": "https://x.example.org/ok-1"}
    with respx.mock(assert_all_called=False) as router:
        router.get("https://ratsdokumente.example.org/api/search").respond(
            200, json={"results": [good, bad, {**good, "id": "ok-2"}]}, headers=JSON
        )
        adapter = RatsdokumenteAdapter(make_client(), base_url="https://ratsdokumente.example.org")
        (body,) = await bodies_of(adapter)
        page = await adapter.fetch_page(body, None)
    assert [r.source_record_id for r in page.records] == ["ok-1", "ok-2"]
    assert len(page.malformed) == 1
    assert page.malformed[0][0] == reason
    assert isinstance(page.malformed[0][1], dict)


async def test_ratsdokumente_non_search_json_is_an_error_not_an_empty_page() -> None:
    with respx.mock(assert_all_called=False) as router:
        router.get("https://ratsdokumente.example.org/api/search").respond(
            200, json={"error": "gone"}, headers=JSON
        )
        adapter = RatsdokumenteAdapter(make_client(), base_url="https://ratsdokumente.example.org")
        (body,) = await bodies_of(adapter)
        with pytest.raises(SourceUnavailable):
            await adapter.fetch_page(body, None)


def test_ratsdokumente_rejects_out_of_range_page_size() -> None:
    with pytest.raises(ValueError, match="page_size"):
        RatsdokumenteAdapter(make_client(), page_size=0)
    with pytest.raises(ValueError, match="page_size"):
        RatsdokumenteAdapter(make_client(), page_size=101)
