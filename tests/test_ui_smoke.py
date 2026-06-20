import os
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QTWEBENGINE_DISABLE_SANDBOX", "1")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--no-sandbox --disable-gpu")

from PIL import Image
from PySide6.QtWidgets import QApplication, QPushButton

from sanzi_photo_tool.models.photo import PhotoInfo
from sanzi_photo_tool.ui.main_window import (
    LOGIN_STORAGE_SCRIPT,
    MainWindow,
    SanziLoginDialog,
    TaskProgressDialog,
    Worker,
    _process_output,
    platform_login_data,
    platform_login_data_from_headers,
)
from sanzi_photo_tool.services.task_control import TaskCancelled, TaskControl
from sanzi_photo_tool.services.watermark_service import WatermarkConfig


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

    assert window.stack.count() == 7
    assert window.watermark_page.preview_image.pixmap() is not None
    assert not window.watermark_page.preview_image.pixmap().isNull()
    window.photo_page.font_size.setValue(36)
    app.processEvents()
    assert window.photo_page.current_config().font_size == 36
    assert window.photo_page.current_config().font_path == ""
    assert window.photo_page.fast_watermark_radio.isChecked()
    assert window.photo_page.compatible_watermark_radio.text() == "兼容处理"
    assert window.photo_page._build_plans([photo])[0].new_filename == "A1.jpg"
    assert window.land_page.photos == []
    window.land_page._photo_scan_done([photo])
    assert window.land_page.photos == [photo]
    assert "有定位 1 张" in window.land_page.photo_source_label.text()
    window.navigation.setCurrentRow(3)
    app.processEvents()
    assert window.status_label.text() == "照片 1 张｜有定位 1 张｜无定位 0 张"
    assert window.route_page.html_path.name == "index.html"
    assert window.map_page.html_path.name == "gps_map.html"
    assert window.upload_page.login_status.text() == "尚未登录"
    shared_login = {
        "token": "test-token",
        "token_header": "Token",
        "districtcode": "410000000000",
        "districtname": "测试地区",
        "cookie": "sid=test",
    }
    window.download_page.login_captured.emit(shared_login)
    assert window.upload_page.login_data["token"] == "test-token"
    assert "测试地区" in window.upload_page.login_status.text()
    assert window.upload_page.login_button.text() == "平台已登录"
    assert window.upload_page.switch_account_button.isEnabled()
    assert window.upload_page.logout_button.isEnabled()
    window.upload_page._show_login_dialog()
    app.processEvents()
    assert window.upload_page._login_dialog is not None
    assert window.upload_page._login_dialog.isVisible()
    assert "fromTokenExpired=1" in SanziLoginDialog.LOGIN_URL
    window.upload_page._login_dialog.reject()
    app.processEvents()
    assert window.upload_page._login_dialog is None
    window.upload_page.login_check_finished({})
    assert "平台已登录" in window.upload_page.login_status.text()
    assert window.upload_page.login_button.text() == "平台已登录"
    request_login = {
        "token": "request-token",
        "token_header": "Token",
        "districtcode": "",
        "districtname": "",
        "cookie": "",
    }
    window.credential_interceptor.credentials_found.emit(request_login)
    app.processEvents()
    assert window.upload_page.login_data["token"] == "request-token"
    assert "平台已登录" in window.upload_page.login_status.text()
    assert window.sanzi_profile.property("platform_login_data")["token"] == "request-token"
    window.upload_page.reset_login_state()
    assert window.upload_page.login_data == {}
    assert window.upload_page.login_status.text() == "尚未登录"
    assert window.upload_page.login_button.text() == "打开登录页面"
    assert not window.upload_page.switch_account_button.isEnabled()
    assert not window.upload_page.logout_button.isEnabled()
    assert window.navigation.item(0).text() == "下载平台图斑"
    assert window.navigation.item(1).text() == "制作无人机航线"
    assert window.navigation.item(2).text() == "给照片加水印"
    assert window.navigation.item(3).text() == "按图斑整理照片"
    assert window.navigation.item(4).text() == "上传照片到平台"
    assert window.navigation.item(5).text() == "查看照片地图"
    assert window.navigation.item(6).text() == "使用说明"
    assert len(window.findChildren(QPushButton, "helpButton")) == 6
    assert window.download_page.export_script.startswith("JSON.stringify(")
    window.download_page._platform_loaded(True)
    assert window.download_page.reload_button.text() == "重新加载平台"
    window.download_page._platform_load_started()
    assert window.download_page.reload_button.text() == "正在加载…"
    assert not window.download_page.reload_button.isEnabled()
    window.download_page._manual_reload_requested = True
    window.download_page._platform_loaded(True)
    assert window.download_page.reload_button.text() == "重新加载平台"
    assert window.download_page.reload_button.isEnabled()
    assert "平台已重新加载" in window.download_page.status_label.text()
    window.download_page._export_finished(
        '{"ok":true,"featureCount":3,"village":"测试村","states":["已完成"]}'
    )
    assert "已读取 3 个图斑" in window.download_page.status_label.text()
    assert window.land_page.copy_radio.text() == "保留原照片（推荐）"
    assert window.land_page.copy_radio.isChecked()
    assert window.land_page.fast_transfer_radio.isChecked()
    assert window.land_page.compatible_transfer_radio.text() == "兼容整理"
    assert window.land_page.supplement_empty_check.isChecked()
    assert window.land_page.supplement_distance_spin.value() == 20
    assert window.land_page.supplement_distance_spin.isEnabled()
    assert "保持不变" in window.land_page.operation_help.text()
    window.land_page.move_radio.setChecked(True)
    assert window.land_page.move_radio.isChecked()
    assert window.land_page.fast_transfer_radio.isChecked()
    assert "从原文件夹中消失" in window.land_page.operation_help.text()
    assert not window.land_page.supplement_empty_check.isEnabled()
    assert not window.land_page.supplement_distance_spin.isEnabled()
    window.land_page.copy_radio.setChecked(True)
    window.land_page.compatible_transfer_radio.setChecked(True)
    assert window.land_page.copy_radio.isChecked()
    assert window.land_page.compatible_transfer_radio.isChecked()
    assert not window.land_page.fast_transfer_radio.isChecked()
    assert window.land_page.supplement_empty_check.isEnabled()
    assert window.land_page.supplement_distance_spin.isEnabled()
    window.state.set_photos([photo] * 501)
    assert window.photo_page.photo_table.rowCount() == 500
    assert window.photo_page.next_photo_page.isEnabled()
    assert len(window.photo_page.selected_photos()) == 501
    window.photo_page._change_photo_page(1)
    assert window.photo_page.photo_table.rowCount() == 1
    window.navigation.setCurrentRow(1)
    app.processEvents()
    assert window.route_page.loaded is True
    window.navigation.setCurrentRow(5)
    app.processEvents()
    assert window.map_page.loaded is True
    window.close()


def test_platform_login_data() -> None:
    assert "sessionStorage" in LOGIN_STORAGE_SCRIPT
    assert "tokenvalue" in LOGIN_STORAGE_SCRIPT
    assert platform_login_data(None) == {}
    result = platform_login_data(
        {
            "token": "abc",
            "tokenName": "Authorization",
            "districtCode": "410100000000",
            "districtName": "测试区",
            "cookie": "sid=1",
        }
    )
    assert result == {
        "token": "bearer abc",
        "token_header": "Authorization",
        "districtcode": "410100000000",
        "districtname": "测试区",
        "cookie": "sid=1",
    }
    assert platform_login_data(
        {
            "localToken": "new-local-token",
            "token": "old-session-token",
            "tokenName": "Token",
            "districtCode": "410100000000",
        }
    ) == {
        "token": "new-local-token",
        "token_header": "Token",
        "districtcode": "410100000000",
        "districtname": "",
        "cookie": "",
    }
    assert platform_login_data(
        '{"token":"abc123456789","tokenName":"Authorization"}'
    ) == {
        "token": "bearer abc123456789",
        "token_header": "Authorization",
        "districtcode": "",
        "districtname": "",
        "cookie": "",
    }
    assert platform_login_data_from_headers(
        {"Token": "abc123456789", "Cookie": "sid=1"}
    ) == {
        "token": "abc123456789",
        "token_header": "Token",
        "districtcode": "",
        "districtname": "",
        "cookie": "sid=1",
    }
    assert platform_login_data_from_headers(
        {"Authorization": "Bearer abc123456789"}
    )["token_header"] == "Authorization"
    assert platform_login_data_from_headers(
        {"Authorization": "Basic c3lzUGVlcjpzeXNQZWVy"}
    ) == {}
    preferred = platform_login_data_from_headers(
        {
            "Authorization": "Bearer old-auth-token",
            "Token": "current-platform-token",
        }
    )
    assert preferred["token"] == "current-platform-token"
    assert preferred["token_header"] == "Token"


def test_upload_login_check_does_not_depend_on_download_page() -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    download_checks: list[bool] = []
    window.download_page.capture_login = lambda: download_checks.append(True)

    window._check_platform_login()
    app.processEvents()

    assert download_checks == []
    assert window.upload_page.login_status.text() == "尚未登录"
    window.close()


def test_worker_emits_result_before_finished() -> None:
    events: list[object] = []
    worker = Worker(lambda: "ok")
    worker.signals.result.connect(lambda value: events.append(("result", value)))
    worker.signals.finished.connect(lambda: events.append(("finished", None)))
    worker.run()
    assert events == [("result", "ok"), ("finished", None)]


def test_progress_dialog_supports_percent_and_cancel() -> None:
    app = QApplication.instance() or QApplication([])
    parent = MainWindow()
    dialog = TaskProgressDialog(
        parent,
        "正在整理照片…",
        cancellable=True,
        determinate=True,
    )
    cancelled: list[bool] = []
    dialog.cancel_requested.connect(lambda: cancelled.append(True))
    dialog.update_progress(42, "正在整理照片 42/100")
    assert dialog.progress.value() == 42
    assert dialog.percent_label.text() == "42%"
    assert dialog.description.text() == "正在整理照片 42/100"
    assert dialog.cancel_button is not None
    dialog.cancel_button.click()
    app.processEvents()
    assert cancelled == [True]
    assert not dialog.cancel_button.isEnabled()
    parent.close()


def test_watermark_output_reports_progress_and_can_cancel(tmp_path: Path) -> None:
    plans = []
    for index in range(8):
        source = tmp_path / f"source_{index}.jpg"
        Image.new("RGB", (120, 80), "white").save(source)
        photo = PhotoInfo(source.name, str(source))
        plans.append(
            SimpleNamespace(photo=photo, new_filename=f"A{index + 1}.jpg")
        )
    output = tmp_path / "output"
    progress: list[int] = []
    control = TaskControl()

    def update(percent: int, _message: str) -> None:
        progress.append(percent)
        if percent >= 15:
            control.cancel()

    control.set_progress_callback(update)
    try:
        _process_output(
            plans,
            str(output),
            WatermarkConfig(),
            "fast",
            task_control=control,
        )
    except TaskCancelled:
        pass
    else:
        raise AssertionError("水印任务应响应取消操作")
    assert progress
    assert 0 < len(list(output.glob("*.jpg"))) < len(plans)
