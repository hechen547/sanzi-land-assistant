from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class LandRecord:
    name: str
    folder: str
    wgs_geom: Any
    metric_geom: Any
    metric_epsg: int = 3857
    landcode: str = ""
    source_file: str = ""
    count: int = 0
