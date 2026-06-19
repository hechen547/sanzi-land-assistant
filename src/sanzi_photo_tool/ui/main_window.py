from __future__ import annotations

import json
import shutil
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from PIL import Image, ImageQt
from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QPixmap
from PySide6.QtWebEngineCore import (
    QWebEnginePage,
    QWebEngineProfile,
    QWebEngineSettings,
    QWebEngineUrlRequestInfo,
    QWebEngineUrlRequestInterceptor,
)
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QColorDialog,
    QDoubleSpinBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..models.photo import PhotoInfo
from ..runtime import APP_NAME, application_resource, package_resource
from ..services.photo_organizer import (
    analyze_photo_land_matches,
    organize_photos_by_land,
    prepare_writable_output,
)
from ..services.photo_scanner import scan_photos
from ..services.rename_service import build_rename_plan, unique_destination
from ..services.sanzi_upload import (
    UploadOptions,
    UploadResult,
    read_upload_landcodes,
    run_upload,
    scan_upload_groups,
    validate_upload_groups,
    write_upload_log,
)
from ..services.watermark_service import (
    WatermarkConfig,
    apply_watermark,
    prepare_preview_image,
    render_watermark_on_preview,
)


class AppState(QObject):
    photos_changed = Signal()
    log_added = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.photos: list[PhotoInfo] = []

    def set_photos(self, photos: list[PhotoInfo]) -> None:
        self.photos = photos
        self.photos_changed.emit()

    def log(self, message: str) -> None:
        self.log_added.emit(message)


class WorkerSignals(QObject):
    result = Signal(object)
    error = Signal(str)
    finished = Signal()


class Worker(QRunnable):
    def __init__(self, function: Callable, *args) -> None:
        super().__init__()
        self.function = function
        self.args = args
        self.signals = WorkerSignals()

    def run(self) -> None:
        try:
            result = self.function(*self.args)
        except Exception as exc:
            self.signals.finished.emit()
            self.signals.error.emit(str(exc))
        else:
            self.signals.finished.emit()
            self.signals.result.emit(result)


class AppMessageDialog(QDialog):
    """统一的软件提示弹窗，主文案面向普通用户，技术详情默认收起。"""

    ICONS = {
        "info": ("i", "#1769e0", "#eaf2ff"),
        "success": ("✓", "#13835e", "#e8f7f1"),
        "warning": ("!", "#a96300", "#fff3df"),
        "error": ("×", "#c0392b", "#fdeceb"),
        "question": ("?", "#1769e0", "#eaf2ff"),
        "help": ("?", "#1769e0", "#eaf2ff"),
    }

    def __init__(
        self,
        parent: QWidget | None,
        title: str,
        message: str,
        kind: str = "info",
        details: str = "",
        confirm: bool = False,
        confirm_text: str = "确定",
    ) -> None:
        super().__init__(parent)
        self.setObjectName("appMessageDialog")
        self.setWindowTitle(title)
        self.setModal(True)
        self.setMinimumWidth(440)
        self.setMaximumWidth(620)

        root = QVBoxLayout(self)
        root.setContentsMargins(24, 22, 24, 20)
        root.setSpacing(16)
        content = QHBoxLayout()
        content.setSpacing(16)
        symbol, color, background = self.ICONS.get(kind, self.ICONS["info"])
        icon = QLabel(symbol)
        icon.setObjectName("dialogIcon")
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setStyleSheet(
            f"background:{background};color:{color};border-radius:22px;"
            "font-size:24px;font-weight:700;min-width:44px;max-width:44px;"
            "min-height:44px;max-height:44px;"
        )
        content.addWidget(icon, 0, Qt.AlignmentFlag.AlignTop)
        text_layout = QVBoxLayout()
        text_layout.setSpacing(7)
        heading = QLabel(title)
        heading.setObjectName("dialogTitle")
        body = QLabel(message)
        body.setObjectName("dialogMessage")
        body.setWordWrap(True)
        body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        text_layout.addWidget(heading)
        text_layout.addWidget(body)
        content.addLayout(text_layout, 1)
        root.addLayout(content)

        if details:
            details_button = QPushButton("查看详细信息")
            details_button.setObjectName("linkButton")
            details_view = QPlainTextEdit(details)
            details_view.setObjectName("dialogDetails")
            details_view.setReadOnly(True)
            details_view.setMaximumHeight(120)
            details_view.hide()
            details_button.clicked.connect(
                lambda: _toggle_details(details_button, details_view)
            )
            root.addWidget(details_button, 0, Qt.AlignmentFlag.AlignLeft)
            root.addWidget(details_view)

        actions = QHBoxLayout()
        actions.addStretch()
        if confirm:
            cancel = QPushButton("返回")
            cancel.clicked.connect(self.reject)
            actions.addWidget(cancel)
        accept = QPushButton(confirm_text)
        accept.setObjectName("primaryButton")
        accept.clicked.connect(self.accept)
        actions.addWidget(accept)
        root.addLayout(actions)


class TaskProgressDialog(QDialog):
    """任务运行时的统一等待窗口。"""

    def __init__(self, parent: QWidget, title: str) -> None:
        super().__init__(parent)
        self.setObjectName("taskProgressDialog")
        self.setWindowTitle("正在处理")
        self.setWindowModality(Qt.WindowModality.ApplicationModal)
        self.setFixedWidth(420)
        root = QVBoxLayout(self)
        root.setContentsMargins(26, 24, 26, 24)
        root.setSpacing(12)
        heading = QLabel(title.rstrip("…"))
        heading.setObjectName("dialogTitle")
        description = QLabel("请稍候，完成后软件会自动显示结果。")
        description.setObjectName("dialogMessage")
        self.progress = QProgressBar()
        self.progress.setObjectName("taskProgressBar")
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        root.addWidget(heading)
        root.addWidget(description)
        root.addWidget(self.progress)

    def closeEvent(self, event) -> None:
        if self.parent() and getattr(self.parent(), "active_tasks", 0):
            event.ignore()
            return
        super().closeEvent(event)


def _toggle_details(button: QPushButton, view: QPlainTextEdit) -> None:
    visible = not view.isVisible()
    view.setVisible(visible)
    button.setText("收起详细信息" if visible else "查看详细信息")


def show_info(parent: QWidget | None, title: str, message: str, details: str = "") -> None:
    AppMessageDialog(parent, title, message, "info", details).exec()


def show_success(parent: QWidget | None, title: str, message: str, details: str = "") -> None:
    AppMessageDialog(parent, title, message, "success", details).exec()


def show_warning(parent: QWidget | None, title: str, message: str, details: str = "") -> None:
    AppMessageDialog(parent, title, message, "warning", details).exec()


def show_error(parent: QWidget | None, title: str, message: str, details: str = "") -> None:
    AppMessageDialog(parent, title, message, "error", details).exec()


def ask_confirm(
    parent: QWidget | None,
    title: str,
    message: str,
    confirm_text: str = "继续",
) -> bool:
    return (
        AppMessageDialog(
            parent,
            title,
            message,
            "question",
            confirm=True,
            confirm_text=confirm_text,
        ).exec()
        == QDialog.DialogCode.Accepted
    )


def show_help(parent: QWidget, title: str, steps: list[str]) -> None:
    lines = "<br>".join(
        f"<b>{index}.</b>&nbsp; {step}" for index, step in enumerate(steps, start=1)
    )
    AppMessageDialog(parent, title, lines, "help").exec()


def help_button(parent: QWidget, title: str, steps: list[str]) -> QPushButton:
    button = QPushButton("使用说明")
    button.setObjectName("helpButton")
    button.clicked.connect(lambda: show_help(parent, title, steps))
    return button


def friendly_error_message(raw_message: str) -> tuple[str, str]:
    lower = raw_message.casefold()
    simple_messages = {
        "请选择输出目录": "请先选择整理结果要保存到哪里。",
        "输出目录不可写": "这个保存位置无法写入文件，请换一个文件夹。",
        "没有可读取的kml图斑文件": "没有读到可用的图斑，请重新选择平台下载的 KML 文件。",
        "匹配距离不能小于0": "“图斑外允许距离”不能小于 0 米。",
        "请选择有效的照片根目录": "请选择一个确实存在的照片文件夹。",
        "请先登录三资平台并获取登录信息": "请先打开登录页面并完成登录。",
        "照片目录中没有找到包含12位以上图斑编号的子文件夹": (
            "所选位置里没有找到按图斑编号命名的照片文件夹。"
            "请先使用“按图斑整理照片”，再选择它生成的结果文件夹。"
        ),
    }
    for keyword, message in simple_messages.items():
        if keyword in lower:
            return (message, raw_message)
    if any(word in lower for word in ("timeout", "timed out", "connecttimeout")):
        return (
            "暂时无法连接平台。请检查网络，或稍后再试。",
            raw_message,
        )
    if any(word in lower for word in ("connection refused", "网络连接失败", "urlopen")):
        return ("平台当前无法访问，请检查网络后重试。", raw_message)
    if "token" in lower or "未授权" in raw_message or "401" in raw_message:
        return ("登录状态已经失效，请重新登录平台。", raw_message)
    if "landcode" in lower and ("空" in raw_message or "missing" in lower):
        return ("没有识别到图斑编号，请检查文件夹名称。", raw_message)
    return ("操作没有完成，请根据提示检查后再试。", raw_message)


class PhotoWorkspacePage(QWidget):
    """照片选择、水印、命名和输出的一体化工作台。"""

    PHOTO_PAGE_SIZE = 500

    def __init__(self, state: AppState, run_task: Callable) -> None:
        super().__init__()
        self.setObjectName("workspacePage")
        self.state = state
        self.run_task = run_task
        self._preview_pixmap = QPixmap()
        self._preview_cache: OrderedDict[str, Image.Image] = OrderedDict()
        self._selected_rows: set[int] = set()
        self._photo_page = 0
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.setInterval(80)
        self._preview_timer.timeout.connect(self.refresh_preview)

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(12)
        root.addWidget(self._build_header())

        self.workspace_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.workspace_splitter.setChildrenCollapsible(False)
        self.workspace_splitter.addWidget(self._build_source_panel())
        self.workspace_splitter.addWidget(self._build_preview_panel())
        self.workspace_splitter.addWidget(self._build_settings_panel())
        self.workspace_splitter.setSizes([310, 620, 360])
        self.workspace_splitter.setStretchFactor(0, 0)
        self.workspace_splitter.setStretchFactor(1, 1)
        self.workspace_splitter.setStretchFactor(2, 0)
        root.addWidget(self.workspace_splitter, 1)
        root.addWidget(self._build_action_bar())

        state.photos_changed.connect(self.refresh_photos)
        self._connect_preview_signals()
        self.refresh_photos()

    def _build_header(self) -> QWidget:
        widget = QWidget()
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        title = QVBoxLayout()
        title.setSpacing(2)
        heading = QLabel("给照片加水印")
        heading.setObjectName("pageTitle")
        subtitle = QLabel("选择照片、查看水印效果，然后生成一份新照片")
        subtitle.setObjectName("pageSubtitle")
        title.addWidget(heading)
        title.addWidget(subtitle)
        layout.addLayout(title)
        layout.addStretch()
        layout.addWidget(
            help_button(
                self,
                "给照片加水印 · 使用说明",
                [
                    "选择存放照片的文件夹，然后点击“读取照片”。",
                    "在右侧选择要显示的内容，中间会立即显示水印效果。",
                    "SSD 使用“快速处理”；老旧电脑、机械硬盘或 U 盘使用“兼容处理”。",
                    "选择新照片的保存位置。",
                    "点击“生成新照片”。原照片不会被修改。",
                ],
            )
        )
        layout.addWidget(_step_badge("1", "选择照片", True))
        layout.addWidget(_step_line())
        layout.addWidget(_step_badge("2", "设置效果", True))
        layout.addWidget(_step_line())
        layout.addWidget(_step_badge("3", "确认输出", True))
        return widget

    def _build_source_panel(self) -> QWidget:
        panel, body = _panel("照片来源")
        self.photo_count_badge = QLabel("0 张")
        self.photo_count_badge.setObjectName("countBadge")
        body.header_layout.insertWidget(1, self.photo_count_badge)
        clear = QPushButton("清空")
        clear.setObjectName("linkButton")
        clear.clicked.connect(lambda: self.state.set_photos([]))
        body.header_layout.addStretch()
        body.header_layout.addWidget(clear)

        source_box = QWidget()
        source_layout = QVBoxLayout(source_box)
        source_layout.setContentsMargins(12, 10, 12, 10)
        source_layout.setSpacing(8)
        path_row = QHBoxLayout()
        self.source_edit = QLineEdit()
        self.source_edit.setPlaceholderText("选择照片目录")
        choose = QPushButton("选择目录")
        choose.clicked.connect(self.choose_source)
        path_row.addWidget(self.source_edit, 1)
        path_row.addWidget(choose)
        source_layout.addLayout(path_row)
        option_row = QHBoxLayout()
        self.recursive_check = QCheckBox("包含子文件夹")
        self.recursive_check.setChecked(True)
        scan = QPushButton("读取照片")
        scan.setObjectName("secondaryButton")
        scan.clicked.connect(self.scan_photos)
        option_row.addWidget(self.recursive_check)
        option_row.addStretch()
        option_row.addWidget(scan)
        source_layout.addLayout(option_row)
        body.layout.addWidget(source_box)

        self.photo_table = QTableWidget(0, 2)
        self.photo_table.setHorizontalHeaderLabels(["", "照片"])
        self.photo_table.verticalHeader().setVisible(False)
        self.photo_table.horizontalHeader().setVisible(False)
        self.photo_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.photo_table.horizontalHeader().resizeSection(0, 34)
        self.photo_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.photo_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.photo_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.photo_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.photo_table.setShowGrid(False)
        self.photo_table.verticalHeader().setDefaultSectionSize(48)
        self.photo_table.itemSelectionChanged.connect(self.schedule_preview)
        self.photo_table.itemChanged.connect(self._selection_changed)
        body.layout.addWidget(self.photo_table, 1)

        pager = QWidget()
        pager_layout = QHBoxLayout(pager)
        pager_layout.setContentsMargins(10, 4, 10, 4)
        self.previous_photo_page = QPushButton("上一页")
        self.next_photo_page = QPushButton("下一页")
        self.photo_page_label = QLabel("第 0 / 0 页")
        self.photo_page_label.setObjectName("mutedLabel")
        self.previous_photo_page.clicked.connect(lambda: self._change_photo_page(-1))
        self.next_photo_page.clicked.connect(lambda: self._change_photo_page(1))
        pager_layout.addWidget(self.previous_photo_page)
        pager_layout.addWidget(self.photo_page_label)
        pager_layout.addWidget(self.next_photo_page)
        body.layout.addWidget(pager)

        self.source_summary = QLabel("尚未选择照片")
        self.source_summary.setObjectName("panelFooter")
        body.layout.addWidget(self.source_summary)
        panel.setMinimumWidth(270)
        return panel

    def _build_preview_panel(self) -> QWidget:
        panel, body = _panel("效果预览")
        body.header_layout.addStretch()
        self.original_button = QPushButton("原图")
        self.processed_button = QPushButton("水印效果")
        self.original_button.setCheckable(True)
        self.processed_button.setCheckable(True)
        self.processed_button.setChecked(True)
        self.original_button.setObjectName("segmentedButton")
        self.processed_button.setObjectName("segmentedButton")
        self.original_button.clicked.connect(lambda: self._set_preview_mode(False))
        self.processed_button.clicked.connect(lambda: self._set_preview_mode(True))
        body.header_layout.addWidget(self.original_button)
        body.header_layout.addWidget(self.processed_button)

        self.preview_label = QLabel("读取照片后，这里会显示水印效果")
        self.preview_image = self.preview_label
        self.preview_label.setObjectName("photoCanvas")
        self.preview_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_label.setMinimumSize(430, 330)
        self.preview_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        body.layout.addWidget(self.preview_label, 1)

        preview_footer = QWidget()
        footer_layout = QHBoxLayout(preview_footer)
        footer_layout.setContentsMargins(12, 7, 12, 7)
        self.preview_filename = QLabel("未选择照片")
        self.preview_filename.setObjectName("mutedLabel")
        footer_layout.addWidget(self.preview_filename)
        footer_layout.addStretch()
        refresh = QPushButton("更新效果")
        refresh.clicked.connect(self.refresh_preview)
        footer_layout.addWidget(refresh)
        body.layout.addWidget(preview_footer)
        return panel

    def _build_settings_panel(self) -> QWidget:
        panel, body = _panel("照片设置")
        reset = QPushButton("恢复默认")
        reset.setObjectName("linkButton")
        reset.clicked.connect(self.reset_watermark)
        body.header_layout.addStretch()
        body.header_layout.addWidget(reset)

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(13, 8, 13, 12)
        content_layout.setSpacing(8)
        content_layout.addWidget(self._build_watermark_group())
        content_layout.addWidget(self._build_naming_group())
        content_layout.addWidget(self._build_output_group())
        content_layout.addWidget(self._build_processing_group())
        content_layout.addStretch()

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        scroll.setWidget(content)
        body.layout.addWidget(scroll, 1)
        panel.setMinimumWidth(330)
        panel.setMaximumWidth(470)
        return panel

    def _build_watermark_group(self) -> QGroupBox:
        group = QGroupBox("1  水印内容")
        group.setCheckable(True)
        group.setChecked(True)
        group.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.watermark_group = group
        form = QFormLayout(group)
        form.setContentsMargins(10, 12, 10, 10)
        form.setSpacing(8)

        checks = QWidget()
        checks_layout = QGridLayout(checks)
        checks_layout.setContentsMargins(0, 0, 0, 0)
        checks_layout.setHorizontalSpacing(8)
        self.title_enabled = QCheckBox("标题")
        self.latitude_enabled = QCheckBox("纬度")
        self.longitude_enabled = QCheckBox("经度")
        self.time_enabled = QCheckBox("时间")
        for index, checkbox in enumerate(
            (self.title_enabled, self.latitude_enabled, self.longitude_enabled, self.time_enabled)
        ):
            checkbox.setChecked(True)
            checks_layout.addWidget(checkbox, index // 2, index % 2)
        form.addRow("显示内容", checks)

        self.title_edit = QLineEdit("现场照片")
        form.addRow("标题文字", self.title_edit)

        self.custom_text = QPlainTextEdit()
        self.custom_text.setMaximumHeight(58)
        self.custom_text.setPlaceholderText("自定义文字，每行一条")
        form.addRow("附加文字", self.custom_text)

        self.font_size = QSpinBox()
        self.font_size.setRange(10, 300)
        self.font_size.setValue(48)
        self.font_size.setSuffix(" px")
        form.addRow("字号", self.font_size)

        self.font_color = QLineEdit("#FFFFFF")
        self.stroke_color = QLineEdit("#000000")
        color_row = QWidget()
        color_layout = QHBoxLayout(color_row)
        color_layout.setContentsMargins(0, 0, 0, 0)
        color_layout.addWidget(_color_button(self, self.font_color))
        color_layout.addWidget(_color_button(self, self.stroke_color))
        color_layout.addStretch()
        form.addRow("文字 / 边框颜色", color_row)

        self.stroke_width = QSpinBox()
        self.stroke_width.setRange(0, 20)
        self.stroke_width.setValue(2)
        self.opacity = QSpinBox()
        self.opacity.setRange(0, 255)
        self.opacity.setValue(255)
        form.addRow("边框粗细 / 透明度", _two_fields(self.stroke_width, self.opacity))
        self.left_margin = QSpinBox()
        self.left_margin.setRange(0, 1000)
        self.left_margin.setValue(40)
        self.bottom_margin = QSpinBox()
        self.bottom_margin.setRange(0, 1000)
        self.bottom_margin.setValue(40)
        form.addRow("离左边 / 离下边", _two_fields(self.left_margin, self.bottom_margin))
        return group

    def _build_naming_group(self) -> QGroupBox:
        group = QGroupBox("2  照片编号")
        group.setCheckable(True)
        group.setChecked(True)
        group.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.naming_group = group
        layout = QVBoxLayout(group)
        layout.setContentsMargins(10, 12, 10, 10)
        layout.setSpacing(8)
        rule = QLabel("按拍摄时间排序，从 A1 开始连续编号")
        rule.setObjectName("mutedLabel")
        rule.setWordWrap(True)
        layout.addWidget(rule)
        self.naming_preview = QLabel("命名预览：A1.jpg、A2.jpg …")
        self.naming_preview.setObjectName("previewHint")
        self.naming_preview.setWordWrap(True)
        layout.addWidget(self.naming_preview)
        return group

    def _build_output_group(self) -> QGroupBox:
        group = QGroupBox("3  保存位置")
        group.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        form = QFormLayout(group)
        form.setContentsMargins(10, 12, 10, 10)
        form.setSpacing(8)
        self.output_edit = QLineEdit()
        self.output_edit.setPlaceholderText("选择新照片要保存到哪里")
        choose = QPushButton("选择")
        choose.clicked.connect(self.choose_output)
        form.addRow("保存目录", _field_button(self.output_edit, choose))
        self.keep_exif = QCheckBox("保留拍摄时间和定位信息")
        self.keep_exif.setChecked(True)
        self.no_overwrite = QCheckBox("不覆盖原图和已有文件")
        self.no_overwrite.setChecked(True)
        self.no_overwrite.setEnabled(False)
        form.addRow("", self.keep_exif)
        form.addRow("", self.no_overwrite)
        return group

    def _build_processing_group(self) -> QGroupBox:
        group = QGroupBox("4  读取与处理速度")
        group.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        layout = QVBoxLayout(group)
        layout.setContentsMargins(10, 12, 10, 10)
        layout.setSpacing(7)
        self.fast_watermark_radio = QRadioButton("快速处理（推荐）")
        self.compatible_watermark_radio = QRadioButton("兼容处理")
        self.fast_watermark_radio.setChecked(True)
        layout.addWidget(self.fast_watermark_radio)
        layout.addWidget(self.compatible_watermark_radio)
        help_text = QLabel(
            "快速处理会并行读取照片，并同时生成最多 2 张照片，适合 SSD；"
            "兼容处理全部使用单线程，适合老旧电脑、机械硬盘或 U 盘。"
        )
        help_text.setObjectName("mutedLabel")
        help_text.setWordWrap(True)
        layout.addWidget(help_text)
        return group

    def _build_action_bar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("actionBar")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(14, 9, 14, 9)
        layout.setSpacing(22)
        self.selected_metric = _metric("待处理", "0 张")
        self.gps_metric = _metric("有定位", "0 张")
        layout.addWidget(self.selected_metric)
        layout.addWidget(self.gps_metric)
        self.output_summary = QLabel("请选择保存位置")
        self.output_summary.setObjectName("mutedLabel")
        layout.addWidget(self.output_summary)
        layout.addStretch()
        open_button = QPushButton("打开保存文件夹")
        open_button.clicked.connect(lambda: _open_directory(self.output_edit.text()))
        process = QPushButton("生成新照片")
        process.setObjectName("primaryButton")
        process.clicked.connect(self.process_photos)
        layout.addWidget(open_button)
        layout.addWidget(process)
        return bar

    def _connect_preview_signals(self) -> None:
        for checkbox in (
            self.watermark_group,
            self.title_enabled,
            self.latitude_enabled,
            self.longitude_enabled,
            self.time_enabled,
        ):
            checkbox.toggled.connect(self.schedule_preview)
        for edit in (
            self.title_edit,
            self.font_color,
            self.stroke_color,
        ):
            edit.textChanged.connect(self._settings_changed)
        self.custom_text.textChanged.connect(self.schedule_preview)
        for spin in (
            self.font_size,
            self.stroke_width,
            self.opacity,
            self.left_margin,
            self.bottom_margin,
        ):
            spin.valueChanged.connect(self._settings_changed)
        self.naming_group.toggled.connect(self._settings_changed)
        self.output_edit.textChanged.connect(self._update_output_summary)

    def choose_source(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "选择照片目录", self.source_edit.text())
        if directory:
            self.source_edit.setText(directory)

    def choose_output(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "选择输出目录", self.output_edit.text())
        if directory:
            self.output_edit.setText(directory)

    def scan_photos(self) -> None:
        source = self.source_edit.text().strip()
        if not source:
            show_warning(self, "还没有选择照片", "请先选择存放照片的文件夹。")
            return
        self.run_task(
            "正在扫描照片…",
            scan_photos,
            (
                source,
                self.recursive_check.isChecked(),
                4 if self.fast_watermark_radio.isChecked() else 1,
            ),
            self._scan_done,
        )

    def _scan_done(self, photos: list[PhotoInfo]) -> None:
        self.state.set_photos(photos)
        self.state.log(f"扫描完成：共 {len(photos)} 张照片。")

    def refresh_photos(self) -> None:
        for cached_image in self._preview_cache.values():
            cached_image.close()
        self._preview_cache.clear()
        self._selected_rows = set(range(len(self.state.photos)))
        self._photo_page = 0
        self._render_photo_page()
        self._update_summary()
        self.refresh_preview()

    def _render_photo_page(self) -> None:
        total = len(self.state.photos)
        page_count = max(1, (total + self.PHOTO_PAGE_SIZE - 1) // self.PHOTO_PAGE_SIZE)
        self._photo_page = min(max(0, self._photo_page), page_count - 1)
        start = self._photo_page * self.PHOTO_PAGE_SIZE
        visible_photos = self.state.photos[start : start + self.PHOTO_PAGE_SIZE]
        self.photo_table.setUpdatesEnabled(False)
        self.photo_table.blockSignals(True)
        self.photo_table.setRowCount(len(visible_photos))
        for row, photo in enumerate(visible_photos):
            global_row = start + row
            check = QTableWidgetItem()
            check.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
            check.setCheckState(
                Qt.CheckState.Checked
                if global_row in self._selected_rows
                else Qt.CheckState.Unchecked
            )
            check.setData(Qt.ItemDataRole.UserRole, global_row)
            self.photo_table.setItem(row, 0, check)
            time_text = photo.shot_time.strftime("%H:%M:%S") if photo.shot_time else "无拍摄时间"
            gps_text = "有定位" if photo.has_gps else "无定位"
            item = QTableWidgetItem(f"{photo.filename}\n{time_text} · {gps_text}")
            item.setData(Qt.ItemDataRole.UserRole, global_row)
            self.photo_table.setItem(row, 1, item)
        self.photo_table.blockSignals(False)
        self.photo_table.setUpdatesEnabled(True)
        if visible_photos:
            self.photo_table.selectRow(0)
        self.photo_page_label.setText(
            f"第 {self._photo_page + 1} / {page_count} 页"
            if total
            else "第 0 / 0 页"
        )
        self.previous_photo_page.setEnabled(self._photo_page > 0)
        self.next_photo_page.setEnabled(self._photo_page + 1 < page_count)

    def _change_photo_page(self, offset: int) -> None:
        self._photo_page += offset
        self._render_photo_page()
        self.schedule_preview()

    def selected_photos(self) -> list[PhotoInfo]:
        return [
            self.state.photos[row]
            for row in sorted(self._selected_rows)
            if 0 <= row < len(self.state.photos)
        ]

    def current_photo(self) -> PhotoInfo | None:
        row = self.photo_table.currentRow()
        item = self.photo_table.item(row, 1)
        global_row = item.data(Qt.ItemDataRole.UserRole) if item else None
        return (
            self.state.photos[global_row]
            if isinstance(global_row, int) and 0 <= global_row < len(self.state.photos)
            else None
        )

    def current_config(self) -> WatermarkConfig:
        return WatermarkConfig(
            enabled=self.watermark_group.isChecked(),
            title_enabled=self.title_enabled.isChecked(),
            title=self.title_edit.text(),
            latitude_enabled=self.latitude_enabled.isChecked(),
            longitude_enabled=self.longitude_enabled.isChecked(),
            time_enabled=self.time_enabled.isChecked(),
            custom_text=self.custom_text.toPlainText(),
            font_size=self.font_size.value(),
            color=self.font_color.text(),
            stroke_color=self.stroke_color.text(),
            stroke_width=self.stroke_width.value(),
            opacity=self.opacity.value(),
            left_margin=self.left_margin.value(),
            bottom_margin=self.bottom_margin.value(),
        )

    def refresh_preview(self, *_args) -> None:
        self._preview_timer.stop()
        photo = self.current_photo()
        if not photo:
            self._preview_pixmap = QPixmap()
            self.preview_label.setPixmap(QPixmap())
            self.preview_label.setText("读取照片后，这里会显示水印效果")
            self.preview_filename.setText("未选择照片")
            return
        try:
            prepared = self._preview_cache.get(photo.full_path)
            if prepared is None:
                with Image.open(photo.full_path) as source:
                    prepared = prepare_preview_image(source)
                self._preview_cache[photo.full_path] = prepared
                while len(self._preview_cache) > 4:
                    _key, old_image = self._preview_cache.popitem(last=False)
                    old_image.close()
            else:
                self._preview_cache.move_to_end(photo.full_path)
            if self.processed_button.isChecked():
                image = render_watermark_on_preview(
                    prepared,
                    photo,
                    self.current_config(),
                )
            else:
                image = prepared
            with image if image is not prepared else prepared.copy() as display_image:
                qt_image = ImageQt.ImageQt(display_image)
                self._preview_pixmap = QPixmap.fromImage(qt_image)
            self.preview_label.setText("")
            self.preview_label.setPixmap(
                self._preview_pixmap.scaled(
                    max(100, self.preview_label.width() - 24),
                    max(100, self.preview_label.height() - 24),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
            self.preview_filename.setText(photo.filename)
        except Exception as exc:
            self.preview_label.setPixmap(QPixmap())
            self.preview_label.setText(f"预览失败：{exc}")

    def schedule_preview(self, *_args) -> None:
        self._preview_timer.start()

    def _set_preview_mode(self, processed: bool) -> None:
        self.processed_button.setChecked(processed)
        self.original_button.setChecked(not processed)
        self.refresh_preview()

    def _settings_changed(self, *_args) -> None:
        self.schedule_preview()
        if self.sender() is self.naming_group:
            self._update_naming_preview()

    def _selection_changed(self, *_args) -> None:
        changed_item = _args[0] if _args and isinstance(_args[0], QTableWidgetItem) else None
        if changed_item is None or changed_item.column() != 0:
            return
        item = changed_item
        global_row = changed_item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(global_row, int):
            return
        if item:
            if item.checkState() == Qt.CheckState.Checked:
                self._selected_rows.add(global_row)
            else:
                self._selected_rows.discard(global_row)
        self._update_summary()

    def _update_summary(self, *_args) -> None:
        selected = self.selected_photos()
        gps = sum(photo.has_gps for photo in selected)
        self.photo_count_badge.setText(f"{len(self.state.photos)} 张")
        self.source_summary.setText(
            f"已选择 {len(selected)} 张 · 有定位 {gps} 张 · 无定位 {len(selected) - gps} 张"
        )
        _set_metric(self.selected_metric, f"{len(selected)} 张")
        _set_metric(self.gps_metric, f"{gps} 张")
        self._update_output_summary()
        self._update_naming_preview()

    def _update_output_summary(self, *_args) -> None:
        output = self.output_edit.text().strip()
        self.output_summary.setText(f"保存到：{output}" if output else "请选择保存位置")

    def _update_naming_preview(self) -> None:
        photos = self.selected_photos()
        if not photos:
            self.naming_preview.setText("命名预览：暂无照片")
            return
        plans = self._build_plans(photos)[:3]
        names = "、".join(plan.new_filename for plan in plans)
        if len(photos) > 3:
            names += " …"
        self.naming_preview.setText(f"命名预览：{names}")

    def _build_plans(self, photos: list[PhotoInfo]):
        if self.naming_group.isChecked():
            return build_rename_plan(
                photos,
                prefix="A",
                start=1,
                digits=1,
                sort_mode="shot_time",
                keep_original=False,
            )
        return [
            type("OutputPlan", (), {"photo": photo, "new_filename": photo.filename})()
            for photo in photos
        ]

    def process_photos(self) -> None:
        selected = self.selected_photos()
        output = self.output_edit.text().strip()
        if not selected:
            show_warning(self, "没有选择照片", "请在左侧勾选至少一张需要处理的照片。")
            return
        if not output:
            show_warning(self, "还没有选择保存位置", "请选择新照片要保存到哪个文件夹。")
            return
        self.run_task(
            "正在处理照片…",
            _process_output,
            (
                self._build_plans(selected),
                output,
                self.current_config(),
                "fast" if self.fast_watermark_radio.isChecked() else "compatible",
            ),
            self._process_done,
        )

    def _process_done(self, result: tuple[int, list[str]]) -> None:
        succeeded, errors = result
        self.state.log(f"照片生成完成：成功 {succeeded}，失败 {len(errors)}。")
        message = f"成功生成 <b>{succeeded}</b> 张照片。"
        if errors:
            message += f"<br>有 {len(errors)} 张没有完成，可查看详细信息。"
        details = "\n".join(errors) if errors else ""
        show_success(self, "照片已经生成", message, details)

    def reset_watermark(self) -> None:
        self._apply_config(WatermarkConfig())

    def _apply_config(self, config: WatermarkConfig) -> None:
        self.watermark_group.setChecked(config.enabled)
        self.title_enabled.setChecked(config.title_enabled)
        self.title_edit.setText(config.title)
        self.latitude_enabled.setChecked(config.latitude_enabled)
        self.longitude_enabled.setChecked(config.longitude_enabled)
        self.time_enabled.setChecked(config.time_enabled)
        self.custom_text.setPlainText(config.custom_text)
        self.font_size.setValue(config.font_size)
        self.font_color.setText(config.color)
        self.stroke_color.setText(config.stroke_color)
        self.stroke_width.setValue(config.stroke_width)
        self.opacity.setValue(config.opacity)
        self.left_margin.setValue(config.left_margin)
        self.bottom_margin.setValue(config.bottom_margin)
        self.refresh_preview()


class LandWorkspacePage(QWidget):
    photo_stats_changed = Signal(int, int)
    PREVIEW_ROW_LIMIT = 500

    def __init__(
        self,
        state: AppState,
        run_task: Callable,
    ) -> None:
        super().__init__()
        self.setObjectName("workspacePage")
        self.state = state
        self.run_task = run_task
        self.photos: list[PhotoInfo] = []
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(12)
        root.addWidget(self._build_header())

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self._build_setup_panel())
        splitter.addWidget(self._build_results_panel())
        splitter.setSizes([345, 900])
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        root.addWidget(splitter, 1)

    def _build_header(self) -> QWidget:
        widget = QWidget()
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        title = QVBoxLayout()
        title.setSpacing(2)
        heading = QLabel("按图斑整理照片")
        heading.setObjectName("pageTitle")
        subtitle = QLabel("把有定位的照片自动放进对应的图斑文件夹")
        subtitle.setObjectName("pageSubtitle")
        title.addWidget(heading)
        title.addWidget(subtitle)
        layout.addLayout(title)
        layout.addStretch()
        layout.addWidget(
            help_button(
                self,
                "按图斑整理照片 · 使用说明",
                [
                    "选择从平台下载的图斑文件。",
                    "选择需要整理的照片文件夹，然后点击“读取照片”。",
                    "软件会读取照片中的定位信息，用它判断照片属于哪个图斑。",
                    "多数电脑选择“快速整理”；老旧硬盘或 U 盘选择“兼容整理”。",
                    "选择整理结果的保存位置。",
                    "点击“先看看整理结果”，确认无误后再开始整理。",
                ],
            )
        )
        layout.addWidget(_step_badge("1", "选择资料", True))
        layout.addWidget(_step_line())
        layout.addWidget(_step_badge("2", "先看结果", True))
        layout.addWidget(_step_line())
        layout.addWidget(_step_badge("3", "整理输出", False))
        return widget

    def _build_setup_panel(self) -> QWidget:
        panel, body = _panel("整理条件")
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(14, 12, 14, 14)
        layout.setSpacing(9)

        layout.addWidget(_caption("1. 选择图斑文件"))
        self.kml_edit = QPlainTextEdit()
        self.kml_edit.setObjectName("dropZone")
        self.kml_edit.setPlaceholderText("选择从平台下载的图斑文件（KML）")
        self.kml_edit.setMaximumHeight(82)
        layout.addWidget(self.kml_edit)
        choose_kml = QPushButton("选择图斑文件")
        choose_kml.clicked.connect(self.choose_kml)
        layout.addWidget(choose_kml)

        layout.addWidget(_caption("2. 选择照片文件夹"))
        self.photo_source_edit = QLineEdit()
        self.photo_source_edit.setPlaceholderText("选择需要按图斑整理的照片文件夹")
        choose_photos = QPushButton("选择")
        choose_photos.clicked.connect(self.choose_photo_source)
        layout.addWidget(_field_button(self.photo_source_edit, choose_photos))
        photo_options = QHBoxLayout()
        self.photo_recursive_check = QCheckBox("包含子文件夹")
        self.photo_recursive_check.setChecked(True)
        read_photos = QPushButton("读取照片")
        read_photos.setObjectName("secondaryButton")
        read_photos.clicked.connect(self.scan_source_photos)
        photo_options.addWidget(self.photo_recursive_check)
        photo_options.addStretch()
        photo_options.addWidget(read_photos)
        layout.addLayout(photo_options)
        self.photo_source_label = QLabel("尚未读取照片")
        self.photo_source_label.setObjectName("infoField")
        self.photo_source_label.setWordWrap(True)
        layout.addWidget(self.photo_source_label)

        layout.addWidget(_caption("3. 照片归属范围"))
        distance_row = QHBoxLayout()
        distance_label = QLabel("图斑外允许距离")
        distance_label.setToolTip("0 米表示只整理确定在图斑内部的照片，最稳妥。")
        distance_row.addWidget(distance_label)
        self.distance_spin = QDoubleSpinBox()
        self.distance_spin.setRange(0, 100000)
        self.distance_spin.setDecimals(2)
        self.distance_spin.setSuffix(" 米")
        self.distance_spin.setToolTip("推荐保持 0 米。数值越大，图斑附近的照片也可能被归入。")
        distance_row.addWidget(self.distance_spin, 1)
        layout.addLayout(distance_row)
        distance_help = QLabel("建议保持 0 米：只整理确定在图斑里面的照片，最稳妥。")
        distance_help.setObjectName("mutedLabel")
        distance_help.setWordWrap(True)
        layout.addWidget(distance_help)

        layout.addWidget(_caption("4. 原照片怎么处理"))
        self.copy_radio = QRadioButton("保留原照片（推荐）")
        self.move_radio = QRadioButton("取走原照片（谨慎）")
        self.copy_radio.setChecked(True)
        self.copy_radio.toggled.connect(self._update_operation_help)
        self.move_radio.toggled.connect(self._update_operation_help)
        radio_layout = QVBoxLayout()
        radio_layout.setSpacing(7)
        radio_layout.addWidget(self.copy_radio)
        radio_layout.addWidget(self.move_radio)
        layout.addLayout(radio_layout)
        self.operation_help = QLabel()
        self.operation_help.setWordWrap(True)
        self.operation_help.setObjectName("safeOperationHint")
        layout.addWidget(self.operation_help)
        self._update_operation_help()

        layout.addWidget(_caption("5. 整理速度"))
        self.fast_transfer_radio = QRadioButton("快速整理（推荐）")
        self.compatible_transfer_radio = QRadioButton("兼容整理")
        self.fast_transfer_radio.setChecked(True)
        speed_layout = QVBoxLayout()
        speed_layout.setSpacing(7)
        speed_layout.addWidget(self.fast_transfer_radio)
        speed_layout.addWidget(self.compatible_transfer_radio)
        layout.addLayout(speed_layout)
        speed_help = QLabel(
            "快速整理会同时处理最多 2 张照片，适合 SSD 和性能较好的电脑；"
            "兼容整理每次只处理 1 张，适合老旧电脑、机械硬盘或不稳定 U 盘。"
        )
        speed_help.setObjectName("mutedLabel")
        speed_help.setWordWrap(True)
        layout.addWidget(speed_help)

        layout.addWidget(_caption("6. 选择保存位置"))
        self.output_edit = QLineEdit()
        self.output_edit.setPlaceholderText("选择整理结果要保存到哪里")
        choose_output = QPushButton("选择")
        choose_output.clicked.connect(self.choose_output)
        layout.addWidget(_field_button(self.output_edit, choose_output))

        analyze = QPushButton("先看看整理结果")
        analyze.setObjectName("darkButton")
        analyze.clicked.connect(self.analyze)
        layout.addWidget(analyze)
        layout.addStretch()
        scroll = QScrollArea()
        scroll.setObjectName("settingsScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(content)
        body.layout.addWidget(scroll, 1)
        panel.setMinimumWidth(315)
        panel.setMaximumWidth(430)
        return panel

    def _build_results_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("panel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        stats = QWidget()
        stats_layout = QHBoxLayout(stats)
        stats_layout.setContentsMargins(0, 0, 0, 0)
        stats_layout.setSpacing(0)
        self.land_metric = _stat("图斑数量", "0")
        self.matched_metric = _stat("找到归属", "0", "success")
        self.unmatched_metric = _stat("未找到归属", "0", "warning")
        self.no_gps_metric = _stat("没有定位", "0")
        self.empty_metric = _stat("没有照片的图斑", "0", "warning")
        for metric in (
            self.land_metric,
            self.matched_metric,
            self.unmatched_metric,
            self.no_gps_metric,
            self.empty_metric,
        ):
            stats_layout.addWidget(metric, 1)
        layout.addWidget(stats)

        self.match_table = QTableWidget(0, 5)
        self.match_table.setHorizontalHeaderLabels(["序号", "照片文件", "归属图斑", "相距", "结果"])
        self.match_table.verticalHeader().setVisible(False)
        self.match_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.match_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.match_table.setAlternatingRowColors(True)
        header = self.match_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.match_table, 1)

        footer = QWidget()
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(14, 9, 14, 9)
        note = QLabel(
            "只复制找到归属的照片；未找到归属的照片不会复制，没有照片的图斑不会建立空文件夹。\n"
            "整理完成后会生成：未匹配照片 KML、无照片图斑 KML 和结果表。\n"
            "“先看看整理结果”不会移动或复制任何照片。"
        )
        note.setObjectName("mutedLabel")
        footer_layout.addWidget(note)
        footer_layout.addStretch()
        open_output = QPushButton("打开保存文件夹")
        open_output.clicked.connect(lambda: _open_directory(self.output_edit.text()))
        organize = QPushButton("确认后开始整理")
        organize.setObjectName("primaryButton")
        organize.clicked.connect(self.organize)
        footer_layout.addWidget(open_output)
        footer_layout.addWidget(organize)
        layout.addWidget(footer)
        return panel

    def choose_kml(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self, "选择一个或多个图斑文件", "", "图斑文件 (*.kml)"
        )
        if files:
            self.kml_edit.setPlainText("\n".join(files))

    def choose_photo_source(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self,
            "选择需要整理的照片文件夹",
            self.photo_source_edit.text(),
        )
        if directory:
            self.photo_source_edit.setText(directory)
            self.photos = []
            self._update_photo_source()

    def scan_source_photos(self) -> None:
        source = self.photo_source_edit.text().strip()
        if not source:
            show_warning(self, "还没有选择照片", "请先选择需要整理的照片文件夹。")
            return
        self.run_task(
            "正在读取照片和定位信息…",
            scan_photos,
            (source, self.photo_recursive_check.isChecked()),
            self._photo_scan_done,
        )

    def _photo_scan_done(self, photos: list[PhotoInfo]) -> None:
        self.photos = photos
        self._update_photo_source()
        if not photos:
            show_warning(
                self,
                "没有找到照片",
                "所选文件夹中没有找到支持的照片，请检查文件夹后重试。",
            )

    def _update_operation_help(self, *_args) -> None:
        if self.copy_radio.isChecked():
            self.operation_help.setObjectName("safeOperationHint")
            self.operation_help.setText(
                "原文件夹里的照片保持不变，软件会另外复制一份到整理结果中。"
            )
        else:
            self.operation_help.setObjectName("dangerOperationHint")
            self.operation_help.setText(
                "注意：照片会从原文件夹中消失，并被转移到整理结果中。"
            )
        self.operation_help.style().unpolish(self.operation_help)
        self.operation_help.style().polish(self.operation_help)

    def choose_output(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "选择整理输出目录", self.output_edit.text())
        if directory:
            self.output_edit.setText(directory)

    def _paths(self) -> list[str]:
        return [line.strip() for line in self.kml_edit.toPlainText().splitlines() if line.strip()]

    def _update_photo_source(self) -> None:
        total = len(self.photos)
        gps = sum(photo.has_gps for photo in self.photos)
        if total:
            self.photo_source_label.setText(
                f"已读取 {total} 张照片｜有定位 {gps} 张｜没有定位 {total - gps} 张"
            )
        else:
            self.photo_source_label.setText("尚未读取照片")
        _set_stat(self.no_gps_metric, str(total - gps))
        self.photo_stats_changed.emit(total, gps)

    def analyze(self) -> None:
        photos = self.photos
        if not photos:
            show_warning(
                self,
                "还没有可整理的照片",
                "请在本页选择照片文件夹，然后点击“读取照片”。",
            )
            return
        if not self._paths():
            show_warning(self, "还没有选择图斑文件", "请选择从平台下载的图斑文件（KML）。")
            return
        self.run_task(
            "正在查看照片会被分到哪里…",
            analyze_photo_land_matches,
            (photos, self._paths(), self.distance_spin.value()),
            self._analysis_done,
        )

    def _analysis_done(self, result) -> None:
        lands, matches, dataset_gap_m = result
        matched = sum(match.land is not None for match in matches)
        unmatched = len(matches) - matched
        counts = {id(land): 0 for land in lands}
        for match in matches:
            if match.land:
                counts[id(match.land)] += 1
        empty = sum(count == 0 for count in counts.values())
        _set_stat(self.land_metric, str(len(lands)))
        _set_stat(self.matched_metric, str(matched))
        _set_stat(self.unmatched_metric, str(unmatched))
        _set_stat(self.no_gps_metric, str(len(self.photos) - len(matches)))
        _set_stat(self.empty_metric, str(empty))

        visible_matches = matches[: self.PREVIEW_ROW_LIMIT]
        self.match_table.setUpdatesEnabled(False)
        self.match_table.setRowCount(len(visible_matches))
        for row, match in enumerate(visible_matches):
            status = "在图斑内" if match.direct_hit else ("附近归入" if match.land else "未找到归属")
            values = [
                str(row + 1),
                match.photo.filename,
                match.land.name if match.land else "—",
                "—" if match.distance_m is None else f"{match.distance_m:.2f} m",
                status,
            ]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 4:
                    item.setForeground(QColor("#14815b" if match.land else "#a66300"))
                self.match_table.setItem(row, column, item)
        self.match_table.setUpdatesEnabled(True)
        if len(matches) > self.PREVIEW_ROW_LIMIT:
            self.state.log(
                f"结果较多，表格只显示前 {self.PREVIEW_ROW_LIMIT} 张；"
                f"统计数据仍包含全部 {len(matches)} 张照片。"
            )
        if dataset_gap_m >= 1000:
            show_warning(
                self,
                "照片和图斑不在同一地点",
                f"照片位置与这份图斑文件相距约 <b>{dataset_gap_m / 1000:.1f} 公里</b>。"
                "<br><br>很可能选错了村庄的 KML 文件，请重新选择后再试。",
            )
        elif not matched and matches:
            show_info(
                self,
                "没有照片落在图斑内部",
                "照片和图斑位于同一片区域，但在严格 0 米条件下没有重合。"
                "<br><br>可以先尝试把“图斑外允许距离”设置为 5～20 米，再查看结果。",
            )
        self.state.log(
            f"整理预览完成：图斑 {len(lands)}，找到归属 {matched}，未找到归属 {unmatched}。"
        )

    def organize(self) -> None:
        photos = self.photos
        if not photos or not self._paths() or not self.output_edit.text().strip():
            show_warning(
                self,
                "资料还没有准备完整",
                "请确认已经选择照片、图斑文件和整理结果的保存位置。",
            )
            return
        if self.move_radio.isChecked():
            if not ask_confirm(
                self,
                "确定要取走原照片吗？",
                "照片会从原文件夹中消失，并被转移到整理结果中。"
                "<br><br>如果只是想整理一份副本，请返回选择“保留原照片（推荐）”。",
                "确定取走",
            ):
                return
        self.run_task(
            "正在按图斑整理照片…",
            organize_photos_by_land,
            (
                photos,
                self._paths(),
                self.output_edit.text().strip(),
                self.copy_radio.isChecked(),
                self.distance_spin.value(),
                "fast" if self.fast_transfer_radio.isChecked() else "compatible",
            ),
            self._organize_done,
        )

    def _organize_done(self, summary) -> None:
        self.state.log(
            f"图斑整理完成：找到归属 {summary.matched}，未找到归属 {summary.unmatched}，"
            f"成功 {summary.succeeded}，失败 {summary.failed}。"
        )
        show_success(
            self,
            "照片整理完成",
            f"已将 <b>{summary.succeeded}</b> 张匹配成功的照片放入图斑文件夹。<br>"
            f"未匹配照片：{summary.unmatched} 张（未复制，仅写入 KML）；"
            f"复制失败：{summary.failed} 张；"
            f"无照片图斑：{summary.empty_lands} 个（未建空文件夹，仅写入 KML）。"
            "<br><br>同时已生成：无照片图斑、未匹配照片、图斑外距离匹配清单"
            "和图斑照片分类工作日志。",
        )


class HtmlToolPage(QWidget):
    """在软件内嵌入项目自带的 HTML 地图工具。"""

    def __init__(
        self,
        title: str,
        subtitle: str,
        html_path: Path,
        help_steps: list[str] | None = None,
    ) -> None:
        super().__init__()
        self.setObjectName("workspacePage")
        self.html_path = html_path
        self.loaded = False
        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(12)

        header = QWidget()
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(0, 0, 0, 0)
        title_layout = QVBoxLayout()
        title_layout.setSpacing(2)
        heading = QLabel(title)
        heading.setObjectName("pageTitle")
        description = QLabel(subtitle)
        description.setObjectName("pageSubtitle")
        title_layout.addWidget(heading)
        title_layout.addWidget(description)
        header_layout.addLayout(title_layout)
        header_layout.addStretch()
        if help_steps:
            header_layout.addWidget(help_button(self, f"{title} · 使用说明", help_steps))
        reload_button = QPushButton("重新加载")
        reload_button.clicked.connect(self.reload)
        browser_button = QPushButton("在浏览器打开")
        browser_button.clicked.connect(self.open_in_browser)
        header_layout.addWidget(reload_button)
        header_layout.addWidget(browser_button)
        root.addWidget(header)

        frame = QFrame()
        frame.setObjectName("panel")
        frame_layout = QVBoxLayout(frame)
        frame_layout.setContentsMargins(1, 1, 1, 1)
        self.web_view = QWebEngineView()
        self.profile = QWebEngineProfile(self)
        self.page = QWebEnginePage(self.profile, self.web_view)
        self.web_view.setPage(self.page)
        settings = self.web_view.settings()
        settings.setAttribute(QWebEngineSettings.WebAttribute.JavascriptEnabled, True)
        settings.setAttribute(
            QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls,
            True,
        )
        settings.setAttribute(
            QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls,
            True,
        )
        self.profile.downloadRequested.connect(self._download_requested)
        frame_layout.addWidget(self.web_view)
        root.addWidget(frame, 1)
        self.web_view.setHtml(
            "<div style='font-family:Microsoft YaHei;padding:30px;color:#64748b'>"
            "点击左侧功能后加载页面…</div>"
        )

    def load(self) -> None:
        if self.html_path.is_file():
            self.web_view.setUrl(QUrl.fromLocalFile(str(self.html_path.resolve())))
        else:
            self.web_view.setHtml(
                f"<h2>文件不存在</h2><p>{self.html_path}</p>",
                QUrl.fromLocalFile(str(self.html_path.parent.resolve())),
            )
        self.loaded = True

    def reload(self) -> None:
        self.web_view.reload()

    def open_in_browser(self) -> None:
        if self.html_path.is_file():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.html_path.resolve())))

    def _download_requested(self, download) -> None:
        suggested = download.downloadFileName() or "下载文件"
        destination, _ = QFileDialog.getSaveFileName(self, "保存文件", suggested)
        if not destination:
            download.cancel()
            self._download_started = False
            return
        path = Path(destination)
        download.setDownloadDirectory(str(path.parent))
        download.setDownloadFileName(path.name)
        download.accept()


class SanziLoginDialog(QDialog):
    LOGIN_URL = (
        "http://222.143.69.159:38590/dist/#/login"
        "?redirect=%2FdataCollection"
    )

    def __init__(
        self,
        profile: QWebEngineProfile,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("登录三资平台")
        self.resize(1100, 760)
        self.login_data: dict[str, str] = {}
        layout = QVBoxLayout(self)
        tip = QLabel(
            "请在下方页面登录。登录成功并进入数据采集页面后，点击“我已登录，继续”。"
            "\n账号和密码只在平台页面中输入，软件不会保存。"
        )
        tip.setObjectName("safeOperationHint")
        tip.setWordWrap(True)
        layout.addWidget(tip)
        self.web_view = QWebEngineView()
        self.profile = profile
        self.page = QWebEnginePage(profile, self.web_view)
        self.web_view.setPage(self.page)
        self.web_view.settings().setAttribute(
            QWebEngineSettings.WebAttribute.JavascriptEnabled,
            True,
        )
        layout.addWidget(self.web_view, 1)
        actions = QHBoxLayout()
        self.status_label = QLabel("请先完成登录")
        self.status_label.setObjectName("mutedLabel")
        refresh = QPushButton("重新打开登录页")
        refresh.clicked.connect(lambda: self.web_view.setUrl(QUrl(self.LOGIN_URL)))
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        extract = QPushButton("我已登录，继续")
        extract.setObjectName("primaryButton")
        extract.clicked.connect(self.extract_login)
        actions.addWidget(self.status_label)
        actions.addStretch()
        actions.addWidget(refresh)
        actions.addWidget(cancel)
        actions.addWidget(extract)
        layout.addLayout(actions)
        self.web_view.setUrl(QUrl(self.LOGIN_URL))

    def extract_login(self) -> None:
        self.status_label.setText("正在确认登录状态…")
        self.web_view.page().runJavaScript(
            f"JSON.stringify({LOGIN_STORAGE_SCRIPT})",
            self._login_extracted,
        )

    def _login_extracted(self, value: object) -> None:
        login_data = platform_login_data(value)
        if not login_data:
            cached = self.profile.property("platform_login_data")
            login_data = cached if isinstance(cached, dict) else {}
        if not login_data:
            self.status_label.setText("还没有检测到登录成功")
            show_warning(
                self,
                "还没有登录成功",
                "请先在上方页面完成登录，进入数据采集页面后，再点击“我已登录，继续”。",
            )
            return
        self.login_data = login_data
        self.accept()


LOGIN_STORAGE_SCRIPT = """
(() => {
  const values = {};
  for (const storage of [localStorage, sessionStorage]) {
    for (let index = 0; index < storage.length; index++) {
      const key = storage.key(index);
      if (key) values[key] = storage.getItem(key) || "";
    }
  }

  const found = {};
  found.token = sessionStorage.getItem("token") || localStorage.getItem("token") || "";
  found.tokenName = sessionStorage.getItem("TokenName")
    || localStorage.getItem("TokenName")
    || "Authorization";
  found.districtCode = sessionStorage.getItem("currentDistrictCode")
    || localStorage.getItem("currentDistrictCode") || "";
  const tokenKeys = new Set(["token", "tokenvalue", "accesstoken"]);
  const headerKeys = new Set(["tokenname"]);
  const codeKeys = new Set(["districtcode", "distinctcode", "currentdistrictcode"]);
  const nameKeys = new Set(["districtname", "distinctname"]);

  function keep(key, value) {
    if (value == null || typeof value === "object") return;
    const text = String(value).trim();
    if (!text) return;
    const normalized = String(key || "").replace(/[-_]/g, "").toLowerCase();
    if (!found.token && tokenKeys.has(normalized) && text.length >= 8) found.token = text;
    if (!found.tokenName && headerKeys.has(normalized)) found.tokenName = text;
    if (!found.districtCode && codeKeys.has(normalized)) found.districtCode = text;
    if (!found.districtName && nameKeys.has(normalized)) found.districtName = text;
  }

  function walk(value, depth = 0) {
    if (depth > 5 || value == null) return;
    if (Array.isArray(value)) {
      value.forEach(item => walk(item, depth + 1));
      return;
    }
    if (typeof value !== "object") return;
    for (const [key, item] of Object.entries(value)) {
      keep(key, item);
      if (item && typeof item === "object") walk(item, depth + 1);
    }
  }

  for (const [key, raw] of Object.entries(values)) {
    keep(key, raw);
    try { walk(JSON.parse(raw)); } catch (_) {}
  }
  return {
    token: found.token || "",
    tokenName: found.tokenName || "Authorization",
    districtCode: found.districtCode || "",
    districtName: found.districtName || "",
    cookie: document.cookie || "",
    href: location.href,
    storageKeys: Object.keys(values)
  };
})()
"""


def javascript_result_dict(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def platform_login_data(value: object) -> dict[str, str]:
    data = javascript_result_dict(value)
    token = str(data.get("token") or "").strip()
    if not token:
        return {}
    token_header = str(data.get("tokenName") or "Authorization").strip()
    if token_header.casefold() == "authorization" and not token.casefold().startswith(
        ("bearer ", "basic ")
    ):
        token = f"bearer {token}"
    return {
        "token": token,
        "token_header": token_header,
        "districtcode": str(data.get("districtCode") or ""),
        "districtname": str(data.get("districtName") or ""),
        "cookie": str(data.get("cookie") or ""),
    }


def platform_login_data_from_headers(headers: dict[str, str]) -> dict[str, str]:
    for name, value in headers.items():
        normalized = name.replace("_", "").replace("-", "").casefold()
        token = value.strip()
        if normalized in {
            "token",
            "tokenvalue",
            "accesstoken",
            "authorization",
            "xauthtoken",
            "xaccesstoken",
        } and len(token) >= 8 and token.casefold() not in {
            "[object object]",
            "undefined",
        }:
            return {
                "token": token,
                "token_header": name,
                "districtcode": "",
                "districtname": "",
                "cookie": headers.get("Cookie", ""),
            }
    return {}


class PlatformCredentialInterceptor(QWebEngineUrlRequestInterceptor):
    """从平台真实接口请求中取得浏览器已经使用的登录凭证。"""

    credentials_found = Signal(dict)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.last_login_data: dict[str, str] = {}
        self.capture_enabled = True

    def interceptRequest(self, info: QWebEngineUrlRequestInfo) -> None:
        if not self.capture_enabled:
            return
        url = info.requestUrl()
        if url.host() != "222.143.69.159":
            return
        headers = {
            bytes(name).decode("latin-1").strip(): bytes(value).decode("latin-1").strip()
            for name, value in info.httpHeaders().items()
        }
        login_data = platform_login_data_from_headers(headers)
        if not login_data:
            return
        login_data["districtcode"] = self.last_login_data.get("districtcode", "")
        login_data["districtname"] = self.last_login_data.get("districtname", "")
        signature = (login_data["token_header"], login_data["token"])
        previous = (
            self.last_login_data.get("token_header", ""),
            self.last_login_data.get("token", ""),
        )
        self.last_login_data = login_data
        if signature != previous:
            self.credentials_found.emit(dict(login_data))

    def merge_page_data(self, login_data: dict[str, str]) -> dict[str, str]:
        merged = dict(self.last_login_data)
        for key, value in login_data.items():
            if value:
                merged[key] = value
        if merged.get("token"):
            self.last_login_data = merged
        return merged


class PlatformWebPage(QWebEnginePage):
    """平台网页专用页面，只过滤已确认会误报的旧下载提示。"""

    def javaScriptAlert(self, security_origin: QUrl, message: str) -> None:
        if message.strip() == "无法读取当前地图图斑":
            return
        super().javaScriptAlert(security_origin, message)


class VisibleLandDownloadPage(QWidget):
    login_captured = Signal(dict)
    login_checked = Signal(dict)
    PLATFORM_URL = "http://222.143.69.159:38590/dist/#/dataCollection"

    def __init__(self, profile: QWebEngineProfile) -> None:
        super().__init__()
        self.setObjectName("workspacePage")
        self.profile = profile
        raw_export_script = (
            package_resource("resources", "visible_land_export.js")
        ).read_text(encoding="utf-8")
        self.export_script = f"JSON.stringify({raw_export_script})"

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(12)
        header = QHBoxLayout()
        title = QVBoxLayout()
        heading = QLabel("下载平台图斑")
        heading.setObjectName("pageTitle")
        subtitle = QLabel("在平台地图中选好村庄和进度，保存当前看到的图斑")
        subtitle.setObjectName("pageSubtitle")
        title.addWidget(heading)
        title.addWidget(subtitle)
        header.addLayout(title)
        header.addStretch()
        header.addWidget(
            help_button(
                self,
                "下载平台图斑 · 使用说明",
                [
                    "点击“登录平台”，完成账号、密码和验证码登录。",
                    "进入数据采集地图后选择需要的村庄。",
                    "在平台左侧勾选需要的工作进度，等待图斑显示。",
                    "点击“保存当前看到的图斑”，选择保存位置。",
                ],
            )
        )
        login = QPushButton("登录平台")
        login.clicked.connect(self.open_login)
        self.reload_button = QPushButton("重新加载平台")
        self.reload_button.clicked.connect(self.reload_platform)
        download = QPushButton("保存当前看到的图斑")
        download.setObjectName("primaryButton")
        download.clicked.connect(self.download_visible_lands)
        header.addWidget(login)
        header.addWidget(self.reload_button)
        header.addWidget(download)
        root.addLayout(header)

        guide = QLabel(
            "按顺序操作：登录平台 → 选择村庄 → 勾选工作进度 → 等图斑显示 → 保存图斑"
        )
        guide.setObjectName("safeOperationHint")
        guide.setWordWrap(True)
        root.addWidget(guide)

        frame = QFrame()
        frame.setObjectName("panel")
        frame_layout = QVBoxLayout(frame)
        frame_layout.setContentsMargins(1, 1, 1, 1)
        self.web_view = QWebEngineView()
        self.page = PlatformWebPage(profile, self.web_view)
        self.web_view.setPage(self.page)
        self.web_view.settings().setAttribute(
            QWebEngineSettings.WebAttribute.JavascriptEnabled,
            True,
        )
        frame_layout.addWidget(self.web_view)
        root.addWidget(frame, 1)
        self.status_label = QLabel("平台页面正在准备")
        self.status_label.setObjectName("mutedLabel")
        root.addWidget(self.status_label)
        self.profile.downloadRequested.connect(self._download_requested)
        self.web_view.loadStarted.connect(self._platform_load_started)
        self.web_view.loadFinished.connect(self._platform_loaded)
        self._download_started = False
        self._manual_reload_requested = False
        self.load_platform()

    def open_login(self) -> None:
        dialog = SanziLoginDialog(self.profile, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.login_captured.emit(dialog.login_data)
            self.status_label.setText("登录成功，正在打开数据采集地图…")
            self.load_platform()

    def load_platform(self) -> None:
        self.web_view.setUrl(QUrl(self.PLATFORM_URL))

    def reload_platform(self) -> None:
        self._manual_reload_requested = True
        self.status_label.setText("正在重新加载平台，请稍候…")
        self.reload_button.setEnabled(False)
        current_url = self.web_view.url()
        if not current_url.isValid() or current_url.isEmpty():
            self.load_platform()
            return
        self.web_view.page().triggerAction(
            QWebEnginePage.WebAction.ReloadAndBypassCache
        )

    def _platform_load_started(self) -> None:
        self.reload_button.setEnabled(False)
        self.reload_button.setText("正在加载…")

    def _platform_loaded(self, loaded: bool) -> None:
        self.reload_button.setEnabled(True)
        self.reload_button.setText("重新加载平台")
        if not loaded:
            self._manual_reload_requested = False
            self.status_label.setText("平台页面暂时没有加载成功，请检查网络后重试")
            return
        if self._manual_reload_requested:
            self.status_label.setText("平台已重新加载，正在确认登录状态…")
        else:
            self.status_label.setText("平台页面已加载，正在确认登录状态…")
        self._manual_reload_requested = False
        QTimer.singleShot(500, self.capture_login)

    def capture_login(self) -> None:
        self.web_view.page().runJavaScript(
            f"JSON.stringify({LOGIN_STORAGE_SCRIPT})",
            self._platform_login_extracted,
        )

    def _platform_login_extracted(self, value: object) -> None:
        login_data = platform_login_data(value)
        if not login_data:
            cached = self.profile.property("platform_login_data")
            login_data = cached if isinstance(cached, dict) else {}
        self.login_checked.emit(login_data)
        if not login_data:
            self.status_label.setText("请先登录平台，然后进入数据采集地图")
            return
        self.login_captured.emit(login_data)
        district = login_data.get("districtname") or "当前账号"
        self.status_label.setText(f"平台已登录｜{district}")

    def download_visible_lands(self) -> None:
        self._download_started = False
        self.status_label.setText("正在整理当前地图上的图斑…")
        self.web_view.page().runJavaScript(self.export_script, self._export_finished)

    def _export_finished(self, value: object) -> None:
        result = javascript_result_dict(value)
        if not result:
            QTimer.singleShot(400, self._finish_download_without_result)
            return
        if not result.get("ok"):
            message = str(result.get("message") or "无法读取当前地图图斑")
            self.status_label.setText(message)
            show_warning(self, "暂时无法保存图斑", message)
            return
        count = int(result.get("featureCount") or 0)
        village = str(result.get("village") or "")
        states = "、".join(result.get("states") or [])
        self.status_label.setText(f"已读取 {count} 个图斑｜{village}｜{states}")

    def _finish_download_without_result(self) -> None:
        if self._download_started:
            self.status_label.setText("图斑文件已经开始保存")
            return
        show_warning(
            self,
            "暂时无法保存图斑",
            "没有读到当前地图上的图斑，请等待地图加载完成后再试。",
        )

    def _download_requested(self, download) -> None:
        self._download_started = True
        suggested = download.downloadFileName() or "三资已显示图斑.kml"
        destination, _ = QFileDialog.getSaveFileName(
            self,
            "保存图斑文件",
            suggested,
            "图斑文件 (*.kml)",
        )
        if not destination:
            download.cancel()
            return
        path = Path(destination)
        download.setDownloadDirectory(str(path.parent))
        download.setDownloadFileName(path.name)
        download.accept()
        self.status_label.setText(f"正在保存：{path.name}")


class SanziUploadPage(QWidget):
    login_check_requested = Signal()
    clear_login_requested = Signal(bool)
    login_captured = Signal(dict)

    def __init__(self, run_task: Callable, profile: QWebEngineProfile) -> None:
        super().__init__()
        self.setObjectName("workspacePage")
        self.run_task = run_task
        self.profile = profile
        self.login_data: dict[str, str] = {}
        self._open_login_after_check = False
        self._login_check_retries = 0
        self.results: list[UploadResult] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(12)
        header = QHBoxLayout()
        title = QVBoxLayout()
        heading = QLabel("上传照片到平台")
        heading.setObjectName("pageTitle")
        subtitle = QLabel("登录平台、选择整理好的照片，检查无误后再上传")
        subtitle.setObjectName("pageSubtitle")
        title.addWidget(heading)
        title.addWidget(subtitle)
        header.addLayout(title)
        header.addStretch()
        header.addWidget(
            help_button(
                self,
                "上传照片到平台 · 使用说明",
                [
                    "登录三资平台，登录信息会自动识别。",
                    "需要更换账号时，点击“切换账号”；只想退出时，点击“退出登录”。",
                    "选择本次上传对应的 KML，软件会核对村庄和每个图斑编号。",
                    "选择“按图斑整理照片”生成的结果文件夹。",
                    "点击“先检查哪些照片能上传”。这一步不会上传照片。",
                    "确认检查结果后，再点击“确认上传照片”。",
                ],
            )
        )
        root.addLayout(header)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self._build_setup_panel())
        splitter.addWidget(self._build_result_panel())
        splitter.setSizes([380, 900])
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        root.addWidget(splitter, 1)

    def _build_setup_panel(self) -> QWidget:
        panel, body = _panel("按顺序完成以下操作")
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(14, 12, 14, 14)
        layout.setSpacing(9)

        layout.addWidget(_caption("1. 登录平台"))
        self.login_status = QLabel("尚未登录")
        self.login_status.setObjectName("dangerOperationHint")
        self.login_status.setWordWrap(True)
        layout.addWidget(self.login_status)
        self.login_button = QPushButton("打开登录页面")
        self.login_button.setObjectName("primaryButton")
        self.login_button.clicked.connect(self.open_login)
        layout.addWidget(self.login_button)
        account_actions = QHBoxLayout()
        self.switch_account_button = QPushButton("切换账号")
        self.switch_account_button.clicked.connect(self.switch_account)
        self.logout_button = QPushButton("退出登录")
        self.logout_button.clicked.connect(self.logout)
        self.switch_account_button.setEnabled(False)
        self.logout_button.setEnabled(False)
        account_actions.addWidget(self.switch_account_button)
        account_actions.addWidget(self.logout_button)
        layout.addLayout(account_actions)

        layout.addWidget(_caption("2. 选择对应的图斑文件"))
        self.upload_kml_edit = QPlainTextEdit()
        self.upload_kml_edit.setPlaceholderText("选择本次照片对应的 KML 文件")
        self.upload_kml_edit.setMaximumHeight(58)
        self.upload_kml_edit.textChanged.connect(self._upload_inputs_changed)
        layout.addWidget(self.upload_kml_edit)
        choose_kml = QPushButton("选择 KML 文件")
        choose_kml.clicked.connect(self.choose_upload_kml)
        layout.addWidget(choose_kml)
        self.kml_match_summary = QLabel("尚未选择 KML")
        self.kml_match_summary.setObjectName("infoField")
        self.kml_match_summary.setWordWrap(True)
        layout.addWidget(self.kml_match_summary)

        layout.addWidget(_caption("3. 选择整理好的照片文件夹"))
        self.photo_root_edit = QLineEdit()
        self.photo_root_edit.setPlaceholderText("选择“按图斑整理照片”生成的结果文件夹")
        self.photo_root_edit.textChanged.connect(self._upload_inputs_changed)
        choose = QPushButton("选择")
        choose.clicked.connect(self.choose_photo_root)
        layout.addWidget(_field_button(self.photo_root_edit, choose))
        self.scan_summary = QLabel("尚未扫描目录")
        self.scan_summary.setObjectName("infoField")
        layout.addWidget(self.scan_summary)

        layout.addWidget(_caption("4. 选择上传数量"))
        form = QFormLayout()
        self.max_photos_spin = QSpinBox()
        self.max_photos_spin.setRange(1, 20)
        self.max_photos_spin.setValue(3)
        self.max_photos_spin.setSuffix(" 张")
        form.addRow("每个图斑最多", self.max_photos_spin)
        self.required_status_check = QCheckBox("资料未填写完整的图斑先不上传")
        self.required_status_check.setChecked(True)
        form.addRow("", self.required_status_check)
        self.skip_uploaded_check = QCheckBox("平台已有的同名照片不重复上传")
        self.skip_uploaded_check.setChecked(True)
        form.addRow("", self.skip_uploaded_check)
        self.average_pick_check = QCheckBox("照片过多时，从前中后均匀选择")
        self.average_pick_check.setChecked(True)
        form.addRow("", self.average_pick_check)
        layout.addLayout(form)
        self.max_photos_spin.valueChanged.connect(self._upload_inputs_changed)
        self.required_status_check.toggled.connect(self._upload_inputs_changed)
        self.skip_uploaded_check.toggled.connect(self._upload_inputs_changed)
        self.average_pick_check.toggled.connect(self._upload_inputs_changed)

        self.check_upload_button = QPushButton("先检查哪些照片能上传")
        self.check_upload_button.setObjectName("darkButton")
        self.check_upload_button.clicked.connect(self.precheck)
        self.upload_button = QPushButton("确认上传照片")
        self.upload_button.setObjectName("primaryButton")
        self.upload_button.setEnabled(False)
        self.upload_button.clicked.connect(self.upload)
        layout.addStretch()

        scroll = QScrollArea()
        scroll.setObjectName("settingsScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        scroll.setWidget(content)
        body.layout.addWidget(scroll, 1)

        actions = QWidget()
        actions_layout = QVBoxLayout(actions)
        actions_layout.setContentsMargins(14, 8, 14, 14)
        actions_layout.setSpacing(8)
        actions_layout.addWidget(self.check_upload_button)
        actions_layout.addWidget(self.upload_button)
        body.layout.addWidget(actions)

        panel.setMinimumWidth(350)
        panel.setMaximumWidth(460)
        return panel

    def _build_result_panel(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("panel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        stats = QWidget()
        stats_layout = QHBoxLayout(stats)
        stats_layout.setContentsMargins(0, 0, 0, 0)
        stats_layout.setSpacing(0)
        self.group_metric = _stat("照片文件夹", "0")
        self.ready_metric = _stat("准备上传", "0", "success")
        self.success_metric = _stat("上传成功", "0", "success")
        self.skip_metric = _stat("暂不上传", "0", "warning")
        self.fail_metric = _stat("失败", "0", "warning")
        for metric in (
            self.group_metric,
            self.ready_metric,
            self.success_metric,
            self.skip_metric,
            self.fail_metric,
        ):
            stats_layout.addWidget(metric, 1)
        layout.addWidget(stats)

        self.result_table = QTableWidget(0, 4)
        self.result_table.setHorizontalHeaderLabels(["图斑编号", "照片文件", "状态", "说明"])
        self.result_table.verticalHeader().setVisible(False)
        self.result_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.result_table.setAlternatingRowColors(True)
        header = self.result_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.result_table, 1)

        footer = QWidget()
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(14, 9, 14, 9)
        note = QLabel("先检查，不会立即上传；确认结果后才会真正上传照片。")
        note.setObjectName("mutedLabel")
        footer_layout.addWidget(note)
        footer_layout.addStretch()
        export = QPushButton("保存结果表")
        export.clicked.connect(self.export_log)
        footer_layout.addWidget(export)
        layout.addWidget(footer)
        return panel

    def open_login(self) -> None:
        if self.login_data.get("token"):
            district = self.login_data.get("districtname") or "当前账号"
            show_info(
                self,
                "平台已经登录",
                f"当前已使用 <b>{district}</b> 的登录状态，不需要再次登录。",
            )
            return
        self._open_login_after_check = True
        self._login_check_retries = 0
        self.login_status.setText("正在读取下载页面的登录状态…")
        self.login_button.setEnabled(False)
        self.login_button.setText("正在确认登录状态…")
        self.login_check_requested.emit()

    def login_check_finished(self, login_data: dict[str, str]) -> None:
        self.login_button.setEnabled(True)
        if login_data.get("token"):
            self._open_login_after_check = False
            self.set_login_data(login_data)
            return
        if self.login_data.get("token"):
            self._open_login_after_check = False
            self.set_login_data(self.login_data)
            return
        self.login_button.setText("打开登录页面")
        self.login_status.setText("尚未登录")
        if not self._open_login_after_check:
            return
        if self._login_check_retries < 2:
            self._login_check_retries += 1
            self.login_status.setText("正在再次确认平台登录状态…")
            QTimer.singleShot(600, self.login_check_requested.emit)
            return
        self._open_login_after_check = False
        self._show_login_dialog()

    def request_login_check(self) -> None:
        if self.login_data.get("token"):
            return
        self.login_status.setText("正在自动确认平台登录状态…")
        self.login_check_requested.emit()

    def _show_login_dialog(self) -> None:
        dialog = SanziLoginDialog(self.profile, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self.login_captured.emit(dialog.login_data)
        self.set_login_data(dialog.login_data)

    def set_login_data(self, login_data: dict[str, str]) -> None:
        if not login_data.get("token"):
            return
        self.login_data = dict(login_data)
        self.login_status.setObjectName("safeOperationHint")
        district = self.login_data.get("districtname", "")
        code = self.login_data.get("districtcode", "")
        if district or code:
            label = district or "当前地区"
            suffix = f"（{code}）" if code else ""
            self.login_status.setText(f"平台已登录｜{label}{suffix}")
        else:
            self.login_status.setText("平台已登录｜可以直接检查并上传照片")
        self.login_button.setText("平台已登录")
        self.switch_account_button.setEnabled(True)
        self.logout_button.setEnabled(True)
        self._upload_inputs_changed()
        self._refresh_upload_source_check()
        self.login_status.style().unpolish(self.login_status)
        self.login_status.style().polish(self.login_status)

    def switch_account(self) -> None:
        if not ask_confirm(
            self,
            "切换平台账号？",
            "当前账号会退出，然后打开新的登录页面。<br>"
            "已选择的照片和上传设置不会被删除。",
            "切换账号",
        ):
            return
        self.login_status.setText("正在退出当前账号…")
        self.clear_login_requested.emit(True)

    def logout(self) -> None:
        if not ask_confirm(
            self,
            "退出平台登录？",
            "软件会清除当前账号的登录状态，下次上传时需要重新登录。",
            "退出登录",
        ):
            return
        self.login_status.setText("正在退出当前账号…")
        self.clear_login_requested.emit(False)

    def reset_login_state(self) -> None:
        self.login_data = {}
        self._open_login_after_check = False
        self._login_check_retries = 0
        self.login_status.setObjectName("dangerOperationHint")
        self.login_status.setText("尚未登录")
        self.login_button.setEnabled(True)
        self.login_button.setText("打开登录页面")
        self.switch_account_button.setEnabled(False)
        self.logout_button.setEnabled(False)
        self._upload_inputs_changed()
        self.login_status.style().unpolish(self.login_status)
        self.login_status.style().polish(self.login_status)

    def choose_upload_kml(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "选择本次上传对应的图斑文件",
            "",
            "图斑文件 (*.kml)",
        )
        if files:
            self.upload_kml_edit.setPlainText("\n".join(files))
            self._refresh_upload_source_check()

    def _upload_kml_paths(self) -> tuple[str, ...]:
        return tuple(
            line.strip()
            for line in self.upload_kml_edit.toPlainText().splitlines()
            if line.strip()
        )

    def choose_photo_root(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self, "选择整理好的照片文件夹", self.photo_root_edit.text()
        )
        if not directory:
            return
        self.photo_root_edit.setText(directory)
        try:
            groups = scan_upload_groups(directory)
            photo_count = sum(len(group.photos) for group in groups)
            _set_stat(self.group_metric, str(len(groups)))
            self.scan_summary.setText(f"找到 {len(groups)} 个图斑文件夹，共 {photo_count} 张照片")
            self._refresh_upload_source_check(groups)
        except Exception as exc:
            friendly, _ = friendly_error_message(str(exc))
            self.scan_summary.setText(friendly)

    def _refresh_upload_source_check(self, groups=None) -> None:
        paths = self._upload_kml_paths()
        photo_root = self.photo_root_edit.text().strip()
        if not paths:
            self.kml_match_summary.setText("尚未选择 KML")
            return
        try:
            codes = read_upload_landcodes(paths)
        except Exception as exc:
            self.kml_match_summary.setText(str(exc))
            return
        if not photo_root:
            self.kml_match_summary.setText(f"KML 中有 {len(codes)} 个图斑，请继续选择照片文件夹")
            return
        try:
            groups = groups if groups is not None else scan_upload_groups(photo_root)
            blocked = validate_upload_groups(
                groups,
                codes,
                self.login_data.get("districtcode", ""),
            )
            if blocked:
                self.kml_match_summary.setObjectName("dangerOperationHint")
                self.kml_match_summary.setText(
                    f"编号校验未通过：{len(blocked)} 个文件夹与当前地区或 KML 不一致，禁止上传"
                )
            else:
                self.kml_match_summary.setObjectName("safeOperationHint")
                self.kml_match_summary.setText(
                    f"编号校验通过：{len(groups)} 个照片文件夹均存在于 KML 中"
                )
            self.kml_match_summary.style().unpolish(self.kml_match_summary)
            self.kml_match_summary.style().polish(self.kml_match_summary)
        except Exception as exc:
            self.kml_match_summary.setText(str(exc))

    def _upload_inputs_changed(self, *_args) -> None:
        self.results = []
        if hasattr(self, "upload_button"):
            self.upload_button.setEnabled(False)
        if hasattr(self, "result_table"):
            self.result_table.setRowCount(0)
        for metric_name in ("ready_metric", "success_metric", "skip_metric", "fail_metric"):
            metric = getattr(self, metric_name, None)
            if metric:
                _set_stat(metric, "0")

    def options(self) -> UploadOptions:
        return UploadOptions(
            token=self.login_data.get("token", ""),
            token_header=self.login_data.get("token_header", "Authorization"),
            cookie=self.login_data.get("cookie", ""),
            districtcode=self.login_data.get("districtcode", ""),
            districtname=self.login_data.get("districtname", ""),
            photo_root=self.photo_root_edit.text().strip(),
            kml_paths=self._upload_kml_paths(),
            max_photos=self.max_photos_spin.value(),
            only_with_use_status=self.required_status_check.isChecked(),
            skip_uploaded=self.skip_uploaded_check.isChecked(),
            average_pick=self.average_pick_check.isChecked(),
        )

    def precheck(self) -> None:
        self._upload_inputs_changed()
        self.run_task(
            "正在检查三资平台和照片…",
            run_upload,
            (self.options(), True),
            self._results_ready,
        )

    def upload(self) -> None:
        if (
            not any(result.status == "可上传" for result in self.results)
            or any(result.status == "阻止" for result in self.results)
        ):
            show_warning(
                self,
                "请先检查照片",
                "请先点击“先检查哪些照片能上传”，确认有准备上传的照片。",
            )
            return
        if not ask_confirm(
            self,
            "确认上传照片吗？",
            "这些照片会真正上传到三资平台对应的图斑中。"
            "<br><br>建议先确认右侧检查结果没有问题。",
            "开始上传",
        ):
            return
        self.run_task(
            "正在上传照片到三资平台…",
            run_upload,
            (self.options(), False),
            self._results_ready,
        )

    def _results_ready(self, results: list[UploadResult]) -> None:
        self.results = results
        blocked = sum(item.status == "阻止" for item in results)
        self.result_table.setRowCount(len(results))
        for row, result in enumerate(results):
            display_message = result.message
            if result.status == "失败" and result.message:
                display_message, _ = friendly_error_message(result.message)
            for column, value in enumerate(
                (result.landcode, result.filename, result.status, display_message)
            ):
                item = QTableWidgetItem(value)
                if column == 2:
                    color = {
                        "成功": "#14815b",
                        "可上传": "#1769e0",
                        "跳过": "#a66300",
                        "阻止": "#c0392b",
                        "失败": "#c0392b",
                    }.get(result.status, "#334155")
                    item.setForeground(QColor(color))
                self.result_table.setItem(row, column, item)
        _set_stat(self.ready_metric, str(sum(item.status == "可上传" for item in results)))
        _set_stat(self.success_metric, str(sum(item.status == "成功" for item in results)))
        _set_stat(self.skip_metric, str(sum(item.status == "跳过" for item in results)))
        _set_stat(
            self.fail_metric,
            str(sum(item.status in {"失败", "阻止"} for item in results)),
        )
        ready = sum(item.status == "可上传" for item in results)
        success = sum(item.status == "成功" for item in results)
        skipped = sum(item.status == "跳过" for item in results)
        failed = sum(item.status == "失败" for item in results)
        self.upload_button.setEnabled(bool(ready) and not blocked)
        if blocked:
            show_error(
                self,
                "已阻止上传",
                f"发现 <b>{blocked}</b> 个照片文件夹与当前登录地区或所选 KML 不一致。"
                "<br><br>请重新选择正确的 KML 或照片文件夹，软件不会上传任何照片。",
            )
        elif ready:
            show_success(
                self,
                "检查完成",
                f"准备上传 {ready} 张照片；暂不上传 {skipped} 项；发现问题 {failed} 项。"
                "<br><br>确认右侧明细后，可以点击“确认上传照片”。",
            )
        else:
            show_success(
                self,
                "处理完成",
                f"上传成功 {success} 张；跳过 {skipped} 项；失败 {failed} 项。",
            )

    def export_log(self) -> None:
        if not self.results:
            show_info(self, "还没有结果", "请先检查或上传照片，再保存结果表。")
            return
        filename, _ = QFileDialog.getSaveFileName(
            self, "保存上传结果", "三资上传结果.csv", "表格文件 (*.csv)"
        )
        if filename:
            write_upload_log(self.results, filename)
            show_success(self, "结果表已保存", f"文件已保存到：<br>{filename}")


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1440, 900)
        self.setMinimumSize(1080, 680)
        self.state = AppState()
        self.pool = QThreadPool.globalInstance()
        self.active_tasks = 0
        self.task_dialog: TaskProgressDialog | None = None
        self.sanzi_profile = QWebEngineProfile("sanzi-platform", self)
        self.sanzi_profile.setPersistentCookiesPolicy(
            QWebEngineProfile.PersistentCookiesPolicy.ForcePersistentCookies
        )
        self.platform_credentials: dict[str, str] = {}
        self.credential_interceptor = PlatformCredentialInterceptor(self)
        self._clearing_platform_login = False
        self.credential_interceptor.credentials_found.connect(
            self._platform_credentials_captured
        )
        self.sanzi_profile.setUrlRequestInterceptor(self.credential_interceptor)

        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_topbar())

        workspace = QWidget()
        workspace_layout = QHBoxLayout(workspace)
        workspace_layout.setContentsMargins(0, 0, 0, 0)
        workspace_layout.setSpacing(0)
        self.navigation = QListWidget()
        self.navigation.setObjectName("navigation")
        self.navigation.addItems(
            [
                "下载平台图斑",
                "制作无人机航线",
                "给照片加水印",
                "按图斑整理照片",
                "上传照片到平台",
                "查看照片地图",
            ]
        )
        self.navigation.setFixedWidth(176)
        self.navigation.currentRowChanged.connect(self._switch_page)
        workspace_layout.addWidget(self.navigation)

        self.stack = QStackedWidget()
        self.photo_page = PhotoWorkspacePage(self.state, self.run_task)
        self.land_page = LandWorkspacePage(
            self.state,
            self.run_task,
        )
        self.route_page = HtmlToolPage(
            "制作无人机航线",
            "在地图上规划飞行路线，并保存为无人机可用的航线文件",
            application_resource("index.html"),
            [
                "导入图斑文件，或在地图上选择需要巡查的区域。",
                "选择无人机型号并填写飞行高度、速度等信息。",
                "生成航线后检查航点和飞行方向。",
                "保存航线文件，再导入无人机应用。",
            ],
        )
        self.map_page = HtmlToolPage(
            "查看照片地图",
            "在地图上查看照片位置、图斑和航线",
            application_resource("gps_map.html"),
            [
                "通过页面中的“图层工具”加载图斑或航线文件。",
                "使用搜索框查找图斑编号或名称。",
                "在图层列表中控制显示和隐藏。",
                "该页面需要联网加载地图底图。",
            ],
        )
        self.download_page = VisibleLandDownloadPage(self.sanzi_profile)
        self.upload_page = SanziUploadPage(self.run_task, self.sanzi_profile)
        self.download_page.login_captured.connect(self.upload_page.set_login_data)
        self.download_page.login_checked.connect(self.upload_page.login_check_finished)
        self.download_page.login_captured.connect(self._platform_credentials_captured)
        self.upload_page.login_captured.connect(self._platform_credentials_captured)
        self.upload_page.login_check_requested.connect(self._check_platform_login)
        self.upload_page.clear_login_requested.connect(self._clear_platform_login)
        if self.platform_credentials:
            self.upload_page.set_login_data(self.platform_credentials)
        self.stack.addWidget(self.download_page)
        self.stack.addWidget(self.route_page)
        self.stack.addWidget(self.photo_page)
        self.stack.addWidget(self.land_page)
        self.stack.addWidget(self.upload_page)
        self.stack.addWidget(self.map_page)
        workspace_layout.addWidget(self.stack, 1)
        root.addWidget(workspace, 1)
        self.setCentralWidget(central)
        self.navigation.setCurrentRow(0)

        # 保留兼容属性，旧测试和已有扩展仍可访问水印/输出配置。
        self.watermark_page = self.photo_page
        self.output_page = self.photo_page

        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(1)
        self.progress.setMaximumWidth(160)
        self.status_label = QLabel("就绪")
        self.statusBar().addWidget(self.status_label, 1)
        self.statusBar().addPermanentWidget(self.progress)
        self.state.photos_changed.connect(self._update_status)
        self.land_page.photo_stats_changed.connect(
            lambda _total, _gps: self._update_status()
        )
        self.state.log_added.connect(self._show_log)
        self._update_status()

    def _build_topbar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("topbar")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(18, 0, 18, 0)
        mark = QLabel("三")
        mark.setObjectName("brandMark")
        brand_text = QVBoxLayout()
        brand_text.setSpacing(0)
        name = QLabel(APP_NAME)
        name.setObjectName("brandName")
        privacy_note = QLabel("照片与本地文件均在本机处理；地图、平台登录及上传功能仅在使用时联网")
        privacy_note.setObjectName("brandSubtitle")
        brand_text.addWidget(name)
        brand_text.addWidget(privacy_note)
        layout.addWidget(mark)
        layout.addLayout(brand_text)
        layout.addStretch()
        return bar

    def _switch_page(self, index: int) -> None:
        self.stack.setCurrentIndex(max(0, index))
        self._update_status()
        if index == 1 and not self.route_page.loaded:
            self.route_page.load()
        elif index == 4:
            self.upload_page.request_login_check()
        elif index == 5:
            self.map_page.load()

    def _platform_credentials_captured(self, login_data: dict[str, str]) -> None:
        merged = self.credential_interceptor.merge_page_data(login_data)
        if not merged.get("token"):
            return
        self.platform_credentials = merged
        self.sanzi_profile.setProperty("platform_login_data", merged)
        if hasattr(self, "upload_page"):
            self.upload_page.set_login_data(merged)

    def _check_platform_login(self) -> None:
        cached = self.credential_interceptor.merge_page_data(self.platform_credentials)
        if cached.get("token"):
            self.upload_page.login_check_finished(cached)
            return
        self.download_page.capture_login()

    def _clear_platform_login(self, open_login_after: bool) -> None:
        if self._clearing_platform_login:
            return
        self._clearing_platform_login = True
        self.credential_interceptor.capture_enabled = False
        self.platform_credentials = {}
        self.credential_interceptor.last_login_data = {}
        self.sanzi_profile.setProperty("platform_login_data", {})
        clear_script = """
        (() => {
          try { localStorage.clear(); } catch (_) {}
          try { sessionStorage.clear(); } catch (_) {}
          return true;
        })()
        """
        self.download_page.web_view.page().runJavaScript(
            clear_script,
            lambda _value: self._finish_clear_platform_login(open_login_after),
        )
        QTimer.singleShot(
            1500,
            lambda: self._finish_clear_platform_login(open_login_after),
        )

    def _finish_clear_platform_login(self, open_login_after: bool) -> None:
        if not self._clearing_platform_login:
            return
        self._clearing_platform_login = False
        self.sanzi_profile.cookieStore().deleteAllCookies()
        self.sanzi_profile.clearHttpCache()
        self.upload_page.reset_login_state()
        self.download_page.status_label.setText("平台账号已退出")
        self.credential_interceptor.capture_enabled = True
        if open_login_after:
            QTimer.singleShot(150, self.upload_page._show_login_dialog)
        else:
            self.download_page.web_view.setUrl(QUrl(SanziLoginDialog.LOGIN_URL))

    def run_task(
        self,
        label: str,
        function: Callable,
        args: tuple,
        on_result: Callable | None = None,
    ) -> None:
        if self.active_tasks:
            show_info(self, "正在处理", "请等待当前操作完成后，再执行其他操作。")
            return
        worker = Worker(function, *args)
        if on_result:
            worker.signals.result.connect(on_result)
        worker.signals.error.connect(self._show_error)
        worker.signals.finished.connect(self._task_finished)
        self.active_tasks += 1
        self.progress.setRange(0, 0)
        self.status_label.setText(label)
        self.task_dialog = TaskProgressDialog(self, label)
        self.task_dialog.show()
        self.pool.start(worker)

    def _task_finished(self) -> None:
        self.active_tasks = max(0, self.active_tasks - 1)
        if not self.active_tasks:
            self.progress.setRange(0, 1)
            self.progress.setValue(1)
            if self.task_dialog:
                self.task_dialog.close()
                self.task_dialog.deleteLater()
                self.task_dialog = None
            self._update_status()

    def _show_error(self, message: str) -> None:
        self.state.log(f"错误：{message}")
        friendly, details = friendly_error_message(message)
        show_error(self, "操作没有完成", friendly, details)

    def _update_status(self) -> None:
        if not hasattr(self, "status_label"):
            return
        if hasattr(self, "navigation") and self.navigation.currentRow() == 3:
            photos = self.land_page.photos
        else:
            photos = self.state.photos
        total = len(photos)
        gps = sum(photo.has_gps for photo in photos)
        self.status_label.setText(f"照片 {total} 张｜有定位 {gps} 张｜无定位 {total - gps} 张")

    def _show_log(self, message: str) -> None:
        self.status_label.setText(message)


def _process_output(
    plans,
    output: str,
    config: WatermarkConfig,
    processing_mode: str = "fast",
) -> tuple[int, list[str]]:
    output_dir = prepare_writable_output(output)
    if processing_mode not in {"fast", "compatible"}:
        raise ValueError("照片处理速度模式无效")
    reserved: set[str] = set()
    prepared_plans = []
    for plan in plans:
        destination = _reserve_output_destination(
            output_dir,
            plan.new_filename,
            reserved,
        )
        prepared_plans.append((plan, destination.name))

    def process_one(item) -> str:
        plan, output_filename = item
        try:
            if config.enabled:
                apply_watermark(plan.photo, output_dir, config, output_filename)
            else:
                destination = output_dir / output_filename
                shutil.copy2(plan.photo.full_path, destination)
            return ""
        except Exception as exc:
            return f"{plan.photo.filename}: {exc}"

    workers = min(2, max(1, len(prepared_plans))) if processing_mode == "fast" else 1
    if workers == 1:
        results = [process_one(item) for item in prepared_plans]
    else:
        with ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="watermark-output",
        ) as executor:
            results = list(executor.map(process_one, prepared_plans))
    errors = [result for result in results if result]
    succeeded = len(results) - len(errors)
    return succeeded, errors


def _reserve_output_destination(
    directory: Path,
    filename: str,
    reserved: set[str],
) -> Path:
    stem = Path(filename).stem
    suffix = Path(filename).suffix
    candidate = directory / filename
    counter = 1
    while candidate.exists() or str(candidate).casefold() in reserved:
        candidate = directory / f"{stem}_{counter}{suffix}"
        counter += 1
    reserved.add(str(candidate).casefold())
    return candidate


class _PanelBody:
    def __init__(self, layout: QVBoxLayout, header_layout: QHBoxLayout) -> None:
        self.layout = layout
        self.header_layout = header_layout


def _panel(title: str) -> tuple[QFrame, _PanelBody]:
    panel = QFrame()
    panel.setObjectName("panel")
    layout = QVBoxLayout(panel)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(0)
    header = QFrame()
    header.setObjectName("panelHeader")
    header_layout = QHBoxLayout(header)
    header_layout.setContentsMargins(14, 0, 12, 0)
    label = QLabel(title)
    label.setObjectName("panelTitle")
    header_layout.addWidget(label)
    layout.addWidget(header)
    return panel, _PanelBody(layout, header_layout)


def _field_button(field: QWidget, button: QWidget) -> QWidget:
    widget = QWidget()
    layout = QHBoxLayout(widget)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(6)
    layout.addWidget(field, 1)
    layout.addWidget(button)
    return widget


def _two_fields(first: QWidget, second: QWidget) -> QWidget:
    widget = QWidget()
    layout = QHBoxLayout(widget)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(6)
    layout.addWidget(first, 1)
    layout.addWidget(second, 1)
    return widget


def _color_button(parent: QWidget, edit: QLineEdit) -> QPushButton:
    button = QPushButton(edit.text())
    button.setMinimumWidth(76)

    def choose() -> None:
        initial = QColor(edit.text()) if QColor(edit.text()).isValid() else QColor("#FFFFFF")
        color = QColorDialog.getColor(initial, parent)
        if color.isValid():
            value = color.name().upper()
            edit.setText(value)
            button.setText(value)

    button.clicked.connect(choose)
    edit.textChanged.connect(button.setText)
    return button


def _step_badge(number: str, text: str, active: bool) -> QWidget:
    widget = QWidget()
    widget.setObjectName("stepActive" if active else "stepInactive")
    layout = QHBoxLayout(widget)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(5)
    badge = QLabel(number)
    badge.setObjectName("stepNumber")
    layout.addWidget(badge)
    layout.addWidget(QLabel(text))
    return widget


def _step_line() -> QFrame:
    line = QFrame()
    line.setObjectName("stepLine")
    line.setFixedSize(26, 1)
    return line


def _metric(label: str, value: str) -> QFrame:
    frame = QFrame()
    frame.setProperty("valueLabel", True)
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(1)
    caption = QLabel(label)
    caption.setObjectName("metricCaption")
    number = QLabel(value)
    number.setObjectName("metricValue")
    layout.addWidget(caption)
    layout.addWidget(number)
    return frame


def _set_metric(frame: QFrame, value: str) -> None:
    label = frame.findChild(QLabel, "metricValue")
    if label:
        label.setText(value)


def _stat(label: str, value: str, tone: str = "") -> QFrame:
    frame = QFrame()
    frame.setObjectName("statCard")
    if tone:
        frame.setProperty("tone", tone)
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(14, 10, 14, 10)
    number = QLabel(value)
    number.setObjectName("statValue")
    caption = QLabel(label)
    caption.setObjectName("statCaption")
    layout.addWidget(number)
    layout.addWidget(caption)
    return frame


def _set_stat(frame: QFrame, value: str) -> None:
    label = frame.findChild(QLabel, "statValue")
    if label:
        label.setText(value)


def _caption(text: str) -> QLabel:
    label = QLabel(text)
    label.setObjectName("sectionCaption")
    return label


def _open_directory(path: str) -> None:
    directory = Path(path).expanduser()
    if directory.is_dir():
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(directory.resolve())))
    else:
        show_info(None, "文件夹还不存在", "请先完成一次处理，或重新选择保存位置。")
