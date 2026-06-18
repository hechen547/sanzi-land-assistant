from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path
from typing import Any

from ..models.photo import PhotoInfo
from ..models.task_result import FileOperationResult, OrganizeSummary
from .kml_exporter import export_empty_lands_kml, export_unmatched_photos_kml
from .kml_parser import read_land_kml_files
from .land_matcher import build_land_records, match_photos_to_lands
from .rename_service import unique_destination
from .report_service import write_organize_report


def organize_photos_by_land(
    img_list: list[dict[str, Any] | PhotoInfo],
    kml_paths: list[str],
    output_dir: str,
    copy_mode: bool = True,
    match_distance_m: float = 0,
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
    operation = shutil.copy2 if copy_mode else shutil.move

    # 每个图斑都有对应目录，即使没有照片，也方便用户核对 KML 图斑清单。
    for land in lands:
        (output / land.folder).mkdir(parents=True, exist_ok=True)

    for match in matches:
        target_dir = output / (match.land.folder if match.land else "未匹配图斑")
        if match.land:
            match.land.count += 1
            summary.matched += 1
        else:
            unmatched_photos.append(match.photo)
            summary.unmatched += 1
        result = FileOperationResult(
            source=match.photo.full_path,
            land_name=match.land.name if match.land else "未匹配图斑",
            distance_m=match.distance_m,
        )
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            destination = unique_destination(target_dir, match.photo.filename)
            operation(match.photo.full_path, destination)
            result.destination = str(destination)
            result.status = "已复制" if copy_mode else "已移动"
            summary.succeeded += 1
        except Exception as exc:
            result.status = "失败"
            result.error = str(exc)
            summary.failed += 1
        summary.results.append(result)

    empty_lands = [land for land in lands if land.count == 0]
    summary.empty_lands = len(empty_lands)
    export_empty_lands_kml(empty_lands, output / "无照片图斑.kml")
    export_unmatched_photos_kml(unmatched_photos, output / "未匹配照片.kml")
    write_organize_report(summary.results, output / "整理结果.csv")
    return summary


def analyze_photo_land_matches(
    img_list: list[dict[str, Any] | PhotoInfo],
    kml_paths: list[str],
    match_distance_m: float = 0,
):
    """只分析、不操作文件，供界面预览匹配结果。"""
    photos = [item if isinstance(item, PhotoInfo) else PhotoInfo.from_mapping(item) for item in img_list]
    lands = build_land_records(read_land_kml_files(kml_paths))
    valid_photos = [photo for photo in photos if _has_valid_gps(photo)]
    return lands, match_photos_to_lands(valid_photos, lands, match_distance_m)


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
