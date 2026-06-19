from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path

from shapely.geometry import Point
from shapely.ops import transform
from shapely.strtree import STRtree

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
WEB_MERCATOR_RADIUS = 6378137.0
WEB_MERCATOR_MAX_LAT = 85.0511287798066


@dataclass(slots=True)
class PhotoMatch:
    photo: PhotoInfo
    land: LandRecord | None
    distance_m: float | None
    direct_hit: bool
    overlap_count: int = 0


def build_land_records(parsed_lands: list[ParsedLand]) -> list[LandRecord]:
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
                metric_geom=transform(project_web_mercator, land.geometry),
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
    geometries = [land.metric_geom for land in lands]
    tree = STRtree(geometries) if geometries else None
    matches: list[PhotoMatch] = []
    for photo in photos:
        if not _valid_gps(photo):
            continue
        wgs_point = Point(photo.lon, photo.lat)
        metric_point = transform(project_web_mercator, wgs_point)
        direct_indices = (
            tree.query(metric_point, predicate="intersects").tolist()
            if tree is not None
            else []
        )
        direct_lands = [lands[index] for index in direct_indices]
        if direct_lands:
            # 重叠图斑时选择面积最小者，通常是更具体的图斑，同时记录重叠数量。
            selected = min(direct_lands, key=lambda item: item.metric_geom.area)
            matches.append(PhotoMatch(photo, selected, 0.0, True, len(direct_lands)))
            continue
        if match_distance_m > 0 and tree is not None:
            nearest_indices, nearest_distances = tree.query_nearest(
                metric_point,
                max_distance=match_distance_m,
                return_distance=True,
                all_matches=True,
            )
            if len(nearest_indices):
                best_position = min(
                    range(len(nearest_indices)),
                    key=lambda index: (
                        float(nearest_distances[index]),
                        geometries[int(nearest_indices[index])].area,
                    ),
                )
                selected = lands[int(nearest_indices[best_position])]
                distance = float(nearest_distances[best_position])
            else:
                selected = None
                distance = float("inf")
            if distance <= match_distance_m:
                matches.append(PhotoMatch(photo, selected, distance, False))
                continue
        matches.append(PhotoMatch(photo, None, None, False))
    return matches


def project_web_mercator(x, y, z=None):
    """纯 Python 的 EPSG:4326 → EPSG:3857，避免后台线程触发 PROJ DLL 崩溃。"""
    if _is_coordinate_sequence(x):
        projected = [_project_one(float(lon), float(lat)) for lon, lat in zip(x, y)]
        xs = tuple(item[0] for item in projected)
        ys = tuple(item[1] for item in projected)
        return (xs, ys, z) if z is not None else (xs, ys)
    projected_x, projected_y = _project_one(float(x), float(y))
    return (projected_x, projected_y, z) if z is not None else (projected_x, projected_y)


def _project_one(lon: float, lat: float) -> tuple[float, float]:
    latitude = max(-WEB_MERCATOR_MAX_LAT, min(WEB_MERCATOR_MAX_LAT, lat))
    x = WEB_MERCATOR_RADIUS * math.radians(lon)
    y = WEB_MERCATOR_RADIUS * math.log(
        math.tan(math.pi / 4 + math.radians(latitude) / 2)
    )
    return x, y


def _is_coordinate_sequence(value: object) -> bool:
    return not isinstance(value, (int, float))


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
