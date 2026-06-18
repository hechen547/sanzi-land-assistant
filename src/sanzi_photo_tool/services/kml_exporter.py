from __future__ import annotations

from pathlib import Path
from xml.etree import ElementTree as ET

from shapely.geometry import MultiPolygon, Polygon

from ..models.land import LandRecord
from ..models.photo import PhotoInfo

KML_NS = "http://www.opengis.net/kml/2.2"
ET.register_namespace("", KML_NS)


def export_empty_lands_kml(lands: list[LandRecord], path: str | Path) -> None:
    root, document = _document("无照片图斑")
    style = ET.SubElement(document, _tag("Style"), id="emptyLand")
    line_style = ET.SubElement(style, _tag("LineStyle"))
    ET.SubElement(line_style, _tag("color")).text = "ff0000ff"
    ET.SubElement(line_style, _tag("width")).text = "2"
    poly_style = ET.SubElement(style, _tag("PolyStyle"))
    ET.SubElement(poly_style, _tag("color")).text = "7d0000ff"

    for land in lands:
        placemark = ET.SubElement(document, _tag("Placemark"))
        ET.SubElement(placemark, _tag("name")).text = land.name
        ET.SubElement(placemark, _tag("styleUrl")).text = "#emptyLand"
        _append_geometry(placemark, land.wgs_geom)
    _write(root, path)


def export_unmatched_photos_kml(photos: list[PhotoInfo], path: str | Path) -> None:
    root, document = _document("未匹配照片")
    for photo in photos:
        if photo.lat is None or photo.lon is None:
            continue
        placemark = ET.SubElement(document, _tag("Placemark"))
        ET.SubElement(placemark, _tag("name")).text = photo.filename
        point = ET.SubElement(placemark, _tag("Point"))
        ET.SubElement(point, _tag("coordinates")).text = f"{photo.lon:.8f},{photo.lat:.8f},0"
    _write(root, path)


def _append_geometry(parent: ET.Element, geometry: Polygon | MultiPolygon) -> None:
    if isinstance(geometry, MultiPolygon):
        container = ET.SubElement(parent, _tag("MultiGeometry"))
        for polygon in geometry.geoms:
            _append_polygon(container, polygon)
    else:
        _append_polygon(parent, geometry)


def _append_polygon(parent: ET.Element, polygon: Polygon) -> None:
    node = ET.SubElement(parent, _tag("Polygon"))
    outer = ET.SubElement(node, _tag("outerBoundaryIs"))
    ring = ET.SubElement(outer, _tag("LinearRing"))
    ET.SubElement(ring, _tag("coordinates")).text = _coordinates_text(polygon.exterior.coords)
    for interior in polygon.interiors:
        inner = ET.SubElement(node, _tag("innerBoundaryIs"))
        ring = ET.SubElement(inner, _tag("LinearRing"))
        ET.SubElement(ring, _tag("coordinates")).text = _coordinates_text(interior.coords)


def _coordinates_text(coordinates: object) -> str:
    return " ".join(f"{lon:.8f},{lat:.8f},0" for lon, lat, *_ in coordinates)


def _document(name: str) -> tuple[ET.Element, ET.Element]:
    root = ET.Element(_tag("kml"))
    document = ET.SubElement(root, _tag("Document"))
    ET.SubElement(document, _tag("name")).text = name
    return root, document


def _write(root: ET.Element, path: str | Path) -> None:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(root).write(output, encoding="utf-8", xml_declaration=True)


def _tag(name: str) -> str:
    return f"{{{KML_NS}}}{name}"

