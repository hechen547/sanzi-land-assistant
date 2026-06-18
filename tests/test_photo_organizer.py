from pathlib import Path

from PIL import Image

from sanzi_photo_tool.services.photo_organizer import organize_photos_by_land


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
    )

    assert summary.succeeded == 2
    assert summary.matched == 1
    assert summary.unmatched == 1
    assert summary.empty_lands == 1
    assert (output / "DK001" / "inside.jpg").exists()
    assert (output / "DK002").is_dir()
    assert (output / "未匹配图斑" / "outside.jpg").exists()
    assert (output / "无照片图斑.kml").exists()
    assert (output / "未匹配照片.kml").exists()
    assert (output / "整理结果.csv").exists()

