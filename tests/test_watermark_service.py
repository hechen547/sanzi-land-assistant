from datetime import datetime
from pathlib import Path

from PIL import Image

from sanzi_photo_tool.models.photo import PhotoInfo
from sanzi_photo_tool.services.watermark_service import WatermarkConfig, apply_watermark


def test_apply_watermark_creates_new_image(tmp_path: Path) -> None:
    source = tmp_path / "source.jpg"
    Image.new("RGB", (640, 480), "#336699").save(source)
    photo = PhotoInfo(
        source.name,
        str(source),
        has_gps=True,
        lat=35.123456,
        lon=113.654321,
        shot_time=datetime(2026, 6, 18, 10, 30),
    )
    output = tmp_path / "output"
    destination = apply_watermark(photo, output, WatermarkConfig(font_size=28))

    assert source.exists()
    assert destination.exists()
    with Image.open(destination) as image:
        assert image.size == (640, 480)

