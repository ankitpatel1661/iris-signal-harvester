"""Tests for the German-aware taxonomy matcher. Offline; no network, no database."""

from __future__ import annotations

import unicodedata
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from iris_signal_harvester.domain.models import Hit, MatchResult
from iris_signal_harvester.domain.taxonomy import Taxonomy, compile_term, fold
from iris_signal_harvester.errors import ConfigurationError

REPO_ROOT = Path(__file__).resolve().parent.parent
TAXONOMY_PATH = REPO_ROOT / "config" / "taxonomy.yaml"

ALL_SIGNAL_TYPES = (
    "digital_infra",
    "grid_connection",
    "heat_planning",
    "land_use_plan",
    "pv_open_space",
    "storage",
    "transport_civils",
    "wind_energy",
)


@pytest.fixture(scope="module")
def taxonomy() -> Taxonomy:
    return Taxonomy.load(TAXONOMY_PATH)


def make_taxonomy(
    terms: dict[str, list[dict[str, Any]]],
    *,
    negation_terms: tuple[str, ...] = (),
    window: int = 4,
    breadth_bonus: float = 0.05,
    cap: float = 1.0,
) -> Taxonomy:
    """A tiny in-memory taxonomy so scoring arithmetic is exact and independent of the YAML."""
    return Taxonomy.from_mapping(
        {
            "version": "test-1",
            "field_weights": {"title": 1.0, "summary": 0.6, "body": 0.4},
            "breadth_bonus": breadth_bonus,
            "cap": cap,
            "emit_min_score": 0.35,
            "negation": {"window": window, "terms": list(negation_terms)},
            "signal_types": {name: {"terms": entries} for name, entries in terms.items()},
        }
    )


# --------------------------------------------------------------------------- fold


class TestFold:
    def test_umlauts_and_sharp_s(self) -> None:
        assert fold("Wärme Größe Über Öl") == "waerme groesse ueber oel"

    def test_casefold(self) -> None:
        assert fold("SOLARPARK") == "solarpark"

    def test_nfkc_composes_decomposed_umlaut(self) -> None:
        decomposed = unicodedata.normalize("NFD", "Wärmeplanung")
        assert decomposed != "Wärmeplanung"
        assert fold(decomposed) == "waermeplanung"

    def test_nfkc_compatibility_forms(self) -> None:
        assert fold("\uff30\uff36") == "pv"  # full-width letters

    def test_idempotent(self) -> None:
        once = fold("Straße Übergröße")
        assert fold(once) == once


# ---------------------------------------------------------------- compound matching


class TestCompoundHeuristic:
    def test_solarpark_found_inside_solarparkerweiterung(self, taxonomy: Taxonomy) -> None:
        result = taxonomy.match({"title": "Solarparkerweiterung Musterdorf"})
        hits = [h for h in result.evidence if h.term == "Solarpark"]
        assert len(hits) == 1
        assert hits[0].surface == "solarparkerweiterung"
        assert hits[0].offset == 0
        assert result.signal_type == "pv_open_space"

    def test_freiflaechen_pv_spelling_variants(self, taxonomy: Taxonomy) -> None:
        for text in ("Freiflächen-PV", "Freiflächen PV", "FreiflächenPV", "FREIFLÄCHEN-PV"):
            result = taxonomy.match({"title": text})
            assert any(h.term == "Freiflächen-PV" for h in result.evidence), text

    def test_freiflaechen_pv_found_inside_freiflaechenphotovoltaikanlage(
        self, taxonomy: Taxonomy
    ) -> None:
        # "Freiflächen-Photovoltaik" is the long spelling of Freiflächen-PV in the taxonomy;
        # the optional hyphen plus the bounded suffix reach the one-word compound.
        result = taxonomy.match({"title": "Bau einer Freiflächenphotovoltaikanlage"})
        hits = [h for h in result.evidence if h.term == "Freiflächen-Photovoltaik"]
        assert len(hits) == 1
        assert hits[0].surface == "freiflaechenphotovoltaikanlage"
        assert result.signal_type == "pv_open_space"

    def test_suffix_is_bounded(self) -> None:
        pattern = compile_term("Solarpark", compound=True)
        assert pattern.search("solarpark" + "a" * 20)
        assert pattern.search("solarpark" + "a" * 21) is None

    def test_non_compound_term_requires_whole_word(self) -> None:
        pattern = compile_term("Solarpark", compound=False)
        assert pattern.search("solarpark")
        assert pattern.search("solarparks") is None

    def test_left_boundary_is_enforced(self) -> None:
        # The heuristic finds a term at the START of a compound, never at the end.
        pattern = compile_term("Solarpark", compound=True)
        assert pattern.search("freiflaechensolarpark") is None
        assert pattern.search("agri-solarpark")  # hyphen is a boundary

    def test_generic_head_noun_is_rejected_as_compound_term(self) -> None:
        # The taxonomy, not the regex, is what prevents false positives.
        with pytest.raises(ConfigurationError, match="generic head noun"):
            make_taxonomy({"x": [{"term": "Park", "weight": 0.5, "compound": True}]})
        with pytest.raises(ConfigurationError, match="generic head noun"):
            make_taxonomy({"x": [{"term": "Wärme", "weight": 0.5, "compound": True}]})

    def test_specific_term_does_not_match_unrelated_compound(self, taxonomy: Taxonomy) -> None:
        # "Parkplatzsanierung" contains "park" but no taxonomy term: no hit.
        assert taxonomy.match({"title": "Sanierung des Parkplatzes"}) == MatchResult.empty()

    def test_shipped_taxonomy_has_no_bare_generic_heads(self, taxonomy: Taxonomy) -> None:
        # Loading succeeded, so the guard passed; assert a generic head would be caught.
        assert "Park" not in {
            h.term for h in taxonomy.match({"title": "Solarpark Parkplatz Park"}).evidence
        }


# ------------------------------------------------------------------ umlaut folding


class TestUmlautFolding:
    def test_document_with_umlaut_matches_term_written_with_ae(self) -> None:
        tax = make_taxonomy({"heat": [{"term": "Waermeplanung", "weight": 0.9, "compound": True}]})
        assert tax.match({"title": "Kommunale Wärmeplanung"}).matched

    def test_document_with_ae_matches_term_written_with_umlaut(self) -> None:
        tax = make_taxonomy({"heat": [{"term": "Wärmeplanung", "weight": 0.9, "compound": True}]})
        assert tax.match({"title": "Kommunale Waermeplanung"}).matched

    def test_sharp_s_both_directions(self) -> None:
        tax = make_taxonomy({"t": [{"term": "Straßenbau", "weight": 0.8, "compound": True}]})
        assert tax.match({"title": "Strassenbau"}).matched
        tax2 = make_taxonomy({"t": [{"term": "Strassenbau", "weight": 0.8, "compound": True}]})
        assert tax2.match({"title": "Straßenbau"}).matched

    def test_hit_term_is_canonical_spelling_and_surface_is_folded(self, taxonomy: Taxonomy) -> None:
        result = taxonomy.match({"title": "Wärmeplanung"})
        (hit,) = [h for h in result.evidence if h.term == "Wärmeplan"]
        assert hit.surface == "waermeplanung"  # folded, includes the compound suffix


# ---------------------------------------------------------------- negative context


class TestNegativeContext:
    def test_ablehnung_suppresses_hit(self, taxonomy: Taxonomy) -> None:
        outcome = taxonomy.match_detailed({"title": "Ablehnung des Solarparks"})
        assert outcome.result == MatchResult.empty()
        assert [h.term for h in outcome.suppressed] == ["Solarpark"]

    @pytest.mark.parametrize(
        "text",
        [
            "Rückbau des Solarparks",
            "Aufhebung des Solarparks",
            "Rücknahme des Antrags Solarpark",
            "Es wird kein Solarpark errichtet",
            "Solarpark wird abgelehnt",
        ],
    )
    def test_each_negation_term_suppresses(self, taxonomy: Taxonomy, text: str) -> None:
        assert not taxonomy.match({"title": text}).matched

    def test_negation_outside_window_does_not_suppress(self) -> None:
        tax = make_taxonomy(
            {"pv": [{"term": "Solarpark", "weight": 0.9, "compound": True}]},
            negation_terms=("Ablehnung",),
            window=2,
        )
        assert not tax.match({"title": "Ablehnung des Solarparks"}).matched  # 1 token away
        assert tax.match({"title": "Ablehnung eins zwei drei Solarpark"}).matched

    def test_window_zero_disables_suppression(self) -> None:
        tax = make_taxonomy(
            {"pv": [{"term": "Solarpark", "weight": 0.9}]}, negation_terms=("kein",), window=0
        )
        assert tax.match({"title": "kein Solarpark"}).matched

    def test_suppression_is_per_hit(self, taxonomy: Taxonomy) -> None:
        text = "Ablehnung des Solarparks Nord. Zustimmung Windpark Süd"
        # Windpark is 4 tokens after "Solarparks"; "Ablehnung" is 7 tokens before it.
        result = taxonomy.match({"title": text})
        assert [h.term for h in result.evidence] == ["Windpark"]


# ---------------------------------------------------------------------------- scoring


class TestScoring:
    def test_title_outranks_summary(self, taxonomy: Taxonomy) -> None:
        in_title = taxonomy.match({"title": "Solarpark Musterdorf"})
        in_summary = taxonomy.match({"summary": "Solarpark Musterdorf"})
        in_body = taxonomy.match({"body": "Solarpark Musterdorf"})
        assert in_title.score > in_summary.score > in_body.score
        assert in_title.score == pytest.approx(0.95 * 1.0)
        assert in_summary.score == pytest.approx(0.95 * 0.6)
        assert in_body.score == pytest.approx(0.95 * 0.4)

    def test_breadth_bonus_arithmetic(self) -> None:
        tax = make_taxonomy(
            {
                "a": [{"term": "Alpha", "weight": 0.5}],
                "b": [{"term": "Beta", "weight": 0.3}],
                "c": [{"term": "Gamma", "weight": 0.2}],
            }
        )
        one = tax.match({"title": "Alpha"})
        two = tax.match({"title": "Alpha Beta"})
        three = tax.match({"title": "Alpha Beta Gamma"})
        assert one.score == pytest.approx(0.5)
        assert two.score == pytest.approx(0.5 + 0.05 * 1)
        assert three.score == pytest.approx(0.5 + 0.05 * 2)
        assert two.signal_type == "a"

    def test_same_type_repeated_gets_no_breadth_bonus(self) -> None:
        tax = make_taxonomy(
            {"a": [{"term": "Alpha", "weight": 0.5}, {"term": "Beta", "weight": 0.4}]}
        )
        assert tax.match({"title": "Alpha Beta Alpha"}).score == pytest.approx(0.5)

    def test_breadth_bonus_uses_effective_max_not_sum(self) -> None:
        tax = make_taxonomy(
            {"a": [{"term": "Alpha", "weight": 0.5}], "b": [{"term": "Beta", "weight": 0.9}]}
        )
        # max(0.5 * 1.0, 0.9 * 0.6) = 0.54; plus one extra type
        result = tax.match({"title": "Alpha", "summary": "Beta"})
        assert result.score == pytest.approx(0.54 + 0.05)
        assert result.signal_type == "b"

    def test_cap_applies(self) -> None:
        tax = make_taxonomy(
            {"a": [{"term": "Alpha", "weight": 1.0}], "b": [{"term": "Beta", "weight": 0.1}]},
            breadth_bonus=0.5,
            cap=0.8,
        )
        assert tax.match({"title": "Alpha Beta"}).score == pytest.approx(0.8)

    def test_score_always_within_unit_interval(self) -> None:
        tax = make_taxonomy(
            {
                "a": [{"term": "Alpha", "weight": 1.0}],
                "b": [{"term": "Beta", "weight": 1.0}],
                "c": [{"term": "Gamma", "weight": 1.0}],
            },
            breadth_bonus=1.0,
            cap=1.0,
        )
        result = tax.match({"title": "Alpha Beta Gamma"})
        assert 0.0 <= result.score <= 1.0
        assert result.score == pytest.approx(1.0)

    def test_tie_between_types_is_alphabetical(self) -> None:
        tax = make_taxonomy(
            {"zeta": [{"term": "Zulu", "weight": 0.5}], "alpha": [{"term": "Alfa", "weight": 0.5}]}
        )
        assert tax.match({"title": "Zulu Alfa"}).signal_type == "alpha"

    def test_should_emit_threshold(self, taxonomy: Taxonomy) -> None:
        assert taxonomy.emit_min_score == pytest.approx(0.35)
        strong = taxonomy.match({"title": "Solarpark"})
        weak = taxonomy.match({"body": "Durchführungsvertrag"})  # 0.45 * 0.4 = 0.18
        assert taxonomy.should_emit(strong)
        assert weak.matched and not taxonomy.should_emit(weak)
        assert not taxonomy.should_emit(MatchResult.empty())


# ---------------------------------------------------------------- explainability


class TestExplainability:
    def test_every_hit_is_fully_populated(self, taxonomy: Taxonomy) -> None:
        result = taxonomy.match(
            {
                "title": "Solarparkerweiterung",
                "summary": "Netzanschluss und Batteriespeicher",
                "body": "Der Bebauungsplan wird geändert.",
            }
        )
        assert result.evidence
        for hit in result.evidence:
            assert isinstance(hit, Hit)
            assert hit.term and hit.surface and hit.signal_type
            assert hit.field in {"title", "summary", "body"}
            assert hit.offset >= 0
            assert 0.0 <= hit.weight <= 1.0

    def test_offset_points_into_the_folded_field(self, taxonomy: Taxonomy) -> None:
        text = "Straße: Größe des Solarparks"
        result = taxonomy.match({"title": text})
        (hit,) = [h for h in result.evidence if h.term == "Solarpark"]
        folded = fold(text)
        assert folded[hit.offset : hit.offset + len(hit.surface)] == hit.surface

    def test_score_is_reconstructable_from_evidence(self, taxonomy: Taxonomy) -> None:
        result = taxonomy.match(
            {
                "title": "Windpark und Umspannwerk",
                "summary": "Batteriespeicher am Netzanschluss",
                "body": "Fernwärme, Glasfaser",
            }
        )
        rebuilt = taxonomy.result_from_evidence(result.evidence)
        assert rebuilt == result

        by_type: dict[str, float] = {}
        for hit in result.evidence:
            eff = hit.weight * taxonomy.field_weights[hit.field]
            by_type[hit.signal_type] = max(by_type.get(hit.signal_type, 0.0), eff)
        expected = min(
            max(by_type.values()) + taxonomy.breadth_bonus * (len(by_type) - 1), taxonomy.cap
        )
        assert result.score == pytest.approx(expected)

    def test_unknown_field_is_an_error_not_a_guess(self, taxonomy: Taxonomy) -> None:
        with pytest.raises(ConfigurationError, match="no field weight"):
            taxonomy.match({"headline": "Solarpark"})

    def test_hit_with_unknown_field_cannot_be_scored(self, taxonomy: Taxonomy) -> None:
        hit = Hit(term="x", surface="x", field="nope", offset=0, weight=0.5, signal_type="storage")
        with pytest.raises(ConfigurationError):
            taxonomy.result_from_evidence([hit])


# ------------------------------------------------------------- empty and determinism


class TestEmptyAndDeterminism:
    def test_document_matching_nothing_returns_empty(self, taxonomy: Taxonomy) -> None:
        result = taxonomy.match({"title": "Haushaltssatzung 2027", "body": "Die Sitzung beginnt."})
        assert result == MatchResult.empty()
        assert not result.matched
        assert result.score == 0.0
        assert result.signal_type is None
        assert result.evidence == ()

    def test_missing_none_and_blank_fields(self, taxonomy: Taxonomy) -> None:
        assert taxonomy.match({}) == MatchResult.empty()
        assert taxonomy.match({"title": None, "summary": "", "body": None}) == MatchResult.empty()

    def test_scores_are_deterministic_across_runs(self) -> None:
        document = {
            "title": "Windpark, Solarparkerweiterung und Umspannwerk",
            "summary": "Glasfaser, Batteriespeicher, Wärmeplanung",
            "body": "Straßenbau und Bebauungsplan " * 5,
        }
        scores: set[float] = set()
        dumps: set[str] = set()
        for _ in range(5):
            fresh = Taxonomy.load(TAXONOMY_PATH)  # recompiled each time
            result = fresh.match(document)
            scores.add(result.score)
            dumps.add(result.model_dump_json())
        assert len(scores) == 1
        assert len(dumps) == 1

    def test_evidence_order_is_stable_and_sorted(self, taxonomy: Taxonomy) -> None:
        result = taxonomy.match(
            {"body": "Windpark", "title": "Umspannwerk Solarpark", "summary": "Glasfaser"}
        )
        fields = [h.field for h in result.evidence]
        assert fields == sorted(fields, key=["title", "summary", "body"].index)
        title_offsets = [h.offset for h in result.evidence if h.field == "title"]
        assert title_offsets == sorted(title_offsets)


# ---------------------------------------------------------------- result contract


class TestMatchResultContract:
    def test_result_is_frozen_with_tuple_evidence(self, taxonomy: Taxonomy) -> None:
        result = taxonomy.match({"title": "Solarpark"})
        assert isinstance(result.evidence, tuple)
        with pytest.raises(ValidationError):
            result.score = 0.0  # type: ignore[misc]

    def test_score_is_clamped_by_model(self) -> None:
        with pytest.raises(ValidationError):
            MatchResult(score=1.5)
        with pytest.raises(ValidationError):
            MatchResult(score=-0.1)

    def test_extra_fields_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            MatchResult(score=0.5, bogus=1)  # type: ignore[call-arg]


# ------------------------------------------------------------ loading and the YAML


class TestLoading:
    def test_version_is_surfaced(self, taxonomy: Taxonomy) -> None:
        assert taxonomy.taxonomy_version == "2026.10.1"

    def test_all_eight_signal_types_present(self, taxonomy: Taxonomy) -> None:
        assert taxonomy.signal_types == ALL_SIGNAL_TYPES

    def test_configured_numbers(self, taxonomy: Taxonomy) -> None:
        assert taxonomy.field_weights == {"title": 1.0, "summary": 0.6, "body": 0.4}
        assert taxonomy.breadth_bonus == pytest.approx(0.05)
        assert taxonomy.cap == pytest.approx(1.0)
        assert taxonomy.emit_min_score == pytest.approx(0.35)

    @pytest.mark.parametrize(
        ("text", "signal_type"),
        [
            ("Aufstellung des Bebauungsplans Nr. 12", "land_use_plan"),
            ("Windenergieanlagen im Gemeindegebiet", "wind_energy"),
            ("Agri-Photovoltaik auf Ackerflächen", "pv_open_space"),
            ("Neuer Netzverknüpfungspunkt", "grid_connection"),
            ("Batteriegroßspeicher am Umspannwerk", "storage"),
            ("Kommunale Wärmeplanung beschlossen", "heat_planning"),
            ("Glasfaserausbau im Ortsteil", "digital_infra"),
            ("Tiefbauarbeiten Ortsumgehung", "transport_civils"),
        ],
    )
    def test_each_signal_type_is_reachable(
        self, taxonomy: Taxonomy, text: str, signal_type: str
    ) -> None:
        result = taxonomy.match({"title": text})
        assert signal_type in {h.signal_type for h in result.evidence}

    def test_duplicate_term_across_types_is_rejected(self) -> None:
        with pytest.raises(ConfigurationError, match="appears under both"):
            make_taxonomy(
                {
                    "a": [{"term": "Wärmeplan", "weight": 0.5}],
                    "b": [{"term": "Waermeplan", "weight": 0.5}],
                }
            )

    def test_invalid_weight_is_rejected(self) -> None:
        with pytest.raises(ConfigurationError):
            make_taxonomy({"a": [{"term": "Alpha", "weight": 1.5}]})

    def test_unknown_key_is_rejected(self) -> None:
        with pytest.raises(ConfigurationError):
            Taxonomy.from_mapping({"version": "x", "surprise": True})

    def test_missing_file_is_a_configuration_error(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigurationError):
            Taxonomy.load(tmp_path / "absent.yaml")

    def test_non_mapping_yaml_is_a_configuration_error(self, tmp_path: Path) -> None:
        path = tmp_path / "t.yaml"
        path.write_text("- just\n- a list\n", encoding="utf-8")
        with pytest.raises(ConfigurationError):
            Taxonomy.load(path)

    def test_empty_term_is_rejected(self) -> None:
        with pytest.raises(ConfigurationError):
            compile_term("  - ", compound=False)
