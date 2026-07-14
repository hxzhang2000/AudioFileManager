"""文件详情面板组件（§8A.3）。

主界面右侧的可编辑元数据面板，展示当前选中文件的完整信息，
并支持手动修正元数据、触发网络搜索、保存修改到文件标签。

功能：
- 显示当前选中文件的元数据：标题、歌手、专辑、年份、流派、封面预览
- 元数据字段均为可编辑文本框（用于手动修正）
- 搜索按钮：以 (歌手, 标题) 触发手动网络搜索
- 保存按钮：将输入框内容写入文件标签
- 封面预览 QLabel：点击放大查看原图

信号：
- ``search_requested(str, str)``：搜索按钮触发，参数为 (artist, title)
- ``save_requested(str, dict)``：保存按钮触发，参数为 (file_path, metadata_dict)

metadata 字典约定（与 :class:`parser.metadata_reader.MetadataReader`
返回结构对齐，并补充 ``file_path`` / ``cover_data``）::

    {
        "file_path": str,
        "title": str,
        "artist": str,
        "album": str,
        "year": str,            # 字符串，便于编辑
        "genre": str,
        "track_number": str,    # 字符串
        "duration": float | None,
        "bitrate": int | None,
        "sample_rate": int | None,
        "cover_data": bytes | None,
    }
"""

from __future__ import annotations

import logging
from typing import Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QPixmap, QImage, QMouseEvent
from PyQt6.QtWidgets import (
    QDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from utils.logger import logger


class _ClickableCoverLabel(QLabel):
    """可点击的封面预览 QLabel。

    点击时发出 ``clicked`` 信号，供面板弹出放大窗口。
    """

    clicked = pyqtSignal()

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("coverLabel")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setMinimumSize(220, 220)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("点击放大查看封面")
        self._set_placeholder()

    def _set_placeholder(self):
        """显示占位文字（无封面时）。"""
        self.setText("无封面\n（点击可放大）")
        self.setStyleSheet(
            "#coverLabel { color: #7f8c8d; "
            "border: 1px dashed #555; border-radius: 6px; }"
        )

    def set_cover(self, cover_data: Optional[bytes]):
        """设置封面图片二进制；``None`` 时显示占位。"""
        if not cover_data:
            self._set_placeholder()
            self._pixmap = None
            return
        img = QImage()
        if img.loadFromData(cover_data):
            pix = QPixmap.fromImage(img)
            self._pixmap = pix
            self.setPixmap(
                pix.scaled(
                    self.size(),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
            self.setStyleSheet("#coverLabel { border: 1px solid #444; border-radius: 6px; }")
        else:
            self._set_placeholder()
            self._pixmap = None

    def resizeEvent(self, event):
        """窗口缩放时按比例刷新缩略图。"""
        pix = getattr(self, "_pixmap", None)
        if pix is not None:
            self.setPixmap(
                pix.scaled(
                    self.size(),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        super().resizeEvent(event)

    def mousePressEvent(self, ev: QMouseEvent):
        """点击封面 → 发出 clicked 信号（仅左键）。"""
        if ev.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(ev)


class FileDetailPanel(QWidget):
    """文件详情面板（§8A.3）。

    右侧可编辑元数据面板，展示并允许修正当前选中文件的元数据。
    """

    # —— 信号 ——
    # 搜索按钮触发：参数为 (artist, title)
    search_requested = pyqtSignal(str, str)
    # 保存按钮触发：参数为 (file_path, metadata_dict)
    save_requested = pyqtSignal(str, dict)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("FileDetailPanel")

        # 当前文件路径与原始封面二进制（供保存时回写）
        self._file_path: str = ""
        self._cover_data: Optional[bytes] = None

        self._setup_ui()

    # ============================================================
    # UI 构建
    # ============================================================
    def _setup_ui(self):
        """构建详情面板：文件信息 + 元数据表单 + 封面 + 操作按钮（可滚动，issue #3 fix）。"""
        # —— 外层：QScrollArea 实现内容滚动 ——
        self._scroll_area = QScrollArea(self)
        self._scroll_area.setWidgetResizable(True)
        self._scroll_area.setFrameShape(QFrame.Shape.NoFrame)

        # 内部 widget 承载所有内容
        inner = QWidget()
        inner.setObjectName("detailPanelInner")
        outer = QVBoxLayout(inner)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(8)

        # —— 标题 ——
        title_label = QLabel("详情面板", inner)
        title_label.setObjectName("panelTitle")
        outer.addWidget(title_label)

        # —— 文件信息区 ——
        info_box = QGroupBox("文件信息", inner)
        info_layout = QFormLayout(info_box)
        info_layout.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self._lbl_filename = QLabel("—")
        self._lbl_filename.setWordWrap(True)
        self._lbl_filename.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self._lbl_path = QLabel("—")
        self._lbl_path.setWordWrap(True)
        self._lbl_path.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self._lbl_size = QLabel("—")
        self._lbl_duration = QLabel("—")
        self._lbl_bitrate = QLabel("—")
        info_layout.addRow("文件名:", self._lbl_filename)
        info_layout.addRow("路径:", self._lbl_path)
        info_layout.addRow("大小:", self._lbl_size)
        info_layout.addRow("时长:", self._lbl_duration)
        info_layout.addRow("比特率:", self._lbl_bitrate)
        outer.addWidget(info_box)

        # —— 元数据（可编辑）区 ——
        meta_box = QGroupBox("元数据（可编辑）", inner)
        meta_form = QFormLayout(meta_box)
        meta_form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self._edit_title = QLineEdit()
        self._edit_artist = QLineEdit()
        self._edit_album = QLineEdit()
        self._edit_year = QLineEdit()
        self._edit_genre = QLineEdit()
        self._edit_track = QLineEdit()
        # 年份/曲目号限制输入长度
        self._edit_year.setMaxLength(4)
        self._edit_year.setPlaceholderText("如 2008")
        self._edit_track.setPlaceholderText("如 3")

        meta_form.addRow("标题:", self._edit_title)
        meta_form.addRow("歌手:", self._edit_artist)
        meta_form.addRow("专辑:", self._edit_album)
        meta_form.addRow("年份:", self._edit_year)
        meta_form.addRow("流派:", self._edit_genre)
        meta_form.addRow("曲目号:", self._edit_track)
        outer.addWidget(meta_box)

        # —— 封面预览 ——
        cover_box = QGroupBox("封面（点击放大）", inner)
        cover_layout = QHBoxLayout(cover_box)
        self._cover_label = _ClickableCoverLabel(cover_box)
        self._cover_label.clicked.connect(self._on_cover_clicked)
        cover_layout.addWidget(self._cover_label, 1)
        outer.addWidget(cover_box)

        # —— 歌词预览（只读，前几行） ——
        lyrics_box = QGroupBox("歌词（LRC）", inner)
        lyrics_layout = QVBoxLayout(lyrics_box)
        self._lyrics_view = QPlainTextEdit(lyrics_box)
        self._lyrics_view.setReadOnly(True)
        self._lyrics_view.setPlaceholderText("无歌词")
        self._lyrics_view.setMaximumHeight(120)
        lyrics_layout.addWidget(self._lyrics_view)
        outer.addWidget(lyrics_box)

        outer.addStretch(1)

        # —— 操作按钮区 ——
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        self._btn_search = QPushButton("网络搜索信息", inner)
        self._btn_search.setObjectName("primaryButton")
        self._btn_search.setToolTip('根据"歌手 + 标题"从网络搜索元数据')
        self._btn_save = QPushButton("保存修改到文件", inner)
        self._btn_save.setObjectName("primaryButton")
        self._btn_save.setToolTip("将当前输入框内容写入音频文件的 ID3 标签")
        self._btn_search.clicked.connect(self._on_search_clicked)
        self._btn_save.clicked.connect(self._on_save_clicked)
        btn_row.addWidget(self._btn_search)
        btn_row.addWidget(self._btn_save)
        outer.addLayout(btn_row)

        self._scroll_area.setWidget(inner)

        # —— 将 QScrollArea 放入面板主布局 ——
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.addWidget(self._scroll_area)

        # 初始状态：无选中文件，禁用编辑
        self._set_editing_enabled(False)

    # ============================================================
    # 元数据读写
    # ============================================================
    def set_metadata(self, metadata: dict):
        """填充面板显示给定元数据。

        Args:
            metadata: 元数据字典（见模块文档字符串中的字段约定）。
        """
        self._file_path = metadata.get("file_path", "") or ""
        self._cover_data = metadata.get("cover_data")

        # 文件信息
        import os
        from pathlib import Path
        self._lbl_filename.setText(os.path.basename(self._file_path) or "—")
        self._lbl_path.setText(self._file_path or "—")
        self._lbl_path.setToolTip(self._file_path)
        try:
            size = os.path.getsize(self._file_path) if self._file_path else 0
            self._lbl_size.setText(self._format_size(size))
        except OSError:
            self._lbl_size.setText("—")

        duration = metadata.get("duration")
        self._lbl_duration.setText(self._format_duration(duration))
        bitrate = metadata.get("bitrate")
        self._lbl_bitrate.setText(f"{bitrate} kbps" if bitrate else "—")

        # 可编辑元数据
        self._edit_title.setText(str(metadata.get("title") or ""))
        self._edit_artist.setText(str(metadata.get("artist") or ""))
        self._edit_album.setText(str(metadata.get("album") or ""))
        year = metadata.get("year")
        self._edit_year.setText("" if year is None else str(year))
        self._edit_genre.setText(str(metadata.get("genre") or ""))
        track = metadata.get("track_number")
        self._edit_track.setText("" if track is None else str(track))

        # 封面
        self._cover_label.set_cover(self._cover_data)

        # 歌词预览
        lyrics = metadata.get("lyrics_text") or metadata.get("lyrics") or ""
        self._lyrics_view.setPlainText(str(lyrics))

        # 有文件则启用编辑
        self._set_editing_enabled(bool(self._file_path))

    def get_metadata(self) -> dict:
        """返回当前输入框中的元数据字典。

        Returns:
            包含 ``file_path`` 及各可编辑字段的字典（见模块文档字符串）。
            封面二进制不在此字典中（保存时由主窗口从原始数据回写）。
        """
        return {
            "file_path": self._file_path,
            "title": self._edit_title.text().strip(),
            "artist": self._edit_artist.text().strip(),
            "album": self._edit_album.text().strip(),
            "year": self._edit_year.text().strip(),
            "genre": self._edit_genre.text().strip(),
            "track_number": self._edit_track.text().strip(),
            "cover_data": self._cover_data,
        }

    def clear_panel(self):
        """清空面板（无选中文件时调用）。"""
        self._file_path = ""
        self._cover_data = None
        self._lbl_filename.setText("—")
        self._lbl_path.setText("—")
        self._lbl_size.setText("—")
        self._lbl_duration.setText("—")
        self._lbl_bitrate.setText("—")
        for edit in (self._edit_title, self._edit_artist, self._edit_album,
                     self._edit_year, self._edit_genre, self._edit_track):
            edit.clear()
        self._cover_label.set_cover(None)
        self._lyrics_view.clear()
        self._set_editing_enabled(False)

    # ============================================================
    # 外部填充辅助（供搜索结果回填）
    # ============================================================
    def fill_from_search_result(self, result_dict: dict):
        """把网络搜索结果填入输入框（不覆盖文件信息）。

        主窗口收到 ``search_requested`` 后调用 ManualSearchService，
        再把结果通过此方法填入面板供用户确认。

        Args:
            result_dict: 搜索结果字典，键为 title/artist/album/year/genre/
                track_number/cover_data/lyrics_text 中任意子集。
        """
        if "title" in result_dict and result_dict["title"]:
            self._edit_title.setText(str(result_dict["title"]))
        if "artist" in result_dict and result_dict["artist"]:
            self._edit_artist.setText(str(result_dict["artist"]))
        if "album" in result_dict and result_dict["album"]:
            self._edit_album.setText(str(result_dict["album"]))
        if "release_year" in result_dict and result_dict["release_year"]:
            self._edit_year.setText(str(result_dict["release_year"]))
        elif "year" in result_dict and result_dict["year"]:
            self._edit_year.setText(str(result_dict["year"]))
        if "genre" in result_dict and result_dict["genre"]:
            self._edit_genre.setText(str(result_dict["genre"]))
        if "track_number" in result_dict and result_dict["track_number"]:
            self._edit_track.setText(str(result_dict["track_number"]))
        if result_dict.get("cover_data"):
            self._cover_data = result_dict["cover_data"]
            self._cover_label.set_cover(self._cover_data)
        if result_dict.get("lyrics_text"):
            self._lyrics_view.setPlainText(str(result_dict["lyrics_text"]))

    # ============================================================
    # 内部事件
    # ============================================================
    def _on_search_clicked(self):
        """搜索按钮：以当前输入框的 (artist, title) 发出搜索请求。"""
        artist = self._edit_artist.text().strip()
        title = self._edit_title.text().strip()
        if not title:
            QMessageBox.information(self, "提示", "请先填写标题后再搜索。")
            return
        self.search_requested.emit(artist, title)

    def _on_save_clicked(self):
        """保存按钮：以 (file_path, metadata_dict) 发出保存请求。"""
        if not self._file_path:
            QMessageBox.information(self, "提示", "没有可保存的文件。")
            return
        self.save_requested.emit(self._file_path, self.get_metadata())

    def _on_cover_clicked(self):
        """封面被点击：弹出放大窗口显示原图。"""
        pix = getattr(self._cover_label, "_pixmap", None)
        if pix is None:
            return
        dialog = _CoverZoomDialog(pix, self)
        dialog.exec()

    # ============================================================
    # 辅助
    # ============================================================
    def _set_editing_enabled(self, enabled: bool):
        """启用/禁用所有编辑控件与按钮。"""
        for edit in (self._edit_title, self._edit_artist, self._edit_album,
                     self._edit_year, self._edit_genre, self._edit_track):
            edit.setEnabled(enabled)
        self._btn_search.setEnabled(enabled)
        self._btn_save.setEnabled(enabled)

    @staticmethod
    def _format_size(size_bytes: int) -> str:
        """字节数 → 人类可读大小（KB/MB）。"""
        try:
            size = float(size_bytes)
        except (TypeError, ValueError):
            return "—"
        if size < 1024:
            return f"{size} B"
        if size < 1024 * 1024:
            return f"{size / 1024:.1f} KB"
        return f"{size / (1024 * 1024):.2f} MB"

    @staticmethod
    def _format_duration(duration) -> str:
        """秒数 → M:SS 格式；``None`` 显示「—」。"""
        if duration is None:
            return "—"
        try:
            total = int(float(duration))
        except (TypeError, ValueError):
            return "—"
        minutes, seconds = divmod(total, 60)
        return f"{minutes}:{seconds:02d}"


class _CoverZoomDialog(QDialog):
    """封面放大显示对话框（模态）。"""

    def __init__(self, pixmap: QPixmap, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle("封面预览")
        self.setModal(True)
        layout = QVBoxLayout(self)
        label = QLabel(self)
        label.setPixmap(pixmap)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(label)
        # 点击任意位置关闭
        self._close_hint = QLabel("点击任意处关闭", self)
        self._close_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._close_hint.setStyleSheet("color: #95a5a6; padding: 4px;")
        layout.addWidget(self._close_hint)

    def mousePressEvent(self, ev: QMouseEvent):
        """点击关闭对话框。"""
        self.close()
