from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET

from shapely import make_valid
from shapely.geometry import MultiPolygon, Polygon
from shapely.ops import unary_union


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
            geometry = repair_polygonal_geometry(
                polygons[0] if len(polygons) == 1 else MultiPolygon(polygons)
            )
            if geometry is None:
                continue
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
        repaired = repair_polygonal_geometry(Polygon(outer, holes))
        if repaired is not None:
            polygons.extend(_extract_polygon_parts(repaired))
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


def repair_polygonal_geometry(
    geometry: object,
) -> Polygon | MultiPolygon | None:
    """修复图斑并只保留有效面，兼容自交面和 GeometryCollection。"""
    if geometry is None or getattr(geometry, "is_empty", True):
        return None
    fixed = geometry
    if not getattr(fixed, "is_valid", False):
        try:
            fixed = make_valid(fixed)
        except Exception:
            fixed = fixed.buffer(0)
    parts = _extract_polygon_parts(fixed)
    if not parts:
        return None
    merged = unary_union(parts)
    if not merged.is_valid:
        try:
            merged = make_valid(merged)
        except Exception:
            merged = merged.buffer(0)
    final_parts = [
        part
        for part in _extract_polygon_parts(merged)
        if not part.is_empty and part.area > 0
    ]
    if not final_parts:
        return None
    result: Polygon | MultiPolygon
    result = final_parts[0] if len(final_parts) == 1 else MultiPolygon(final_parts)
    return result if result.is_valid else None


def _find_all(node: ET.Element, local_name: str) -> list[ET.Element]:
    return [child for child in node.iter() if _local_name(child.tag) == local_name]


def _first_descendant(node: ET.Element, local_name: str) -> ET.Element | None:
    return next((child for child in node.iter() if _local_name(child.tag) == local_name), None)


def _child_text(node: ET.Element, local_name: str) -> str:
    child = next((item for item in node if _local_name(item.tag) == local_name), None)
    return child.text if child is not None and child.text else ""


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]
