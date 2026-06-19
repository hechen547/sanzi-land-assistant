from __future__ import annotations

from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Iterable

from PIL import ExifTags, Image

from ..models.photo import PhotoInfo

SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp"}
GPS_TAG_ID = next(key for key, value in ExifTags.TAGS.items() if value == "GPSInfo")
DATETIME_TAG_IDS = tuple(
    key
    for key, value in ExifTags.TAGS.items()
    if value in {"DateTimeOriginal", "DateTimeDigitized", "DateTime"}
)


def collect_image_paths(
    source: str | Path | Iterable[str | Path],
    recursive: bool = True,
) -> list[Path]:
    if isinstance(source, (str, Path)):
        path = Path(source)
        if path.is_file():
            candidates = [path]
        elif path.is_dir():
            candidates = path.rglob("*") if recursive else path.glob("*")
        else:
            return []
    else:
        candidates = (Path(item) for item in source)
    return sorted(
        (path.resolve() for path in candidates if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS),
        key=lambda item: item.name.casefold(),
    )


def scan_photos(
    source: str | Path | Iterable[str | Path],
    recursive: bool = True,
    workers: int = 1,
) -> list[PhotoInfo]:
    paths = collect_image_paths(source, recursive)
    worker_count = max(1, min(int(workers), 4, len(paths) or 1))
    if worker_count == 1:
        return [read_photo_info(path) for path in paths]
    with ThreadPoolExecutor(
        max_workers=worker_count,
        thread_name_prefix="photo-scan",
    ) as executor:
        return list(executor.map(read_photo_info, paths))


def read_photo_info(path: str | Path) -> PhotoInfo:
    image_path = Path(path)
    info = PhotoInfo(filename=image_path.name, full_path=str(image_path.resolve()))
    try:
        with Image.open(image_path) as image:
            info.width, info.height = image.size
            exif = image.getexif()
            info.shot_time = _read_shot_time(exif)
            lat, lon = _read_gps(exif)
            info.lat, info.lon = lat, lon
            info.has_gps = lat is not None and lon is not None
    except Exception as exc:
        info.error = str(exc)
    return info


def _read_shot_time(exif: Image.Exif) -> datetime | None:
    for tag_id in DATETIME_TAG_IDS:
        value = exif.get(tag_id)
        if not value:
            continue
        try:
            return datetime.strptime(str(value), "%Y:%m:%d %H:%M:%S")
        except ValueError:
            continue
    return None


def _read_gps(exif: Image.Exif) -> tuple[float | None, float | None]:
    gps_ifd = exif.get_ifd(GPS_TAG_ID)
    if not gps_ifd:
        return None, None
    gps = {ExifTags.GPSTAGS.get(key, key): value for key, value in gps_ifd.items()}
    lat = _dms_to_decimal(gps.get("GPSLatitude"), gps.get("GPSLatitudeRef"))
    lon = _dms_to_decimal(gps.get("GPSLongitude"), gps.get("GPSLongitudeRef"))
    if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None, None
    return lat, lon


def _dms_to_decimal(value: object, ref: object) -> float | None:
    if not value or not ref:
        return None
    try:
        degrees, minutes, seconds = (float(part) for part in value)
        decimal = degrees + minutes / 60 + seconds / 3600
        if str(ref).upper() in {"S", "W"}:
            decimal = -decimal
        return decimal
    except (TypeError, ValueError, ZeroDivisionError):
        return None
