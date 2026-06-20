from __future__ import annotations

import os
import shutil
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from ..models.photo import PhotoInfo
from ..models.task_result import FileOperationResult, OrganizeSummary
from .kml_exporter import export_empty_lands_kml, export_unmatched_photos_kml
from .kml_parser import read_land_kml_files
from .land_matcher import (
    build_land_records,
    find_empty_land_supplements,
    match_photos_to_lands,
    project_web_mercator,
)
from .rename_service import unique_destination
from .report_service import (
    write_distance_match_list,
    write_invalid_land_list,
    write_land_classification_log,
    write_organize_report,
)
from .task_control import TaskControl


@dataclass(slots=True)
class AnalysisPreviewRow:
    filename: str
    land_name: str
    distance_m: float | None
    direct_hit: bool
    matched: bool


@dataclass(slots=True)
class AnalysisPreview:
    land_count: int
    matched: int
    unmatched: int
    gps_photos: int
    empty_lands: int
    supplemented_lands: int
    supplement_copies: int
    dataset_gap_m: float
    rows: list[AnalysisPreviewRow]


def organize_photos_by_land(
    img_list: list[dict[str, Any] | PhotoInfo],
    kml_paths: list[str],
    output_dir: str,
    copy_mode: bool = True,
    match_distance_m: float = 0,
    transfer_mode: Literal["fast", "compatible"] = "fast",
    supplement_empty_lands: bool = False,
    supplement_distance_m: float = 20,
    task_control: TaskControl | None = None,
) -> OrganizeSummary:
    """按 KML 图斑归属复制或移动照片，并返回完整处理汇总。"""
    _report(task_control, 1, "正在检查保存位置…")
    output = prepare_writable_output(output_dir)
    photos = [item if isinstance(item, PhotoInfo) else PhotoInfo.from_mapping(item) for item in img_list]
    _report(task_control, 3, "正在读取图斑文件…")
    parsed_lands = read_land_kml_files(kml_paths)
    lands = build_land_records(parsed_lands, task_control, (4, 12))

    valid_photos = [photo for photo in photos if _has_valid_gps(photo)]
    summary = OrganizeSummary(
        total_photos=len(photos),
        gps_photos=len(valid_photos),
        skipped_no_gps=len(photos) - len(valid_photos),
    )
    # KML中的全部有效编码都预先创建目录。空目录用于上传前完整核对，
    # 重复编码已在 build_land_records 中合并为同一个目录。
    for land_index, land in enumerate(lands, start=1):
        if task_control and (land_index == 1 or land_index % 32 == 0):
            task_control.report_range(
                12,
                14,
                land_index,
                len(lands),
                f"正在创建完整图斑目录 {land_index}/{len(lands)}",
            )
        if land.landcode:
            (output / land.folder).mkdir(parents=True, exist_ok=True)
    matches = match_photos_to_lands(
        valid_photos,
        lands,
        match_distance_m,
        task_control,
        (14, 32),
    )
    unmatched_photos: list[PhotoInfo] = []
    if transfer_mode not in {"fast", "compatible"}:
        raise ValueError("整理速度模式无效")
    operation = shutil.copy2 if copy_mode else shutil.move
    transfer_plans: list[tuple[PhotoInfo, Path, FileOperationResult, Any]] = []
    reserved_destinations: set[str] = set()

    for match_index, match in enumerate(matches, start=1):
        if task_control and (match_index == 1 or match_index % 64 == 0):
            task_control.report_range(
                32,
                36,
                match_index,
                len(matches),
                f"正在准备整理任务 {match_index}/{len(matches)}",
            )
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
    transfer_results = _run_transfer_plans(
        transfer_plans,
        workers,
        lambda plan: _transfer_photo(
            plan[0],
            plan[1],
            plan[2],
            operation,
            copy_mode,
        ),
        task_control,
        (36, 80),
        "正在整理照片",
        "photo-transfer",
    )
    for plan, succeeded in zip(transfer_plans, transfer_results):
        if succeeded:
            summary.succeeded += 1
            plan[3].count += 1
        else:
            summary.failed += 1

    if supplement_empty_lands and copy_mode and supplement_distance_m > 0:
        supplements = find_empty_land_supplements(
            valid_photos,
            lands,
            matches,
            supplement_distance_m,
            task_control,
            (80, 86),
        )
        supplement_plans: list[
            tuple[PhotoInfo, Path, FileOperationResult, Any]
        ] = []
        supplement_items = list(supplements.items())
        for supplement_index, (land_index, candidates) in enumerate(
            supplement_items,
            start=1,
        ):
            if task_control:
                task_control.report_range(
                    86,
                    88,
                    supplement_index,
                    len(supplement_items),
                    f"正在准备补充照片 {supplement_index}/{len(supplement_items)}",
                )
            land = lands[land_index]
            if land.count > 0:
                continue
            target_dir = output / land.folder
            target_dir.mkdir(parents=True, exist_ok=True)
            for photo, distance in candidates:
                result = FileOperationResult(
                    source=photo.full_path,
                    land_name=land.name,
                    distance_m=distance,
                    status="等待补充复制",
                )
                destination = _reserve_destination(
                    target_dir,
                    photo.filename,
                    reserved_destinations,
                )
                supplement_plans.append((photo, destination, result, land))
                summary.results.append(result)
        supplement_results = _run_transfer_plans(
            supplement_plans,
            workers,
            lambda plan: _transfer_supplement(
                plan[0],
                plan[1],
                plan[2],
            ),
            task_control,
            (88, 96),
            "正在补充空图斑照片",
            "photo-supplement",
        )
        for plan, succeeded in zip(supplement_plans, supplement_results):
            if succeeded:
                summary.succeeded += 1
                summary.supplemented += 1
                plan[3].count += 1
            else:
                summary.failed += 1

    empty_lands = [land for land in lands if land.count == 0]
    summary.empty_lands = len(empty_lands)
    _report(task_control, 97, "正在生成整理报告…")
    export_empty_lands_kml(empty_lands, output / "无照片图斑.kml")
    export_unmatched_photos_kml(unmatched_photos, output / "未匹配照片.kml")
    write_distance_match_list(
        summary.results,
        output / "图斑外距离匹配照片清单.txt",
    )
    write_invalid_land_list(lands, output / "编码异常图斑清单.txt")
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
        supplemented=summary.supplemented,
        supplement_distance_m=(
            supplement_distance_m
            if supplement_empty_lands and copy_mode
            else 0
        ),
    )
    write_organize_report(summary.results, output / "整理结果.csv")
    _report(task_control, 100, "整理完成")
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


def _transfer_supplement(
    photo: PhotoInfo,
    destination: Path,
    result: FileOperationResult,
) -> bool:
    try:
        shutil.copy2(photo.full_path, destination)
        result.destination = str(destination)
        result.status = "已补充复制"
        return True
    except Exception as exc:
        result.status = "补充失败"
        result.error = str(exc)
        return False


def analyze_photo_land_matches(
    img_list: list[dict[str, Any] | PhotoInfo],
    kml_paths: list[str],
    match_distance_m: float = 0,
    supplement_empty_lands: bool = False,
    supplement_distance_m: float = 20,
    task_control: TaskControl | None = None,
):
    """只分析、不操作文件，供界面预览匹配结果。"""
    photos = [item if isinstance(item, PhotoInfo) else PhotoInfo.from_mapping(item) for item in img_list]
    _report(task_control, 2, "正在读取图斑文件…")
    lands = build_land_records(
        read_land_kml_files(kml_paths),
        task_control,
        (3, 15),
    )
    valid_photos = [photo for photo in photos if _has_valid_gps(photo)]
    matches = match_photos_to_lands(
        valid_photos,
        lands,
        match_distance_m,
        task_control,
        (15, 78),
    )
    supplements = (
        find_empty_land_supplements(
            valid_photos,
            lands,
            matches,
            supplement_distance_m,
            task_control,
            (78, 92),
        )
        if supplement_empty_lands
        else {}
    )
    return lands, matches, _dataset_bounds_gap_m(valid_photos, lands), supplements


def analyze_photo_land_preview(
    img_list: list[dict[str, Any] | PhotoInfo],
    kml_paths: list[str],
    match_distance_m: float = 0,
    supplement_empty_lands: bool = False,
    supplement_distance_m: float = 20,
    task_control: TaskControl | None = None,
) -> AnalysisPreview:
    """生成不包含 Shapely/PROJ 对象的界面预览，避免原生对象跨线程。"""
    lands, matches, dataset_gap_m, supplements = analyze_photo_land_matches(
        img_list,
        kml_paths,
        match_distance_m,
        supplement_empty_lands,
        supplement_distance_m,
        task_control,
    )
    _report(task_control, 94, "正在汇总预览结果…")
    matched = sum(match.land is not None for match in matches)
    counts = {id(land): 0 for land in lands}
    for match in matches:
        if match.land is not None:
            counts[id(match.land)] += 1
    empty_before_supplement = sum(count == 0 for count in counts.values())
    supplemented_lands = len(supplements)
    preview = AnalysisPreview(
        land_count=len(lands),
        matched=matched,
        unmatched=len(matches) - matched,
        gps_photos=len(matches),
        empty_lands=max(0, empty_before_supplement - supplemented_lands),
        supplemented_lands=supplemented_lands,
        supplement_copies=sum(len(items) for items in supplements.values()),
        dataset_gap_m=dataset_gap_m,
        rows=[
            AnalysisPreviewRow(
                filename=match.photo.filename,
                land_name=match.land.name if match.land else "",
                distance_m=match.distance_m,
                direct_hit=match.direct_hit,
                matched=match.land is not None,
            )
            for match in matches
        ],
    )
    _report(task_control, 100, "分析完成")
    return preview


def _run_transfer_plans(
    plans: list,
    workers: int,
    process_one,
    task_control: TaskControl | None,
    progress_range: tuple[float, float],
    message: str,
    thread_name: str,
) -> list[bool]:
    if not plans:
        _report(task_control, progress_range[1], message)
        return []
    results: list[bool] = []
    if workers <= 1:
        for index, plan in enumerate(plans, start=1):
            if task_control:
                task_control.checkpoint()
            results.append(process_one(plan))
            if task_control:
                task_control.report_range(
                    *progress_range,
                    index,
                    len(plans),
                    f"{message} {index}/{len(plans)}",
                )
        return results

    executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix=thread_name)
    next_index = 0
    active = {}
    try:
        while next_index < len(plans) and len(active) < workers:
            if task_control:
                task_control.checkpoint()
            future = executor.submit(process_one, plans[next_index])
            active[future] = next_index
            next_index += 1
        completed = 0
        ordered: dict[int, bool] = {}
        while active:
            done, _pending = wait(active, return_when=FIRST_COMPLETED)
            for future in done:
                plan_index = active.pop(future)
                ordered[plan_index] = future.result()
                completed += 1
                if task_control:
                    task_control.report_range(
                        *progress_range,
                        completed,
                        len(plans),
                        f"{message} {completed}/{len(plans)}",
                    )
                if next_index < len(plans):
                    if task_control:
                        task_control.checkpoint()
                    new_future = executor.submit(process_one, plans[next_index])
                    active[new_future] = next_index
                    next_index += 1
        return [ordered[index] for index in range(len(plans))]
    finally:
        executor.shutdown(wait=True, cancel_futures=True)


def _report(
    task_control: TaskControl | None,
    percent: float,
    message: str,
) -> None:
    if task_control:
        task_control.report(percent, message)


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
