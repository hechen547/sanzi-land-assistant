from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path

from shapely.geometry import Point
from shapely.ops import transform, unary_union
from shapely.strtree import STRtree

from ..models.land import LandRecord
from ..models.photo import PhotoInfo
from .kml_parser import ParsedLand
from .task_control import TaskControl

INVALID_FOLDER_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
PLATFORM_LANDCODE_PATTERN = re.compile(r"\d{12,}")
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
WGS84_A = 6378137.0
WGS84_F = 1 / 298.257223563
WGS84_E2 = WGS84_F * (2 - WGS84_F)
WGS84_EP2 = WGS84_E2 / (1 - WGS84_E2)
UTM_SCALE = 0.9996


@dataclass(slots=True)
class PhotoMatch:
    photo: PhotoInfo
    land: LandRecord | None
    distance_m: float | None
    direct_hit: bool
    overlap_count: int = 0


def build_land_records(
    parsed_lands: list[ParsedLand],
    task_control: TaskControl | None = None,
    progress_range: tuple[float, float] = (0, 100),
) -> list[LandRecord]:
    used_folders: set[str] = set()
    records: list[LandRecord] = []
    grouped: list[tuple[str, list[ParsedLand]]] = []
    grouped_by_code: dict[str, list[ParsedLand]] = {}
    for index, land in enumerate(parsed_lands, start=1):
        code = normalized_landcode(land.landcode)
        if not code and is_platform_landcode(land.name):
            code = land.name.strip()
        if code:
            if code not in grouped_by_code:
                grouped_by_code[code] = []
                grouped.append((code, grouped_by_code[code]))
            grouped_by_code[code].append(land)
        else:
            grouped.append((f"__invalid_{index}", [land]))
    metric_epsg = estimate_local_utm_epsg(
        [land.geometry for land in parsed_lands]
    )
    projector = utm_projector(metric_epsg)
    for index, (group_key, source_lands) in enumerate(grouped, start=1):
        if task_control and (index == 1 or index % 16 == 0):
            task_control.report_range(
                *progress_range,
                index,
                len(grouped),
                f"正在准备图斑 {index}/{len(grouped)}",
            )
        first_land = source_lands[0]
        landcode = "" if group_key.startswith("__invalid_") else group_key
        display_name = (
            landcode or first_land.name or f"缺少编码图斑_{index}"
        ).strip()
        base_folder = safe_folder_name(
            landcode or f"编码异常_{first_land.name or index}"
        )
        folder = _unique_folder(base_folder, used_folders)
        merged_geometry = unary_union(
            [land.geometry for land in source_lands]
        )
        records.append(
            LandRecord(
                name=display_name,
                folder=folder,
                wgs_geom=merged_geometry,
                metric_geom=transform(projector, merged_geometry),
                metric_epsg=metric_epsg,
                landcode=landcode,
                source_file="；".join(
                    sorted({land.source_file for land in source_lands})
                ),
            )
        )
    return records


def match_photos_to_lands(
    photos: list[PhotoInfo],
    lands: list[LandRecord],
    match_distance_m: float = 0,
    task_control: TaskControl | None = None,
    progress_range: tuple[float, float] = (0, 100),
) -> list[PhotoMatch]:
    if match_distance_m < 0:
        raise ValueError("匹配距离不能小于0")
    geometries = [land.metric_geom for land in lands]
    tree = STRtree(geometries) if geometries else None
    projector = utm_projector(lands[0].metric_epsg) if lands else None
    matches: list[PhotoMatch] = []
    for photo_index, photo in enumerate(photos, start=1):
        if task_control and (photo_index == 1 or photo_index % 32 == 0):
            task_control.report_range(
                *progress_range,
                photo_index,
                len(photos),
                f"正在判断照片位置 {photo_index}/{len(photos)}",
            )
        if not _valid_gps(photo):
            continue
        metric_point = Point(*projector(photo.lon, photo.lat))
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


def find_empty_land_supplements(
    photos: list[PhotoInfo],
    lands: list[LandRecord],
    primary_matches: list[PhotoMatch],
    supplement_distance_m: float,
    task_control: TaskControl | None = None,
    progress_range: tuple[float, float] = (0, 100),
) -> dict[int, list[tuple[PhotoInfo, float]]]:
    """为第一轮无照片图斑查找附近照片，允许同一照片补充到多个图斑。"""
    if supplement_distance_m <= 0 or not photos or not lands:
        return {}
    occupied = {
        id(match.land)
        for match in primary_matches
        if match.land is not None
    }
    empty_indexes = [
        index for index, land in enumerate(lands) if id(land) not in occupied
    ]
    if not empty_indexes:
        return {}
    projector = utm_projector(lands[0].metric_epsg)
    metric_points = [
        Point(*projector(photo.lon, photo.lat))
        for photo in photos
        if _valid_gps(photo)
    ]
    valid_photos = [photo for photo in photos if _valid_gps(photo)]
    if not metric_points:
        return {}
    point_tree = STRtree(metric_points)
    supplements: dict[int, list[tuple[PhotoInfo, float]]] = {}
    for empty_position, land_index in enumerate(empty_indexes, start=1):
        if task_control and (
            empty_position == 1 or empty_position % 8 == 0
        ):
            task_control.report_range(
                *progress_range,
                empty_position,
                len(empty_indexes),
                f"正在为无照片图斑查找附近照片 "
                f"{empty_position}/{len(empty_indexes)}",
            )
        land = lands[land_index]
        candidate_indexes = point_tree.query(
            land.metric_geom.buffer(supplement_distance_m),
            predicate="intersects",
        ).tolist()
        candidates = []
        for photo_index in candidate_indexes:
            distance = float(
                land.metric_geom.distance(metric_points[int(photo_index)])
            )
            if distance <= supplement_distance_m:
                candidates.append((valid_photos[int(photo_index)], distance))
        if candidates:
            candidates.sort(
                key=lambda item: (
                    item[1],
                    item[0].filename.casefold(),
                    item[0].full_path.casefold(),
                )
            )
            supplements[land_index] = candidates
    return supplements


def estimate_local_utm_epsg(geometries: list[object]) -> int:
    """根据整批图斑中心选择当地 UTM 分区，返回米制 EPSG 编号。"""
    valid = [
        geometry
        for geometry in geometries
        if geometry is not None and not getattr(geometry, "is_empty", True)
    ]
    if not valid:
        return 3857
    center = unary_union(valid).centroid
    lon = max(-180.0, min(180.0, float(center.x)))
    lat = max(-90.0, min(90.0, float(center.y)))
    zone = max(1, min(60, int((lon + 180.0) // 6.0) + 1))
    return (32600 if lat >= 0 else 32700) + zone


def utm_projector(epsg: int):
    """返回纯 Python WGS84 → UTM 投影函数，避免 PROJ 在后台线程中崩溃。"""
    if 32601 <= epsg <= 32660:
        zone = epsg - 32600
        north = True
    elif 32701 <= epsg <= 32760:
        zone = epsg - 32700
        north = False
    else:
        return project_web_mercator
    central_meridian = math.radians((zone - 1) * 6 - 180 + 3)

    def project(x, y, z=None):
        if _is_coordinate_sequence(x):
            projected = [
                _project_utm_one(
                    float(lon),
                    float(lat),
                    central_meridian,
                    north,
                )
                for lon, lat in zip(x, y)
            ]
            xs = tuple(item[0] for item in projected)
            ys = tuple(item[1] for item in projected)
            return (xs, ys, z) if z is not None else (xs, ys)
        projected_x, projected_y = _project_utm_one(
            float(x),
            float(y),
            central_meridian,
            north,
        )
        return (
            (projected_x, projected_y, z)
            if z is not None
            else (projected_x, projected_y)
        )

    return project


def _project_utm_one(
    lon: float,
    lat: float,
    central_meridian: float,
    north: bool,
) -> tuple[float, float]:
    latitude = math.radians(max(-80.0, min(84.0, lat)))
    longitude = math.radians(lon)
    sin_lat = math.sin(latitude)
    cos_lat = math.cos(latitude)
    tan_lat = math.tan(latitude)
    n = WGS84_A / math.sqrt(1 - WGS84_E2 * sin_lat * sin_lat)
    t = tan_lat * tan_lat
    c = WGS84_EP2 * cos_lat * cos_lat
    a = cos_lat * (longitude - central_meridian)
    e4 = WGS84_E2 * WGS84_E2
    e6 = e4 * WGS84_E2
    meridian = WGS84_A * (
        (1 - WGS84_E2 / 4 - 3 * e4 / 64 - 5 * e6 / 256) * latitude
        - (3 * WGS84_E2 / 8 + 3 * e4 / 32 + 45 * e6 / 1024)
        * math.sin(2 * latitude)
        + (15 * e4 / 256 + 45 * e6 / 1024) * math.sin(4 * latitude)
        - (35 * e6 / 3072) * math.sin(6 * latitude)
    )
    easting = 500000 + UTM_SCALE * n * (
        a
        + (1 - t + c) * a**3 / 6
        + (5 - 18 * t + t * t + 72 * c - 58 * WGS84_EP2) * a**5 / 120
    )
    northing = UTM_SCALE * (
        meridian
        + n
        * tan_lat
        * (
            a * a / 2
            + (5 - t + 9 * c + 4 * c * c) * a**4 / 24
            + (61 - 58 * t + t * t + 600 * c - 330 * WGS84_EP2)
            * a**6
            / 720
        )
    )
    if not north:
        northing += 10_000_000
    return easting, northing


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


def normalized_landcode(value: str) -> str:
    return str(value or "").strip()


def is_platform_landcode(value: str) -> bool:
    return bool(PLATFORM_LANDCODE_PATTERN.fullmatch(str(value or "").strip()))


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
