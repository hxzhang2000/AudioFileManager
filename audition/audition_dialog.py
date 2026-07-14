"""试听弹窗（§8B.1 / §8B.3）。

``AuditionDialog`` 是一个独立 ``QDialog``，用于试听音频并同步/编辑歌词。

布局结构（左右分栏 + 底部按钮）::

    ┌────────────────────────────────────────────────────┐
    │  左侧                        │  右侧               │
    │  ┌─封面────────┐             │  ┌─歌词列表──────┐  │
    │  │             │             │  │ QListWidget   │  │
    │  └─────────────┘             │  │               │  │
    │  标题 / 歌手 / 专辑          │  │               │  │
    │  ┌─播放控制────┐             │  └───────────────┘  │
    │  │ 进度条 时间  │             │  编辑模式切换       │
    │  │ ◀ ▶/⏸ ⏹ ▶▶  │             │  偏移 -0.5s/+0.5s  │
    │  │ 音量         │             │  重置               │
    │  └─────────────┘             │                     │
    ├────────────────────────────────────────────────────┤
    │  [搜索歌词] [保存歌词]        [确定] [取消]         │
    └────────────────────────────────────────────────────┘

核心功能：
- 使用 ``QMediaPlayer`` + ``QAudioOutput`` 播放本地音频文件。
- 播放进度变化时高亮当前歌词行并自动滚动居中。
- 编辑模式下双击歌词行弹出时间输入对话框。
- 保存歌词：写入文件标签（``services.tag_writer.write_lyrics``）
  + 生成同目录 ``.lrc`` 文件。
- 封面加载 4 级优先级：``cover_data`` → 标签内嵌 → ``cover.jpg`` → 占位图。
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from utils.logger import logger

from PyQt6.QtCore import Qt, QPoint
from PyQt6.QtGui import QPixmap, QImage
from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSlider,
    QStyle,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from audition.lyrics_controller import LyricsController
from audition.lyrics_io import (
    LyricLine,
    format_lrc,
    load_lyrics_from_metadata,
    parse_lrc,
    save_lrc,
)

from PyQt6.QtCore import QThread, pyqtSignal

if TYPE_CHECKING:
    from search.provider import TrackMetadata
    from search.provider import SearchEngine


class _LyricsSearchWorker(QThread):
    """Worker to search lyrics in background thread."""

    finished_search = pyqtSignal(str)

    def __init__(self, search_engine: SearchEngine, title: str, artist: str, album: str):
        super().__init__()
        self._search_engine = search_engine
        self._title = title
        self._artist = artist
        self._album = album
        self._stop_event = threading.Event()

    def request_stop(self):
        """请求停止搜索（调用方应尽快结束 run 方法）。"""
        self._stop_event.set()

    def run(self):
        try:
            lrc = self._search_engine.search_lyrics(
                self._title, self._artist, self._album
            )
            if not self._stop_event.is_set():
                self.finished_search.emit(lrc or "")
        except Exception as e:
            logger.warning(f"歌词搜索失败: {e}")
            if not self._stop_event.is_set():
                self.finished_search.emit("")


class AuditionDialog(QDialog):
    """试听弹窗 — 播放音频、同步歌词、编辑歌词时间戳。

    Args:
        file_path: 音频文件路径（用于 QMediaPlayer 播放与歌词读写）。
        metadata: ``TrackMetadata`` 对象（含标题/歌手/专辑/封面/歌词等）。
            也兼容 dict（取 ``title``/``artist``/``album``/``release_year``
            /``cover_data``/``lyrics_text``/``lyrics`` 键）。
        parent: 父窗口。
    """

    def __init__(self, file_path: str, metadata=None, parent=None, search_engine=None):
        super().__init__(parent)
        self.file_path = file_path
        self.metadata = metadata
        self._search_engine = search_engine
        # 编辑模式标志
        self._is_editing = False
        # 歌词多版本（搜索后可切换）
        self._lyrics_versions: list[str] = []
        self._lyrics_version_idx = 0
        # 歌词搜索工作线程（closeEvent 时需清理）
        self._lyrics_worker = None

        # 歌词控制器
        self.lyrics_ctrl = LyricsController()

        # 初始化歌词：优先 metadata 中的歌词，其次从文件标签读取
        lrc_text = self._extract_lyrics_text(metadata)
        if not lrc_text:
            # 从音频文件标签读取
            try:
                lines = load_lyrics_from_metadata(file_path)
                if lines:
                    self.lyrics_ctrl.lines = lines
                    self.lyrics_ctrl.snapshot()
            except Exception as e:
                logger.debug(f"从文件标签读取歌词失败: {e}")
        else:
            self.lyrics_ctrl.set_lyrics(lrc_text)

        # 播放器
        self.player = QMediaPlayer()
        self.audio_output = QAudioOutput()
        self.player.setAudioOutput(self.audio_output)
        self.audio_output.setVolume(0.8)

        # 设置播放源（本地文件）
        if file_path and os.path.exists(file_path):
            self.player.setSource(self._to_url(file_path))

        # 构建 UI
        self._build_ui()
        self._connect_signals()

    # ============================================================
    # 辅助：从 metadata 提取歌词文本
    # ============================================================

    @staticmethod
    def _extract_lyrics_text(metadata) -> Optional[str]:
        """从 metadata 对象或 dict 中提取歌词文本。"""
        if metadata is None:
            return None
        # TrackMetadata 对象
        for attr in ("lyrics_text", "lyrics"):
            val = getattr(metadata, attr, None)
            if val:
                return val
        # dict 兼容
        if isinstance(metadata, dict):
            for key in ("lyrics_text", "lyrics"):
                val = metadata.get(key)
                if val:
                    return val
        return None

    @staticmethod
    def _to_url(file_path: str):
        """将本地文件路径转为 QMediaContent / QUrl。"""
        from PyQt6.QtCore import QUrl
        return QUrl.fromLocalFile(file_path)

    # ============================================================
    # UI 构建
    # ============================================================

    def _build_ui(self):
        """构建试听窗口 UI（左右分栏 + 底部按钮）。"""
        title = self._get_meta("title") or os.path.basename(self.file_path)
        self.setWindowTitle(f"试听：{title}")
        self.setMinimumSize(820, 600)

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(12, 12, 12, 12)

        # 左右分栏
        content_layout = QHBoxLayout()
        content_layout.setSpacing(12)

        # === 左侧：封面 + 信息 + 播放控制 ===
        left_widget = self._build_left_panel()
        content_layout.addWidget(left_widget, stretch=0)

        # === 右侧：歌词列表 + 编辑工具栏 ===
        right_widget = self._build_right_panel()
        content_layout.addWidget(right_widget, stretch=1)

        main_layout.addLayout(content_layout, stretch=1)

        # === 底部按钮栏 ===
        self._build_bottom_buttons(main_layout)

    def _build_left_panel(self) -> QWidget:
        """构建左侧面板：封面 + 歌曲信息 + 播放控制。"""
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setSpacing(10)

        # --- 封面 ---
        self.cover_label = QLabel()
        self.cover_label.setFixedSize(260, 260)
        self.cover_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.cover_label.setStyleSheet(
            "background: #2d2d2d; border-radius: 8px; color: #666; font-size: 24px;"
        )
        self._load_cover(self.cover_label)
        layout.addWidget(self.cover_label, alignment=Qt.AlignmentFlag.AlignCenter)

        # --- 歌曲信息 ---
        title = self._get_meta("title") or "未知标题"
        artist = self._get_meta("artist") or "未知歌手"
        album = self._get_meta("album") or "未知专辑"
        year = self._get_meta("release_year")
        year_str = f" · ({year})" if year else ""
        info_text = (
            f"<h2 style='color:#ffffff;'>{title}</h2>"
            f"<p style='color:#999;'>{artist} · {album}{year_str}</p>"
            f"<p style='color:#666; font-size:11px;'>{os.path.basename(self.file_path)}</p>"
        )
        self.info_label = QLabel(info_text)
        self.info_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.info_label.setWordWrap(True)
        layout.addWidget(self.info_label)

        layout.addStretch()

        # --- 进度条 + 时间 ---
        progress_layout = QHBoxLayout()
        self.progress_slider = QSlider(Qt.Orientation.Horizontal)
        self.progress_slider.setRange(0, 0)
        self.time_label = QLabel("00:00 / 00:00")
        self.time_label.setMinimumWidth(110)
        self.time_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        progress_layout.addWidget(self.progress_slider, stretch=1)
        progress_layout.addWidget(self.time_label)
        layout.addLayout(progress_layout)

        # --- 播放控制按钮 ---
        controls = QHBoxLayout()
        controls.setSpacing(6)
        self.btn_prev = QPushButton("◀◀")
        self.btn_prev.setToolTip("后退 5 秒")
        self.btn_play = QPushButton("▶")
        self.btn_play.setToolTip("播放/暂停 (空格)")
        self.btn_play.setMinimumWidth(60)
        self.btn_stop = QPushButton("⏹")
        self.btn_stop.setToolTip("停止")
        self.btn_next = QPushButton("▶▶")
        self.btn_next.setToolTip("前进 5 秒")

        controls.addStretch()
        controls.addWidget(self.btn_prev)
        controls.addWidget(self.btn_play)
        controls.addWidget(self.btn_stop)
        controls.addWidget(self.btn_next)
        controls.addStretch()
        layout.addLayout(controls)

        # --- 音量 ---
        volume_layout = QHBoxLayout()
        vol_label = QLabel("🔊")
        self.volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(80)
        self.volume_slider.setMaximumWidth(160)
        volume_layout.addStretch()
        volume_layout.addWidget(vol_label)
        volume_layout.addWidget(self.volume_slider)
        volume_layout.addStretch()
        layout.addLayout(volume_layout)

        return widget

    def _build_right_panel(self) -> QWidget:
        """构建右侧面板：歌词列表 + 编辑工具栏。"""
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setSpacing(8)

        # --- 歌词列表 ---
        lyrics_group = QGroupBox("歌词（双击跳转 / 编辑模式下双击改时间）")
        lyrics_layout = QVBoxLayout(lyrics_group)

        self.lyrics_list_widget = QListWidget()
        self.lyrics_list_widget.setAlternatingRowColors(True)
        self.lyrics_list_widget.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        # 非编辑模式默认连接双击跳转
        self.lyrics_list_widget.itemDoubleClicked.connect(
            self._on_lyric_double_clicked
        )
        self._populate_lyrics()
        lyrics_layout.addWidget(self.lyrics_list_widget)

        # --- 歌词编辑工具栏 ---
        edit_toolbar = QHBoxLayout()
        edit_toolbar.setSpacing(6)

        self.btn_toggle_edit = QPushButton("进入编辑模式")
        self.btn_offset_minus = QPushButton("<< -0.5s")
        self.btn_offset_plus = QPushButton("+0.5s >>")
        self.btn_reset = QPushButton("重置")
        self.lbl_edit_hint = QLabel("编辑模式：双击行修改时间")
        self.lbl_edit_hint.setStyleSheet("color: #FF9800; font-size: 11px;")
        self.lbl_edit_hint.hide()
        self.lbl_version = QLabel("")
        self.lbl_version.setStyleSheet("color: #888; font-size: 11px;")

        edit_toolbar.addWidget(self.btn_toggle_edit)
        edit_toolbar.addWidget(self.btn_offset_minus)
        edit_toolbar.addWidget(self.btn_offset_plus)
        edit_toolbar.addWidget(self.btn_reset)
        edit_toolbar.addStretch()
        edit_toolbar.addWidget(self.lbl_version)
        edit_toolbar.addWidget(self.lbl_edit_hint)
        lyrics_layout.addLayout(edit_toolbar)

        layout.addWidget(lyrics_group)
        return widget

    def _build_bottom_buttons(self, main_layout: QVBoxLayout):
        """构建底部按钮栏：搜索歌词 / 保存歌词 / 确定 / 取消。"""
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(8)

        self.btn_search = QPushButton("搜索歌词")
        self.btn_save = QPushButton("保存歌词")
        self.btn_save.setObjectName("btnSave")
        self.btn_ok = QPushButton("确定")
        self.btn_cancel = QPushButton("取消")

        btn_layout.addWidget(self.btn_search)
        btn_layout.addWidget(self.btn_save)
        btn_layout.addStretch()
        btn_layout.addWidget(self.btn_ok)
        btn_layout.addWidget(self.btn_cancel)
        main_layout.addLayout(btn_layout)

    # ============================================================
    # 封面加载（4 级优先级）
    # ============================================================

    def _load_cover(self, cover_label: QLabel):
        """加载封面图片到 QLabel（4 级优先级）。

        优先级：
        1. ``metadata.cover_data``（已下载的二进制数据）
        2. 音频文件标签内嵌封面（``services.tag_writer.read_cover``）
        3. 同目录 ``cover.jpg``
        4. 占位图（文字）
        """
        # 1. 优先使用已下载的封面二进制
        cover_data = self._get_meta("cover_data")
        if cover_data and self._set_pixmap_from_bytes(cover_label, cover_data):
            return

        # 2. 尝试从本地文件标签读取内嵌封面
        try:
            from services.tag_writer import read_cover
            cover_bytes = read_cover(self.file_path)
            if cover_bytes and self._set_pixmap_from_bytes(cover_label, cover_bytes):
                return
        except Exception as e:
            logger.debug(f"读取内嵌封面失败: {e}")

        # 3. 尝试同目录 cover.jpg
        cover_path = os.path.join(
            os.path.dirname(self.file_path), "cover.jpg"
        )
        if os.path.exists(cover_path):
            pixmap = QPixmap(cover_path)
            if not pixmap.isNull():
                scaled = pixmap.scaled(
                    260, 260,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
                cover_label.setPixmap(scaled)
                return

        # 4. 无封面 → 显示占位文字
        cover_label.setText("♪\n无封面")
        cover_label.setStyleSheet(
            "background: #2d2d2d; border-radius: 8px; "
            "color: #555; font-size: 28px;"
        )

    @staticmethod
    def _set_pixmap_from_bytes(label: QLabel, data: bytes) -> bool:
        """从二进制数据加载 pixmap 到 label，成功返回 True。"""
        img = QImage()
        if img.loadFromData(data):
            pixmap = QPixmap.fromImage(img).scaled(
                260, 260,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            label.setPixmap(pixmap)
            return True
        return False

    # ============================================================
    # 歌词列表填充
    # ============================================================

    def _populate_lyrics(self):
        """填充歌词列表（清空后重新填入）。"""
        self.lyrics_list_widget.clear()
        for line in self.lyrics_ctrl.lines:
            text = f"[{self._ms_to_str(line.time)}] {line.text}"
            item = QListWidgetItem(text)
            self.lyrics_list_widget.addItem(item)

    @staticmethod
    def _ms_to_str(ms: int) -> str:
        """毫秒转 ``MM:SS.ss`` 显示字符串。"""
        minutes = ms // 60_000
        seconds = (ms % 60_000) / 1_000
        return f"{minutes:02d}:{seconds:05.2f}"

    def _refresh_lyrics_display(self):
        """刷新歌词显示（编辑后调用）。"""
        self._populate_lyrics()

    # ============================================================
    # 信号连接
    # ============================================================

    def _connect_signals(self):
        """连接所有信号。"""
        # 播放控制
        self.btn_play.clicked.connect(self._on_play_pause)
        self.btn_stop.clicked.connect(self._on_stop)
        self.btn_prev.clicked.connect(
            lambda: self.player.setPosition(max(0, self.player.position() - 5000))
        )
        self.btn_next.clicked.connect(
            lambda: self.player.setPosition(
                min(self.player.duration(), self.player.position() + 5000)
            )
        )

        # 进度条 / 音量
        self.progress_slider.sliderMoved.connect(self.player.setPosition)
        self.volume_slider.valueChanged.connect(
            lambda v: self.audio_output.setVolume(v / 100.0)
        )

        # 播放器状态
        self.player.positionChanged.connect(self._on_position_changed)
        self.player.durationChanged.connect(self._on_duration_changed)
        self.player.playbackStateChanged.connect(self._on_playback_state_changed)

        # 歌词编辑工具栏
        self.btn_toggle_edit.clicked.connect(self._toggle_edit_mode)
        self.btn_offset_minus.clicked.connect(lambda: self._offset_lyrics(-500))
        self.btn_offset_plus.clicked.connect(lambda: self._offset_lyrics(500))
        self.btn_reset.clicked.connect(self._reset_lyrics)

        # 底部按钮
        self.btn_search.clicked.connect(self._search_lyrics)
        self.btn_save.clicked.connect(self._save_to_file)
        self.btn_ok.clicked.connect(self._on_accept)
        self.btn_cancel.clicked.connect(self.reject)

    # ============================================================
    # 播放控制
    # ============================================================

    def _on_play_pause(self):
        """播放/暂停切换。"""
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def _on_stop(self):
        """停止播放并重置位置到开头。"""
        self.player.stop()

    def _on_playback_state_changed(self, state):
        """播放状态变化时更新按钮文本。"""
        if state == QMediaPlayer.PlaybackState.PlayingState:
            self.btn_play.setText("⏸")
        else:
            self.btn_play.setText("▶")

    def _on_position_changed(self, pos: int):
        """播放进度变化 → 更新进度条 + 时间显示 + 歌词高亮。

        Args:
            pos: 当前播放位置（毫秒）。
        """
        self.progress_slider.setValue(pos)
        current = pos // 1000
        total = self.player.duration() // 1000
        self.time_label.setText(
            f"{current // 60:02d}:{current % 60:02d}"
            f" / {total // 60:02d}:{total % 60:02d}"
        )

        # 高亮当前歌词行
        idx = self.lyrics_ctrl.update_position(pos)
        if 0 <= idx < self.lyrics_list_widget.count():
            self.lyrics_list_widget.setCurrentRow(idx)
            item = self.lyrics_list_widget.item(idx)
            if item is not None:
                self.lyrics_list_widget.scrollToItem(
                    item, QAbstractItemView.ScrollHint.PositionAtCenter
                )

    def _on_duration_changed(self, duration: int):
        """总时长变化 → 设置进度条范围。"""
        self.progress_slider.setRange(0, duration)

    # ============================================================
    # 歌词编辑模式
    # ============================================================

    def _toggle_edit_mode(self):
        """切换编辑模式。

        编辑模式下，双击歌词行弹出时间编辑对话框；
        非编辑模式下，双击歌词行跳转到该行时间。

        注意：``disconnect()`` 必须用 ``try/except TypeError`` 包裹，
        因为首次进入/退出时可能没有已连接的信号，直接 disconnect 会抛 TypeError。
        """
        self._is_editing = not self._is_editing
        if self._is_editing:
            self.btn_toggle_edit.setText("退出编辑模式")
            self.btn_toggle_edit.setObjectName("btnEditActive")
            self.btn_toggle_edit.style().unpolish(self.btn_toggle_edit)
            self.btn_toggle_edit.style().polish(self.btn_toggle_edit)
            self.lbl_edit_hint.show()
            # 编辑模式：切换双击行为为时间编辑
            try:
                self.lyrics_list_widget.itemDoubleClicked.disconnect()
            except TypeError:
                pass  # 首次进入时无连接，安全跳过
            self.lyrics_list_widget.itemDoubleClicked.connect(
                self._on_edit_lyric_time
            )
        else:
            self.btn_toggle_edit.setText("进入编辑模式")
            self.btn_toggle_edit.setObjectName("")
            self.btn_toggle_edit.style().unpolish(self.btn_toggle_edit)
            self.btn_toggle_edit.style().polish(self.btn_toggle_edit)
            self.lbl_edit_hint.hide()
            # 退出编辑：恢复双击跳转行为
            try:
                self.lyrics_list_widget.itemDoubleClicked.disconnect()
            except TypeError:
                pass
            self.lyrics_list_widget.itemDoubleClicked.connect(
                self._on_lyric_double_clicked
            )

    def _on_lyric_double_clicked(self, item: QListWidgetItem):
        """非编辑模式下双击歌词行 → 跳转到该行时间。

        Args:
            item: 被双击的列表项。
        """
        idx = self.lyrics_list_widget.row(item)
        if 0 <= idx < len(self.lyrics_ctrl.lines):
            self.player.setPosition(self.lyrics_ctrl.lines[idx].time)

    def _on_edit_lyric_time(self, item: QListWidgetItem):
        """编辑模式双击 → 弹出时间编辑对话框。

        Args:
            item: 被双击的列表项。
        """
        idx = self.lyrics_list_widget.row(item)
        if idx < 0 or idx >= len(self.lyrics_ctrl.lines):
            return
        line = self.lyrics_ctrl.lines[idx]
        current_time = self._ms_to_str(line.time)
        new_time, ok = QInputDialog.getText(
            self, "编辑歌词时间",
            f"当前行: {line.text[:30]}\n时间 (MM:SS.ss):",
            text=current_time,
        )
        if ok and new_time:
            try:
                parts = new_time.strip().split(":")
                minutes = int(parts[0])
                seconds = float(parts[1])
                new_ms = int(minutes * 60_000 + seconds * 1_000)
                self.lyrics_ctrl.adjust_line_time(idx, new_ms)
                self._refresh_lyrics_display()
            except (ValueError, IndexError):
                QMessageBox.warning(self, "格式错误", "时间格式应为 MM:SS.ss")

    # ============================================================
    # 歌词偏移 / 重置
    # ============================================================

    def _offset_lyrics(self, offset_ms: int):
        """批量偏移所有歌词时间。

        Args:
            offset_ms: 偏移量（毫秒）。
        """
        self.lyrics_ctrl.offset(offset_ms)
        self._refresh_lyrics_display()
        offset_sec = offset_ms / 1000
        QToolTip.showText(
            self.btn_offset_plus.mapToGlobal(QPoint(0, 0)),
            f"偏移: {offset_sec:+.1f}s",
        )

    def _reset_lyrics(self):
        """重置到原始歌词。"""
        if self.lyrics_ctrl.reset():
            self._refresh_lyrics_display()
            QToolTip.showText(
                self.btn_reset.mapToGlobal(QPoint(0, 0)),
                "已重置为原始歌词",
            )
        else:
            QToolTip.showText(
                self.btn_reset.mapToGlobal(QPoint(0, 0)),
                "无原始歌词可重置",
            )

    # ============================================================
    # 歌词版本切换
    # ============================================================

    def set_lyrics_versions(self, versions: list[str]):
        """设置多版本歌词列表（由搜索阶段传入）。

        Args:
            versions: LRC 文本列表（多个搜索结果）。
        """
        self._lyrics_versions = versions
        self._lyrics_version_idx = 0
        if versions:
            self.lbl_version.setText(
                f"版本 {self._lyrics_version_idx + 1}/{len(versions)}"
            )

    def _switch_lyrics_version(self, direction: int):
        """切换歌词版本（1=下一个，-1=上一个）。"""
        if not self._lyrics_versions:
            return
        self._lyrics_version_idx = (
            (self._lyrics_version_idx + direction) % len(self._lyrics_versions)
        )
        lrc_text = self._lyrics_versions[self._lyrics_version_idx]
        self.lyrics_ctrl.set_lyrics(lrc_text)
        self._refresh_lyrics_display()
        self.lbl_version.setText(
            f"版本 {self._lyrics_version_idx + 1}/{len(self._lyrics_versions)}"
        )

    # ============================================================
    # 搜索歌词
    # ============================================================

    def _stop_lyrics_worker(self):
        """停止并等待正在运行的歌词搜索线程。

        在关闭弹窗或确认时调用，避免线程悬空导致崩溃。

        重要：禁止使用 ``terminate()``。强制终止正在执行网络 I/O 的原生
        线程会破坏解释器/SSL 库状态并导致进程崩溃（Windows 上尤为常见）。
        这里仅通过停止事件请求退出并有限等待；若超时仍未退出，则交由线程
        自行结束后 ``finished → deleteLater`` 清理，绝不强制杀线程。
        """
        worker = self._lyrics_worker
        if worker is None:
            return
        if worker.isRunning():
            worker.request_stop()
            # 断开结果回调，避免关闭后弹窗已隐藏仍触发回调弹出消息框
            try:
                worker.finished_search.disconnect(self._on_lyrics_search_done)
            except (TypeError, RuntimeError):
                pass
            worker.wait(2000)
        self._lyrics_worker = None

    def _search_lyrics(self):
        """搜索歌词（调用 SearchEngine）。

        在工作线程中执行网络搜索，避免阻塞 UI。
        搜索成功后更新歌词列表。
        """
        title = self._get_meta("title") or ""
        artist = self._get_meta("artist") or ""
        album = self._get_meta("album") or ""

        if not title:
            QMessageBox.warning(self, "提示", "缺少标题，无法搜索歌词")
            return

        if self._search_engine is None:
            QMessageBox.warning(self, "提示", "搜索引擎不可用，无法搜索歌词")
            return

        # 取消上一个可能仍在运行的搜索：仅设置停止事件并断开其回调，
        # 不阻塞等待（wait 会卡住 UI 且 terminate 会崩溃）。旧 worker 在
        # run() 结束后通过 finished → deleteLater 自行清理。
        old = self._lyrics_worker
        if old is not None:
            try:
                old.request_stop()
                old.finished_search.disconnect(self._on_lyrics_search_done)
            except (TypeError, RuntimeError):
                pass

        self.btn_search.setEnabled(False)
        self.btn_search.setText("搜索中...")

        # 在独立线程中执行同步搜索，避免阻塞 GUI
        worker = _LyricsSearchWorker(self._search_engine, title, artist, album)
        worker.finished_search.connect(self._on_lyrics_search_done)
        # 线程结束后自动释放，避免对象泄漏
        worker.finished.connect(worker.deleteLater)
        self._lyrics_worker = worker
        worker.start()

    def _on_lyrics_search_done(self, lrc_text: str):
        """歌词搜索完成回调。

        仅处理当前活跃 worker 的结果。若本次结果来自已被新搜索取代的
        旧 worker（用户中途再次点击搜索），则忽略，避免覆盖最新结果。
        """
        if self.sender() is not self._lyrics_worker:
            return
        self.btn_search.setEnabled(True)
        self.btn_search.setText("搜索歌词")

        if not lrc_text:
            QMessageBox.information(self, "搜索结果", "未找到匹配的歌词")
            return

        self.lyrics_ctrl.set_lyrics(lrc_text)
        self._refresh_lyrics_display()
        QMessageBox.information(self, "搜索成功", "已加载搜索到的歌词")

    # ============================================================
    # 保存歌词
    # ============================================================

    def _save_to_file(self):
        """将编辑后的歌词保存到音频文件标签 + 同目录 LRC 文件。

        保存方式：
        1. 调用 ``services.tag_writer.write_lyrics`` 写入文件标签
           （支持 MP3/FLAC/M4A/OGG/WMA/APE）。
        2. 生成同目录 ``.lrc`` 文件（UTF-8 编码）。
        """
        if not self.lyrics_ctrl.lines:
            QMessageBox.warning(self, "提示", "没有歌词可保存")
            return

        lrc_text = format_lrc(self.lyrics_ctrl.lines)

        # 1. 写入文件标签（多格式路由）
        tag_ok = False
        try:
            from services.tag_writer import write_lyrics
            write_lyrics(self.file_path, lrc_text)
            tag_ok = True
        except Exception as e:
            logger.warning(f"标签写入失败: {e}")
            QMessageBox.warning(self, "写入失败", f"标签写入失败: {e}")

        # 2. 生成同目录 LRC 文件
        lrc_path = os.path.splitext(self.file_path)[0] + ".lrc"
        lrc_ok = save_lrc(lrc_path, self.lyrics_ctrl.lines)

        if tag_ok or lrc_ok:
            parts = []
            if tag_ok:
                parts.append("文件标签")
            if lrc_ok:
                parts.append(lrc_path)
            QMessageBox.information(
                self, "保存成功", "歌词已保存到:\n" + "\n".join(parts)
            )
            # 更新原始快照
            self.lyrics_ctrl.snapshot()

    # ============================================================
    # 确定 / 元数据返回
    # ============================================================

    def _on_accept(self):
        """点击确定：停止播放、清理搜索线程并接受对话框。"""
        self._stop_lyrics_worker()
        try:
            self.player.stop()
        except Exception:
            pass
        self.accept()

    def get_metadata(self):
        """返回编辑后的元数据。

        若原始 metadata 为 ``TrackMetadata`` 对象，则更新其 ``lyrics`` /
        ``lyrics_text`` / ``has_synced_lyrics`` 字段后返回；
        若为 dict 则返回包含更新后歌词的 dict。

        Returns:
            更新后的 metadata 对象（与输入同类型）。
        """
        lrc_text = format_lrc(self.lyrics_ctrl.lines) if self.lyrics_ctrl.lines else ""

        if self.metadata is None:
            return {"lyrics_text": lrc_text, "lyrics": lrc_text}

        # TrackMetadata 对象
        if hasattr(self.metadata, "lyrics_text"):
            if lrc_text:
                self.metadata.lyrics_text = lrc_text
                self.metadata.lyrics = lrc_text
                self.metadata.has_synced_lyrics = lrc_text.strip().startswith("[")
            return self.metadata

        # dict 兼容
        if isinstance(self.metadata, dict):
            if lrc_text:
                self.metadata["lyrics_text"] = lrc_text
                self.metadata["lyrics"] = lrc_text
            return self.metadata

        return self.metadata

    # ============================================================
    # 辅助：从 metadata 取字段
    # ============================================================

    def _get_meta(self, field: str, default=None):
        """从 metadata 对象或 dict 中取字段值。"""
        if self.metadata is None:
            return default
        # 对象属性
        val = getattr(self.metadata, field, None)
        if val is not None:
            return val
        # dict
        if isinstance(self.metadata, dict):
            return self.metadata.get(field, default)
        return default

    # ============================================================
    # 窗口关闭清理
    # ============================================================

    def closeEvent(self, event):
        """窗口关闭时安全停止播放并清理资源，防止播放中关闭导致的崩溃（issue #4 fix）。

        关键：必须先断开播放器信号连接再 stop()，避免 stop() 触发的
        positionChanged/playbackStateChanged 回调在信号已断开的情况下仍尝试
        操作已被销毁的 UI 控件。
        """
        # 1. 停止歌词搜索线程
        self._stop_lyrics_worker()

        # 2. 断开所有播放器信号，防止停顿时回调到 UI
        self._disconnect_player_signals()

        # 3. 停止播放
        try:
            self.player.stop()
        except Exception:
            pass
        self.player.setAudioOutput(None)

        super().closeEvent(event)

    def _disconnect_player_signals(self):
        """断开所有播放器相关信号连接（线程安全）。"""
        try:
            self.player.positionChanged.disconnect()
        except (TypeError, RuntimeError):
            pass
        try:
            self.player.playbackStateChanged.disconnect()
        except (TypeError, RuntimeError):
            pass
        try:
            self.player.durationChanged.disconnect()
        except (TypeError, RuntimeError):
            pass
        try:
            self.progress_slider.sliderMoved.disconnect()
        except (TypeError, RuntimeError):
            pass

    def keyPressEvent(self, event):
        """快捷键支持：空格播放/暂停、← 后退、→ 前进。"""
        from PyQt6.QtCore import QEvent
        key = event.key()
        if key == Qt.Key.Key_Space:
            self._on_play_pause()
        elif key == Qt.Key.Key_Left:
            self.player.setPosition(max(0, self.player.position() - 5000))
        elif key == Qt.Key.Key_Right:
            self.player.setPosition(
                min(self.player.duration(), self.player.position() + 5000)
            )
        else:
            super().keyPressEvent(event)
