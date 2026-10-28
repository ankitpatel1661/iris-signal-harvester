#!/usr/bin/env python3
# ruff: noqa: E501  (long prose strings and German vocabulary lists)
"""Seeded, deterministic generator for the SYNTHETIC demo corpus.

THIS CORPUS IS GENERATED. It is NOT real municipal data. Every paper title, id, reference
number, date and coordinate is invented by this script from a fixed seed. What is real is
only (a) the AGS codes (8-digit Amtlicher Gemeindeschluessel of real cities, needed so the
geo tier-4 lookup and the municipality containment check are exercised honestly) and
(b) the *structure* of the responses, which is modelled on what was observed on the live
OParl endpoint ``https://www.muenchen-transparent.de/oparl/v1.0`` on 2026-10-27/28
(see ``docs/evidence/``): ``links.next`` pagination with ``?id=`` cursors,
``pagination.{elementsPerPage,totalPages}``, ``Paper`` objects with ``reference``,
``paperType``, ``keyword``, ``created``/``modified``, the corrupt ``-0001-11-30T00:00:00+00:53``
sentinel, and bodies that declare no licence and often no AGS.

Output (all deterministic for a given ``--seed``; running twice yields identical bytes)::

    tests/fixtures/cassettes/corpus/corpus_main.json      replayable by FixtureAdapter
    tests/fixtures/cassettes/corpus/corpus_degraded.json  same papers, deliberately damaged
    docs/samples/corpus_gazetteer.json                    config/gazetteer.json + 5 more cities
    docs/samples/corpus_labels.json                       generator intent per paper (ground truth
                                                          of what was *meant*, not of what the
                                                          matcher decides)

The cassettes live in a sub-directory on purpose: ``FixtureAdapter.available_scenarios``
globs only the top level of ``tests/fixtures/cassettes``, and a test pins that set to the 13
hand-written scenarios.

Replay::

    IRIS_FIXTURE_DIR=tests/fixtures/cassettes/corpus IRIS_DB_DSN=... \\
      .venv/bin/iris-harvest harvest --source fixture --scenario corpus_main \\
      --gazetteer docs/samples/corpus_gazetteer.json

Requires the project virtualenv (``pyproj`` is used for the CRS arithmetic). Standard
library otherwise. Run: ``.venv/bin/python scripts/generate_corpus.py``.
"""

from __future__ import annotations

import argparse
import json
import math
import random
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from pyproj import Transformer

REPO = Path(__file__).resolve().parent.parent
DEFAULT_SEED = 20261028
GENERATOR_VERSION = 1
SYSTEM_URL = "https://oparl.example.org/oparl/v1.0"
SENTINEL = "-0001-11-30T00:00:00+00:53"
BERLIN = ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 10, 27, 12, 0, tzinfo=BERLIN)

_T_UTM32_TO_UTM33 = Transformer.from_crs("EPSG:25832", "EPSG:25833", always_xy=True)
_T_UTM32_TO_WGS84 = Transformer.from_crs("EPSG:25832", "EPSG:4326", always_xy=True)
_T_WGS84_TO_UTM32 = Transformer.from_crs("EPSG:4326", "EPSG:25832", always_xy=True)

# ---------------------------------------------------------------------------
# Bodies. AGS values are real; everything else about a body is invented. `gaz` False means
# the city is deliberately NOT in the gazetteer (AGS declared, but tier 4 unavailable).
# `ags` None means the body declares no AGS at all (25 of the 26 bodies on the probed
# endpoint declare none).  `lonlat` is only used to place the five added gazetteer cities.
# ---------------------------------------------------------------------------
BODIES: list[dict[str, Any]] = [
    {
        "city": "München",
        "ags": "09162000",
        "ars": "091620000000",
        "land": "Bayern",
        "w": 9,
        "lic": None,
        "ps": 25,
    },
    {
        "city": "Berlin",
        "ags": "11000000",
        "ars": "110000000000",
        "land": "Berlin",
        "w": 9,
        "lic": "dl",
        "ps": 25,
    },
    {
        "city": "Hamburg",
        "ags": "02000000",
        "ars": "020000000000",
        "land": "Hamburg",
        "w": 8,
        "lic": "dl",
        "ps": 20,
    },
    {
        "city": "Köln",
        "ags": "05315000",
        "ars": "053150000000",
        "land": "Nordrhein-Westfalen",
        "w": 8,
        "lic": None,
        "ps": 20,
    },
    {
        "city": "Frankfurt am Main",
        "ags": "06412000",
        "ars": "064120000000",
        "land": "Hessen",
        "w": 7,
        "lic": "cc",
        "ps": 20,
    },
    {
        "city": "Stuttgart",
        "ags": "08111000",
        "ars": "081110000000",
        "land": "Baden-Württemberg",
        "w": 7,
        "lic": None,
        "ps": 25,
    },
    {
        "city": "Dresden",
        "ags": "14612000",
        "ars": "146120000000",
        "land": "Sachsen",
        "w": 7,
        "lic": "cc",
        "ps": 20,
    },
    {
        "city": "Leipzig",
        "ags": "14713000",
        "ars": "147130000000",
        "land": "Sachsen",
        "w": 7,
        "lic": None,
        "ps": 20,
    },
    {
        "city": "Düsseldorf",
        "ags": "05111000",
        "ars": "051110000000",
        "land": "Nordrhein-Westfalen",
        "w": 6,
        "lic": "dl",
        "ps": 20,
    },
    {
        "city": "Hannover",
        "ags": "03241001",
        "ars": "032410001001",
        "land": "Niedersachsen",
        "w": 6,
        "lic": None,
        "ps": 15,
        "lonlat": (9.7320, 52.3759),
        "kreis": "Region Hannover",
        "gaz": True,
    },
    {
        "city": "Nürnberg",
        "ags": "09564000",
        "ars": "095640000000",
        "land": "Bayern",
        "w": 6,
        "lic": "cc",
        "ps": 15,
        "lonlat": (11.0767, 49.4521),
        "kreis": "Kreisfreie Stadt Nürnberg",
        "gaz": True,
    },
    {
        "city": "Bremen",
        "ags": "04011000",
        "ars": "040110000000",
        "land": "Bremen",
        "w": 5,
        "lic": None,
        "ps": 15,
        "lonlat": (8.8017, 53.0793),
        "kreis": "Bremen",
        "gaz": True,
    },
    {
        "city": "Essen",
        "ags": "05113000",
        "ars": "051130000000",
        "land": "Nordrhein-Westfalen",
        "w": 5,
        "lic": None,
        "ps": 15,
        "lonlat": (7.0116, 51.4556),
        "kreis": "Kreisfreie Stadt Essen",
        "gaz": True,
    },
    {
        "city": "Potsdam",
        "ags": "12054000",
        "ars": "120540000000",
        "land": "Brandenburg",
        "w": 4,
        "lic": "dl",
        "ps": 15,
        "lonlat": (13.0645, 52.3906),
        "kreis": "Kreisfreie Stadt Potsdam",
        "gaz": True,
    },
    {
        "city": "Mainz",
        "ags": "07315000",
        "ars": "073150000000",
        "land": "Rheinland-Pfalz",
        "w": 4,
        "lic": None,
        "ps": 15,
        "gaz": False,
    },
    # Bodies with no AGS at all, as on the probed endpoint (Bezirksausschuesse).
    {
        "city": "Bezirksausschuss Beispielviertel",
        "ags": None,
        "ars": None,
        "land": None,
        "w": 3,
        "lic": None,
        "ps": 10,
    },
    {
        "city": "Ortsbeirat Nord",
        "ags": None,
        "ars": None,
        "land": None,
        "w": 2,
        "lic": None,
        "ps": 10,
    },
]

LICENCES = {
    "cc": "https://creativecommons.org/licenses/by/4.0/",
    "dl": "https://www.govdata.de/dl-de/by-2-0",
}

# ---------------------------------------------------------------------------
# Vocabulary. Terms below come from config/taxonomy.yaml; phrasing is generic council German.
# All place names are generic or invented.
# ---------------------------------------------------------------------------
AREAS = [
    "Am Mühlbach",
    "Hohenfeld-Süd",
    "Gewerbegebiet Nord",
    "Kiesgrube Oberdorf",
    "Schlossfeld",
    "Lindenhof",
    "Am Fuchsberg",
    "Ostfeld",
    "Industriepark West",
    "Brückenfeld",
    "Altstadt-Ost",
    "Rosenau",
    "Unterfeld",
    "Am Sandberg",
    "Neue Mitte",
    "Wiesengrund",
    "Haidhof",
    "Bruchfeld",
    "Am Güterbahnhof",
    "Seeblick",
    "Im Hopfengarten",
    "Tannenhof",
    "Auf der Heide",
    "Kreuzfeld",
]
STREETS = [
    "Mühlenweg",
    "Bahnhofstraße",
    "Hauptstraße",
    "Lindenallee",
    "Ringstraße",
    "Gartenstraße",
    "Schulstraße",
    "Industriestraße",
    "Parkstraße",
    "Am Hafen",
    "Kirchgasse",
    "Talstraße",
    "Waldweg",
    "Feldstraße",
    "Poststraße",
    "Bergstraße",
    "Wiesenstraße",
    "Römerstraße",
]
CLUBS = [
    "Turnverein",
    "Schützenverein",
    "Sportclub",
    "Musikverein",
    "Heimatverein",
    "Kleingartenverein",
]
COMMITTEES = [
    "Bauausschuss",
    "Planungsausschuss",
    "Finanzausschuss",
    "Umweltausschuss",
    "Hauptausschuss",
    "Schulausschuss",
    "Verkehrsausschuss",
    "Stadtrat",
    "Bezirksausschuss",
]
PROJECT_NOUNS = ["Vorhaben", "Projekt", "Maßnahme"]

# signal type -> relative weight among positives
POSITIVE_MIX = [
    ("land_use_plan", 30),
    ("pv_open_space", 14),
    ("wind_energy", 10),
    ("grid_connection", 10),
    ("storage", 7),
    ("heat_planning", 11),
    ("digital_infra", 8),
    ("transport_civils", 10),
]


def _nn(rng: random.Random) -> int:
    return rng.randint(2, 189)


# Each template is a str.format string with {area} {street} {nn} {nn2} {club}.
POSITIVE_TEMPLATES: dict[str, list[str]] = {
    "land_use_plan": [
        "Aufstellungsbeschluss Bebauungsplan Nr. {nn} „{area}“",
        "Bebauungsplan Nr. {nn} „{area}“: Satzungsbeschluss",
        "Bebauungsplan Nr. {nn} „{area}“: Billigung des Entwurfs und Auslegungsbeschluss",
        "Teilfortschreibung Flächennutzungsplan: Änderungsverfahren {area}",
        "Flächennutzungsplan, {nn}. Änderung „{area}“: Feststellungsbeschluss",
        "Vorhaben- und Erschließungsplan „{area}“: Beteiligung der Träger öffentlicher Belange",
        "Städtebaulicher Vertrag zum Bebauungsplan Nr. {nn} „{area}“",
        "Durchführungsvertrag zum Bebauungsplan Nr. {nn}",
        "Bauleitplanung {area}: Frühzeitige Öffentlichkeitsbeteiligung",
        "Regionalplan: Stellungnahme der Stadt zur Fortschreibung",
        "Sondergebiet {area}: Änderung der Festsetzungen",
        "Bebauungsplan Nr. {nn}, {street}: Abwägung der eingegangenen Stellungnahmen",
        "Niederschrift {committee} vom {dmy}: Bebauungsplan Nr. {nn} „{area}“",
    ],
    "pv_open_space": [
        "Solarpark {area}: Aufstellungsbeschluss und Einleitung des Bauleitverfahrens",
        "Freiflächen-Photovoltaik {area}: Antrag der Betreiberin auf Bauleitplanung",
        "Freiflächen-PV an der Autobahn: Flächenkulisse für das Stadtgebiet",
        "Agri-Photovoltaik {area}: Pilotvorhaben mit Landwirtschaftsbetrieb",
        "Photovoltaik-Freiflächenanlage {area}: Stellungnahme der Verwaltung",
        "Solarfeld {area}: Gestattungsvertrag mit der Stadt",
        "PV-Park {area}: Beteiligung der Bürgerenergiegenossenschaft",
        "Solarkraftwerk {area}: Änderung des Bebauungsplans Nr. {nn}",
        "Antrag: Freiflächen-Solaranlage {area}",
        "Photovoltaikanlage auf dem ehemaligen Deponiegelände {area}",
    ],
    "wind_energy": [
        "Windpark {area}: Antrag auf immissionsschutzrechtliche Genehmigung",
        "Ausweisung von Windvorranggebieten im Stadtgebiet",
        "Repowering der Windenergieanlagen {area}",
        "Windenergie: Flächenbeitragswert und Windpotenzialfläche {area}",
        "Windkraftanlagen {area}: Stellungnahme zum Regionalplanentwurf",
        "Windeignungsgebiet {area}: Abstandsflächen und Artenschutz",
        "Windräder {area}: Informationsveranstaltung für die Bürgerschaft",
        "Windkonzentrationszone {area}: Teilflächennutzungsplan",
    ],
    "grid_connection": [
        "Netzanschluss {area}: Stellungnahme zum Netzverknüpfungspunkt",
        "Umspannwerk {area}: Neubau und Erweiterung",
        "Erdkabeltrasse {street}, {area}: Wegerechte",
        "Netzausbau Hochspannungsleitung {area}: Beteiligung der Stadt",
        "Trafostation {street}: Grundstücksverkauf an den Netzbetreiber",
        "Schaltanlage {area}: Antrag auf Befreiung von den Festsetzungen",
        "Einspeisezusage für die Freiflächenanlage {area}",
        "Umspannanlage {area}: Bauvoranfrage",
    ],
    "storage": [
        "Batteriespeicher {area}: Bauvoranfrage",
        "Batteriegroßspeicher im Gewerbegebiet {area}",
        "Energiespeicher {area}: Städtebaulicher Vertrag",
        "Stromspeicher am Umspannwerk {area}: Stellungnahme",
        "Power-to-Gas-Pilotanlage {area}",
        "Elektrolyseur {area}: Förderantrag und Flächenvergabe",
        "Wasserstoffspeicher {area}: Informationsvorlage",
        "Pumpspeicher-Machbarkeitsstudie {area}",
    ],
    "heat_planning": [
        "Kommunale Wärmeplanung: Zwischenbericht und Beteiligung",
        "Kommunale Wärmeplanung: Beschluss zur Bestandsanalyse",
        "Wärmenetz {area}: Machbarkeitsstudie",
        "Fernwärme-Ausbau {street}: Trassenplanung",
        "Nahwärme im Quartier {area}: Betreiberkonzept",
        "Geothermie-Erkundungsbohrung {area}",
        "Wärmeplan {area}: Eignungsgebiete",
        "Abwärmenutzung Rechenzentrum {area}: Einspeisung ins Wärmenetz",
        "Anschluss- und Benutzungszwang Fernwärme {area}: Satzungsänderung",
        "Wärmespeicher {area}: Standortentscheidung",
    ],
    "digital_infra": [
        "Glasfaser-Ausbau {area}: Kooperationsvereinbarung",
        "Breitbandförderung: Förderaufruf und Eigenanteil der Stadt",
        "FTTH-Erschließung {area}: Trassenplanung",
        "Mobilfunkmast {street}: Standortabstimmung",
        "Rechenzentrum {area}: Aufstellungsbeschluss",
        "Gigabit-Ausbau in den Ortsteilen: Sachstand",
        "Leerrohr-Mitverlegung {street}",
        "Mobilfunkstandort {area}: Stellungnahme der Verwaltung",
        "Data Center {area}: Anfrage zur Flächenverfügbarkeit",
    ],
    "transport_civils": [
        "Straßenausbau {street}: Vergabe der Bauleistungen",
        "Straßenbau {street}, Bauabschnitt {nn2}",
        "Brückenneubau {area}: Planfeststellung",
        "Radweg {street}: Lückenschluss",
        "Ortsumgehung {area}: Sachstandsbericht",
        "Kanalbau {street}: Auftragsvergabe",
        "Ladeinfrastruktur für Elektromobilität {street}",
        "Tiefbau {area}: Erschließungsstraße im Neubaugebiet",
        "Umgehungsstraße {area}: Trassenvarianten",
        "Brückensanierung {street}: Verkehrsführung während der Bauzeit",
    ],
}

ORDINARY_TEMPLATES: list[str] = [
    # budgets and finance
    "Haushaltssatzung {year} und Haushaltsplan",
    "Nachtragshaushalt {year}: Beschlussfassung",
    "Jahresabschluss {pyear} der Stadtkasse",
    "Gebührensatzung für die Benutzung der Friedhöfe: Änderung",
    "Festsetzung der Hebesätze für die Grund- und Gewerbesteuer",
    "Hundesteuersatzung: Neufassung",
    "Zuschuss für den {club} {area}",
    "Stellenplan {year}: Änderungen in der Verwaltung",
    "Überplanmäßige Ausgabe für die Unterhaltung städtischer Gebäude",
    # schools and childcare
    "Schulentwicklungsplanung Grundschulen: Fortschreibung",
    "Erweiterung der Mensa der Grundschule {area}",
    "Kindertagesstätte {area}: Betriebskostenzuschuss",
    "Platzvergabe in den städtischen Kindertagesstätten",
    "Schulsozialarbeit: Stellenplan und Finanzierung",
    "Ganztagsbetreuung an Grundschulen: Rechtsanspruch ab 2026",
    "Ferienprogramm für Kinder und Jugendliche {year}",
    # playgrounds, parks, sport, culture
    "Spielplatz {street}: Neugestaltung",
    "Sanierung der Spielgeräte auf dem Spielplatz {area}",
    "Bolzplatz {area}: Ersatz der Ballfangzäune",
    "Stadtbibliothek: Öffnungszeiten und Gebühren",
    "Sommerfest auf dem Marktplatz: Sondernutzungserlaubnis",
    "Städtepartnerschaft: Delegationsreise im Herbst",
    "Hallenbad: Preisgestaltung ab dem neuen Schuljahr",
    "Sportstättenentwicklungsplan: Zwischenstand",
    # administration and governance
    "Besetzung der Ausschüsse nach der Nachwahl",
    "Geschäftsordnung des Stadtrats: Änderung",
    "Sitzungskalender {year}",
    "Ehrenamtskarte: Einführung und Leistungen",
    "Einteilung der Wahlbezirke",
    "Marktsatzung für den Wochenmarkt",
    "Bürgerbeteiligung: Leitlinien",
    "Seniorenbeirat: Tätigkeitsbericht",
    "Niederschrift {committee} vom {dmy}",
    "Niederschrift der öffentlichen Sitzung des {committee} vom {dmy}",
    # social, health, safety
    "Betriebskosten der Flüchtlingsunterkunft {street}",
    "Freiwillige Feuerwehr: Beschaffung eines Löschfahrzeugs",
    "Rettungsdienst: Bedarfsplan",
    "Kulturförderung: Vergaberichtlinien",
    # environment and city services without taxonomy vocabulary
    "Baumschutzsatzung: Evaluation",
    "Müllgebührensatzung {year}",
    "Pflege der Stadtbäume im Bereich {street}",
    "Anwohnerparken {street}: Antrag auf Ausweitung",
    "Tempo-30-Zone {street}: Verkehrsberuhigung",
    "Friedhofsentwicklung: Urnengemeinschaftsanlagen",
    "Bürgeranfrage zur Straßenreinigung in der {street}",
    "Hochwasserschutz-Übung: Bericht der Feuerwehr",
    "Straßenbeleuchtung {street}: Umrüstung der Leuchtmittel",
    "Wohnungsbaugesellschaft: Bericht zur Mietentwicklung",
    "Antrag der Fraktion: Mehr Sitzgelegenheiten in der Fußgängerzone",
]

# Topically related to energy/infrastructure but containing NO taxonomy term: the pipeline is
# expected to (correctly, by its own rules) miss these. They keep the mix honest.
NEAR_MISS_TEMPLATES: list[str] = [
    "Energiebericht {pyear} für die städtischen Liegenschaften",
    "Klimaschutzkonzept: Fortschrittsbericht",
    "Stromverbrauch der Straßenbeleuchtung: Auswertung",
    "Dach-PV auf städtischen Schulgebäuden: Prüfauftrag",
    "Mobilfunk: Informationsabend für Anwohner {street}",
    "Verkehrsentwicklungsplan: Beteiligung der Ortsbeiräte",
    "Elektrobusse im Stadtverkehr: Machbarkeit",
    "Energieberatung für private Haushalte: Zuschussprogramm",
    "Anfrage zum Stand des Ausbaus der schnellen Internetanschlüsse",
    "Sanierung des Rathauses: Heizungsanlage",
]

# A negation term sits within four tokens of the hit (taxonomy `negation.window`), so the
# matcher suppresses the hit: the document is correctly below threshold for the matcher, and
# several of these are in fact land-use events (the documented Aufhebung limitation).
NEGATION_TEMPLATES: list[str] = [
    "Ablehnung des Antrags auf Errichtung eines Solarparks {area}",
    "Ablehnung des Solarparks {area}",
    "Aufhebung des Bebauungsplans Nr. {nn} „{area}“",
    "Rückbau der Windenergieanlage {area}: Sachstand",
    "Verzicht auf den Windpark {area}",
    "Antrag auf Batteriespeicher {area} abgelehnt",
]

# Ordinary papers that a German-compound heuristic will nevertheless match. Intended label:
# ordinary; expected outcome: a false positive. Shown, not hidden.
LEXICAL_TRAPS: list[str] = [
    "Straßenausbaubeitragssatzung: Neufassung",
    "Änderung der Radwegebenutzungspflicht {street}",
]

# Taxonomy term mentioned only in `keyword` (field weight 0.4): normally stays below 0.35.
KEYWORD_ONLY_TERMS = ["Bebauungsplan", "Flächennutzungsplan", "Netzausbau", "Fernwärme"]
GENERIC_KEYWORDS = [
    ["Haushalt"],
    ["Verkehr"],
    ["Bildung"],
    ["Soziales"],
    ["Umwelt"],
    ["Kultur"],
    ["Sicherheit"],
]

DOC_TYPES_BY_STYLE = {
    "stadt": [
        "Beschlussvorlage",
        "Stadtratsvorlage",
        "Antrag",
        "Stadtratsantrag",
        "Mitteilung",
        "Bericht",
        "Anfrage",
        "Niederschrift",
        "Vorlage",
    ],
    "ba": [
        "BA-Antrag",
        "BA-Antrag",
        "BA-Antrag",
        "Stadtratsvorlage",
        "Niederschrift",
        "Mitteilung",
    ],
}
UNMAPPED_TYPES = [
    "Petition",
    "Bürgerbegehren",
    "Einladung",
    "Tagesordnung",
    "Stellungnahme der Fachbehörde",
]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def iso(dt: datetime) -> str:
    return dt.astimezone(BERLIN).isoformat(timespec="seconds")


def rand_dt(rng: random.Random, start: datetime, end: datetime) -> datetime:
    span = int((end - start).total_seconds())
    return start + timedelta(seconds=rng.randint(0, span))


def disc_offset(rng: random.Random, radius: float) -> tuple[float, float]:
    r = radius * math.sqrt(rng.random())
    a = rng.random() * 2 * math.pi
    return (r * math.cos(a), r * math.sin(a))


def polygon_12(cx: int, cy: int, radius: int) -> list[list[int]]:
    pts = [
        [
            round(cx + radius * math.cos(2 * math.pi * i / 12)),
            round(cy + radius * math.sin(2 * math.pi * i / 12)),
        ]
        for i in range(12)
    ]
    pts.append(pts[0])
    return pts


def build_gazetteer() -> dict[str, Any]:
    """config/gazetteer.json (verbatim entries) plus five cities. Demo geometry, see _comment."""
    base = json.loads((REPO / "config" / "gazetteer.json").read_text(encoding="utf-8"))
    munis = list(base["municipalities"])
    for b in BODIES:
        if not b.get("gaz") or "lonlat" not in b:
            continue
        x, y = _T_WGS84_TO_UTM32.transform(*b["lonlat"])
        cx, cy = round(x), round(y)
        munis.append(
            {
                "ags": b["ags"],
                "name": b["city"],
                "kreis": b["kreis"],
                "bundesland": b["land"],
                "centroid": [cx, cy],
                "polygon": polygon_12(cx, cy, 8000),
            }
        )
    return {
        "_comment": (
            "DEMO DATA ONLY, used by the SYNTHETIC corpus. Entries 1-9 are copied verbatim from "
            "config/gazetteer.json; the 5 further cities (Hannover, Nürnberg, Bremen, Essen, "
            "Potsdam) were added by scripts/generate_corpus.py with centroids from approximate "
            "city-centre lon/lat and crude 8 km 12-gons. AGS codes are real; geometry is NOT the "
            "official VG250 boundary. Mainz (07315000) is deliberately absent so the "
            "'AGS declared but not in gazetteer' path is exercised."
        ),
        "crs": "EPSG:25832",
        "municipalities": munis,
    }


class Corpus:
    """All state of one generation run."""

    def __init__(self, seed: int) -> None:
        self.seed = seed
        self.rng = random.Random(seed)
        self.gaz = {m["ags"]: m for m in build_gazetteer()["municipalities"]}
        self.next_id = 1000 + self.rng.randint(0, 5000)
        self.bodies: list[dict[str, Any]] = []
        self.papers: list[dict[str, Any]] = []  # in page order per body
        self.labels: dict[str, dict[str, Any]] = {}

    # -- ids and text -------------------------------------------------------------------
    def paper_id(self) -> int:
        self.next_id += self.rng.randint(7, 311)
        return self.next_id

    def fill(self, template: str, body: dict[str, Any]) -> str:
        rng = self.rng
        d = rand_dt(rng, datetime(2025, 1, 1, tzinfo=BERLIN), NOW)
        return template.format(
            area=rng.choice(AREAS),
            street=rng.choice(STREETS),
            nn=_nn(rng),
            nn2=rng.randint(1, 6),
            club=rng.choice(CLUBS),
            committee=rng.choice(COMMITTEES),
            dmy=d.strftime("%d.%m.%Y"),
            year=rng.choice([2026, 2027]),
            pyear=rng.choice([2024, 2025]),
        )

    # -- one paper ----------------------------------------------------------------------
    def make_paper(self, body: dict[str, Any], category: str, signal: str | None) -> dict[str, Any]:
        rng = self.rng
        if category == "positive":
            assert signal
            title = self.fill(rng.choice(POSITIVE_TEMPLATES[signal]), body)
        elif category == "near_miss":
            title = self.fill(rng.choice(NEAR_MISS_TEMPLATES), body)
        elif category == "negation":
            title = self.fill(rng.choice(NEGATION_TEMPLATES), body)
        elif category == "lexical_trap":
            title = self.fill(rng.choice(LEXICAL_TRAPS), body)
        else:  # ordinary or keyword_mention
            title = self.fill(rng.choice(ORDINARY_TEMPLATES), body)
        title = " ".join(title.split())

        style = "ba" if body["ags"] is None else "stadt"
        ptype: str | None
        roll = rng.random()
        if "Niederschrift" in title:
            ptype = "Niederschrift"
        elif roll < 0.05:
            ptype = rng.choice(UNMAPPED_TYPES)
        elif roll < 0.08:
            ptype = None
        else:
            ptype = rng.choice(DOC_TYPES_BY_STYLE[style])
        if (
            ptype in ("Antrag", "Stadtratsantrag", "BA-Antrag")
            and rng.random() < 0.25
            and category == "positive"
        ):
            title = f"Antrag: {title}"
        if (
            ptype in ("Beschlussvorlage", "Vorlage")
            and rng.random() < 0.15
            and category == "positive"
        ):
            title = f"Beschlussvorlage: {title}"

        pid = self.paper_id()
        created = rand_dt(
            rng, datetime(2024, 1, 8, tzinfo=BERLIN), datetime(2026, 9, 20, tzinfo=BERLIN)
        )
        modified = created + timedelta(seconds=rng.randint(0, 40) * 86400 + rng.randint(0, 80000))
        if modified > NOW:
            modified = NOW - timedelta(hours=rng.randint(1, 90))
        meeting_day = created + timedelta(days=rng.randint(1, 60))
        if meeting_day > NOW:
            meeting_day = created
        leg = rng.choice(["20-26", "20-26", "14-20", "21-26"])
        letter = {"BA-Antrag": "B", "Antrag": "A", "Stadtratsantrag": "A"}.get(ptype or "", "V")
        paper_url = f"{SYSTEM_URL}/paper/{pid}"
        paper: dict[str, Any] = {
            "id": paper_url,
            "type": "https://schema.oparl.org/1.0/Paper",
            "body": body["id"],
            "name": title,
            "reference": f"{leg}/{letter}{rng.randint(1, 99999):05d}",
        }
        if ptype is not None:
            paper["paperType"] = ptype
        paper["date"] = meeting_day.date().isoformat()  # may be replaced by the imperfection pass
        paper.update(
            {
                "auxiliaryFile": [],
                "originatorPerson": [],
                "originatorOrganization": [],
                "underDirectionOf": [f"{SYSTEM_URL}/organization/referat/{rng.randint(1, 14)}"],
                "keyword": [],
                "web": f"https://ris.example.org/vorlagen/{pid}",
                "created": iso(created),
                "modified": iso(modified),
                "relatedPaper": [paper_url],
            }
        )
        if category == "keyword_mention":
            paper["keyword"] = [rng.choice(KEYWORD_ONLY_TERMS)]
        elif rng.random() < 0.12 and category != "positive":
            paper["keyword"] = list(rng.choice(GENERIC_KEYWORDS))
        self.labels[paper_url] = {
            "body": body["key"],
            "category": category,
            "intended_signal_type": signal,
            "imperfections": [],
        }
        return paper

    # -- the whole corpus ---------------------------------------------------------------
    def build(self, total: int) -> None:
        rng = self.rng
        wsum = sum(b["w"] for b in BODIES)
        for n, spec in enumerate(BODIES, start=1):
            body = dict(spec)
            body["key"] = n
            body["id"] = f"{SYSTEM_URL}/body/{n}"
            body["count"] = max(spec["ps"], round(total * spec["w"] / wsum))
            self.bodies.append(body)

        pos_types = [t for t, _ in POSITIVE_MIX]
        pos_weights = [w for _, w in POSITIVE_MIX]
        for body in self.bodies:
            body["papers"] = []
            for _ in range(body["count"]):
                roll = rng.random()
                if roll < 0.32:
                    category, signal = "positive", rng.choices(pos_types, pos_weights)[0]
                elif roll < 0.35:
                    category, signal = "negation", None
                elif roll < 0.39:
                    category, signal = "near_miss", None
                elif roll < 0.42:
                    category, signal = "keyword_mention", None
                elif roll < 0.425:
                    category, signal = "lexical_trap", None
                else:
                    category, signal = "ordinary", None
                body["papers"].append(self.make_paper(body, category, signal))

    # -- imperfection pass ----------------------------------------------------------------
    def all_papers(self) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        return [(b, p) for b in self.bodies for p in b["papers"]]

    def tag(self, paper: dict[str, Any], what: str) -> None:
        self.labels[paper["id"]]["imperfections"].append(what)

    def valid_point(self, body: dict[str, Any]) -> tuple[float, float]:
        m = self.gaz[body["ags"]]
        dx, dy = disc_offset(self.rng, 3500.0)
        return (round(m["centroid"][0] + dx, 2), round(m["centroid"][1] + dy, 2))

    def imperfections(self) -> None:
        rng = self.rng
        everyone = self.all_papers()
        positives = [(b, p) for b, p in everyone if self.labels[p["id"]]["category"] == "positive"]
        pos_in_gaz = [(b, p) for b, p in positives if b["ags"] in self.gaz]
        used: set[str] = set()

        def take(
            pool: list[tuple[dict[str, Any], dict[str, Any]]], k: int
        ) -> list[tuple[dict[str, Any], dict[str, Any]]]:
            free = [bp for bp in pool if bp[1]["id"] not in used]
            chosen = rng.sample(free, k)
            used.update(p["id"] for _, p in chosen)
            return chosen

        # geography ------------------------------------------------------------------
        # (a) lat/lon written into the projected field (one is the (0,0) placeholder)
        for i, (b, p) in enumerate(take(pos_in_gaz, 3)):
            m = self.gaz[b["ags"]]
            lon, lat = _T_UTM32_TO_WGS84.transform(*m["centroid"])
            if i == 2:
                loc = {"x": 0.0, "y": 0.0, "crs": "EPSG:25832", "description": "Platzhalter 0/0"}
            else:
                loc = {
                    "x": round(lon, 4),
                    "y": round(lat, 4),
                    "crs": "EPSG:25832",
                    "description": "Lage laut Antrag",
                }
            p["location"] = loc
            self.tag(p, "latlon_in_projected_field")
        # (b) finite but outside the CRS domain
        ood = [(1234567.0, 9876543.0), (-45210.5, 5300000.0), (2999999.0, 6000000.0)]
        for (_b, p), (x, y) in zip(take(pos_in_gaz, 3), ood, strict=True):
            p["location"] = {"x": x, "y": y, "crs": "EPSG:25832", "description": "Lage laut Antrag"}
            self.tag(p, "coordinate_out_of_crs_domain")
        # (c) zone-33 coordinates in Dresden / Leipzig: reprojected, not rejected
        east = [(b, p) for b, p in pos_in_gaz if b["ags"] in ("14612000", "14713000")]
        for b, p in take(east, 2):
            x, y = self.valid_point(b)
            x33, y33 = _T_UTM32_TO_UTM33.transform(x, y)
            p["location"] = {
                "x": round(x33, 2),
                "y": round(y33, 2),
                "crs": "EPSG:25833",
                "description": "Lage laut Antrag (UTM 33)",
            }
            self.tag(p, "zone33_coordinates")
        # (d) a minority of VALID source coordinates (tier 1). The probed endpoint serves none;
        #     other OParl servers do, and the tier must be exercised.
        valid_n = round(len(pos_in_gaz) * 0.06)
        for b, p in take(pos_in_gaz, valid_n):
            x, y = self.valid_point(b)
            p["location"] = {"x": x, "y": y, "crs": "EPSG:25832", "description": "Lage laut Antrag"}
            self.tag(p, "valid_source_coordinates")

        # dates -------------------------------------------------------------------------
        # two positives get a corrupt date: it must reject, not fall through
        for p, bad, tagname in (
            (take(positives, 1)[0][1], "31.02.2026", "date_unparseable"),
            (take(positives, 1)[0][1], "1850-03-01", "date_out_of_range"),
        ):
            p["date"] = bad
            self.tag(p, tagname)
        dated = [p for _, p in everyone if p["id"] not in used]
        n = len(everyone)
        for p in rng.sample(dated, round(n * 0.08)):
            p["date"] = None  # explicit null
            self.tag(p, "date_null")
        rest = [
            p
            for p in dated
            if p["date"] is not None and "date_null" not in self.labels[p["id"]]["imperfections"]
        ]
        for p in rng.sample(rest, round(n * 0.06)):
            del p["date"]  # key absent altogether, as on the probed endpoint
            self.tag(p, "date_absent")
        rest = [p for p in rest if "date" in p]
        for p in rng.sample(rest, round(n * 0.03)):
            p["date"] = datetime.fromisoformat(p["date"]).strftime("%d.%m.%Y")  # German TT.MM.JJJJ
            self.tag(p, "date_german_format")

        # modified: the real sentinel on a handful (and sometimes `created` too)
        for _, p in rng.sample(everyone, 6):
            p["modified"] = SENTINEL
            self.tag(p, "modified_sentinel")
            if rng.random() < 0.5:
                p["created"] = SENTINEL

        # missing title -> missing_required_field (any category)
        for _, p in take(positives, 2):
            del p["name"]
            self.tag(p, "title_absent")

        # licence --------------------------------------------------------------------
        for b in self.bodies:
            b["license"] = LICENCES.get(b["lic"]) if b["lic"] else None

    # -- pagination ---------------------------------------------------------------------
    def paginate(self, body: dict[str, Any]) -> list[list[Any]]:
        """Pages for a body: duplicates across pages and malformed items are injected here."""
        rng = self.rng
        papers = list(body["papers"])
        size = body["ps"]
        pages = [papers[i : i + size] for i in range(0, len(papers), size)]
        body["injected"] = {"duplicates": 0, "malformed": 0}
        for i in range(1, len(pages)):
            # unstable cursor: the last paper of the previous page shows up again
            if body["key"] in DUP_BODIES and i == 1:
                prev = pages[i - 1][-1]
                if isinstance(prev, dict):
                    pages[i] = [json.loads(json.dumps(prev))] + pages[i]
                    body["injected"]["duplicates"] += 1
                    self.tag_dup(prev)
        # one more duplicate that straddles two pages in the opposite direction
        if body["key"] in DUP_BODIES and len(pages) >= 3:
            dup = json.loads(json.dumps(pages[0][2]))
            pages[2] = pages[2] + [dup]
            body["injected"]["duplicates"] += 1
            self.tag_dup(dup)
        if body["key"] in MALFORMED_BODIES and pages:
            kind = MALFORMED_BODIES[body["key"]]
            if kind == "string_item":
                pages[0].insert(rng.randint(1, len(pages[0])), "paper-oparl-export-error")
            else:  # object without id
                orphan = {
                    "type": "https://schema.oparl.org/1.0/Paper",
                    "name": "Eintrag ohne Kennung",
                    "body": body["id"],
                }
                pages[0].insert(rng.randint(1, len(pages[0])), orphan)
            body["injected"]["malformed"] += 1
        return pages

    def tag_dup(self, paper: dict[str, Any]) -> None:
        imps = self.labels[paper["id"]]["imperfections"]
        imps.append("duplicate_across_pages")


# Bodies (by key) that receive duplicates / malformed items. Chosen to spread them out.
DUP_BODIES = {1, 2, 4, 5, 7, 9, 10, 12}
MALFORMED_BODIES = {3: "string_item", 6: "no_id", 11: "no_id"}


# ---------------------------------------------------------------------------
# cassette assembly
# ---------------------------------------------------------------------------
def body_object(b: dict[str, Any]) -> dict[str, Any]:
    obj: dict[str, Any] = {
        "id": b["id"],
        "type": "https://schema.oparl.org/1.0/Body",
        "system": SYSTEM_URL,
        "name": f"{b['city']} (synthetisch)",
        "shortName": b["city"],
        "paper": f"{b['id']}/list/paper",
        "web": "https://ris.example.org/",
        "created": "2020-01-01T00:00:00+01:00",
        "modified": "2026-09-30T08:15:00+02:00",
        "license": b["license"],
    }
    if b["ags"]:
        obj["ags"] = b["ags"]
        obj["rgs"] = b["ars"]
    return obj


def ok(url: str, doc: dict[str, Any]) -> dict[str, Any]:
    return {"request": {"method": "GET", "url": url}, "response": {"status": 200, "json": doc}}


def build_cassette(
    corpus: Corpus,
    *,
    scenario: str,
    description: str,
    meta: dict[str, Any],
    per_body_pages: dict[int, list[list[Any]]],
    overrides: dict[tuple[int, int], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    interactions: list[dict[str, Any]] = [
        ok(
            SYSTEM_URL,
            {
                "id": SYSTEM_URL,
                "type": "https://schema.oparl.org/1.0/System",
                "oparlVersion": "https://schema.oparl.org/1.0/",
                "otherOparlVersions": [],
                "body": f"{SYSTEM_URL}/list/body",
                "name": "Synthetisches Beispiel-Ratsinformationssystem (generiert)",
                "contactEmail": "ris-kontakt@example.org",
                "website": "https://ris.example.org",
            },
        )
    ]
    # body list: two pages of up to 10, linked with ?id= like every other collection
    body_objs = [body_object(b) for b in corpus.bodies]
    bsize = 10
    bpages = [body_objs[i : i + bsize] for i in range(0, len(body_objs), bsize)]
    base = f"{SYSTEM_URL}/list/body"
    for i, chunk in enumerate(bpages):
        url = base if i == 0 else f"{base}?id={chunk[0]['id'].rsplit('/', 1)[1]}"
        links: dict[str, str] = {"first": base}
        if i + 1 < len(bpages):
            links["next"] = f"{base}?id={bpages[i + 1][0]['id'].rsplit('/', 1)[1]}"
        interactions.append(
            ok(
                url,
                {
                    "data": chunk,
                    "pagination": {"elementsPerPage": bsize, "totalPages": len(bpages)},
                    "links": links,
                },
            )
        )
    for body in corpus.bodies:
        pages = per_body_pages[body["key"]]
        first = f"{body['id']}/list/paper"
        for i, chunk in enumerate(pages):

            def first_id(items: list[Any]) -> str:
                for it in items:
                    if isinstance(it, dict) and "id" in it:
                        return str(it["id"]).rsplit("/", 1)[1]
                return "0"

            url = first if i == 0 else f"{first}?id={first_id(chunk)}"
            links = {"first": first}
            if i + 1 < len(pages):
                links["next"] = f"{first}?id={first_id(pages[i + 1])}"
            override = (overrides or {}).get((body["key"], i))
            if override is not None:
                interactions.append(
                    {"request": {"method": "GET", "url": url}, "response": override}
                )
                continue
            interactions.append(
                ok(
                    url,
                    {
                        "data": chunk,
                        "pagination": {"elementsPerPage": body["ps"], "totalPages": len(pages)},
                        "links": links,
                    },
                )
            )
    return {
        "cassette_version": 1,
        "scenario": scenario,
        "description": description,
        "system_url": SYSTEM_URL,
        "_meta": meta,
        "interactions": interactions,
    }


SYNTHETIC_NOTICE = (
    "SYNTHETIC. Generated by scripts/generate_corpus.py from a fixed seed; reproducible; NOT real municipal "
    "data. Structure modelled on OParl responses observed on https://www.muenchen-transparent.de/oparl/v1.0 "
    "(probed 2026-10-27/28, see docs/evidence/). Paper titles, ids, references, dates and coordinates are "
    "invented. AGS codes are real; everything else is not."
)


# ---------------------------------------------------------------------------
# degradation: same papers, deliberately damaged
# ---------------------------------------------------------------------------
def degrade(
    corpus: Corpus, per_body_pages: dict[int, list[list[Any]]]
) -> tuple[dict[int, list[list[Any]]], dict[tuple[int, int], dict[str, Any]], Counter[str]]:
    rng = random.Random(corpus.seed + 1)
    pages = {int(k): v for k, v in json.loads(json.dumps(per_body_pages)).items()}
    applied: Counter[str] = Counter()
    positives = [
        (bk, pi, ii, it)
        for bk, ps in pages.items()
        for pi, pg in enumerate(ps)
        for ii, it in enumerate(pg)
        if isinstance(it, dict)
        and corpus.labels.get(it.get("id", ""), {}).get("category") == "positive"
        and corpus.bodies[bk - 1]["ags"] in corpus.gaz
        and "location" not in it
        and "name" in it
    ]
    rng.shuffle(positives)
    k = len(positives)
    plan = [
        ("latlon_in_projected_field", int(k * 0.17)),
        ("coordinate_out_of_crs_domain", int(k * 0.15)),
        ("non_finite_coordinate", int(k * 0.05)),
        ("unsupported_crs", int(k * 0.07)),
        ("date_unparseable", int(k * 0.07)),
        ("date_out_of_range", int(k * 0.05)),
        ("coordinate_in_wrong_city", int(k * 0.05)),
    ]
    cursor = 0
    for what, n in plan:
        for bk, _pi, _ii, it in positives[cursor : cursor + n]:
            body = corpus.bodies[bk - 1]
            m = corpus.gaz[body["ags"]]
            if what == "latlon_in_projected_field":
                lon, lat = _T_UTM32_TO_WGS84.transform(*m["centroid"])
                it["location"] = {
                    "x": round(lon, 4),
                    "y": round(lat, 4),
                    "crs": "EPSG:25832",
                    "description": "Lage laut Antrag",
                }
            elif what == "coordinate_out_of_crs_domain":
                it["location"] = {
                    "x": rng.choice([1234567.0, -45210.5, 2999999.0]),
                    "y": rng.choice([9876543.0, 5300000.0, 6000000.0]),
                    "crs": "EPSG:25832",
                    "description": "Lage laut Antrag",
                }
            elif what == "non_finite_coordinate":
                it["location"] = {
                    "x": "NaN",
                    "y": m["centroid"][1],
                    "crs": "EPSG:25832",
                    "description": "Lage laut Antrag",
                }
            elif what == "unsupported_crs":
                it["location"] = {
                    "x": 3690000.5,
                    "y": 5335000.5,
                    "crs": "EPSG:31468",
                    "description": "Lage laut Antrag (Gauß-Krüger)",
                }
            elif what == "date_unparseable":
                it["date"] = rng.choice(["31.02.2026", "demnächst", "2026-13-45"])
            elif what == "date_out_of_range":
                it["date"] = rng.choice(["1850-03-01", "1899-12-31"])
            elif what == "coordinate_in_wrong_city":
                # valid UTM32 point, but 200 km from the claimed municipality
                it["location"] = {
                    "x": round(m["centroid"][0] + 200000.0, 2),
                    "y": round(m["centroid"][1] - 90000.0, 2),
                    "crs": "EPSG:25832",
                    "description": "Lage laut Antrag",
                }
            applied[what] += 1
        cursor += n
    # transport damage: one body is down, one returns truncated JSON on its second page
    overrides: dict[tuple[int, int], dict[str, Any]] = {}
    down = 9  # body 9 = Düsseldorf: its first page always answers 500
    overrides[(down, 0)] = {
        "status": 500,
        "headers": {"Content-Type": "text/plain"},
        "text": "Internal Server Error",
    }
    applied["body_http_500"] += 1
    trunc_body = 6  # body 6 = Stuttgart: second page cut off mid-document
    if len(pages[trunc_body]) >= 2:
        full = {
            "data": pages[trunc_body][1],
            "pagination": {
                "elementsPerPage": corpus.bodies[trunc_body - 1]["ps"],
                "totalPages": len(pages[trunc_body]),
            },
            "links": {},
        }
        text = json.dumps(full, ensure_ascii=False)
        overrides[(trunc_body, 1)] = {
            "status": 200,
            "headers": {"Content-Type": "application/json"},
            "text": text[: len(text) // 2],
        }
        applied["page_truncated_json"] += 1
    return pages, overrides, applied


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def dump(path: Path, doc: Any) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(doc, ensure_ascii=False, indent=1) + "\n"
    path.write_text(text, encoding="utf-8")
    return len(text.encode("utf-8"))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--total", type=int, default=760, help="approximate number of distinct papers")
    ap.add_argument(
        "--cassette-dir", type=Path, default=REPO / "tests" / "fixtures" / "cassettes" / "corpus"
    )
    ap.add_argument("--samples-dir", type=Path, default=REPO / "docs" / "samples")
    args = ap.parse_args()

    corpus = Corpus(args.seed)
    corpus.build(args.total)
    corpus.imperfections()
    per_body = {b["key"]: corpus.paginate(b) for b in corpus.bodies}

    unique = sum(len(b["papers"]) for b in corpus.bodies)
    items = sum(len(pg) for pages in per_body.values() for pg in pages)
    cats = Counter(v["category"] for v in corpus.labels.values())
    imps = Counter(i for v in corpus.labels.values() for i in v["imperfections"])
    pages_total = sum(len(p) for p in per_body.values())
    meta: dict[str, Any] = {
        "synthetic": True,
        "notice": SYNTHETIC_NOTICE,
        "generator": "scripts/generate_corpus.py",
        "generator_version": GENERATOR_VERSION,
        "seed": args.seed,
        "reproduce": f".venv/bin/python scripts/generate_corpus.py --seed {args.seed}",
        "modelled_on": "https://www.muenchen-transparent.de/oparl/v1.0 (structure only; probed 2026-10-27/28)",
        "bodies": len(corpus.bodies),
        "bodies_with_real_ags": sum(1 for b in corpus.bodies if b["ags"]),
        "bundeslaender": sorted({b["land"] for b in corpus.bodies if b["land"]}),
        "distinct_papers": unique,
        "paper_items_served": items,
        "paper_pages": pages_total,
        "category_counts": dict(sorted(cats.items())),
        "imperfection_counts": dict(sorted(imps.items())),
    }
    main_cassette = build_cassette(
        corpus,
        scenario="corpus_main",
        description=(
            f"SYNTHETIC corpus: {unique} distinct papers over {len(corpus.bodies)} bodies "
            f"({meta['bodies_with_real_ags']} with a real AGS), {pages_total} paper pages. "
            "Generated, seeded, not real data."
        ),
        meta=meta,
        per_body_pages=per_body,
    )
    sizes = {"corpus_main.json": dump(args.cassette_dir / "corpus_main.json", main_cassette)}

    d_pages, d_over, d_applied = degrade(corpus, per_body)
    d_meta = dict(meta)
    d_meta.update(
        {
            "variant": "degraded",
            "notice": SYNTHETIC_NOTICE
            + " This variant is DELIBERATELY DAMAGED to demonstrate error visibility.",
            "degradation_applied": dict(sorted(d_applied.items())),
        }
    )
    d_cassette = build_cassette(
        corpus,
        scenario="corpus_degraded",
        description="SYNTHETIC, DELIBERATELY DEGRADED variant of corpus_main: injected bad coordinates and dates, "
        "one body answering HTTP 500 and one page of truncated JSON. Generated, seeded, not real data.",
        meta=d_meta,
        per_body_pages=d_pages,
        overrides=d_over,
    )
    sizes["corpus_degraded.json"] = dump(args.cassette_dir / "corpus_degraded.json", d_cassette)

    sizes["corpus_gazetteer.json"] = dump(
        args.samples_dir / "corpus_gazetteer.json", build_gazetteer()
    )
    sizes["corpus_labels.json"] = dump(
        args.samples_dir / "corpus_labels.json",
        {
            "_meta": {
                "synthetic": True,
                "notice": SYNTHETIC_NOTICE,
                "what_this_is": "The generator's INTENT for each paper (category, intended signal type, injected "
                "imperfections). It is not an evaluation of the matcher and not a gold standard: the categories "
                "were written by the same hand as the titles.",
                "seed": args.seed,
            },
            "labels": corpus.labels,
        },
    )
    print(
        json.dumps(
            {
                "meta": {k: v for k, v in meta.items() if k not in ("notice",)},
                "degradation": dict(d_applied),
                "bytes": sizes,
            },
            ensure_ascii=False,
            indent=1,
        )
    )


if __name__ == "__main__":
    main()
