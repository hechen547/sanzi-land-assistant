from __future__ import annotations

import csv
import re
from datetime import datetime
from pathlib import Path

from ..models.land import LandRecord
from ..models.task_result import FileOperationResult


def write_organize_report(results: list[FileOperationResult], path: str | Path) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["源照片", "输出位置", "图斑", "匹配距离(米)", "状态", "错误"])
        for result in results:
            writer.writerow(
                [
                    result.source,
                    result.destination,
                    result.land_name,
                    "" if result.distance_m is None else f"{result.distance_m:.3f}",
                    result.status,
                    result.error,
                ]
            )


def write_land_classification_log(
    lands: list[LandRecord],
    results: list[FileOperationResult],
    path: str | Path,
    *,
    output_dir: str | Path,
    match_distance_m: float,
    copy_mode: bool,
    total_photos: int,
    gps_photos: int,
    unmatched: int,
    failed: int,
    supplemented: int = 0,
    supplement_distance_m: float = 0,
) -> None:
    """写出适合直接用记事本查看的图斑照片分类工作日志。"""
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    succeeded = sum(
        result.status in {"已复制", "已移动", "已补充复制"}
        for result in results
    )
    operation = "复制（保留原照片）" if copy_mode else "移动（取走原照片）"
    lines = [
        "根据图斑分类照片工作日志",
        f"生成时间：{datetime.now():%Y-%m-%d %H:%M:%S}",
        f"输出目录：{Path(output_dir)}",
        f"照片处理方式：{operation}",
        f"图斑外允许距离：{match_distance_m:.2f} 米",
        (
            f"空图斑补充：已开启，补充距离 {supplement_distance_m:.2f} 米"
            if supplement_distance_m > 0
            else "空图斑补充：未开启"
        ),
        f"读取照片：{total_photos} 张",
        f"有定位照片：{gps_photos} 张",
        f"没有定位照片：{total_photos - gps_photos} 张",
        f"成功处理：{succeeded} 张",
        f"其中空图斑补充复制：{supplemented} 张",
        f"未匹配照片：{unmatched} 张",
        f"处理失败：{failed} 张",
        "",
        "各图斑成功处理数量：",
    ]
    lines.extend(f"{land.name}: {land.count} 张" for land in lands)
    output.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")


def write_distance_match_list(
    results: list[FileOperationResult],
    path: str | Path,
) -> None:
    """记录依靠图斑外允许距离匹配成功的照片。"""
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    distance_matches = [
        result
        for result in results
        if result.status in {"已复制", "已移动", "已补充复制"}
        and result.distance_m is not None
        and result.distance_m > 0
    ]
    lines = ["照片文件\t匹配图斑\t距离米\t目标文件"]
    lines.extend(
        "\t".join(
            [
                Path(result.source).name,
                result.land_name,
                f"{result.distance_m:.2f}",
                result.destination,
            ]
        )
        for result in distance_matches
    )
    if not distance_matches:
        lines.append("没有依靠图斑外距离匹配的照片")
    output.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")


def write_invalid_land_list(
    lands: list[LandRecord],
    path: str | Path,
) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    invalid = [
        land
        for land in lands
        if not re.fullmatch(r"\d{12,}", land.landcode or "")
    ]
    lines = ["图斑名称\t来源文件\t说明"]
    lines.extend(
        f"{land.name}\t{land.source_file}\tKML中缺少有效landcode，不参与平台上传"
        for land in invalid
    )
    if not invalid:
        lines.append("没有发现缺少地块编码的图斑")
    output.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
