"""German-aware, explainable keyword matching against a versioned taxonomy.

What this module does
---------------------
Documents and taxonomy terms are both passed through :func:`fold` (NFKC, casefold,
umlaut/ß folding), so ``Wärmeplanung`` and ``Waermeplanung`` are the same string on
either side. Each term is compiled once into a regular expression, run over every
field of a document, and every match is kept as a :class:`~iris_signal_harvester.domain
.models.Hit` carrying term, surface, field, offset, weight and signal type. The score is
a pure function of those hits and the taxonomy's configuration (see
:meth:`Taxonomy.result_from_evidence`), so a score can always be reconstructed from the
evidence stored beside it. A score that cannot be explained is a defect.

The compound-suffix allowance is a heuristic, NOT morphological analysis
-------------------------------------------------------------------------
German writes compounds as one word, so ``Solarpark`` has to be found inside
``Solarparkerweiterung``. For terms marked ``compound=True`` the pattern accepts a
bounded run of up to 20 letters (``[a-z]{0,20}``) after the term, then requires a word
boundary. That is all it does. It does not split compounds, does not know where a
morpheme ends, does not lemmatise, and will accept any 20-letter tail, meaningful or
not. It also anchors on the left at a word boundary only, so it finds a term at the
*start* of a compound but never in the middle or at the end
(``Freiflächensolarpark`` is not found by ``Solarpark``).

What actually prevents false positives is the content of the taxonomy, not this regex:
the taxonomy must never contain a bare generic head noun. The term is ``Solarpark``,
never ``Park``; ``Netzanschluss``, never ``Netz``; ``Batteriespeicher``, never
``Speicher``. With a generic head such as ``Park`` the suffix allowance would match
``Parkplatzsanierung``; with a specific term it cannot. :meth:`Taxonomy.from_mapping`
rejects the most common generic heads when they are given ``compound=True``, but that
list is a safety net and cannot be complete.

Other documented limits
-----------------------
* Negative context (``Ablehnung``, ``Rückbau`` ...) is a token window, not sentence
  parsing. It ignores punctuation, so a negation in the previous sentence can suppress
  a hit near a sentence boundary. Suppressed hits are not discarded silently: see
  :meth:`Taxonomy.match_detailed`.
* ``Hit.offset`` and ``Hit.surface`` refer to the *folded* field, because folding can
  change string length (``ß`` becomes ``ss``) and no offset map back to the original is
  kept.
* Scores are deterministic: no randomness, no dict/set iteration order in the result,
  stable tie-breaking. The same text and taxonomy version always give the same number.
"""

from __future__ import annotations

import re
import unicodedata
from bisect import bisect_left, bisect_right
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from iris_signal_harvester.domain.models import Hit, MatchResult
from iris_signal_harvester.errors import ConfigurationError

__all__ = [
    "GENERIC_HEAD_NOUNS",
    "MatchOutcome",
    "Taxonomy",
    "compile_term",
    "fold",
]

#: Maximum number of letters accepted after a compound=True term. A heuristic bound.
COMPOUND_SUFFIX_MAX = 20

#: Safety net, not a specification: folded generic head nouns that must never be a
#: ``compound=True`` term. Compared after folding and with hyphens/spaces removed.
GENERIC_HEAD_NOUNS: frozenset[str] = frozenset(
    {
        "anlage",
        "anlagen",
        "bau",
        "energie",
        "feld",
        "flaeche",
        "gebiet",
        "kraft",
        "leitung",
        "mast",
        "netz",
        "park",
        "plan",
        "platz",
        "punkt",
        "rad",
        "speicher",
        "station",
        "strasse",
        "strom",
        "verkehr",
        "waerme",
        "wasser",
        "weg",
        "werk",
        "zentrum",
    }
)

_UMLAUTS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})
_SEPARATOR_RUN = re.compile(r"[-\s]+")
_TOKEN = re.compile(r"\w+")


def fold(text: str) -> str:
    """Normalise text for matching: NFKC, casefold, then fold umlauts and ß.

    Applied to document text and to taxonomy terms alike. Folding is lossy on purpose
    (``Wärme`` and ``Waerme`` become one string) and idempotent. NFKC runs first so a
    decomposed ``a`` + U+0308 is composed into ``ä`` before the umlaut table sees it.
    """
    return unicodedata.normalize("NFKC", text).casefold().translate(_UMLAUTS)


def compile_term(term: str, compound: bool) -> re.Pattern[str]:
    """Compile one taxonomy term into a regex over *folded* text.

    ``term`` is folded here, so callers may pass it as written in the taxonomy. A
    hyphen-only separator is optional (``Freiflächen-PV``, ``Freiflächen PV`` and
    ``FreiflächenPV`` all match); a separator containing whitespace requires a
    separator. The match must start at a word boundary. With ``compound=True`` a
    bounded suffix of up to :data:`COMPOUND_SUFFIX_MAX` letters is allowed, then a word
    boundary is required. See the module docstring: this is a heuristic, not morphology.
    """
    folded = fold(term).strip()
    if not folded or _SEPARATOR_RUN.fullmatch(folded):
        raise ConfigurationError(f"taxonomy term {term!r} is empty after folding")
    if folded[0] in "-" or folded[-1] == "-":
        raise ConfigurationError(f"taxonomy term {term!r} must not start or end with a hyphen")

    pieces: list[str] = []
    cursor = 0
    for sep in _SEPARATOR_RUN.finditer(folded):
        pieces.append(re.escape(folded[cursor : sep.start()]))
        pieces.append(r"[-\s]+" if any(ch.isspace() for ch in sep.group()) else r"[-\s]?")
        cursor = sep.end()
    pieces.append(re.escape(folded[cursor:]))

    suffix = rf"[a-z]{{0,{COMPOUND_SUFFIX_MAX}}}" if compound else ""
    return re.compile(rf"(?<!\w){''.join(pieces)}{suffix}(?!\w)")


# --------------------------------------------------------------------------- config


class _TermSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    term: str = Field(min_length=1)
    weight: float = Field(ge=0.0, le=1.0)
    compound: bool = False


class _SignalTypeSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    terms: tuple[_TermSpec, ...] = Field(min_length=1)


class _NegationSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    window: int = Field(ge=0, le=50)
    terms: tuple[str, ...] = ()


class _TaxonomySpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = Field(min_length=1)
    field_weights: dict[str, float] = Field(min_length=1)
    breadth_bonus: float = Field(ge=0.0, le=1.0)
    cap: float = Field(gt=0.0, le=1.0)
    emit_min_score: float = Field(ge=0.0, le=1.0)
    negation: _NegationSpec
    signal_types: dict[str, _SignalTypeSpec] = Field(min_length=1)


@dataclass(frozen=True, slots=True)
class _CompiledTerm:
    term: str
    signal_type: str
    weight: float
    pattern: re.Pattern[str]


@dataclass(frozen=True, slots=True)
class MatchOutcome:
    """A :class:`MatchResult` plus the hits that negative context removed from it."""

    result: MatchResult
    suppressed: tuple[Hit, ...] = ()


# ------------------------------------------------------------------------- taxonomy


class Taxonomy:
    """A loaded, compiled taxonomy. Immutable after construction."""

    def __init__(self, spec: _TaxonomySpec) -> None:
        self.taxonomy_version: str = spec.version
        self.field_weights: dict[str, float] = dict(spec.field_weights)
        self.breadth_bonus: float = spec.breadth_bonus
        self.cap: float = spec.cap
        self.emit_min_score: float = spec.emit_min_score
        self.negation_window: int = spec.negation.window
        self._negation_terms: tuple[str, ...] = tuple(
            dict.fromkeys(fold(t).strip() for t in spec.negation.terms if fold(t).strip())
        )
        self._terms: tuple[_CompiledTerm, ...] = self._compile(spec)

    @staticmethod
    def _compile(spec: _TaxonomySpec) -> tuple[_CompiledTerm, ...]:
        compiled: list[_CompiledTerm] = []
        seen: dict[str, str] = {}
        for signal_type in sorted(spec.signal_types):
            for entry in spec.signal_types[signal_type].terms:
                key = fold(entry.term).strip()
                if entry.compound and _SEPARATOR_RUN.sub("", key) in GENERIC_HEAD_NOUNS:
                    raise ConfigurationError(
                        f"term {entry.term!r} ({signal_type}) is a bare generic head noun and "
                        "must not be compound=True; use a specific term such as 'Solarpark'"
                    )
                if key in seen:
                    raise ConfigurationError(
                        f"term {entry.term!r} appears under both {seen[key]!r} and "
                        f"{signal_type!r} (or twice in one type) after folding"
                    )
                seen[key] = signal_type
                compiled.append(
                    _CompiledTerm(
                        term=entry.term,
                        signal_type=signal_type,
                        weight=entry.weight,
                        pattern=compile_term(entry.term, entry.compound),
                    )
                )
        return tuple(compiled)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> Taxonomy:
        """Build a taxonomy from already-parsed data (what ``load`` does after YAML)."""
        try:
            spec = _TaxonomySpec.model_validate(dict(data))
        except ValidationError as exc:
            raise ConfigurationError(f"invalid taxonomy: {exc}") from exc
        return cls(spec)

    @classmethod
    def load(cls, path: Path | str) -> Taxonomy:
        """Read a taxonomy YAML file. Its ``version`` becomes ``taxonomy_version``."""
        source = Path(path)
        try:
            raw = yaml.safe_load(source.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise ConfigurationError(f"cannot read taxonomy {source}: {exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigurationError(f"taxonomy {source} must be a YAML mapping")
        return cls.from_mapping(raw)

    @property
    def signal_types(self) -> tuple[str, ...]:
        return tuple(sorted({t.signal_type for t in self._terms}))

    # ---------------------------------------------------------------- matching

    def match(self, fields: Mapping[str, str | None]) -> MatchResult:
        """Match a document given as ``{field_name: text}``. See :meth:`match_detailed`."""
        return self.match_detailed(fields).result

    def match_detailed(self, fields: Mapping[str, str | None]) -> MatchOutcome:
        """Match a document and also report hits removed by negative context.

        Field names must be keys of ``field_weights``; an unknown field raises
        ``ConfigurationError`` rather than being scored with a guessed weight.
        """
        unknown = sorted(set(fields) - set(self.field_weights))
        if unknown:
            raise ConfigurationError(f"no field weight configured for field(s): {unknown}")

        hits: list[Hit] = []
        suppressed: list[Hit] = []
        for field_name in self.field_weights:  # configured order: deterministic
            raw = fields.get(field_name)
            if not raw:
                continue
            text = fold(raw)
            token_spans = [(m.start(), m.group()) for m in _TOKEN.finditer(text)]
            starts = [s for s, _ in token_spans]
            tokens = [t for _, t in token_spans]
            for compiled in self._terms:
                for m in compiled.pattern.finditer(text):
                    hit = Hit(
                        term=compiled.term,
                        surface=m.group(),
                        field=field_name,
                        offset=m.start(),
                        weight=compiled.weight,
                        signal_type=compiled.signal_type,
                    )
                    if self._negated(tokens, starts, m.start(), m.end()):
                        suppressed.append(hit)
                    else:
                        hits.append(hit)

        order = {name: i for i, name in enumerate(self.field_weights)}

        def sort_key(h: Hit) -> tuple[int, int, str, str]:
            return (order[h.field], h.offset, h.signal_type, h.term)

        hits.sort(key=sort_key)
        suppressed.sort(key=sort_key)
        return MatchOutcome(self.result_from_evidence(hits), tuple(suppressed))

    def _negated(self, tokens: list[str], starts: list[int], start: int, end: int) -> bool:
        if not self._negation_terms or self.negation_window == 0:
            return False
        first = bisect_right(starts, start) - 1  # token containing the match start
        last = bisect_left(starts, end) - 1  # last token starting inside the match
        before = tokens[max(0, first - self.negation_window) : max(0, first)]
        after = tokens[last + 1 : last + 1 + self.negation_window]
        return any(tok.startswith(self._negation_terms) for tok in (*before, *after))

    # ----------------------------------------------------------------- scoring

    def effective_weight(self, hit: Hit) -> float:
        """``hit.weight * field_weight`` — the quantity the score takes the max of."""
        try:
            return hit.weight * self.field_weights[hit.field]
        except KeyError:
            raise ConfigurationError(
                f"no field weight configured for field {hit.field!r}"
            ) from None

    def result_from_evidence(self, evidence: Iterable[Hit]) -> MatchResult:
        """Compute the :class:`MatchResult` for a set of hits.

        This is the one scoring function. ``match`` uses it, and anyone holding stored
        evidence plus the same taxonomy version can call it to reproduce the score::

            score = min(max(hit.weight * field_weight) + breadth_bonus * (distinct_types - 1), cap)

        clamped to [0.0, 1.0]. ``signal_type`` is the type of the highest effective
        weight; ties go to the alphabetically first type.
        """
        hits = tuple(evidence)
        if not hits:
            return MatchResult.empty()

        best_by_type: dict[str, float] = {}
        for hit in hits:
            weight = self.effective_weight(hit)
            if weight > best_by_type.get(hit.signal_type, -1.0):
                best_by_type[hit.signal_type] = weight

        best = max(best_by_type.values())
        raw = best + self.breadth_bonus * (len(best_by_type) - 1)
        score = max(0.0, min(raw, self.cap, 1.0))
        primary = min(t for t, w in best_by_type.items() if w == best)
        return MatchResult(signal_type=primary, score=score, evidence=hits)

    def should_emit(self, result: MatchResult) -> bool:
        """Whether a result clears ``emit_min_score``. The caller decides what to do."""
        return result.matched and result.score >= self.emit_min_score
