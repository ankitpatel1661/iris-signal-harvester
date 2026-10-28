"""Gazetteer: AGS -> municipality geometry and administrative names.

``StaticGazetteer`` loads a small JSON file for the demo. Its polygons are crude
approximations, not VG250 boundaries; production would implement the same
``Gazetteer`` Protocol over a PostGIS VG250 table.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from shapely.geometry import Point, Polygon

from iris_signal_harvester.errors import ConfigurationError

__all__ = ["Gazetteer", "Municipality", "StaticGazetteer"]


@dataclass(frozen=True)
class Municipality:
    """One municipality, with its geometry in EPSG:25832."""

    ags: str
    name: str
    kreis: str | None
    bundesland: str | None
    centroid: tuple[float, float]
    polygon: Polygon


class Gazetteer(Protocol):
    """Looks a municipality up by its 8-digit AGS. ``None`` when unknown."""

    def get(self, ags: str) -> Municipality | None: ...


class StaticGazetteer:
    """In-memory gazetteer built from a JSON file (see ``config/gazetteer.json``)."""

    def __init__(self, municipalities: list[Municipality]) -> None:
        self._by_ags = {m.ags: m for m in municipalities}

    @classmethod
    def from_file(cls, path: Path) -> StaticGazetteer:
        try:
            doc: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigurationError(f"cannot read gazetteer {path}: {exc}") from exc
        if doc.get("crs") != "EPSG:25832":
            raise ConfigurationError(
                f"gazetteer {path} must declare crs EPSG:25832, got {doc.get('crs')!r}"
            )
        items: list[Municipality] = []
        for raw in doc.get("municipalities", []):
            try:
                ags = str(raw["ags"])
                polygon = Polygon(raw["polygon"])
                cx, cy = (float(v) for v in raw["centroid"])
                name = str(raw["name"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ConfigurationError(f"malformed gazetteer entry {raw!r}: {exc}") from exc
            if len(ags) != 8 or not ags.isdigit():
                raise ConfigurationError(f"gazetteer AGS must be 8 digits, got {ags!r}")
            if not polygon.is_valid or not polygon.covers(Point(cx, cy)):
                raise ConfigurationError(
                    f"gazetteer entry {ags}: polygon invalid or does not contain its centroid"
                )
            items.append(
                Municipality(
                    ags=ags,
                    name=name,
                    kreis=raw.get("kreis"),
                    bundesland=raw.get("bundesland"),
                    centroid=(cx, cy),
                    polygon=polygon,
                )
            )
        return cls(items)

    def get(self, ags: str) -> Municipality | None:
        return self._by_ags.get(ags)

    def __len__(self) -> int:
        return len(self._by_ags)
