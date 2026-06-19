from __future__ import annotations

import csv
import json
import mimetypes
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .kml_parser import read_land_kml_files

PHOTO_SUFFIXES = {".jpg", ".jpeg", ".png"}
LANDCODE_PATTERN = re.compile(r"\d{12,}")


@dataclass(slots=True)
class UploadOptions:
    base_url: str = "http://222.143.69.159:38762"
    origin: str = "http://222.143.69.159:38590"
    token: str = ""
    token_header: str = "Authorization"
    cookie: str = ""
    districtcode: str = ""
    districtname: str = ""
    photo_root: str = ""
    kml_paths: tuple[str, ...] = ()
    max_photos: int = 3
    only_with_use_status: bool = True
    skip_uploaded: bool = True
    average_pick: bool = True
    delay_seconds: float = 0.15


@dataclass(slots=True)
class UploadResult:
    landcode: str
    filename: str
    status: str
    message: str = ""


@dataclass(slots=True)
class PhotoGroup:
    landcode: str
    folder_name: str
    photos: list[Path] = field(default_factory=list)


def scan_upload_groups(photo_root: str | Path) -> list[PhotoGroup]:
    root = Path(photo_root).expanduser()
    if not root.is_dir():
        raise ValueError("请选择有效的照片根目录")
    groups: list[PhotoGroup] = []
    for folder in sorted((item for item in root.iterdir() if item.is_dir()), key=lambda p: p.name):
        match = LANDCODE_PATTERN.search(folder.name)
        if not match:
            continue
        photos = sorted(
            (
                item
                for item in folder.rglob("*")
                if item.is_file() and item.suffix.lower() in PHOTO_SUFFIXES
            ),
            key=lambda p: (p.name.casefold(), str(p).casefold()),
        )
        groups.append(PhotoGroup(match.group(0), folder.name, photos))
    return groups


def read_upload_landcodes(kml_paths: list[str] | tuple[str, ...]) -> set[str]:
    if not kml_paths:
        raise ValueError("请选择本次上传对应的KML图斑文件")
    codes: set[str] = set()
    for land in read_land_kml_files(list(kml_paths)):
        raw = (land.landcode or land.name or "").strip()
        match = LANDCODE_PATTERN.search(raw)
        if match:
            codes.add(match.group(0))
    if not codes:
        raise ValueError("所选KML中没有识别到图斑编号")
    return codes


def validate_upload_groups(
    groups: list[PhotoGroup],
    kml_codes: set[str],
    districtcode: str,
) -> list[UploadResult]:
    district = districtcode.strip()
    if not district:
        return [
            UploadResult(
                group.landcode,
                f"{len(group.photos)} 张",
                "阻止",
                "没有识别到当前登录地区，请重新进入下载平台图斑页面后再试",
            )
            for group in groups
        ]

    invalid: list[UploadResult] = []
    kml_outside_district = sorted(
        code for code in kml_codes if not code.startswith(district)
    )
    if kml_outside_district:
        sample = "、".join(kml_outside_district[:3])
        return [
            UploadResult(
                group.landcode,
                f"{len(group.photos)} 张",
                "阻止",
                f"所选KML不属于当前登录地区 {district}，示例编号：{sample}",
            )
            for group in groups
        ]

    for group in groups:
        if not group.landcode.startswith(district):
            invalid.append(
                UploadResult(
                    group.landcode,
                    f"{len(group.photos)} 张",
                    "阻止",
                    f"文件夹编号不属于当前登录地区 {district}",
                )
            )
        elif group.landcode not in kml_codes:
            invalid.append(
                UploadResult(
                    group.landcode,
                    f"{len(group.photos)} 张",
                    "阻止",
                    "文件夹编号不在所选KML中",
                )
            )
    return invalid


def average_pick(photos: list[Path], count: int) -> list[Path]:
    if count <= 0 or not photos:
        return []
    if len(photos) <= count:
        return list(photos)
    if count == 1:
        return [photos[0]]
    indexes = [int(index * (len(photos) - 1) / (count - 1)) for index in range(count)]
    return [photos[index] for index in indexes]


class SanziClient:
    def __init__(self, options: UploadOptions) -> None:
        self.options = options

    def query_detail(self, landcode: str) -> dict[str, Any]:
        payload = self._json_request(
            "GET",
            "/scgl/services/acquisition/queryDetail?" + urlencode({"landcode": landcode}),
            timeout=20,
        )
        if not is_success(payload):
            raise RuntimeError(_response_message(payload, "平台返回查询失败"))
        data = extract_data(payload)
        return data if isinstance(data, dict) else {}

    def query_documents(self, landcode: str) -> list[dict[str, Any]]:
        try:
            payload = self._json_request(
                "POST",
                "/scgl/services/acquisition/doc",
                {"landcode": landcode},
                timeout=20,
            )
            if not is_success(payload):
                return []
            return extract_rows(payload)
        except Exception:
            return []

    def upload_photo(self, landcode: str, photo: Path) -> None:
        boundary = f"----SanziPhoto{uuid.uuid4().hex}"
        content_type = mimetypes.guess_type(photo.name)[0] or "image/jpeg"
        body = _multipart_body(
            boundary,
            {"landcode": landcode, "accessorytype": "5"},
            "file",
            photo.name,
            photo.read_bytes(),
            content_type,
        )
        request = Request(
            self.options.base_url.rstrip("/") + "/scgl/services/acquisition/doc/uploadFile",
            data=body,
            method="POST",
            headers={
                **self._headers(),
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Content-Length": str(len(body)),
            },
        )
        payload = _open_json(request, timeout=120)
        if not is_success(payload):
            raise RuntimeError(_response_message(payload, "上传失败"))

    def _json_request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        timeout: int = 20,
    ) -> dict[str, Any]:
        data = None
        headers = self._headers()
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json;charset=UTF-8"
        request = Request(
            self.options.base_url.rstrip("/") + path,
            data=data,
            method=method,
            headers=headers,
        )
        return _open_json(request, timeout)

    def _headers(self) -> dict[str, str]:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/120 Safari/537.36"
            ),
            "Origin": self.options.origin,
            "Referer": self.options.origin.rstrip("/") + "/",
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
        }
        if self.options.token:
            header_name = self.options.token_header or "Authorization"
            token = self.options.token.strip()
            if header_name.casefold() == "authorization" and not token.casefold().startswith(
                ("bearer ", "basic ")
            ):
                token = f"bearer {token}"
            headers[header_name] = token
        if self.options.cookie:
            headers["Cookie"] = self.options.cookie
        return headers


def run_upload(
    options: UploadOptions,
    check_only: bool = False,
    client: SanziClient | None = None,
) -> list[UploadResult]:
    if not options.token.strip():
        raise ValueError("请先登录三资平台并获取登录信息")
    groups = scan_upload_groups(options.photo_root)
    if not groups:
        raise ValueError("照片目录中没有找到包含12位以上图斑编号的子文件夹")
    kml_codes = read_upload_landcodes(options.kml_paths)
    blocked = validate_upload_groups(groups, kml_codes, options.districtcode)
    if blocked:
        blocked_codes = {item.landcode for item in blocked}
        for group in groups:
            if group.landcode not in blocked_codes:
                blocked.append(
                    UploadResult(
                        group.landcode,
                        f"{len(group.photos)} 张",
                        "阻止",
                        "发现其他编号不一致，已取消整批上传",
                    )
                )
        return sorted(blocked, key=lambda item: item.landcode)
    api = client or SanziClient(options)
    results: list[UploadResult] = []
    for group in groups:
        if not group.photos:
            results.append(UploadResult(group.landcode, "0 张", "跳过", "文件夹内没有照片"))
            continue
        try:
            detail = api.query_detail(group.landcode)
        except Exception as exc:
            results.append(UploadResult(group.landcode, f"{len(group.photos)} 张", "失败", f"查询失败：{exc}"))
            continue
        if options.only_with_use_status and not _required_status_complete(detail):
            results.append(
                UploadResult(group.landcode, f"{len(group.photos)} 张", "跳过", "使用状态或地类现状未填写")
            )
            continue
        existing = {
            name.casefold()
            for row in api.query_documents(group.landcode)
            if (name := _document_name(row))
        }
        candidates = [
            photo
            for photo in group.photos
            if not options.skip_uploaded or photo.name.casefold() not in existing
        ]
        if not candidates:
            results.append(UploadResult(group.landcode, f"{len(group.photos)} 张", "跳过", "平台已存在同名照片"))
            continue
        selected = (
            average_pick(candidates, options.max_photos)
            if options.average_pick
            else candidates[: options.max_photos]
        )
        for photo in selected:
            if check_only:
                results.append(UploadResult(group.landcode, photo.name, "可上传", ""))
                continue
            try:
                api.upload_photo(group.landcode, photo)
                results.append(UploadResult(group.landcode, photo.name, "成功", ""))
            except Exception as exc:
                results.append(UploadResult(group.landcode, photo.name, "失败", str(exc)))
            if options.delay_seconds > 0:
                time.sleep(options.delay_seconds)
    return results


def write_upload_log(results: list[UploadResult], path: str | Path) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["图斑编号", "照片文件", "状态", "说明"])
        for result in results:
            writer.writerow([result.landcode, result.filename, result.status, result.message])


def is_success(data: dict[str, Any]) -> bool:
    success = data.get("success")
    if success in (True, "true", "True", 1):
        return True
    return data.get("code") in (200, "200")


def extract_data(data: dict[str, Any]) -> Any:
    return data.get("data") if "data" in data else data


def extract_rows(data: dict[str, Any]) -> list[dict[str, Any]]:
    value = extract_data(data)
    if isinstance(value, list):
        return [row for row in value if isinstance(row, dict)]
    if isinstance(value, dict):
        rows = value.get("rows") or value.get("list") or value.get("records") or []
        return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
    return []


def _required_status_complete(detail: dict[str, Any]) -> bool:
    use_status = _first_value(detail, ("useStatus", "usestatus", "use_state", "syzt"))
    land_status = _first_value(detail, ("landStatus", "landactuality", "djxz"))
    return bool(str(use_status or "").strip() and str(land_status or "").strip())


def _document_name(row: dict[str, Any]) -> str:
    value = _first_value(
        row,
        ("docname", "fileName", "filename", "name", "originalName", "filepath"),
    )
    return Path(str(value)).name if value else ""


def _first_value(data: dict[str, Any], keys: tuple[str, ...]) -> Any:
    normalized = {str(key).casefold(): value for key, value in data.items()}
    for key in keys:
        value = normalized.get(key.casefold())
        if value not in (None, ""):
            return value
    return None


def _open_json(request: Request, timeout: int) -> dict[str, Any]:
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8-sig", errors="replace")
    except HTTPError as exc:
        message = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}：{message[:200]}") from exc
    except URLError as exc:
        raise RuntimeError(f"网络连接失败：{exc.reason}") from exc
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"平台返回了无法识别的数据：{raw[:200]}") from exc
    return value if isinstance(value, dict) else {"data": value}


def _multipart_body(
    boundary: str,
    fields: dict[str, str],
    file_field: str,
    filename: str,
    content: bytes,
    content_type: str,
) -> bytes:
    parts: list[bytes] = []
    for key, value in fields.items():
        parts.extend(
            [
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{key}"\r\n\r\n'.encode(),
                str(value).encode("utf-8"),
                b"\r\n",
            ]
        )
    safe_filename = filename.replace('"', "_")
    parts.extend(
        [
            f"--{boundary}\r\n".encode(),
            (
                f'Content-Disposition: form-data; name="{file_field}"; '
                f'filename="{safe_filename}"\r\n'
            ).encode("utf-8"),
            f"Content-Type: {content_type}\r\n\r\n".encode(),
            content,
            b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ]
    )
    return b"".join(parts)


def _response_message(data: dict[str, Any], fallback: str) -> str:
    return str(data.get("message") or data.get("msg") or data.get("error") or fallback)
