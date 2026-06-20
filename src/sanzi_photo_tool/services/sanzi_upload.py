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
from .task_control import TaskControl

PHOTO_SUFFIXES = {".jpg", ".jpeg", ".png"}
LANDCODE_PATTERN = re.compile(r"\d{12,}")
BLOCKING_UPLOAD_STATUSES = {"编码异常", "地区不一致", "缺少文件夹"}


class PlatformAuthenticationExpired(RuntimeError):
    """平台明确返回401/未登录。"""


@dataclass(slots=True)
class UploadOptions:
    base_url: str = "http://222.143.69.159:38762"
    origin: str = "http://222.143.69.159:38590"
    token: str = ""
    token_header: str = "Token"
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
    retry_count: int = 2


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


@dataclass(slots=True)
class UploadInventory:
    root: Path
    groups: list[PhotoGroup] = field(default_factory=list)
    invalid_folders: list[str] = field(default_factory=list)


def scan_upload_groups(photo_root: str | Path) -> list[PhotoGroup]:
    return scan_upload_inventory(photo_root).groups


def scan_upload_inventory(photo_root: str | Path) -> UploadInventory:
    root = Path(photo_root).expanduser()
    if not root.is_dir():
        raise ValueError("请选择有效的照片根目录")
    groups: list[PhotoGroup] = []
    invalid_folders: list[str] = []
    for folder in sorted((item for item in root.iterdir() if item.is_dir()), key=lambda p: p.name):
        landcode = folder.name.strip()
        if not LANDCODE_PATTERN.fullmatch(landcode):
            invalid_folders.append(folder.name)
            continue
        photos = sorted(
            (
                item
                for item in folder.rglob("*")
                if item.is_file() and item.suffix.lower() in PHOTO_SUFFIXES
            ),
            key=lambda p: (p.name.casefold(), str(p).casefold()),
        )
        groups.append(PhotoGroup(landcode, folder.name, photos))
    return UploadInventory(root, groups, invalid_folders)


def read_upload_landcodes(kml_paths: list[str] | tuple[str, ...]) -> set[str]:
    if not kml_paths:
        raise ValueError("请选择本次上传对应的KML图斑文件")
    codes: set[str] = set()
    for land in read_land_kml_files(list(kml_paths)):
        raw = (land.landcode or "").strip()
        if not raw and LANDCODE_PATTERN.fullmatch((land.name or "").strip()):
            raw = land.name.strip()
        if LANDCODE_PATTERN.fullmatch(raw):
            codes.add(raw)
    if not codes:
        raise ValueError("所选KML中没有识别到图斑编号")
    return codes


def validate_upload_groups(
    groups: list[PhotoGroup],
    kml_codes: set[str],
    districtcode: str,
    invalid_folders: list[str] | None = None,
) -> list[UploadResult]:
    district = districtcode.strip()
    invalid: list[UploadResult] = []
    for folder_name in invalid_folders or []:
        invalid.append(
            UploadResult(
                folder_name,
                "—",
                "编码异常",
                "文件夹名称必须完全等于KML中的完整地块编码",
            )
        )
    if not district:
        invalid.extend(
            UploadResult(
                code,
                "—",
                "地区不一致",
                "没有识别到当前登录地区，请重新进入下载平台图斑页面后再试",
            )
            for code in sorted(kml_codes)
        )
        return invalid

    kml_outside_district = sorted(
        code for code in kml_codes if not code.startswith(district)
    )
    if kml_outside_district:
        invalid.extend(
            UploadResult(
                code,
                "—",
                "地区不一致",
                f"KML地块编码不属于当前登录地区 {district}",
            )
            for code in kml_outside_district
        )
        return invalid

    group_codes = {group.landcode for group in groups}
    for group in groups:
        if not group.landcode.startswith(district):
            invalid.append(
                UploadResult(
                    group.landcode,
                    f"{len(group.photos)} 张",
                    "地区不一致",
                    f"文件夹编号不属于当前登录地区 {district}",
                )
            )
        elif group.landcode not in kml_codes:
            invalid.append(
                UploadResult(
                    group.landcode,
                    f"{len(group.photos)} 张",
                    "编码异常",
                    "文件夹编号不在所选KML中",
                )
            )
    for code in sorted(kml_codes - group_codes):
        invalid.append(
            UploadResult(
                code,
                "0 张",
                "缺少文件夹",
                "KML中有该地块，但整理结果中没有对应文件夹",
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
        if _is_authentication_error(payload):
            raise PlatformAuthenticationExpired("HTTP 401：平台登录状态已失效")
        if not is_success(payload):
            raise RuntimeError(_response_message(payload, "平台返回查询失败"))
        data = extract_data(payload)
        return data if isinstance(data, dict) else {}

    def query_documents(self, landcode: str) -> list[dict[str, Any]]:
        path = "/scgl/services/acquisition/doc"
        try:
            payload = self._json_request(
                "POST",
                path,
                {"landcode": landcode},
                timeout=20,
            )
            if is_success(payload):
                return extract_rows(payload)
        except Exception:
            pass
        try:
            payload = self._form_request(
                "POST",
                path,
                {"landcode": landcode},
                timeout=20,
            )
            return extract_rows(payload) if is_success(payload) else []
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

    def _form_request(
        self,
        method: str,
        path: str,
        body: dict[str, Any],
        timeout: int = 20,
    ) -> dict[str, Any]:
        data = urlencode(
            {key: "" if value is None else str(value) for key, value in body.items()}
        ).encode("utf-8")
        headers = self._headers()
        headers["Content-Type"] = "application/x-www-form-urlencoded;charset=UTF-8"
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
            header_name = self.options.token_header or "Token"
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
    task_control: TaskControl | None = None,
) -> list[UploadResult]:
    if task_control:
        task_control.report(1, "正在读取KML和照片目录…")
    if not options.token.strip():
        raise ValueError("请先登录三资平台并获取登录信息")
    inventory = scan_upload_inventory(options.photo_root)
    groups = inventory.groups
    kml_codes = read_upload_landcodes(options.kml_paths)
    blocked = validate_upload_groups(
        groups,
        kml_codes,
        options.districtcode,
        inventory.invalid_folders,
    )
    if blocked:
        if task_control:
            task_control.report(100, "完整性检查未通过")
        return sorted(blocked, key=lambda item: item.landcode)
    api = client or SanziClient(options)
    results: list[UploadResult] = []
    groups_by_code = {group.landcode: group for group in groups}
    ordered_codes = sorted(kml_codes)
    total_groups = max(1, len(ordered_codes))

    def report_position(position: float, message: str) -> None:
        if task_control:
            task_control.report(3 + 93 * (position / total_groups), message)

    for group_index, landcode in enumerate(ordered_codes, start=1):
        report_position(
            group_index - 1,
            (
                f"正在检查图斑 {group_index}/{len(ordered_codes)}"
                if check_only
                else f"正在准备图斑 {group_index}/{len(ordered_codes)}"
            ),
        )
        group = groups_by_code[landcode]
        if not group.photos:
            results.append(
                UploadResult(
                    group.landcode,
                    "0 张",
                    "没有照片",
                    "该KML图斑已有对应文件夹，但文件夹内没有照片",
                )
            )
            report_position(group_index, f"已处理图斑 {group_index}/{len(ordered_codes)}")
            continue
        try:
            detail = api.query_detail(group.landcode)
        except PlatformAuthenticationExpired:
            raise
        except Exception as exc:
            results.append(
                UploadResult(
                    group.landcode,
                    f"{len(group.photos)} 张",
                    "平台查不到",
                    f"查询失败：{exc}",
                )
            )
            report_position(group_index, f"已处理图斑 {group_index}/{len(ordered_codes)}")
            continue
        returned_code = str(
            _first_value(
                detail,
                ("landcode", "landCode", "dkbm", "tbbh"),
            )
            or ""
        ).strip()
        if returned_code and returned_code != group.landcode:
            results.append(
                UploadResult(
                    group.landcode,
                    f"{len(group.photos)} 张",
                    "编码异常",
                    f"平台返回的地块编码为 {returned_code}，与文件夹不一致",
                )
            )
            report_position(group_index, f"已处理图斑 {group_index}/{len(ordered_codes)}")
            continue
        if options.only_with_use_status and not _required_status_complete(detail):
            results.append(
                UploadResult(
                    group.landcode,
                    f"{len(group.photos)} 张",
                    "资料未完善",
                    "使用状态或地类现状未填写",
                )
            )
            report_position(group_index, f"已处理图斑 {group_index}/{len(ordered_codes)}")
            continue
        existing = {
            name.casefold()
            for row in api.query_documents(group.landcode)
            if (name := _document_name(row))
        }
        already_uploaded = [
            photo
            for photo in group.photos
            if options.skip_uploaded and photo.name.casefold() in existing
        ]
        if already_uploaded:
            results.append(
                UploadResult(
                    group.landcode,
                    f"{len(already_uploaded)} 张",
                    "已经上传",
                    "平台已存在同名照片，本次自动跳过",
                )
            )
        candidates = [
            photo
            for photo in group.photos
            if not options.skip_uploaded or photo.name.casefold() not in existing
        ]
        if not candidates:
            report_position(group_index, f"已处理图斑 {group_index}/{len(ordered_codes)}")
            continue
        selected = (
            average_pick(candidates, options.max_photos)
            if options.average_pick
            else candidates[: options.max_photos]
        )
        for photo_index, photo in enumerate(selected, start=1):
            if task_control:
                task_control.checkpoint()
            action = "检查" if check_only else "上传"
            report_position(
                group_index - 1 + (photo_index / max(1, len(selected))),
                (
                    f"正在{action}照片 {photo_index}/{len(selected)}｜"
                    f"图斑 {group_index}/{len(ordered_codes)}"
                ),
            )
            if check_only:
                results.append(UploadResult(group.landcode, photo.name, "可上传", ""))
                continue
            last_error: Exception | None = None
            attempts = max(1, min(int(options.retry_count) + 1, 5))
            for attempt in range(attempts):
                try:
                    api.upload_photo(group.landcode, photo)
                    last_error = None
                    break
                except Exception as exc:
                    last_error = exc
                    if attempt + 1 < attempts:
                        wait_seconds = min(3.0, 0.8 * (attempt + 1))
                        if task_control:
                            task_control.wait(wait_seconds)
                        else:
                            time.sleep(wait_seconds)
            if last_error is None:
                results.append(UploadResult(group.landcode, photo.name, "成功", ""))
            else:
                results.append(
                    UploadResult(
                        group.landcode,
                        photo.name,
                        "失败",
                        f"已尝试 {attempts} 次：{last_error}",
                    )
                )
            if options.delay_seconds > 0:
                if task_control:
                    task_control.wait(options.delay_seconds)
                else:
                    time.sleep(options.delay_seconds)
        report_position(group_index, f"已处理图斑 {group_index}/{len(ordered_codes)}")
    if task_control:
        task_control.report(100, "检查完成" if check_only else "上传完成")
    return results


def write_upload_log(results: list[UploadResult], path: str | Path) -> None:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["图斑编号", "照片文件", "状态", "说明"])
        for result in results:
            writer.writerow([result.landcode, result.filename, result.status, result.message])
        writer.writerow([])
        writer.writerow(["汇总状态", "数量"])
        status_counts: dict[str, int] = {}
        for result in results:
            status_counts[result.status] = status_counts.get(result.status, 0) + 1
        for status, count in sorted(status_counts.items()):
            writer.writerow([status, count])


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
    key_set = {key.casefold() for key in keys}
    for key, value in data.items():
        if str(key).casefold() in key_set and _clean_field_value(value):
            return value
    for value in data.values():
        if isinstance(value, dict):
            found = _first_value(value, keys)
            if found not in (None, ""):
                return found
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    found = _first_value(item, keys)
                    if found not in (None, ""):
                        return found
    return None


def _clean_field_value(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.casefold() in {"", "null", "none", "undefined", "nan"} else text


def _open_json(request: Request, timeout: int) -> dict[str, Any]:
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8-sig", errors="replace")
    except HTTPError as exc:
        message = exc.read().decode("utf-8", errors="replace")
        if exc.code == 401:
            raise PlatformAuthenticationExpired(
                f"HTTP 401：平台登录状态已失效：{message[:120]}"
            ) from exc
        raise RuntimeError(f"HTTP {exc.code}：{message[:200]}") from exc
    except URLError as exc:
        raise RuntimeError(f"网络连接失败：{exc.reason}") from exc
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"平台返回了无法识别的数据：{raw[:200]}") from exc
    return value if isinstance(value, dict) else {"data": value}


def _is_authentication_error(data: dict[str, Any]) -> bool:
    code = data.get("code")
    message = str(data.get("message") or data.get("msg") or "")
    return code in (401, "401") or "未登录" in message


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
