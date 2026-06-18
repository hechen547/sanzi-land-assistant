from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class PhotoInfo:
    filename: str
    full_path: str
    has_gps: bool = False
    lat: float | None = None
    lon: float | None = None
    shot_time: datetime | None = None
    width: int | None = None
    height: int | None = None
    error: str = ""

    @property
    def path(self) -> Path:
        return Path(self.full_path)

    @property
    def shot_time_text(self) -> str:
        return self.shot_time.strftime("%Y-%m-%d %H:%M:%S") if self.shot_time else ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["shot_time"] = self.shot_time_text
        return data

    @classmethod
    def from_mapping(cls, data: dict[str, Any]) -> "PhotoInfo":
        shot_time = data.get("shot_time")
        if isinstance(shot_time, str) and shot_time:
            try:
                shot_time = datetime.fromisoformat(shot_time)
            except ValueError:
                shot_time = None
        return cls(
            filename=str(data.get("filename", "")),
            full_path=str(data.get("full_path", "")),
            has_gps=bool(data.get("has_gps", False)),
            lat=_optional_float(data.get("lat")),
            lon=_optional_float(data.get("lon")),
            shot_time=shot_time if isinstance(shot_time, datetime) else None,
            width=data.get("width"),
            height=data.get("height"),
            error=str(data.get("error", "")),
        )


def _optional_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None

