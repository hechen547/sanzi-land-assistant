from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET

from shapely.geometry import MultiPolygon, Polygon


@dataclass(slots=True)
class ParsedLand:
    geometry: Polygon | MultiPolygon
    name: str
    landcode: str
    source_file: str


def read_land_kml_files(kml_paths: list[str]) -> list[ParsedLand]:
    """手动解析 KML，避免依赖 GDAL/Fiona 的 KML 驱动。"""
    lands: list[ParsedLand] = []
    for raw_path in kml_paths:
        path = Path(raw_path)
        try:
            root = ET.parse(path).getroot()
        except (OSError, ET.ParseError):
            continue
        for placemark in _find_all(root, "Placemark"):
            polygons = _parse_placemark_polygons(placemark)
            if not polygons:
                continue
            geometry: Polygon | MultiPolygon
            geometry = polygons[0] if len(polygons) == 1 else MultiPolygon(polygons)
            if not geometry.is_valid:
                geometry = geometry.buffer(0)
            polygon_parts = _extract_polygon_parts(geometry)
            if not polygon_parts:
                continue
            geometry = polygon_parts[0] if len(polygon_parts) == 1 else MultiPolygon(polygon_parts)
            name = _child_text(placemark, "name").strip()
            extended = _read_extended_data(placemark)
            landcode = _find_landcode(extended)
            lands.append(
                ParsedLand(
                    geometry=geometry,
                    name=name,
                    landcode=landcode,
                    source_file=str(path.resolve()),
                )
            )
    if not lands:
        raise ValueError("没有可读取的KML图斑文件")
    return lands


def _parse_placemark_polygons(placemark: ET.Element) -> list[Polygon]:
    polygons: list[Polygon] = []
    for polygon_node in _find_all(placemark, "Polygon"):
        outer_node = _first_descendant(polygon_node, "outerBoundaryIs")
        if outer_node is None:
            continue
        outer = _read_boundary(outer_node)
        if len(outer) < 3:
            continue
        holes = [
            coordinates
            for inner_node in _find_all(polygon_node, "innerBoundaryIs")
            if len(coordinates := _read_boundary(inner_node)) >= 3
        ]
        polygon = Polygon(outer, holes)
        if not polygon.is_valid:
            polygon = polygon.buffer(0)
        polygons.extend(_extract_polygon_parts(polygon))
    return polygons


def _read_boundary(boundary_node: ET.Element) -> list[tuple[float, float]]:
    coordinates_node = _first_descendant(boundary_node, "coordinates")
    if coordinates_node is None or not coordinates_node.text:
        return []
    coordinates: list[tuple[float, float]] = []
    for chunk in coordinates_node.text.replace("\n", " ").replace("\t", " ").split():
        parts = chunk.split(",")
        if len(parts) < 2:
            continue
        try:
            coordinates.append((float(parts[0]), float(parts[1])))
        except ValueError:
            continue
    return coordinates


def _read_extended_data(placemark: ET.Element) -> dict[str, str]:
    values: dict[str, str] = {}
    for node in placemark.iter():
        local = _local_name(node.tag)
        if local == "Data":
            key = (node.attrib.get("name") or "").strip()
            value_node = _first_descendant(node, "value")
            value = (value_node.text or "").strip() if value_node is not None else ""
        elif local == "SimpleData":
            key = (node.attrib.get("name") or "").strip()
            value = (node.text or "").strip()
        else:
            continue
        if key and value:
            values[key] = value
    return values


def _find_landcode(values: dict[str, str]) -> str:
    normalized = {key.casefold().replace("_", ""): value for key, value in values.items()}
    for candidate in ("landcode", "dkbm", "tbbh", "code", "编号", "地块编号", "图斑编号"):
        value = normalized.get(candidate.casefold().replace("_", ""))
        if value:
            return value
    return ""


def _extract_polygon_parts(geometry: object) -> list[Polygon]:
    if isinstance(geometry, Polygon):
        return [geometry] if not geometry.is_empty else []
    if isinstance(geometry, MultiPolygon):
        return [part for part in geometry.geoms if not part.is_empty]
    geoms = getattr(geometry, "geoms", ())
    return [part for geom in geoms for part in _extract_polygon_parts(geom)]


def _find_all(node: ET.Element, local_name: str) -> list[ET.Element]:
    return [child for child in node.iter() if _local_name(child.tag) == local_name]


def _first_descendant(node: ET.Element, local_name: str) -> ET.Element | None:
    return next((child for child in node.iter() if _local_name(child.tag) == local_name), None)


def _child_text(node: ET.Element, local_name: str) -> str:
    child = next((item for item in node if _local_name(item.tag) == local_name), None)
    return child.text if child is not None and child.text else ""


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]

