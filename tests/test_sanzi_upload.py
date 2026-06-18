from pathlib import Path

from PIL import Image

from sanzi_photo_tool.services.sanzi_upload import (
    UploadOptions,
    average_pick,
    run_upload,
    scan_upload_groups,
)


class FakeClient:
    def query_detail(self, landcode: str):
        return {"useStatus": "已使用", "landStatus": "耕地"}

    def query_documents(self, landcode: str):
        return [{"fileName": "A2.jpg"}]

    def upload_photo(self, landcode: str, photo: Path):
        raise AssertionError("预检查不应上传文件")


def test_scan_and_precheck_upload_groups(tmp_path: Path) -> None:
    folder = tmp_path / "图斑_410726203205072100"
    folder.mkdir()
    for index in range(1, 6):
        Image.new("RGB", (20, 20), "white").save(folder / f"A{index}.jpg")
    (tmp_path / "无编号文件夹").mkdir()

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

    options = UploadOptions(
        token="test-token",
        photo_root=str(tmp_path),
        max_photos=3,
        skip_uploaded=True,
        average_pick=True,
    )
    results = run_upload(options, check_only=True, client=FakeClient())
    assert [result.filename for result in results] == ["A1.jpg", "A3.jpg", "A5.jpg"]
    assert all(result.status == "可上传" for result in results)


def test_average_pick_spreads_photos() -> None:
    photos = [Path(f"{index}.jpg") for index in range(8)]
    assert [path.name for path in average_pick(photos, 3)] == ["0.jpg", "3.jpg", "7.jpg"]
