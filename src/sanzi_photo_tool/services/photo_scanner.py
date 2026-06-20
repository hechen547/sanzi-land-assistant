from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
import os
from pathlib import Path
import re
import threading
from typing import Iterable
import uuid

from PIL import ExifTags, Image

from ..models.photo import PhotoInfo
from ..runtime import user_data_dir

SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp"}
CACHE_SCHEMA_VERSION = 1
CACHE_FILENAME = "照片信息缓存.json"
CACHE_MAX_ENTRIES = 100_000
GPS_TAG_ID = next(key for key, value in ExifTags.TAGS.items() if value == "GPSInfo")
DATETIME_TAG_IDS = tuple(
    key
    for key, value in ExifTags.TAGS.items()
    if value in {"DateTimeOriginal", "DateTimeDigitized", "DateTime"}
)
_CACHE_LOCK = threading.Lock()
_XMP_ATTRIBUTE = re.compile(
    r"""(?:[A-Za-z_][\w.-]*:)?(?P<name>
        GPSLatitude|GPSLongitude|GpsLatitude|GpsLongitude|
        DateTimeOriginal|CreateDate|DateCreated
    )\s*=\s*["'](?P<value>[^"']+)["']""",
    re.IGNORECASE | re.VERBOSE,
)
_XMP_ELEMENT = re.compile(
    r"""<(?:[A-Za-z_][\w.-]*:)?(?P<name>
        GPSLatitude|GPSLongitude|GpsLatitude|GpsLongitude|
        DateTimeOriginal|CreateDate|DateCreated
    )\b[^>]*>(?P<value>.*?)</(?:[A-Za-z_][\w.-]*:)?(?P=name)>""",
    re.IGNORECASE | re.DOTALL | re.VERBOSE,
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
    *,
    use_cache: bool = True,
    cache_path: str | Path | None = None,
) -> list[PhotoInfo]:
    paths = collect_image_paths(source, recursive)
    if not paths:
        return []
    cache_file = (
        Path(cache_path)
        if cache_path is not None
        else user_data_dir() / CACHE_FILENAME
    )
    cache = _load_cache(cache_file) if use_cache else {}
    photos_by_path: dict[Path, PhotoInfo] = {}
    paths_to_read: list[Path] = []
    for path in paths:
        cached = _cached_photo(path, cache)
        if cached is None:
            paths_to_read.append(path)
        else:
            photos_by_path[path] = cached

    worker_count = max(1, min(int(workers), 4, len(paths) or 1))
    if worker_count == 1 or len(paths_to_read) <= 1:
        scanned = [read_photo_info(path) for path in paths_to_read]
    else:
        with ThreadPoolExecutor(
            max_workers=worker_count,
            thread_name_prefix="photo-scan",
        ) as executor:
            scanned = list(executor.map(read_photo_info, paths_to_read))
    for path, photo in zip(paths_to_read, scanned):
        photos_by_path[path] = photo
        if use_cache and not photo.error:
            cache[_cache_key(path)] = _cache_record(path, photo)
    if use_cache and paths_to_read:
        _save_cache(cache_file, cache)
    return [photos_by_path[path] for path in paths]


def read_photo_info(path: str | Path) -> PhotoInfo:
    image_path = Path(path)
    info = PhotoInfo(filename=image_path.name, full_path=str(image_path.resolve()))
    try:
        with Image.open(image_path) as image:
            info.width, info.height = image.size
            exif = image.getexif()
            info.shot_time = _read_shot_time(exif)
            lat, lon = _read_gps(exif)
            if lat is None or lon is None or info.shot_time is None:
                xmp_lat, xmp_lon, xmp_time = _read_xmp_metadata(image)
                if lat is None or lon is None:
                    lat, lon = xmp_lat, xmp_lon
                if info.shot_time is None:
                    info.shot_time = xmp_time
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


def _read_xmp_metadata(
    image: Image.Image,
) -> tuple[float | None, float | None, datetime | None]:
    values: dict[str, str] = {}
    xmp_texts = _xmp_texts(image)
    for text in xmp_texts:
        for pattern in (_XMP_ATTRIBUTE, _XMP_ELEMENT):
            for match in pattern.finditer(text):
                key = match.group("name").lower()
                values.setdefault(key, _strip_xml_text(match.group("value")))
    lat = _parse_xmp_coordinate(values.get("gpslatitude"))
    lon = _parse_xmp_coordinate(values.get("gpslongitude"))
    if lat is None or lon is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
        lat, lon = None, None
    shot_time = _parse_xmp_datetime(
        values.get("datetimeoriginal")
        or values.get("createdate")
        or values.get("datecreated")
    )
    return lat, lon, shot_time


def _xmp_texts(image: Image.Image) -> list[str]:
    payloads: list[bytes | str] = []
    for key in ("xmp", "XML:com.adobe.xmp"):
        value = image.info.get(key)
        if value:
            payloads.append(value)
    applist = getattr(image, "applist", ())
    for marker, payload in applist:
        if marker == "APP1" and b"xmp" in payload[:80].lower():
            payloads.append(payload)
    texts: list[str] = []
    for payload in payloads:
        if isinstance(payload, bytes):
            texts.append(payload.decode("utf-8", errors="ignore"))
        else:
            texts.append(str(payload))
    return texts


def _parse_xmp_coordinate(value: str | None) -> float | None:
    if not value:
        return None
    text = value.strip().upper().replace("º", "°")
    sign = -1 if any(marker in text for marker in ("S", "W")) else 1
    numbers = [
        float(number)
        for number in re.findall(r"[-+]?\d+(?:\.\d+)?", text)
    ]
    if not numbers:
        return None
    if numbers[0] < 0:
        sign = -1
    degrees = abs(numbers[0])
    if len(numbers) >= 3:
        decimal = degrees + numbers[1] / 60 + numbers[2] / 3600
    elif len(numbers) == 2:
        decimal = degrees + numbers[1] / 60
    else:
        decimal = degrees
    return sign * decimal


def _parse_xmp_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    text = value.strip()
    candidates = (
        text,
        text.replace("Z", "+00:00"),
        text.replace(":", "-", 2),
    )
    for candidate in candidates:
        try:
            parsed = datetime.fromisoformat(candidate)
            return parsed.replace(tzinfo=None)
        except ValueError:
            pass
    for format_string in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text[:19], format_string)
        except ValueError:
            continue
    return None


def _strip_xml_text(value: str) -> str:
    return re.sub(r"<[^>]+>", "", value).strip()


def _cache_key(path: Path) -> str:
    return os.path.normcase(str(path.resolve()))


def _file_signature(path: Path) -> dict[str, int]:
    stat = path.stat()
    return {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def _cached_photo(path: Path, cache: dict[str, dict]) -> PhotoInfo | None:
    record = cache.get(_cache_key(path))
    if not isinstance(record, dict):
        return None
    try:
        if record.get("signature") != _file_signature(path):
            return None
        photo = PhotoInfo.from_mapping(record["photo"])
        photo.filename = path.name
        photo.full_path = str(path.resolve())
        return photo
    except (KeyError, OSError, TypeError, ValueError):
        return None


def _cache_record(path: Path, photo: PhotoInfo) -> dict:
    return {
        "signature": _file_signature(path),
        "photo": photo.to_dict(),
    }


def _load_cache(path: Path) -> dict[str, dict]:
    with _CACHE_LOCK:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("schema_version") != CACHE_SCHEMA_VERSION:
                return {}
            entries = data.get("entries", {})
            return entries if isinstance(entries, dict) else {}
        except (OSError, ValueError, TypeError):
            return {}


def _save_cache(path: Path, entries: dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if len(entries) > CACHE_MAX_ENTRIES:
        entries = dict(list(entries.items())[-CACHE_MAX_ENTRIES:])
    payload = {
        "schema_version": CACHE_SCHEMA_VERSION,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "entries": entries,
    }
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with _CACHE_LOCK:
        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            os.replace(temporary, path)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
