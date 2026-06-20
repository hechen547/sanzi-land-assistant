from pathlib import Path

from sanzi_photo_tool.services.kml_parser import read_land_kml_files
from shapely.geometry import Polygon

from sanzi_photo_tool.services.kml_parser import repair_polygonal_geometry


def test_read_simple_kml(tmp_path: Path) -> None:
    kml = tmp_path / "lands.kml"
    kml.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <Placemark>
      <name>测试图斑</name>
      <ExtendedData><Data name="landcode"><value>DK001</value></Data></ExtendedData>
      <Polygon>
        <outerBoundaryIs><LinearRing><coordinates>
          113,35,0 113.01,35,0 113.01,35.01,0 113,35.01,0 113,35,0
        </coordinates></LinearRing></outerBoundaryIs>
      </Polygon>
    </Placemark>
  </Document>
</kml>""",
        encoding="utf-8",
    )
    lands = read_land_kml_files([str(kml)])
    assert len(lands) == 1
    assert lands[0].name == "测试图斑"
    assert lands[0].landcode == "DK001"
    assert lands[0].geometry.is_valid


def test_repair_self_intersecting_polygon_keeps_polygon_parts() -> None:
    bow_tie = Polygon([(0, 0), (2, 2), (0, 2), (2, 0), (0, 0)])
    repaired = repair_polygonal_geometry(bow_tie)
    assert repaired is not None
    assert repaired.is_valid
    assert repaired.geom_type in {"Polygon", "MultiPolygon"}
    assert repaired.area > 0
