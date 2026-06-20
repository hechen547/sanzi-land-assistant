from pathlib import Path

from PIL import Image

from sanzi_photo_tool.services.photo_organizer import (
    analyze_photo_land_preview,
    organize_photos_by_land,
)
from sanzi_photo_tool.services.task_control import TaskCancelled, TaskControl


def test_organize_photos_by_land_end_to_end(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    inside = source / "inside.jpg"
    outside = source / "outside.jpg"
    Image.new("RGB", (80, 60), "white").save(inside)
    Image.new("RGB", (80, 60), "gray").save(outside)

    kml = tmp_path / "lands.kml"
    kml.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2"><Document>
<Placemark><name>地块一</name><ExtendedData>
<Data name="landcode"><value>DK001</value></Data></ExtendedData>
<Polygon><outerBoundaryIs><LinearRing><coordinates>
113,35,0 113.01,35,0 113.01,35.01,0 113,35.01,0 113,35,0
</coordinates></LinearRing></outerBoundaryIs></Polygon></Placemark>
<Placemark><name>空地块</name><ExtendedData>
<Data name="landcode"><value>DK002</value></Data></ExtendedData>
<Polygon><outerBoundaryIs><LinearRing><coordinates>
114,36,0 114.01,36,0 114.01,36.01,0 114,36.01,0 114,36,0
</coordinates></LinearRing></outerBoundaryIs></Polygon></Placemark>
</Document></kml>""",
        encoding="utf-8",
    )

    output = tmp_path / "output"
    summary = organize_photos_by_land(
        [
            {
                "filename": inside.name,
                "full_path": str(inside),
                "lat": 35.005,
                "lon": 113.005,
                "has_gps": True,
            },
            {
                "filename": outside.name,
                "full_path": str(outside),
                "lat": 34,
                "lon": 112,
                "has_gps": True,
            },
        ],
        [str(kml)],
        str(output),
        transfer_mode="compatible",
    )

    assert summary.succeeded == 1
    assert summary.matched == 1
    assert summary.unmatched == 1
    assert summary.empty_lands == 1
    assert (output / "DK001" / "inside.jpg").exists()
    assert (output / "DK002").is_dir()
    assert not any((output / "DK002").iterdir())
    assert not (output / "未匹配图斑").exists()
    assert not any(output.rglob("outside.jpg"))
    assert (output / "无照片图斑.kml").exists()
    assert (output / "未匹配照片.kml").exists()
    assert (output / "图斑外距离匹配照片清单.txt").exists()
    assert (output / "图斑照片分类工作日志.txt").exists()
    assert (output / "编码异常图斑清单.txt").exists()
    assert (output / "整理结果.csv").exists()
    log_text = (output / "图斑照片分类工作日志.txt").read_text(
        encoding="utf-8-sig"
    )
    assert "DK001: 1 张" in log_text
    assert "DK002: 0 张" in log_text
    distance_text = (output / "图斑外距离匹配照片清单.txt").read_text(
        encoding="utf-8-sig"
    )
    assert "没有依靠图斑外距离匹配的照片" in distance_text


def test_fast_mode_handles_duplicate_filenames(tmp_path: Path) -> None:
    photos = []
    for index in range(4):
        folder = tmp_path / f"source_{index}"
        folder.mkdir()
        photo = folder / "same.jpg"
        Image.new("RGB", (20, 20), "white").save(photo)
        photos.append(
            {
                "filename": photo.name,
                "full_path": str(photo),
                "lat": 35.005,
                "lon": 113.005,
                "has_gps": True,
            }
        )
    kml = tmp_path / "land.kml"
    kml.write_text(
        """<kml xmlns="http://www.opengis.net/kml/2.2"><Placemark><name>DK1</name>
<ExtendedData><Data name="landcode"><value>DK1</value></Data></ExtendedData>
<Polygon><outerBoundaryIs><LinearRing><coordinates>
113,35,0 113.01,35,0 113.01,35.01,0 113,35.01,0 113,35,0
</coordinates></LinearRing></outerBoundaryIs></Polygon></Placemark></kml>""",
        encoding="utf-8",
    )
    output = tmp_path / "fast"
    summary = organize_photos_by_land(
        photos,
        [str(kml)],
        str(output),
        transfer_mode="fast",
    )
    copied = list((output / "DK1").glob("*.jpg"))
    assert summary.succeeded == 4
    assert len(copied) == 4
    assert len({path.name.casefold() for path in copied}) == 4


def test_empty_land_can_receive_nearby_supplement_photo(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    photo = source / "nearby.jpg"
    Image.new("RGB", (20, 20), "white").save(photo)
    kml = tmp_path / "lands.kml"
    kml.write_text(
        """<kml xmlns="http://www.opengis.net/kml/2.2"><Document>
<Placemark><name>DK1</name><ExtendedData><Data name="landcode"><value>DK1</value></Data></ExtendedData><Polygon><outerBoundaryIs><LinearRing><coordinates>
113,35,0 113.001,35,0 113.001,35.001,0 113,35.001,0 113,35,0
</coordinates></LinearRing></outerBoundaryIs></Polygon></Placemark>
<Placemark><name>DK2</name><ExtendedData><Data name="landcode"><value>DK2</value></Data></ExtendedData><Polygon><outerBoundaryIs><LinearRing><coordinates>
113.0011,35,0 113.0021,35,0 113.0021,35.001,0 113.0011,35.001,0 113.0011,35,0
</coordinates></LinearRing></outerBoundaryIs></Polygon></Placemark>
</Document></kml>""",
        encoding="utf-8",
    )
    output = tmp_path / "supplemented"
    summary = organize_photos_by_land(
        [
            {
                "filename": photo.name,
                "full_path": str(photo),
                "lat": 35.0005,
                "lon": 113.00099,
                "has_gps": True,
            }
        ],
        [str(kml)],
        str(output),
        transfer_mode="compatible",
        supplement_empty_lands=True,
        supplement_distance_m=20,
    )
    assert summary.matched == 1
    assert summary.supplemented == 1
    assert summary.succeeded == 2
    assert summary.empty_lands == 0
    assert (output / "DK1" / photo.name).exists()
    assert (output / "DK2" / photo.name).exists()


def test_preview_can_run_repeatedly_with_different_distances(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    photo = source / "near.jpg"
    Image.new("RGB", (20, 20), "white").save(photo)
    kml = tmp_path / "land.kml"
    kml.write_text(
        """<kml xmlns="http://www.opengis.net/kml/2.2"><Placemark><name>DK1</name>
<Polygon><outerBoundaryIs><LinearRing><coordinates>
113,35,0 113.001,35,0 113.001,35.001,0 113,35.001,0 113,35,0
</coordinates></LinearRing></outerBoundaryIs></Polygon></Placemark></kml>""",
        encoding="utf-8",
    )
    photos = [
        {
            "filename": photo.name,
            "full_path": str(photo),
            "lat": 35.0005,
            "lon": 113.0017,
            "has_gps": True,
        }
    ]
    first = analyze_photo_land_preview(photos, [str(kml)], 100)
    second = analyze_photo_land_preview(photos, [str(kml)], 50)
    assert first.matched == 1
    assert second.matched == 0
    assert first.rows[0].land_name == "DK1"
    assert second.rows[0].land_name == ""


def test_preview_reports_progress_and_can_be_cancelled(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    photos = []
    for index in range(100):
        photo = source / f"{index}.jpg"
        Image.new("RGB", (10, 10), "white").save(photo)
        photos.append(
            {
                "filename": photo.name,
                "full_path": str(photo),
                "lat": 35.0005,
                "lon": 113.0005,
                "has_gps": True,
            }
        )
    kml = tmp_path / "land.kml"
    kml.write_text(
        """<kml xmlns="http://www.opengis.net/kml/2.2"><Placemark><name>DK1</name>
<Polygon><outerBoundaryIs><LinearRing><coordinates>
113,35,0 113.001,35,0 113.001,35.001,0 113,35.001,0 113,35,0
</coordinates></LinearRing></outerBoundaryIs></Polygon></Placemark></kml>""",
        encoding="utf-8",
    )
    progress: list[int] = []
    control = TaskControl(lambda percent, _message: progress.append(percent))
    preview = analyze_photo_land_preview(
        photos,
        [str(kml)],
        20,
        task_control=control,
    )
    assert preview.matched == 100
    assert progress[-1] == 100
    assert progress == sorted(progress)

    cancelled_control = TaskControl()
    cancelled_control.cancel()
    try:
        analyze_photo_land_preview(
            photos,
            [str(kml)],
            20,
            task_control=cancelled_control,
        )
    except TaskCancelled:
        pass
    else:
        raise AssertionError("取消后的分析任务应立即停止")


def test_organizer_creates_all_numeric_land_folders_and_merges_duplicates(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    photo = source / "inside.jpg"
    Image.new("RGB", (20, 20), "white").save(photo)
    code = "410726203205000001"
    empty_code = "410726203205000002"
    kml = tmp_path / "lands.kml"
    kml.write_text(
        f"""<kml xmlns="http://www.opengis.net/kml/2.2"><Document>
<Placemark><name>一</name><ExtendedData><Data name="landcode"><value>{code}</value></Data></ExtendedData>
<Polygon><outerBoundaryIs><LinearRing><coordinates>
113,35,0 113.001,35,0 113.001,35.001,0 113,35.001,0 113,35,0
</coordinates></LinearRing></outerBoundaryIs></Polygon></Placemark>
<Placemark><name>重复面</name><ExtendedData><Data name="landcode"><value>{code}</value></Data></ExtendedData>
<Polygon><outerBoundaryIs><LinearRing><coordinates>
113.001,35,0 113.002,35,0 113.002,35.001,0 113.001,35.001,0 113.001,35,0
</coordinates></LinearRing></outerBoundaryIs></Polygon></Placemark>
<Placemark><name>空图斑</name><ExtendedData><Data name="landcode"><value>{empty_code}</value></Data></ExtendedData>
<Polygon><outerBoundaryIs><LinearRing><coordinates>
114,36,0 114.001,36,0 114.001,36.001,0 114,36.001,0 114,36,0
</coordinates></LinearRing></outerBoundaryIs></Polygon></Placemark>
</Document></kml>""",
        encoding="utf-8",
    )
    output = tmp_path / "output"
    summary = organize_photos_by_land(
        [
            {
                "filename": photo.name,
                "full_path": str(photo),
                "lat": 35.0005,
                "lon": 113.0015,
                "has_gps": True,
            }
        ],
        [str(kml)],
        str(output),
        transfer_mode="compatible",
    )
    assert summary.matched == 1
    assert sorted(path.name for path in output.iterdir() if path.is_dir()) == [
        code,
        empty_code,
    ]
    assert (output / code / photo.name).exists()
    assert not (output / f"{code}_1").exists()
    assert (output / empty_code).is_dir()
