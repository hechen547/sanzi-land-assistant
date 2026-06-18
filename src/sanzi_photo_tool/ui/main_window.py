from __future__ import annotations

import shutil
from pathlib import Path
from typing import Callable

from PIL import Image, ImageQt
from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QPixmap
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineSettings
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
    QMessageBox,
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
    run_upload,
    scan_upload_groups,
    write_upload_log,
)
from ..services.watermark_service import (
    WatermarkConfig,
    apply_watermark,
    render_watermarked_image,
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
            self.signals.result.emit(self.function(*self.args))
        except Exception as exc:
            self.signals.error.emit(str(exc))
        finally:
            self.signals.finished.emit()


class PhotoWorkspacePage(QWidget):
    """照片选择、水印、命名和输出的一体化工作台。"""

    def __init__(self, state: AppState, run_task: Callable) -> None:
        super().__init__()
        self.setObjectName("workspacePage")
        self.state = state
        self.run_task = run_task
        self._preview_pixmap = QPixmap()

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
        heading = QLabel("照片处理")
        heading.setObjectName("pageTitle")
        subtitle = QLabel("在一个工作台内完成照片选择、水印设计、批量命名与输出")
        subtitle.setObjectName("pageSubtitle")
        title.addWidget(heading)
        title.addWidget(subtitle)
        layout.addLayout(title)
        layout.addStretch()
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
        scan = QPushButton("扫描照片")
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
        self.photo_table.itemSelectionChanged.connect(self.refresh_preview)
        self.photo_table.itemChanged.connect(self._selection_changed)
        body.layout.addWidget(self.photo_table, 1)

        self.source_summary = QLabel("尚未选择照片")
        self.source_summary.setObjectName("panelFooter")
        body.layout.addWidget(self.source_summary)
        panel.setMinimumWidth(270)
        return panel

    def _build_preview_panel(self) -> QWidget:
        panel, body = _panel("效果预览")
        body.header_layout.addStretch()
        self.original_button = QPushButton("原图")
        self.processed_button = QPushButton("处理后")
        self.original_button.setCheckable(True)
        self.processed_button.setCheckable(True)
        self.processed_button.setChecked(True)
        self.original_button.setObjectName("segmentedButton")
        self.processed_button.setObjectName("segmentedButton")
        self.original_button.clicked.connect(lambda: self._set_preview_mode(False))
        self.processed_button.clicked.connect(lambda: self._set_preview_mode(True))
        body.header_layout.addWidget(self.original_button)
        body.header_layout.addWidget(self.processed_button)

        self.preview_label = QLabel("扫描并选择照片后显示处理效果")
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
        refresh = QPushButton("刷新预览")
        refresh.clicked.connect(self.refresh_preview)
        footer_layout.addWidget(refresh)
        body.layout.addWidget(preview_footer)
        return panel

    def _build_settings_panel(self) -> QWidget:
        panel, body = _panel("处理设置")
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
        group = QGroupBox("1  左下角水印")
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
        form.addRow("字色 / 描边", color_row)

        self.stroke_width = QSpinBox()
        self.stroke_width.setRange(0, 20)
        self.stroke_width.setValue(2)
        self.opacity = QSpinBox()
        self.opacity.setRange(0, 255)
        self.opacity.setValue(255)
        form.addRow("描边 / 透明", _two_fields(self.stroke_width, self.opacity))
        self.left_margin = QSpinBox()
        self.left_margin.setRange(0, 1000)
        self.left_margin.setValue(40)
        self.bottom_margin = QSpinBox()
        self.bottom_margin.setRange(0, 1000)
        self.bottom_margin.setValue(40)
        form.addRow("左边 / 下边", _two_fields(self.left_margin, self.bottom_margin))
        return group

    def _build_naming_group(self) -> QGroupBox:
        group = QGroupBox("2  批量命名")
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
        group = QGroupBox("3  输出设置")
        group.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        form = QFormLayout(group)
        form.setContentsMargins(10, 12, 10, 10)
        form.setSpacing(8)
        self.output_edit = QLineEdit()
        self.output_edit.setPlaceholderText("选择独立输出目录")
        choose = QPushButton("选择")
        choose.clicked.connect(self.choose_output)
        form.addRow("保存目录", _field_button(self.output_edit, choose))
        self.keep_exif = QCheckBox("保留照片 EXIF 信息")
        self.keep_exif.setChecked(True)
        self.no_overwrite = QCheckBox("不覆盖原图和已有文件")
        self.no_overwrite.setChecked(True)
        self.no_overwrite.setEnabled(False)
        form.addRow("", self.keep_exif)
        form.addRow("", self.no_overwrite)
        return group

    def _build_action_bar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("actionBar")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(14, 9, 14, 9)
        layout.setSpacing(22)
        self.selected_metric = _metric("待处理", "0 张")
        self.gps_metric = _metric("含 GPS", "0 张")
        layout.addWidget(self.selected_metric)
        layout.addWidget(self.gps_metric)
        self.output_summary = QLabel("请选择输出目录")
        self.output_summary.setObjectName("mutedLabel")
        layout.addWidget(self.output_summary)
        layout.addStretch()
        open_button = QPushButton("打开输出目录")
        open_button.clicked.connect(lambda: _open_directory(self.output_edit.text()))
        process = QPushButton("开始处理照片")
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
            checkbox.toggled.connect(self.refresh_preview)
        for edit in (
            self.title_edit,
            self.font_color,
            self.stroke_color,
        ):
            edit.textChanged.connect(self._settings_changed)
        self.custom_text.textChanged.connect(self.refresh_preview)
        for spin in (
            self.font_size,
            self.stroke_width,
            self.opacity,
            self.left_margin,
            self.bottom_margin,
        ):
            spin.valueChanged.connect(self._settings_changed)
        self.naming_group.toggled.connect(self._settings_changed)
        self.output_edit.textChanged.connect(self._update_summary)

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
            QMessageBox.warning(self, "提示", "请先选择照片目录。")
            return
        self.run_task(
            "正在扫描照片…",
            scan_photos,
            (source, self.recursive_check.isChecked()),
            self._scan_done,
        )

    def _scan_done(self, photos: list[PhotoInfo]) -> None:
        self.state.set_photos(photos)
        self.state.log(f"扫描完成：共 {len(photos)} 张照片。")

    def refresh_photos(self) -> None:
        self.photo_table.blockSignals(True)
        self.photo_table.setRowCount(len(self.state.photos))
        for row, photo in enumerate(self.state.photos):
            check = QTableWidgetItem()
            check.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
            check.setCheckState(Qt.CheckState.Checked)
            self.photo_table.setItem(row, 0, check)
            time_text = photo.shot_time.strftime("%H:%M:%S") if photo.shot_time else "无拍摄时间"
            gps_text = "GPS 正常" if photo.has_gps else "无 GPS"
            item = QTableWidgetItem(f"{photo.filename}\n{time_text} · {gps_text}")
            item.setData(Qt.ItemDataRole.UserRole, row)
            self.photo_table.setItem(row, 1, item)
            self.photo_table.setRowHeight(row, 54)
        self.photo_table.blockSignals(False)
        if self.state.photos:
            self.photo_table.selectRow(0)
        self._update_summary()
        self.refresh_preview()

    def selected_photos(self) -> list[PhotoInfo]:
        selected: list[PhotoInfo] = []
        for row, photo in enumerate(self.state.photos):
            item = self.photo_table.item(row, 0)
            if item and item.checkState() == Qt.CheckState.Checked:
                selected.append(photo)
        return selected

    def current_photo(self) -> PhotoInfo | None:
        row = self.photo_table.currentRow()
        return self.state.photos[row] if 0 <= row < len(self.state.photos) else None

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
        photo = self.current_photo()
        if not photo:
            self._preview_pixmap = QPixmap()
            self.preview_label.setPixmap(QPixmap())
            self.preview_label.setText("扫描并选择照片后显示处理效果")
            self.preview_filename.setText("未选择照片")
            return
        try:
            with Image.open(photo.full_path) as source:
                if self.processed_button.isChecked():
                    image = render_watermarked_image(source, photo, self.current_config(), (1200, 850))
                else:
                    image = source.convert("RGBA")
                    image.thumbnail((1200, 850), Image.Resampling.LANCZOS)
                qt_image = ImageQt.ImageQt(image)
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

    def _set_preview_mode(self, processed: bool) -> None:
        self.processed_button.setChecked(processed)
        self.original_button.setChecked(not processed)
        self.refresh_preview()

    def _settings_changed(self, *_args) -> None:
        self.refresh_preview()
        self._update_summary()

    def _selection_changed(self, *_args) -> None:
        self._update_summary()

    def _update_summary(self, *_args) -> None:
        selected = self.selected_photos()
        gps = sum(photo.has_gps for photo in selected)
        self.photo_count_badge.setText(f"{len(self.state.photos)} 张")
        self.source_summary.setText(
            f"已选择 {len(selected)} 张 · 有 GPS {gps} 张 · 无 GPS {len(selected) - gps} 张"
        )
        _set_metric(self.selected_metric, f"{len(selected)} 张")
        _set_metric(self.gps_metric, f"{gps} 张")
        output = self.output_edit.text().strip()
        self.output_summary.setText(f"输出到：{output}" if output else "请选择输出目录")
        self._update_naming_preview()

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
            QMessageBox.warning(self, "提示", "请至少选择一张照片。")
            return
        if not output:
            QMessageBox.warning(self, "提示", "请选择输出目录。")
            return
        self.run_task(
            "正在处理照片…",
            _process_output,
            (self._build_plans(selected), output, self.current_config()),
            self._process_done,
        )

    def _process_done(self, result: tuple[int, list[str]]) -> None:
        succeeded, errors = result
        self.state.log(f"照片处理完成：成功 {succeeded}，失败 {len(errors)}。")
        details = "\n".join(errors[:5])
        message = f"成功：{succeeded}\n失败：{len(errors)}"
        if details:
            message += f"\n\n{details}"
        QMessageBox.information(self, "处理完成", message)

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
    def __init__(
        self,
        state: AppState,
        run_task: Callable,
        photo_provider: Callable[[], list[PhotoInfo]],
    ) -> None:
        super().__init__()
        self.setObjectName("workspacePage")
        self.state = state
        self.run_task = run_task
        self.photo_provider = photo_provider
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
        state.photos_changed.connect(self._update_photo_source)

    def _build_header(self) -> QWidget:
        widget = QWidget()
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        title = QVBoxLayout()
        title.setSpacing(2)
        heading = QLabel("根据图斑整理照片")
        heading.setObjectName("pageTitle")
        subtitle = QLabel("导入 KML，预分析 GPS 匹配结果，确认后按图斑建立文件夹")
        subtitle.setObjectName("pageSubtitle")
        title.addWidget(heading)
        title.addWidget(subtitle)
        layout.addLayout(title)
        layout.addStretch()
        layout.addWidget(_step_badge("1", "导入资料", True))
        layout.addWidget(_step_line())
        layout.addWidget(_step_badge("2", "预分析", True))
        layout.addWidget(_step_line())
        layout.addWidget(_step_badge("3", "整理输出", False))
        return widget

    def _build_setup_panel(self) -> QWidget:
        panel, body = _panel("整理条件")
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(14, 12, 14, 14)
        layout.setSpacing(9)

        layout.addWidget(_caption("1. KML 图斑文件"))
        self.kml_edit = QPlainTextEdit()
        self.kml_edit.setObjectName("dropZone")
        self.kml_edit.setPlaceholderText("选择一个或多个 KML 文件")
        self.kml_edit.setMaximumHeight(82)
        layout.addWidget(self.kml_edit)
        choose_kml = QPushButton("选择 KML 文件")
        choose_kml.clicked.connect(self.choose_kml)
        layout.addWidget(choose_kml)

        layout.addWidget(_caption("2. 照片来源"))
        self.photo_source_label = QLabel("照片处理页中尚无照片")
        self.photo_source_label.setObjectName("infoField")
        self.photo_source_label.setWordWrap(True)
        layout.addWidget(self.photo_source_label)

        layout.addWidget(_caption("3. 匹配规则"))
        distance_row = QHBoxLayout()
        distance_row.addWidget(QLabel("最近匹配距离"))
        self.distance_spin = QDoubleSpinBox()
        self.distance_spin.setRange(0, 100000)
        self.distance_spin.setDecimals(2)
        self.distance_spin.setSuffix(" 米")
        distance_row.addWidget(self.distance_spin, 1)
        layout.addLayout(distance_row)

        layout.addWidget(_caption("4. 文件操作"))
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

        layout.addWidget(_caption("5. 输出目录"))
        self.output_edit = QLineEdit()
        self.output_edit.setPlaceholderText("选择整理输出目录")
        choose_output = QPushButton("选择")
        choose_output.clicked.connect(self.choose_output)
        layout.addWidget(_field_button(self.output_edit, choose_output))

        analyze = QPushButton("预分析匹配结果")
        analyze.setObjectName("darkButton")
        analyze.clicked.connect(self.analyze)
        layout.addWidget(analyze)
        layout.addStretch()
        body.layout.addWidget(content, 1)
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
        self.land_metric = _stat("图斑总数", "0")
        self.matched_metric = _stat("成功匹配", "0", "success")
        self.unmatched_metric = _stat("未匹配照片", "0", "warning")
        self.no_gps_metric = _stat("无 GPS 照片", "0")
        self.empty_metric = _stat("无照片图斑", "0", "warning")
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
        self.match_table.setHorizontalHeaderLabels(["序号", "照片文件", "匹配图斑", "距离", "状态"])
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
            "整理后同时生成：未匹配照片.kml、无照片图斑.kml、整理结果.csv\n"
            "预分析不会复制或移动任何文件"
        )
        note.setObjectName("mutedLabel")
        footer_layout.addWidget(note)
        footer_layout.addStretch()
        open_output = QPushButton("打开输出目录")
        open_output.clicked.connect(lambda: _open_directory(self.output_edit.text()))
        organize = QPushButton("确认并开始整理")
        organize.setObjectName("primaryButton")
        organize.clicked.connect(self.organize)
        footer_layout.addWidget(open_output)
        footer_layout.addWidget(organize)
        layout.addWidget(footer)
        return panel

    def choose_kml(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(self, "选择一个或多个 KML", "", "KML 文件 (*.kml)")
        if files:
            self.kml_edit.setPlainText("\n".join(files))

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
        photos = self.photo_provider()
        total = len(photos)
        gps = sum(photo.has_gps for photo in photos)
        self.photo_source_label.setText(f"使用照片处理页勾选的 {total} 张照片，其中 {gps} 张含 GPS")
        _set_stat(self.no_gps_metric, str(total - gps))

    def analyze(self) -> None:
        photos = self.photo_provider()
        if not photos:
            QMessageBox.warning(self, "提示", "请先在照片处理页扫描并勾选照片。")
            return
        if not self._paths():
            QMessageBox.warning(self, "提示", "请选择 KML 文件。")
            return
        self.run_task(
            "正在分析图斑匹配…",
            analyze_photo_land_matches,
            (photos, self._paths(), self.distance_spin.value()),
            self._analysis_done,
        )

    def _analysis_done(self, result) -> None:
        lands, matches = result
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
        _set_stat(self.no_gps_metric, str(len(self.photo_provider()) - len(matches)))
        _set_stat(self.empty_metric, str(empty))

        self.match_table.setRowCount(len(matches))
        for row, match in enumerate(matches):
            status = "直接命中" if match.direct_hit else ("距离匹配" if match.land else "未匹配")
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
        self.state.log(
            f"图斑预分析完成：图斑 {len(lands)}，匹配 {matched}，未匹配 {unmatched}。"
        )

    def organize(self) -> None:
        photos = self.photo_provider()
        if not photos or not self._paths() or not self.output_edit.text().strip():
            QMessageBox.warning(self, "提示", "请准备照片、KML 文件和输出目录。")
            return
        if self.move_radio.isChecked():
            answer = QMessageBox.question(
                self,
                "确认取走原照片",
                "选择此方式后，照片会从原文件夹中消失，并被转移到整理结果中。\n\n"
                "如果只是想整理一份副本，请返回选择“保留原照片（推荐）”。\n\n"
                "确定继续吗？",
            )
            if answer != QMessageBox.StandardButton.Yes:
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
            ),
            self._organize_done,
        )

    def _organize_done(self, summary) -> None:
        self.state.log(
            f"图斑整理完成：匹配 {summary.matched}，未匹配 {summary.unmatched}，"
            f"成功 {summary.succeeded}，失败 {summary.failed}。"
        )
        QMessageBox.information(
            self,
            "整理完成",
            f"成功：{summary.succeeded}\n失败：{summary.failed}\n"
            f"未匹配：{summary.unmatched}\n无照片图斑：{summary.empty_lands}",
        )


class HtmlToolPage(QWidget):
    """在软件内嵌入项目自带的 HTML 地图工具。"""

    def __init__(
        self,
        title: str,
        subtitle: str,
        html_path: Path,
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
            return
        path = Path(destination)
        download.setDownloadDirectory(str(path.parent))
        download.setDownloadFileName(path.name)
        download.accept()


class SanziLoginDialog(QDialog):
    LOGIN_URL = (
        "http://222.143.69.159:38590/dist/#/login"
        "?redirect=%2FdataCollection&fromTokenExpired=1&prevUserId=410223206217"
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
            "请在下方平台页面中登录。登录成功进入数据采集页面后，点击“登录完成，自动获取”。"
            "\n软件不会保存你的账号和密码。"
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
        self.status_label = QLabel("等待登录")
        self.status_label.setObjectName("mutedLabel")
        refresh = QPushButton("重新打开登录页")
        refresh.clicked.connect(lambda: self.web_view.setUrl(QUrl(self.LOGIN_URL)))
        cancel = QPushButton("取消")
        cancel.clicked.connect(self.reject)
        extract = QPushButton("登录完成，自动获取")
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
        self.status_label.setText("正在读取登录信息…")
        script = """
        (() => ({
          token: localStorage.getItem('token') || '',
          tokenName: localStorage.getItem('tokenName') || 'Token',
          districtCode: localStorage.getItem('districtcode')
            || localStorage.getItem('districtCode')
            || localStorage.getItem('distinctCode') || '',
          districtName: localStorage.getItem('districtname')
            || localStorage.getItem('districtName')
            || localStorage.getItem('distinctName') || '',
          cookie: document.cookie || '',
          href: location.href
        }))()
        """
        self.web_view.page().runJavaScript(script, self._login_extracted)

    def _login_extracted(self, value: object) -> None:
        data = value if isinstance(value, dict) else {}
        token = str(data.get("token") or "").strip()
        if not token:
            self.status_label.setText("未获取到登录信息，请确认已经登录成功")
            QMessageBox.warning(
                self,
                "尚未登录",
                "没有检测到 Token。\n\n请先在上方页面完成登录，进入数据采集页面后再点击获取。",
            )
            return
        self.login_data = {
            "token": token,
            "token_header": str(data.get("tokenName") or "Token"),
            "districtcode": str(data.get("districtCode") or ""),
            "districtname": str(data.get("districtName") or ""),
            "cookie": str(data.get("cookie") or ""),
        }
        self.accept()


class VisibleLandDownloadPage(QWidget):
    PLATFORM_URL = "http://222.143.69.159:38590/dist/#/dataCollection"

    def __init__(self, profile: QWebEngineProfile) -> None:
        super().__init__()
        self.setObjectName("workspacePage")
        self.profile = profile
        self.export_script = (
            Path(__file__).resolve().parents[1] / "resources" / "visible_land_export.js"
        ).read_text(encoding="utf-8")

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(12)
        header = QHBoxLayout()
        title = QVBoxLayout()
        heading = QLabel("图斑下载")
        heading.setObjectName("pageTitle")
        subtitle = QLabel("登录三资平台，选择村庄和工作进度后，下载地图上当前显示的图斑")
        subtitle.setObjectName("pageSubtitle")
        title.addWidget(heading)
        title.addWidget(subtitle)
        header.addLayout(title)
        header.addStretch()
        login = QPushButton("登录三资平台")
        login.clicked.connect(self.open_login)
        reload_button = QPushButton("重新加载平台")
        reload_button.clicked.connect(self.load_platform)
        download = QPushButton("下载当前已显示图斑 KML")
        download.setObjectName("primaryButton")
        download.clicked.connect(self.download_visible_lands)
        header.addWidget(login)
        header.addWidget(reload_button)
        header.addWidget(download)
        root.addLayout(header)

        guide = QLabel(
            "使用方法：① 登录平台　② 进入数据采集地图并选择村庄　"
            "③ 勾选需要的工作进度，等图斑显示　④ 点击右上角下载按钮"
        )
        guide.setObjectName("safeOperationHint")
        guide.setWordWrap(True)
        root.addWidget(guide)

        frame = QFrame()
        frame.setObjectName("panel")
        frame_layout = QVBoxLayout(frame)
        frame_layout.setContentsMargins(1, 1, 1, 1)
        self.web_view = QWebEngineView()
        self.page = QWebEnginePage(profile, self.web_view)
        self.web_view.setPage(self.page)
        self.web_view.settings().setAttribute(
            QWebEngineSettings.WebAttribute.JavascriptEnabled,
            True,
        )
        frame_layout.addWidget(self.web_view)
        root.addWidget(frame, 1)
        self.status_label = QLabel("等待打开三资平台")
        self.status_label.setObjectName("mutedLabel")
        root.addWidget(self.status_label)
        self.profile.downloadRequested.connect(self._download_requested)
        self.load_platform()

    def open_login(self) -> None:
        dialog = SanziLoginDialog(self.profile, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.status_label.setText("登录成功，正在打开数据采集地图…")
            self.load_platform()

    def load_platform(self) -> None:
        self.web_view.setUrl(QUrl(self.PLATFORM_URL))

    def download_visible_lands(self) -> None:
        self.status_label.setText("正在读取地图上已显示的图斑…")
        self.web_view.page().runJavaScript(self.export_script, self._export_finished)

    def _export_finished(self, value: object) -> None:
        result = value if isinstance(value, dict) else {}
        if not result.get("ok"):
            message = str(result.get("message") or "无法读取当前地图图斑")
            self.status_label.setText(message)
            QMessageBox.warning(self, "无法下载", message)
            return
        count = int(result.get("featureCount") or 0)
        village = str(result.get("village") or "")
        states = "、".join(result.get("states") or [])
        self.status_label.setText(f"已读取 {count} 个图斑｜{village}｜{states}")

    def _download_requested(self, download) -> None:
        suggested = download.downloadFileName() or "三资已显示图斑.kml"
        destination, _ = QFileDialog.getSaveFileName(
            self,
            "保存图斑 KML",
            suggested,
            "KML 文件 (*.kml)",
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
    def __init__(self, run_task: Callable, profile: QWebEngineProfile) -> None:
        super().__init__()
        self.setObjectName("workspacePage")
        self.run_task = run_task
        self.profile = profile
        self.login_data: dict[str, str] = {}
        self.results: list[UploadResult] = []

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 16, 18, 16)
        root.setSpacing(12)
        header = QHBoxLayout()
        title = QVBoxLayout()
        heading = QLabel("三资上传")
        heading.setObjectName("pageTitle")
        subtitle = QLabel("登录三资平台，检查照片后按图斑批量上传")
        subtitle.setObjectName("pageSubtitle")
        title.addWidget(heading)
        title.addWidget(subtitle)
        header.addLayout(title)
        header.addStretch()
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
        panel, body = _panel("上传设置")
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(14, 12, 14, 14)
        layout.setSpacing(9)

        layout.addWidget(_caption("1. 登录三资平台"))
        self.login_status = QLabel("尚未登录")
        self.login_status.setObjectName("dangerOperationHint")
        self.login_status.setWordWrap(True)
        layout.addWidget(self.login_status)
        login_button = QPushButton("登录平台并自动获取")
        login_button.setObjectName("primaryButton")
        login_button.clicked.connect(self.open_login)
        layout.addWidget(login_button)

        layout.addWidget(_caption("2. 选择整理好的照片目录"))
        self.photo_root_edit = QLineEdit()
        self.photo_root_edit.setPlaceholderText("目录内应包含以图斑编号命名的子文件夹")
        choose = QPushButton("选择")
        choose.clicked.connect(self.choose_photo_root)
        layout.addWidget(_field_button(self.photo_root_edit, choose))
        self.scan_summary = QLabel("尚未扫描目录")
        self.scan_summary.setObjectName("infoField")
        layout.addWidget(self.scan_summary)

        layout.addWidget(_caption("3. 上传规则"))
        form = QFormLayout()
        self.max_photos_spin = QSpinBox()
        self.max_photos_spin.setRange(1, 20)
        self.max_photos_spin.setValue(3)
        self.max_photos_spin.setSuffix(" 张")
        form.addRow("每个图斑最多", self.max_photos_spin)
        self.required_status_check = QCheckBox("只上传已填写使用状态和地类现状的图斑")
        self.required_status_check.setChecked(True)
        form.addRow("", self.required_status_check)
        self.skip_uploaded_check = QCheckBox("跳过平台已有的同名照片")
        self.skip_uploaded_check.setChecked(True)
        form.addRow("", self.skip_uploaded_check)
        self.average_pick_check = QCheckBox("照片过多时平均抽取")
        self.average_pick_check.setChecked(True)
        form.addRow("", self.average_pick_check)
        layout.addLayout(form)

        check_button = QPushButton("上传前检查")
        check_button.setObjectName("darkButton")
        check_button.clicked.connect(self.precheck)
        upload_button = QPushButton("确认并开始上传")
        upload_button.setObjectName("primaryButton")
        upload_button.clicked.connect(self.upload)
        layout.addWidget(check_button)
        layout.addWidget(upload_button)
        layout.addStretch()
        body.layout.addWidget(content, 1)
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
        self.group_metric = _stat("图斑文件夹", "0")
        self.ready_metric = _stat("可上传", "0", "success")
        self.success_metric = _stat("上传成功", "0", "success")
        self.skip_metric = _stat("跳过", "0", "warning")
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
        note = QLabel("请先执行上传前检查，确认结果后再上传。")
        note.setObjectName("mutedLabel")
        footer_layout.addWidget(note)
        footer_layout.addStretch()
        export = QPushButton("导出上传日志")
        export.clicked.connect(self.export_log)
        footer_layout.addWidget(export)
        layout.addWidget(footer)
        return panel

    def open_login(self) -> None:
        dialog = SanziLoginDialog(self.profile, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self.login_data = dialog.login_data
        district = self.login_data.get("districtname") or "行政区未识别"
        code = self.login_data.get("districtcode") or "编码未识别"
        token = self.login_data["token"]
        masked = f"{token[:6]}…{token[-4:]}" if len(token) > 12 else "已获取"
        self.login_status.setObjectName("safeOperationHint")
        self.login_status.setText(f"登录成功｜{district}（{code}）｜Token：{masked}")
        self.login_status.style().unpolish(self.login_status)
        self.login_status.style().polish(self.login_status)

    def choose_photo_root(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self, "选择按图斑整理好的照片根目录", self.photo_root_edit.text()
        )
        if not directory:
            return
        self.photo_root_edit.setText(directory)
        try:
            groups = scan_upload_groups(directory)
            photo_count = sum(len(group.photos) for group in groups)
            _set_stat(self.group_metric, str(len(groups)))
            self.scan_summary.setText(f"识别到 {len(groups)} 个图斑文件夹，共 {photo_count} 张照片")
        except Exception as exc:
            self.scan_summary.setText(str(exc))

    def options(self) -> UploadOptions:
        return UploadOptions(
            token=self.login_data.get("token", ""),
            token_header=self.login_data.get("token_header", "Token"),
            cookie=self.login_data.get("cookie", ""),
            districtcode=self.login_data.get("districtcode", ""),
            districtname=self.login_data.get("districtname", ""),
            photo_root=self.photo_root_edit.text().strip(),
            max_photos=self.max_photos_spin.value(),
            only_with_use_status=self.required_status_check.isChecked(),
            skip_uploaded=self.skip_uploaded_check.isChecked(),
            average_pick=self.average_pick_check.isChecked(),
        )

    def precheck(self) -> None:
        self.run_task(
            "正在检查三资平台和照片…",
            run_upload,
            (self.options(), True),
            self._results_ready,
        )

    def upload(self) -> None:
        if not any(result.status == "可上传" for result in self.results):
            QMessageBox.warning(self, "请先检查", "请先点击“上传前检查”，确认存在可上传照片。")
            return
        answer = QMessageBox.question(
            self,
            "确认上传",
            "照片将真实上传到三资平台对应图斑。\n\n确认开始上传吗？",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.run_task(
            "正在上传照片到三资平台…",
            run_upload,
            (self.options(), False),
            self._results_ready,
        )

    def _results_ready(self, results: list[UploadResult]) -> None:
        self.results = results
        self.result_table.setRowCount(len(results))
        for row, result in enumerate(results):
            for column, value in enumerate(
                (result.landcode, result.filename, result.status, result.message)
            ):
                item = QTableWidgetItem(value)
                if column == 2:
                    color = {
                        "成功": "#14815b",
                        "可上传": "#1769e0",
                        "跳过": "#a66300",
                        "失败": "#c0392b",
                    }.get(result.status, "#334155")
                    item.setForeground(QColor(color))
                self.result_table.setItem(row, column, item)
        _set_stat(self.ready_metric, str(sum(item.status == "可上传" for item in results)))
        _set_stat(self.success_metric, str(sum(item.status == "成功" for item in results)))
        _set_stat(self.skip_metric, str(sum(item.status == "跳过" for item in results)))
        _set_stat(self.fail_metric, str(sum(item.status == "失败" for item in results)))
        QMessageBox.information(
            self,
            "检查完成" if any(item.status == "可上传" for item in results) else "处理完成",
            f"共生成 {len(results)} 条结果。",
        )

    def export_log(self) -> None:
        if not self.results:
            QMessageBox.information(self, "提示", "当前没有可导出的结果。")
            return
        filename, _ = QFileDialog.getSaveFileName(
            self, "导出上传日志", "三资上传日志.csv", "CSV 文件 (*.csv)"
        )
        if filename:
            write_upload_log(self.results, filename)
            QMessageBox.information(self, "导出完成", f"日志已保存到：\n{filename}")


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("三资图斑辅助工具")
        self.resize(1440, 900)
        self.setMinimumSize(1080, 680)
        self.state = AppState()
        self.pool = QThreadPool.globalInstance()
        self.active_tasks = 0
        self.sanzi_profile = QWebEngineProfile(self)

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
            ["图斑下载", "航线规划", "照片处理", "图斑整理", "三资上传", "地图软件"]
        )
        self.navigation.setFixedWidth(176)
        self.navigation.currentRowChanged.connect(self._switch_page)
        workspace_layout.addWidget(self.navigation)

        self.stack = QStackedWidget()
        self.photo_page = PhotoWorkspacePage(self.state, self.run_task)
        self.land_page = LandWorkspacePage(
            self.state,
            self.run_task,
            self.photo_page.selected_photos,
        )
        project_root = Path(__file__).resolve().parents[3]
        self.route_page = HtmlToolPage(
            "航线规划",
            "规划无人机航线并导出 KML / KMZ",
            project_root / "index.html",
        )
        self.map_page = HtmlToolPage(
            "地图软件",
            "查看 GPS 点位、照片和 KML 图层",
            project_root / "gps_map.html",
        )
        self.download_page = VisibleLandDownloadPage(self.sanzi_profile)
        self.upload_page = SanziUploadPage(self.run_task, self.sanzi_profile)
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
        self.state.log_added.connect(self._show_log)

    def _build_topbar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("topbar")
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(18, 0, 18, 0)
        mark = QLabel("三")
        mark.setObjectName("brandMark")
        brand_text = QVBoxLayout()
        brand_text.setSpacing(0)
        name = QLabel("三资图斑辅助工具")
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
        if index == 3:
            self.land_page._update_photo_source()
        elif index == 1 and not self.route_page.loaded:
            self.route_page.load()
        elif index == 5:
            self.map_page.load()

    def run_task(
        self,
        label: str,
        function: Callable,
        args: tuple,
        on_result: Callable | None = None,
    ) -> None:
        if self.active_tasks:
            QMessageBox.information(self, "任务进行中", "请等待当前任务完成后再执行其他操作。")
            return
        worker = Worker(function, *args)
        if on_result:
            worker.signals.result.connect(on_result)
        worker.signals.error.connect(self._show_error)
        worker.signals.finished.connect(self._task_finished)
        self.active_tasks += 1
        self.progress.setRange(0, 0)
        self.status_label.setText(label)
        self.pool.start(worker)

    def _task_finished(self) -> None:
        self.active_tasks = max(0, self.active_tasks - 1)
        if not self.active_tasks:
            self.progress.setRange(0, 1)
            self.progress.setValue(1)
            self._update_status()

    def _show_error(self, message: str) -> None:
        self.state.log(f"错误：{message}")
        QMessageBox.critical(self, "操作失败", message)

    def _update_status(self) -> None:
        total = len(self.state.photos)
        gps = sum(photo.has_gps for photo in self.state.photos)
        self.status_label.setText(f"照片 {total} 张｜有 GPS {gps} 张｜无 GPS {total - gps} 张")

    def _show_log(self, message: str) -> None:
        self.status_label.setText(message)


def _process_output(plans, output: str, config: WatermarkConfig) -> tuple[int, list[str]]:
    output_dir = prepare_writable_output(output)
    succeeded = 0
    errors: list[str] = []
    for plan in plans:
        try:
            if config.enabled:
                apply_watermark(plan.photo, output_dir, config, plan.new_filename)
            else:
                destination = unique_destination(output_dir, plan.new_filename)
                shutil.copy2(plan.photo.full_path, destination)
            succeeded += 1
        except Exception as exc:
            errors.append(f"{plan.photo.filename}: {exc}")
    return succeeded, errors


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
        QMessageBox.information(None, "提示", "输出目录尚不存在。")
