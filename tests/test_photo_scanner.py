from datetime import datetime
import json
from pathlib import Path

from PIL import Image

from sanzi_photo_tool.services import photo_scanner
from sanzi_photo_tool.services.photo_scanner import read_photo_info, scan_photos


def test_read_photo_exif_time_and_gps(tmp_path: Path) -> None:
    path = tmp_path / "gps.jpg"
    exif = Image.Exif()
    exif[36867] = "2026:06:18 12:34:56"
    exif[34853] = {
        1: "N",
        2: (35.0, 7.0, 24.0),
        3: "E",
        4: (113.0, 27.0, 21.6),
    }
    Image.new("RGB", (100, 80), "white").save(path, exif=exif)

    photo = read_photo_info(path)

    assert photo.error == ""
    assert photo.has_gps is True
    assert photo.lat == 35.123333333333335
    assert photo.lon == 113.456
    assert photo.shot_time == datetime(2026, 6, 18, 12, 34, 56)
    assert (photo.width, photo.height) == (100, 80)


def test_read_photo_xmp_gps_and_time_when_exif_is_missing(tmp_path: Path) -> None:
    path = tmp_path / "xmp.jpg"
    xmp = b"""<?xpacket begin=""?>
<x:xmpmeta xmlns:x="adobe:ns:meta/">
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
<rdf:Description xmlns:drone-dji="http://www.dji.com/drone-dji/1.0/"
 xmlns:exif="http://ns.adobe.com/exif/1.0/"
 drone-dji:GpsLatitude="+35.123456"
 drone-dji:GpsLongitude="+113.654321"
 exif:DateTimeOriginal="2026-06-19T08:09:10+08:00"/>
</rdf:RDF></x:xmpmeta>"""
    Image.new("RGB", (60, 40), "white").save(path, xmp=xmp)

    photo = read_photo_info(path)

    assert photo.error == ""
    assert photo.has_gps is True
    assert photo.lat == 35.123456
    assert photo.lon == 113.654321
    assert photo.shot_time == datetime(2026, 6, 19, 8, 9, 10)


def test_read_photo_xmp_degree_minute_coordinates(tmp_path: Path) -> None:
    path = tmp_path / "xmp-dms.jpg"
    xmp = b"""<x:xmpmeta xmlns:x="adobe:ns:meta/">
<exif:GPSLatitude xmlns:exif="http://ns.adobe.com/exif/1.0/">35,7.4N</exif:GPSLatitude>
<exif:GPSLongitude xmlns:exif="http://ns.adobe.com/exif/1.0/">113,27.36E</exif:GPSLongitude>
</x:xmpmeta>"""
    Image.new("RGB", (60, 40), "white").save(path, xmp=xmp)

    photo = read_photo_info(path)

    assert photo.has_gps is True
    assert round(photo.lat or 0, 6) == 35.123333
    assert round(photo.lon or 0, 6) == 113.456


def test_scan_photos_uses_cache_and_invalidates_changed_file(
    tmp_path: Path,
    monkeypatch,
) -> None:
    photo_path = tmp_path / "cached.jpg"
    cache_path = tmp_path / "photo-cache.json"
    Image.new("RGB", (20, 10), "white").save(photo_path)

    first = scan_photos(tmp_path, cache_path=cache_path)
    assert first[0].width == 20
    assert cache_path.exists()
    cache_data = json.loads(cache_path.read_text(encoding="utf-8"))
    assert len(cache_data["entries"]) == 1

    calls = 0
    original = photo_scanner.read_photo_info

    def tracked(path):
        nonlocal calls
        calls += 1
        return original(path)

    monkeypatch.setattr(photo_scanner, "read_photo_info", tracked)
    cached = scan_photos(tmp_path, cache_path=cache_path)
    assert cached[0].width == 20
    assert calls == 0

    Image.new("RGB", (30, 15), "black").save(photo_path)
    changed = scan_photos(tmp_path, cache_path=cache_path)
    assert changed[0].width == 30
    assert calls == 1
