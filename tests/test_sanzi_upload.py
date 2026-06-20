from pathlib import Path

from PIL import Image

from sanzi_photo_tool.services.sanzi_upload import (
    PlatformAuthenticationExpired,
    SanziClient,
    UploadOptions,
    _required_status_complete,
    average_pick,
    run_upload,
    scan_upload_groups,
    scan_upload_inventory,
    write_upload_log,
)
from sanzi_photo_tool.services.task_control import TaskControl


class FakeClient:
    def query_detail(self, landcode: str):
        return {"useStatus": "已使用", "landStatus": "耕地"}

    def query_documents(self, landcode: str):
        return [{"fileName": "A2.jpg"}]

    def upload_photo(self, landcode: str, photo: Path):
        raise AssertionError("预检查不应上传文件")


class RetryClient(FakeClient):
    def __init__(self) -> None:
        self.attempts = 0

    def query_documents(self, landcode: str):
        return []

    def upload_photo(self, landcode: str, photo: Path):
        self.attempts += 1
        if self.attempts < 3:
            raise RuntimeError("临时网络错误")


class DocumentFallbackClient(SanziClient):
    def __init__(self) -> None:
        super().__init__(UploadOptions(token="test-token"))
        self.calls: list[str] = []

    def _json_request(self, *args, **kwargs):
        self.calls.append("json")
        return {"success": False, "message": "不支持 JSON 请求"}

    def _form_request(self, *args, **kwargs):
        self.calls.append("form")
        return {
            "success": True,
            "data": {"rows": [{"fileName": "A1.jpg"}]},
        }


class ExpiredClient(FakeClient):
    def __init__(self) -> None:
        self.query_count = 0

    def query_detail(self, landcode: str):
        self.query_count += 1
        raise PlatformAuthenticationExpired("HTTP 401：平台登录状态已失效")


def test_scan_and_precheck_upload_groups(tmp_path: Path) -> None:
    folder = tmp_path / "410726203205072100"
    folder.mkdir()
    for index in range(1, 6):
        Image.new("RGB", (20, 20), "white").save(folder / f"A{index}.jpg")
    groups = scan_upload_groups(tmp_path)
    assert len(groups) == 1
    assert groups[0].landcode == "410726203205072100"
    assert [path.name for path in groups[0].photos] == [
        "A1.jpg",
        "A2.jpg",
        "A3.jpg",
        "A4.jpg",
        "A5.jpg",
    ]
    kml = tmp_path / "lands.kml"
    kml.write_text(
        """<kml xmlns="http://www.opengis.net/kml/2.2"><Placemark>
<name>410726203205072100</name><ExtendedData>
<Data name="landcode"><value>410726203205072100</value></Data>
</ExtendedData><Polygon><outerBoundaryIs><LinearRing><coordinates>
113,35,0 113.01,35,0 113.01,35.01,0 113,35.01,0 113,35,0
</coordinates></LinearRing></outerBoundaryIs></Polygon></Placemark></kml>""",
        encoding="utf-8",
    )

    options = UploadOptions(
        token="test-token",
        districtcode="410726203205",
        photo_root=str(tmp_path),
        kml_paths=(str(kml),),
        max_photos=3,
        skip_uploaded=True,
        average_pick=True,
    )
    results = run_upload(options, check_only=True, client=FakeClient())
    assert [
        result.filename for result in results if result.status == "可上传"
    ] == ["A1.jpg", "A3.jpg", "A5.jpg"]
    assert any(result.status == "已经上传" for result in results)


def test_upload_scan_requires_exact_folder_name(tmp_path: Path) -> None:
    (tmp_path / "图斑_410726203205072100").mkdir()
    (tmp_path / "410726203205072101").mkdir()
    inventory = scan_upload_inventory(tmp_path)
    assert [group.landcode for group in inventory.groups] == [
        "410726203205072101"
    ]
    assert inventory.invalid_folders == ["图斑_410726203205072100"]


def test_complete_precheck_reports_empty_folder(tmp_path: Path) -> None:
    code = "410726203205072100"
    (tmp_path / code).mkdir()
    kml = tmp_path / "lands.kml"
    kml.write_text(
        f"""<kml xmlns="http://www.opengis.net/kml/2.2"><Placemark>
<name>{code}</name><ExtendedData>
<Data name="landcode"><value>{code}</value></Data>
</ExtendedData><Polygon><outerBoundaryIs><LinearRing><coordinates>
113,35,0 113.01,35,0 113.01,35.01,0 113,35.01,0 113,35,0
</coordinates></LinearRing></outerBoundaryIs></Polygon></Placemark></kml>""",
        encoding="utf-8",
    )
    results = run_upload(
        UploadOptions(
            token="test-token",
            districtcode="410726203205",
            photo_root=str(tmp_path),
            kml_paths=(str(kml),),
        ),
        check_only=True,
        client=FakeClient(),
    )
    assert len(results) == 1
    assert results[0].status == "没有照片"


def test_complete_precheck_reports_missing_and_invalid_folders(
    tmp_path: Path,
) -> None:
    code = "410726203205072100"
    (tmp_path / f"图斑_{code}").mkdir()
    kml = tmp_path / "lands.kml"
    kml.write_text(
        f"""<kml xmlns="http://www.opengis.net/kml/2.2"><Placemark>
<name>{code}</name><ExtendedData>
<Data name="landcode"><value>{code}</value></Data>
</ExtendedData><Polygon><outerBoundaryIs><LinearRing><coordinates>
113,35,0 113.01,35,0 113.01,35.01,0 113,35.01,0 113,35,0
</coordinates></LinearRing></outerBoundaryIs></Polygon></Placemark></kml>""",
        encoding="utf-8",
    )
    results = run_upload(
        UploadOptions(
            token="test-token",
            districtcode="410726203205",
            photo_root=str(tmp_path),
            kml_paths=(str(kml),),
        ),
        check_only=True,
        client=FakeClient(),
    )
    assert {result.status for result in results} == {
        "编码异常",
        "缺少文件夹",
    }


def test_upload_blocks_folder_not_in_current_kml(tmp_path: Path) -> None:
    folder = tmp_path / "411627204206000001"
    folder.mkdir()
    Image.new("RGB", (20, 20), "white").save(folder / "A1.jpg")
    kml = tmp_path / "current.kml"
    kml.write_text(
        """<kml xmlns="http://www.opengis.net/kml/2.2"><Placemark>
<name>410223206217000001</name><ExtendedData>
<Data name="landcode"><value>410223206217000001</value></Data>
</ExtendedData><Polygon><outerBoundaryIs><LinearRing><coordinates>
113,35,0 113.01,35,0 113.01,35.01,0 113,35.01,0 113,35,0
</coordinates></LinearRing></outerBoundaryIs></Polygon></Placemark></kml>""",
        encoding="utf-8",
    )
    results = run_upload(
        UploadOptions(
            token="test-token",
            districtcode="410223206217",
            photo_root=str(tmp_path),
            kml_paths=(str(kml),),
        ),
        check_only=True,
        client=FakeClient(),
    )
    assert len(results) == 2
    statuses = {result.landcode: result.status for result in results}
    assert statuses["410223206217000001"] == "缺少文件夹"
    assert statuses["411627204206000001"] == "地区不一致"


def test_average_pick_spreads_photos() -> None:
    photos = [Path(f"{index}.jpg") for index in range(8)]
    assert [path.name for path in average_pick(photos, 3)] == ["0.jpg", "3.jpg", "7.jpg"]


def test_current_platform_authorization_header() -> None:
    client = SanziClient(UploadOptions(token="abc123456789"))
    assert client._headers()["Token"] == "abc123456789"
    client = SanziClient(
        UploadOptions(
            token="Bearer abc123456789",
            token_header="Authorization",
        )
    )
    assert client._headers()["Authorization"] == "Bearer abc123456789"


def test_required_status_supports_nested_platform_data() -> None:
    assert _required_status_complete(
        {
            "data": {
                "rows": [
                    {
                        "useStatus": "正常使用",
                        "landActuality": "耕地",
                    }
                ]
            }
        }
    )
    assert not _required_status_complete(
        {"data": {"useStatus": "null", "landActuality": "耕地"}}
    )


def test_query_documents_falls_back_to_form_request() -> None:
    client = DocumentFallbackClient()
    assert client.query_documents("410726203205072100") == [{"fileName": "A1.jpg"}]
    assert client.calls == ["json", "form"]


def test_upload_retries_transient_failure(tmp_path: Path) -> None:
    folder = tmp_path / "410726203205072100"
    folder.mkdir()
    Image.new("RGB", (20, 20), "white").save(folder / "A1.jpg")
    kml = tmp_path / "lands.kml"
    kml.write_text(
        """<kml xmlns="http://www.opengis.net/kml/2.2"><Placemark>
<name>410726203205072100</name><ExtendedData>
<Data name="landcode"><value>410726203205072100</value></Data>
</ExtendedData><Polygon><outerBoundaryIs><LinearRing><coordinates>
113,35,0 113.01,35,0 113.01,35.01,0 113,35.01,0 113,35,0
</coordinates></LinearRing></outerBoundaryIs></Polygon></Placemark></kml>""",
        encoding="utf-8",
    )
    client = RetryClient()
    results = run_upload(
        UploadOptions(
            token="test-token",
            districtcode="410726203205",
            photo_root=str(tmp_path),
            kml_paths=(str(kml),),
            retry_count=2,
            delay_seconds=0,
        ),
        client=client,
    )
    assert client.attempts == 3
    assert results[0].status == "成功"


def test_upload_precheck_reports_progress_and_log_summary(tmp_path: Path) -> None:
    code = "410726203205072100"
    folder = tmp_path / code
    folder.mkdir()
    Image.new("RGB", (20, 20), "white").save(folder / "A1.jpg")
    kml = tmp_path / "lands.kml"
    kml.write_text(
        f"""<kml xmlns="http://www.opengis.net/kml/2.2"><Placemark>
<name>{code}</name><ExtendedData>
<Data name="landcode"><value>{code}</value></Data>
</ExtendedData><Polygon><outerBoundaryIs><LinearRing><coordinates>
113,35,0 113.01,35,0 113.01,35.01,0 113,35.01,0 113,35,0
</coordinates></LinearRing></outerBoundaryIs></Polygon></Placemark></kml>""",
        encoding="utf-8",
    )
    progress: list[int] = []
    results = run_upload(
        UploadOptions(
            token="test-token",
            districtcode="410726203205",
            photo_root=str(tmp_path),
            kml_paths=(str(kml),),
        ),
        check_only=True,
        client=FakeClient(),
        task_control=TaskControl(
            lambda percent, _message: progress.append(percent)
        ),
    )
    assert progress[-1] == 100
    log = tmp_path / "result.csv"
    write_upload_log(results, log)
    text = log.read_text(encoding="utf-8-sig")
    assert "汇总状态,数量" in text
    assert "可上传,1" in text


def test_first_401_stops_remaining_land_queries(tmp_path: Path) -> None:
    codes = ["410726203205072100", "410726203205072101"]
    placemarks = []
    for index, code in enumerate(codes):
        folder = tmp_path / code
        folder.mkdir()
        Image.new("RGB", (20, 20), "white").save(folder / "A1.jpg")
        lon = 113 + index * 0.02
        placemarks.append(
            f"""<Placemark><name>{code}</name><ExtendedData>
<Data name="landcode"><value>{code}</value></Data></ExtendedData>
<Polygon><outerBoundaryIs><LinearRing><coordinates>
{lon},35,0 {lon + 0.01},35,0 {lon + 0.01},35.01,0 {lon},35.01,0 {lon},35,0
</coordinates></LinearRing></outerBoundaryIs></Polygon></Placemark>"""
        )
    kml = tmp_path / "lands.kml"
    kml.write_text(
        '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
        + "".join(placemarks)
        + "</Document></kml>",
        encoding="utf-8",
    )
    client = ExpiredClient()
    try:
        run_upload(
            UploadOptions(
                token="expired-token",
                districtcode="410726203205",
                photo_root=str(tmp_path),
                kml_paths=(str(kml),),
            ),
            check_only=True,
            client=client,
        )
    except PlatformAuthenticationExpired:
        pass
    else:
        raise AssertionError("401应立即终止完整检查")
    assert client.query_count == 1
