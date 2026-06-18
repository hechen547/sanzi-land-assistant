from datetime import datetime
from pathlib import Path

from PIL import Image

from sanzi_photo_tool.services.photo_scanner import read_photo_info


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

