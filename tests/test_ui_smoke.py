import os
from datetime import datetime
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--no-sandbox --disable-gpu")

from PIL import Image
from PySide6.QtWidgets import QApplication

from sanzi_photo_tool.models.photo import PhotoInfo
from sanzi_photo_tool.ui.main_window import MainWindow


def test_main_window_and_watermark_preview(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    path = tmp_path / "preview.jpg"
    Image.new("RGB", (600, 400), "#668855").save(path)
    photo = PhotoInfo(
        path.name,
        str(path),
        True,
        35.123456,
        113.654321,
        datetime(2026, 6, 18, 10, 30),
    )

    window = MainWindow()
    window.state.set_photos([photo])
    window.navigation.setCurrentRow(2)
    window.watermark_page.refresh_preview()
    app.processEvents()

    assert window.stack.count() == 6
    assert window.watermark_page.preview_image.pixmap() is not None
    assert not window.watermark_page.preview_image.pixmap().isNull()
    window.photo_page.font_size.setValue(36)
    app.processEvents()
    assert window.photo_page.current_config().font_size == 36
    assert window.photo_page.current_config().font_path == ""
    assert window.photo_page._build_plans([photo])[0].new_filename == "A1.jpg"
    assert window.land_page.photo_provider() == [photo]
    assert window.route_page.html_path.name == "index.html"
    assert window.map_page.html_path.name == "gps_map.html"
    assert window.upload_page.login_status.text() == "尚未登录"
    assert window.navigation.item(0).text() == "图斑下载"
    assert window.download_page.export_script
    assert window.land_page.copy_radio.text() == "保留原照片（推荐）"
    assert "保持不变" in window.land_page.operation_help.text()
    window.land_page.move_radio.setChecked(True)
    assert "从原文件夹中消失" in window.land_page.operation_help.text()
    window.navigation.setCurrentRow(1)
    app.processEvents()
    assert window.route_page.loaded is True
    window.navigation.setCurrentRow(5)
    app.processEvents()
    assert window.map_page.loaded is True
    window.close()
