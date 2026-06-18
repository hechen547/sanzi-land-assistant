from datetime import datetime

from sanzi_photo_tool.models.photo import PhotoInfo
from sanzi_photo_tool.services.rename_service import build_rename_plan


def test_build_rename_plan_orders_by_shot_time() -> None:
    photos = [
        PhotoInfo("b.JPG", "C:/photos/b.JPG", shot_time=datetime(2025, 1, 2)),
        PhotoInfo("a.JPG", "C:/photos/a.JPG", shot_time=datetime(2025, 1, 1)),
    ]
    plans = build_rename_plan(photos, prefix="A", start=1, digits=3)
    assert [plan.photo.filename for plan in plans] == ["a.JPG", "b.JPG"]
    assert [plan.new_filename for plan in plans] == ["A001.jpg", "A002.jpg"]

