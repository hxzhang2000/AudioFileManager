"""批处理进度组件（§8A.4）。

主界面底部的进度显示区，展示批处理的总进度、当前文件、子步骤进度与
实时日志输出，并提供开始/停止按钮。

功能：
- 总进度条 QProgressBar（已完成文件数 / 总文件数）
- 当前文件名 QLabel
- 子步骤进度（步骤名 + 步骤序号/总数 + 子进度条）
- 日志输出 QPlainTextEdit（只读，自动滚动到底部）
- 开始/停止按钮（运行态自动切换可用性）

信号：
- ``start_requested()``：用户点击「开始」
- ``stop_requested()``：用户点击「停止」

与 :class:`processor.batch_processor.BatchProcessor` 的对接：
- ``progress_updated(int, int, str, int, int)`` 信号直接连到
  :meth:`update_progress`，参数为
  ``(current, total, step_name, step_index, step_total)``。
- ``file_finished`` / ``batch_finished`` 等信号由主窗口转发为
  :meth:`log_message` 调用。
"""

from __future__ import annotations

import logging
import math
import time
from typing import Optional

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtGui import QTextCursor
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from utils.logger import logger

# 子步骤名称（英文短码）→ 中文显示（与 BatchProcessor._SUB_STEPS 对齐）
_STEP_TEXT = {
    "parsing": "文件名解析",
    "searching": "元数据搜索",
    "enriching": "封面/歌词补全",
    "encoding": "编码统一",
    "writing": "标签写入",
    "organizing": "目录整理",
    "skipped": "已跳过",
}

# 总进度条基础文本格式（%v=当前 %m=总数 %p=百分比），ETA 文本追加其后
_BASE_FORMAT = "总进度 %v / %m  (%p%)"


def _format_duration(seconds: float) -> str:
    """将秒数格式化为中文时长字符串（如 ``"2 分 05 秒"``）。

    Args:
        seconds: 时长（秒），可为负数或无穷大（返回占位符）。

    Returns:
        中文时长字符串；非法输入返回 ``"—"``。
    """
    if not math.isfinite(seconds) or seconds < 0:
        return "—"
    total = int(round(seconds))
    if total < 60:
        return f"{total} 秒"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes} 分 {secs:02d} 秒"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} 时 {minutes:02d} 分"


class ProgressWidget(QWidget):
    """批处理进度显示组件（§8A.4）。"""

    # —— 信号 ——
    # 用户点击「开始」按钮
    start_requested = pyqtSignal()
    # 用户点击「停止」按钮
    stop_requested = pyqtSignal()
    # 用户点击「暂停/继续」按钮
    pause_requested = pyqtSignal()

    # 日志最大保留行数（超出后从头部截断，避免内存无限增长）
    _MAX_LOG_BLOCKS = 2000

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("ProgressWidget")
        # 防止垂直分隔条将进度区完全压扁（issue #1 fix）
        self.setMinimumHeight(160)

        self._running = False
        self._start_time = None  # ETA 计时起点（每个批次重置）

        self._setup_ui()
        self.set_running(False)

    # ============================================================
    # UI 构建
    # ============================================================
    def _setup_ui(self):
        """构建进度区：总进度 + 当前文件 + 子步骤 + 日志 + 按钮。"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(6)

        # —— 顶部：总进度 + 按钮 ——
        top_row = QHBoxLayout()
        top_row.setSpacing(8)

        # 总进度条
        self._total_bar = QProgressBar(self)
        self._total_bar.setObjectName("totalProgressBar")
        self._total_bar.setFormat("总进度 %v / %m  (%p%)")
        self._total_bar.setValue(0)
        self._total_bar.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        top_row.addWidget(self._total_bar, 1)

        # 开始 / 停止按钮
        self._btn_start = QPushButton("开始", self)
        self._btn_start.setObjectName("primaryButton")
        self._btn_start.setToolTip("开始批处理（处理列表中的所有文件）")
        self._btn_stop = QPushButton("停止", self)
        self._btn_stop.setObjectName("dangerButton")
        self._btn_stop.setToolTip("停止批处理（当前文件完成后停止）")
        self._btn_start.clicked.connect(self._on_start_clicked)
        self._btn_stop.clicked.connect(self._on_stop_clicked)
        self._btn_pause = QPushButton("暂停", self)
        self._btn_pause.setObjectName("warningButton")
        self._btn_pause.setToolTip("暂停批处理（当前文件完成后暂停）；再次点击继续")
        self._btn_pause.clicked.connect(self._on_pause_clicked)
        top_row.addWidget(self._btn_start)
        top_row.addWidget(self._btn_pause)
        top_row.addWidget(self._btn_stop)

        layout.addLayout(top_row)

        # —— 当前文件 + 子步骤行 ——
        info_row = QHBoxLayout()
        info_row.setSpacing(12)

        self._lbl_current = QLabel("当前文件: —", self)
        self._lbl_current.setObjectName("currentFileLabel")
        self._lbl_current.setToolTip("")
        info_row.addWidget(self._lbl_current, 3)

        self._lbl_step = QLabel("步骤: —", self)
        self._lbl_step.setObjectName("stepLabel")
        info_row.addWidget(self._lbl_step, 2)

        # 子步骤进度条（mini）
        self._step_bar = QProgressBar(self)
        self._step_bar.setObjectName("stepProgressBar")
        self._step_bar.setFixedWidth(160)
        self._step_bar.setFormat("子步骤 %p%")
        self._step_bar.setValue(0)
        info_row.addWidget(self._step_bar)

        layout.addLayout(info_row)

        # —— 日志输出 ——
        self._log_view = QPlainTextEdit(self)
        self._log_view.setObjectName("logView")
        self._log_view.setReadOnly(True)
        self._log_view.setMaximumHeight(140)
        self._log_view.setPlaceholderText("处理日志将在此显示…")
        layout.addWidget(self._log_view)

    # ============================================================
    # 公开接口
    # ============================================================
    def update_progress(self, current: int, total: int,
                        step_name: str, step_index: int, step_total: int):
        """更新进度显示。

        与 :class:`BatchProcessor.progress_updated` 信号参数完全一致，
        可直接 ``progress_updated.connect(progress_widget.update_progress)``。

        Args:
            current: 当前文件索引（从 0 起）。
            total: 文件总数。
            step_name: 子步骤名称英文短码（parsing/searching/...）。
            step_index: 子步骤序号（从 1 起）。
            step_total: 子步骤总数（BatchProcessor 为 6）。
        """
        # 总进度：以「已完成数」计，current 是从 0 起的索引，
        # 显示为 current+1 / total，避免初始显示 0/0
        if total > 0:
            # 剩余预估时长（ETA）追加到进度条文本之后
            self._total_bar.setFormat(f"{_BASE_FORMAT}  ·  {self._compute_eta(current, total)}")
            self._total_bar.setMaximum(total)
            self._total_bar.setValue(current + 1)
        else:
            self._total_bar.setFormat(_BASE_FORMAT)
            self._total_bar.setMaximum(0)  # 忙碌态
            self._total_bar.setValue(0)

        # 子步骤
        step_text = _STEP_TEXT.get(step_name, step_name)
        self._lbl_step.setText(
            f"步骤: {step_text}（{step_index}/{step_total}）")
        if step_total > 0:
            percent = int((step_index - 1) / step_total * 100)
            self._step_bar.setValue(percent)
        else:
            self._step_bar.setValue(0)

    def _compute_eta(self, current: int, total: int) -> str:
        """根据已用时间与当前进度估算剩余时长。

        以「已完成/进行中的文件数」为基准估算单文件平均耗时，
        再乘以剩余文件数。开局（< 2 个文件或 < 1 秒）数据不足时返回
        「计算中…」，避免抖动。

        Args:
            current: 当前文件索引（从 0 起）。
            total: 文件总数。

        Returns:
            形如 ``"预计剩余 2 分 05 秒"`` 或 ``"预计剩余 计算中…"``。
        """
        done = current + 1  # 已处理/处理中的文件数（含当前）
        if self._start_time is None:
            self._start_time = time.monotonic()
        elapsed = time.monotonic() - self._start_time
        # 至少累计 2 个文件、经过 1 秒后再给出估计，避免开局抖动
        if done < 2 or elapsed < 1.0:
            return "预计剩余 计算中…"
        avg_per_file = elapsed / done
        remaining = max(total - done, 0)
        return f"预计剩余 {_format_duration(avg_per_file * remaining)}"

    def set_current_file(self, file_path: str):
        """设置当前正在处理的文件名显示。

        Args:
            file_path: 当前文件完整路径（显示文件名，tooltip 显示完整路径）。
        """
        import os
        name = os.path.basename(file_path) if file_path else "—"
        self._lbl_current.setText(f"当前文件: {name}")
        self._lbl_current.setToolTip(file_path)

    def log_message(self, message: str):
        """向日志区追加一行消息（自动加时间戳并滚动到底部）。

        Args:
            message: 日志文本（单行或多行均可）。
        """
        from datetime import datetime
        timestamp = datetime.now().strftime("%H:%M:%S")
        # 多行消息首行加时间戳，后续行保持缩进
        lines = message.splitlines() or [""]
        formatted = f"[{timestamp}] {lines[0]}"
        if len(lines) > 1:
            formatted += "\n" + "\n".join("    " + ln for ln in lines[1:])

        self._log_view.appendPlainText(formatted)

        # 超过最大行数时从头部截断
        doc = self._log_view.document()
        if doc.blockCount() > self._MAX_LOG_BLOCKS:
            cursor = QTextCursor(doc)
            cursor.movePosition(QTextCursor.MoveOperation.Start)
            cursor.movePosition(
                QTextCursor.MoveOperation.Down,
                QTextCursor.MoveMode.KeepAnchor,
                doc.blockCount() - self._MAX_LOG_BLOCKS,
            )
            cursor.removeSelectedText()
            cursor.deleteChar()

        # 滚动到底部
        self._log_view.moveCursor(QTextCursor.MoveOperation.End)

    def set_running(self, running: bool):
        """切换运行状态：禁用/启用开始与停止按钮。

        Args:
            running: 是否正在运行批处理。
        """
        self._running = running
        self._btn_start.setEnabled(not running)
        self._btn_stop.setEnabled(running)
        self._btn_pause.setEnabled(running)
        self._btn_pause.setText("暂停")
        if running:
            self._btn_start.setText("运行中…")
            self._start_time = None  # 新批次重新计时
            self._total_bar.setFormat(_BASE_FORMAT)
        else:
            self._btn_start.setText("开始")
            self._total_bar.setFormat(_BASE_FORMAT)

    def is_running(self) -> bool:
        """返回当前是否处于运行态。"""
        return self._running

    def set_paused(self, paused: bool):
        """更新暂停按钮的显示状态（按钮文本在「暂停 / 继续」间切换）。

        Args:
            paused: 是否已暂停（``True`` 显示「继续」，``False`` 显示「暂停」）。
        """
        self._btn_pause.setText("继续" if paused else "暂停")

    def reset(self):
        """重置进度显示（清空进度条与当前文件标签，保留日志）。"""
        self._start_time = None  # 重置 ETA 计时
        self._total_bar.setValue(0)
        self._total_bar.setMaximum(0)
        self._total_bar.setFormat(_BASE_FORMAT)
        self._step_bar.setValue(0)
        self._lbl_current.setText("当前文件: —")
        self._lbl_current.setToolTip("")
        self._lbl_step.setText("步骤: —")

    def clear_log(self):
        """清空日志区。"""
        self._log_view.clear()

    # ============================================================
    # 内部事件
    # ============================================================
    def _on_start_clicked(self):
        """「开始」按钮：发出 start_requested 信号。"""
        self.start_requested.emit()

    def _on_pause_clicked(self):
        """「暂停/继续」按钮：发出 pause_requested 信号。"""
        self.pause_requested.emit()

    def _on_stop_clicked(self):
        """「停止」按钮：发出 stop_requested 信号。"""
        self.stop_requested.emit()
