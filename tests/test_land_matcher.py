from sanzi_photo_tool.models.photo import PhotoInfo
from sanzi_photo_tool.services.kml_parser import ParsedLand
from sanzi_photo_tool.services.land_matcher import (
    build_land_records,
    estimate_local_utm_epsg,
    match_photos_to_lands,
    safe_folder_name,
    utm_projector,
)
from shapely.geometry import Polygon


def test_point_inside_land_is_matched() -> None:
    parsed = [
        ParsedLand(
            geometry=Polygon([(113, 35), (113.01, 35), (113.01, 35.01), (113, 35.01)]),
            name="地块一",
            landcode="DK001",
            source_file="test.kml",
        )
    ]
    lands = build_land_records(parsed)
    photo = PhotoInfo("a.jpg", __file__, True, 35.005, 113.005)
    matches = match_photos_to_lands([photo], lands)
    assert matches[0].land is lands[0]
    assert matches[0].direct_hit is True


def test_nearby_point_uses_distance_fallback() -> None:
    parsed = [
        ParsedLand(
            geometry=Polygon([(113, 35), (113.01, 35), (113.01, 35.01), (113, 35.01)]),
            name="地块一",
            landcode="DK001",
            source_file="test.kml",
        )
    ]
    lands = build_land_records(parsed)
    photo = PhotoInfo("a.jpg", __file__, True, 35.005, 113.01005)
    assert match_photos_to_lands([photo], lands, 0)[0].land is None
    nearby = match_photos_to_lands([photo], lands, 10)[0]
    assert nearby.land is lands[0]
    assert nearby.direct_hit is False
    assert 0 < nearby.distance_m <= 10


def test_safe_folder_name_handles_windows_reserved_name() -> None:
    assert safe_folder_name("CON") == "_CON"
    assert safe_folder_name('地块<01>:"') == "地块_01___"


def test_henan_lands_use_local_utm_zone() -> None:
    geometry = Polygon([(113, 35), (113.01, 35), (113.01, 35.01), (113, 35.01)])
    assert estimate_local_utm_epsg([geometry]) == 32649
    lands = build_land_records(
        [ParsedLand(geometry, "地块一", "DK001", "test.kml")]
    )
    assert lands[0].metric_epsg == 32649


def test_pure_python_utm_matches_reference_coordinates() -> None:
    project = utm_projector(32649)
    easting, northing = project(113, 35)
    assert abs(easting - 682516.0936154537) < 0.01
    assert abs(northing - 3874870.634730801) < 0.01


def test_duplicate_landcodes_are_merged_into_one_record() -> None:
    code = "410726203205000001"
    records = build_land_records(
        [
            ParsedLand(
                Polygon([(113, 35), (113.01, 35), (113.01, 35.01), (113, 35.01)]),
                "一",
                code,
                "a.kml",
            ),
            ParsedLand(
                Polygon([(113.01, 35), (113.02, 35), (113.02, 35.01), (113.01, 35.01)]),
                "二",
                code,
                "b.kml",
            ),
        ]
    )
    assert len(records) == 1
    assert records[0].folder == code
    assert records[0].wgs_geom.area > 0
