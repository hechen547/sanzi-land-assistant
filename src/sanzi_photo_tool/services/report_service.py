from __future__ import annotations

import csv
from pathlib import Path

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

