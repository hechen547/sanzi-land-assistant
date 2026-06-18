from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class FileOperationResult:
    source: str
    destination: str = ""
    status: str = ""
    land_name: str = ""
    distance_m: float | None = None
    error: str = ""


@dataclass(slots=True)
class OrganizeSummary:
    total_photos: int = 0
    gps_photos: int = 0
    skipped_no_gps: int = 0
    matched: int = 0
    unmatched: int = 0
    succeeded: int = 0
    failed: int = 0
    empty_lands: int = 0
    results: list[FileOperationResult] = field(default_factory=list)

