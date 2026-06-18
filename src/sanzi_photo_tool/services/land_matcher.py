from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path

from pyproj import Transformer
from shapely.geometry import Point
from shapely.ops import transform

from ..models.land import LandRecord
from ..models.photo import PhotoInfo
from .kml_parser import ParsedLand

INVALID_FOLDER_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
WINDOWS_RESERVED_NAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


@dataclass(slots=True)
class PhotoMatch:
    photo: PhotoInfo
    land: LandRecord | None
    distance_m: float | None
    direct_hit: bool
    overlap_count: int = 0


def build_land_records(parsed_lands: list[ParsedLand]) -> list[LandRecord]:
    transformer = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
    used_folders: set[str] = set()
    records: list[LandRecord] = []
    for index, land in enumerate(parsed_lands, start=1):
        display_name = (land.landcode or land.name or f"图斑_{index}").strip()
        base_folder = safe_folder_name(display_name)
        folder = _unique_folder(base_folder, used_folders)
        records.append(
            LandRecord(
                name=display_name,
                folder=folder,
                wgs_geom=land.geometry,
                metric_geom=transform(transformer.transform, land.geometry),
                landcode=land.landcode,
                source_file=land.source_file,
            )
        )
    return records


def match_photos_to_lands(
    photos: list[PhotoInfo],
    lands: list[LandRecord],
    match_distance_m: float = 0,
) -> list[PhotoMatch]:
    if match_distance_m < 0:
        raise ValueError("匹配距离不能小于0")
    transformer = Transformer.from_crs("EPSG:4326", "EPSG:3857", always_xy=True)
    matches: list[PhotoMatch] = []
    for photo in photos:
        if not _valid_gps(photo):
            continue
        wgs_point = Point(photo.lon, photo.lat)
        metric_point = transform(transformer.transform, wgs_point)
        direct_lands = [
            land
            for land in lands
            if land.metric_geom.contains(metric_point) or land.metric_geom.touches(metric_point)
        ]
        if direct_lands:
            # 重叠图斑时选择面积最小者，通常是更具体的图斑，同时记录重叠数量。
            selected = min(direct_lands, key=lambda item: item.metric_geom.area)
            matches.append(PhotoMatch(photo, selected, 0.0, True, len(direct_lands)))
            continue
        if match_distance_m > 0 and lands:
            selected = min(lands, key=lambda item: item.metric_geom.distance(metric_point))
            distance = float(selected.metric_geom.distance(metric_point))
            if distance <= match_distance_m:
                matches.append(PhotoMatch(photo, selected, distance, False))
                continue
        matches.append(PhotoMatch(photo, None, None, False))
    return matches


def safe_folder_name(value: str, max_length: int = 80) -> str:
    cleaned = INVALID_FOLDER_CHARS.sub("_", value).strip().rstrip(". ")
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = (cleaned or "未命名图斑")[:max_length].rstrip(". ")
    if cleaned.casefold().upper() in WINDOWS_RESERVED_NAMES:
        cleaned = f"_{cleaned}"
    return cleaned


def _unique_folder(base: str, used: set[str]) -> str:
    candidate = base
    counter = 1
    while candidate.casefold() in used:
        suffix = f"_{counter}"
        candidate = f"{base[: max(1, 80 - len(suffix))]}{suffix}"
        counter += 1
    used.add(candidate.casefold())
    return candidate


def _valid_gps(photo: PhotoInfo) -> bool:
    return (
        photo.has_gps
        and photo.lat is not None
        and photo.lon is not None
        and math.isfinite(photo.lat)
        and math.isfinite(photo.lon)
        and -90 <= photo.lat <= 90
        and -180 <= photo.lon <= 180
        and Path(photo.full_path).is_file()
    )
