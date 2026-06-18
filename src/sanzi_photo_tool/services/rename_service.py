from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Literal

from ..models.photo import PhotoInfo

SortMode = Literal["filename", "shot_time"]


@dataclass(slots=True)
class RenamePlan:
    photo: PhotoInfo
    new_filename: str


def build_rename_plan(
    photos: Iterable[PhotoInfo],
    prefix: str = "A",
    start: int = 1,
    digits: int = 3,
    sort_mode: SortMode = "shot_time",
    keep_original: bool = False,
) -> list[RenamePlan]:
    items = list(photos)
    if sort_mode == "shot_time":
        items.sort(key=lambda photo: (photo.shot_time is None, photo.shot_time, photo.filename.casefold()))
    else:
        items.sort(key=lambda photo: photo.filename.casefold())

    plans: list[RenamePlan] = []
    for index, photo in enumerate(items, start=start):
        number = str(index).zfill(max(1, digits))
        stem = f"{photo.path.stem}_{prefix}{number}" if keep_original else f"{prefix}{number}"
        plans.append(RenamePlan(photo=photo, new_filename=f"{stem}{photo.path.suffix.lower()}"))
    return plans


def unique_destination(directory: str | Path, filename: str) -> Path:
    directory = Path(directory)
    candidate = directory / filename
    counter = 1
    while candidate.exists():
        candidate = directory / f"{Path(filename).stem}_{counter}{Path(filename).suffix}"
        counter += 1
    return candidate

