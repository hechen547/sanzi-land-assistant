from __future__ import annotations

import os
import shutil
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Literal

from ..models.photo import PhotoInfo
from ..models.task_result import FileOperationResult, OrganizeSummary
from .kml_exporter import export_empty_lands_kml, export_unmatched_photos_kml
from .kml_parser import read_land_kml_files
from .land_matcher import (
    build_land_records,
    match_photos_to_lands,
    project_web_mercator,
)
from .rename_service import unique_destination
from .report_service import (
    write_distance_match_list,
    write_land_classification_log,
    write_organize_report,
)


def organize_photos_by_land(
    img_list: list[dict[str, Any] | PhotoInfo],
    kml_paths: list[str],
    output_dir: str,
    copy_mode: bool = True,
    match_distance_m: float = 0,
    transfer_mode: Literal["fast", "compatible"] = "fast",
) -> OrganizeSummary:
    """按 KML 图斑归属复制或移动照片，并返回完整处理汇总。"""
    output = prepare_writable_output(output_dir)
    photos = [item if isinstance(item, PhotoInfo) else PhotoInfo.from_mapping(item) for item in img_list]
    parsed_lands = read_land_kml_files(kml_paths)
    lands = build_land_records(parsed_lands)

    valid_photos = [photo for photo in photos if _has_valid_gps(photo)]
    summary = OrganizeSummary(
        total_photos=len(photos),
        gps_photos=len(valid_photos),
        skipped_no_gps=len(photos) - len(valid_photos),
    )
    matches = match_photos_to_lands(valid_photos, lands, match_distance_m)
    unmatched_photos: list[PhotoInfo] = []
    if transfer_mode not in {"fast", "compatible"}:
        raise ValueError("整理速度模式无效")
    operation = shutil.copy2 if copy_mode else shutil.move
    transfer_plans: list[tuple[PhotoInfo, Path, FileOperationResult, Any]] = []
    reserved_destinations: set[str] = set()

    for match in matches:
        if not match.land:
            unmatched_photos.append(match.photo)
            summary.unmatched += 1
            summary.results.append(
                FileOperationResult(
                    source=match.photo.full_path,
                    land_name="未匹配",
                    distance_m=match.distance_m,
                    status="未复制（已记录到KML）",
                )
            )
            continue

        summary.matched += 1
        target_dir = output / match.land.folder
        result = FileOperationResult(
            source=match.photo.full_path,
            land_name=match.land.name,
            distance_m=match.distance_m,
        )
        target_dir.mkdir(parents=True, exist_ok=True)
        destination = _reserve_destination(
            target_dir,
            match.photo.filename,
            reserved_destinations,
        )
        transfer_plans.append((match.photo, destination, result, match.land))
        summary.results.append(result)

    # 文件通常从相机盘/U盘读取；2 路并发能提速，又不会像高并发那样拖慢机械盘。
    workers = min(2, max(1, len(transfer_plans))) if transfer_mode == "fast" else 1
    if workers == 1:
        transfer_results = [
            _transfer_photo(photo, destination, result, operation, copy_mode)
            for photo, destination, result, _land in transfer_plans
        ]
    else:
        with ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="photo-transfer",
        ) as executor:
            transfer_results = list(
                executor.map(
                    lambda plan: _transfer_photo(
                        plan[0],
                        plan[1],
                        plan[2],
                        operation,
                        copy_mode,
                    ),
                    transfer_plans,
                )
            )
    for plan, succeeded in zip(transfer_plans, transfer_results):
        if succeeded:
            summary.succeeded += 1
            plan[3].count += 1
        else:
            summary.failed += 1

    empty_lands = [land for land in lands if land.count == 0]
    summary.empty_lands = len(empty_lands)
    export_empty_lands_kml(empty_lands, output / "无照片图斑.kml")
    export_unmatched_photos_kml(unmatched_photos, output / "未匹配照片.kml")
    write_distance_match_list(
        summary.results,
        output / "图斑外距离匹配照片清单.txt",
    )
    write_land_classification_log(
        lands,
        summary.results,
        output / "图斑照片分类工作日志.txt",
        output_dir=output,
        match_distance_m=match_distance_m,
        copy_mode=copy_mode,
        total_photos=summary.total_photos,
        gps_photos=summary.gps_photos,
        unmatched=summary.unmatched,
        failed=summary.failed,
    )
    write_organize_report(summary.results, output / "整理结果.csv")
    return summary


def _reserve_destination(
    directory: Path,
    filename: str,
    reserved: set[str],
) -> Path:
    candidate = unique_destination(directory, filename)
    stem = Path(filename).stem
    suffix = Path(filename).suffix
    counter = 1
    while candidate.exists() or str(candidate).casefold() in reserved:
        candidate = directory / f"{stem}_{counter}{suffix}"
        counter += 1
    reserved.add(str(candidate).casefold())
    return candidate


def _transfer_photo(
    photo: PhotoInfo,
    destination: Path,
    result: FileOperationResult,
    operation,
    copy_mode: bool,
) -> bool:
    try:
        operation(photo.full_path, destination)
        result.destination = str(destination)
        result.status = "已复制" if copy_mode else "已移动"
        return True
    except Exception as exc:
        result.status = "失败"
        result.error = str(exc)
        return False


def analyze_photo_land_matches(
    img_list: list[dict[str, Any] | PhotoInfo],
    kml_paths: list[str],
    match_distance_m: float = 0,
):
    """只分析、不操作文件，供界面预览匹配结果。"""
    photos = [item if isinstance(item, PhotoInfo) else PhotoInfo.from_mapping(item) for item in img_list]
    lands = build_land_records(read_land_kml_files(kml_paths))
    valid_photos = [photo for photo in photos if _has_valid_gps(photo)]
    matches = match_photos_to_lands(valid_photos, lands, match_distance_m)
    return lands, matches, _dataset_bounds_gap_m(valid_photos, lands)


def _dataset_bounds_gap_m(photos: list[PhotoInfo], lands) -> float:
    """估算两组数据整体范围的间隔，用于识别选错村庄等明显错误。"""
    if not photos or not lands:
        return 0.0
    from shapely.geometry import box
    from shapely.ops import transform

    photo_bounds = box(
        min(photo.lon for photo in photos),
        min(photo.lat for photo in photos),
        max(photo.lon for photo in photos),
        max(photo.lat for photo in photos),
    )
    land_bounds = box(
        min(land.wgs_geom.bounds[0] for land in lands),
        min(land.wgs_geom.bounds[1] for land in lands),
        max(land.wgs_geom.bounds[2] for land in lands),
        max(land.wgs_geom.bounds[3] for land in lands),
    )
    return float(
        transform(project_web_mercator, photo_bounds).distance(
            transform(project_web_mercator, land_bounds)
        )
    )


def prepare_writable_output(output_dir: str | Path) -> Path:
    if not str(output_dir).strip():
        raise ValueError("请选择输出目录")
    output = Path(output_dir).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    probe = output / f".write_test_{uuid.uuid4().hex}.tmp"
    try:
        probe.write_bytes(b"ok")
        probe.unlink()
    except OSError as exc:
        raise PermissionError(f"输出目录不可写：{output}") from exc
    return output


def _has_valid_gps(photo: PhotoInfo) -> bool:
    return (
        photo.has_gps
        and photo.lat is not None
        and photo.lon is not None
        and -90 <= photo.lat <= 90
        and -180 <= photo.lon <= 180
        and os.path.isfile(photo.full_path)
    )
