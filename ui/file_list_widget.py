"""文件列表面板组件（§8A.2）。

带工具栏的音频文件列表，是主界面左侧的核心组件。

功能：
- 工具栏按钮：添加文件夹 / 添加文件 / 移除选中 / 清空
- QTableWidget 显示文件列表，列含：选择、序号、文件名、歌手、标题、专辑、状态、格式
- 支持拖放添加文件（单个或多个文件、文件夹）
- 提供文件路径列表供批处理使用；批处理仅处理「已勾选」的文件（默认全选）
- 「序号」列按当前显示顺序自动编号

信号：
- ``files_changed()``：文件列表发生变化（添加/移除/清空）
- ``file_double_clicked(str)``：某一行被双击，参数为文件完整路径

设计要点：
- 文件名解析复用 :mod:`parser.filename_parser`（懒加载，失败时回退到
  简单的 ``-`` 分割启发式），保证与批处理流程解析结果一致。
- 状态字段以英文短码存储在 ``Qt.ItemDataRole.UserRole``，显示时翻译为中文，
  便于批处理线程通过 :meth:`set_file_status` 直接更新。
- 文件路径去重，保持插入顺序。
- 「选择」列存放勾选状态，批处理入口 :meth:`get_checked_files` 仅返回已勾选项。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QDragEnterEvent, QDropEvent, QAction
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QHeaderView,
    QLabel,
    QMessageBox,
    QProgressDialog,
    QStyle,
    QTableWidget,
    QTableWidgetItem,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from utils.logger import logger
from ui.file_load_worker import FileLoadWorker

# 状态码 → 中文显示文本（状态码与 BatchProcessor 的子步骤/完成状态对齐）
_STATUS_TEXT = {
    "pending": "待处理",
    "parsing": "解析中",
    "searching": "搜索中",
    "enriching": "补全中",
    "encoding": "编码中",
    "writing": "写入中",
    "organizing": "整理中",
    "done": "已处理",
    "failed": "失败",
    "skipped": "已跳过",
}


class FileListWidget(QWidget):
    """文件列表面板（§8A.2）。

    带工具栏的音频文件列表，支持拖放添加、文件夹/文件添加、移除选中、清空。
    """

    # —— 信号 ——
    # 文件列表发生变化（添加/移除/清空），主窗口据此更新状态栏与批处理可用性
    files_changed = pyqtSignal()
    # 某一行被双击，参数为文件完整路径（用于打开试听弹窗）
    file_double_clicked = pyqtSignal(str)
    # 选中行变化，参数为当前选中的文件路径（无选中为空字符串）
    selection_changed = pyqtSignal(str)

    # 表格列索引常量（增删列时仅需调整此处）
    _COL_CHECK = 0       # 勾选列（批处理仅处理已勾选项）
    _COL_INDEX = 1       # 序号列（按显示顺序自动编号）
    _COL_NAME = 2        # 文件名（单元格 data 携带完整路径）
    _COL_ARTIST = 3      # 歌手
    _COL_TITLE = 4       # 标题
    _COL_ALBUM = 5       # 专辑
    _COL_STATUS = 6      # 状态
    _COL_FORMAT = 7      # 格式

    # 表格列定义（顺序即显示顺序）
    _COLUMNS = ["选择", "序号", "文件名", "歌手", "标题", "专辑", "状态", "格式"]

    # 文件路径在 QTableWidgetItem 中存储的 data role
    _PATH_ROLE = Qt.ItemDataRole.UserRole
    # 状态码在「状态」列 item 中存储的 data role
    _STATUS_ROLE = Qt.ItemDataRole.UserRole + 1

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("FileListWidget")

        # 文件路径列表（去重，保持插入顺序）
        self._files: list[str] = []
        # 文件路径 → 行号 映射（O(1) 查找，用于状态更新与去重）
        self._path_to_row: dict[str, int] = {}

        # 后台加载任务（递归扫描 + 元数据读取迁移到 FileLoadWorker，避免 UI 卡死）
        self._load_worker = None
        self._load_dialog = None

        self._setup_ui()
        # 启用拖放接收（拖入文件/文件夹即可添加）
        self.setAcceptDrops(True)

    # ============================================================
    # UI 构建
    # ============================================================
    def _setup_ui(self):
        """构建工具栏 + 表格的纵向布局。"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # —— 工具栏 ——
        self._toolbar = QToolBar(self)
        self._toolbar.setObjectName("fileListToolbar")
        self._toolbar.setMovable(False)
        self._toolbar.setIconSize(self._toolbar.iconSize())

        # 添加文件夹
        self._act_add_folder = QAction(
            self.style().standardIcon(QStyle.StandardPixmap.SP_DirIcon),
            "添加文件夹", self)
        self._act_add_folder.setToolTip("选择一个文件夹，递归添加其中的音频文件")
        # triggered 信号会附带一个 bool（checked 状态），用 lambda 丢弃该参数，
        # 否则 add_folder(False) 会绕过「folder is None」判断而直接传入 os.walk
        self._act_add_folder.triggered.connect(lambda: self.add_folder())
        self._toolbar.addAction(self._act_add_folder)

        # 添加文件
        self._act_add_files = QAction(
            self.style().standardIcon(QStyle.StandardPixmap.SP_FileIcon),
            "添加文件", self)
        self._act_add_files.setToolTip("选择一个或多个音频文件添加到列表")
        self._act_add_files.triggered.connect(self._on_add_files_clicked)
        self._toolbar.addAction(self._act_add_files)

        self._toolbar.addSeparator()

        # 移除选中
        self._act_remove = QAction(
            self.style().standardIcon(QStyle.StandardPixmap.SP_TrashIcon),
            "移除选中", self)
        self._act_remove.setToolTip("从列表中移除当前选中的文件")
        self._act_remove.triggered.connect(self.remove_selected)
        self._toolbar.addAction(self._act_remove)

        # 清空
        self._act_clear = QAction(
            self.style().standardIcon(QStyle.StandardPixmap.SP_DialogDiscardButton),
            "清空", self)
        self._act_clear.setToolTip("清空整个文件列表")
        self._act_clear.triggered.connect(self.clear_all)
        self._toolbar.addAction(self._act_clear)

        layout.addWidget(self._toolbar)

        # —— 文件表格 ——
        self._table = QTableWidget(self)
        self._table.setObjectName("fileTable")
        self._table.setColumnCount(len(self._COLUMNS))
        self._table.setHorizontalHeaderLabels(self._COLUMNS)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)
        self._table.setSortingEnabled(True)

        # 列宽策略：允许用户手动拖拽调整列宽（Interactive）；
        # 同时给勾选列/序号列设置较窄的默认宽度，避免占用过多空间。
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(False)
        header.setMinimumSectionSize(36)
        self._table.setColumnWidth(self._COL_CHECK, 44)
        self._table.setColumnWidth(self._COL_INDEX, 50)
        self._table.setColumnWidth(self._COL_NAME, 240)
        self._table.setColumnWidth(self._COL_ARTIST, 110)
        self._table.setColumnWidth(self._COL_TITLE, 170)
        self._table.setColumnWidth(self._COL_ALBUM, 140)
        self._table.setColumnWidth(self._COL_STATUS, 90)
        self._table.setColumnWidth(self._COL_FORMAT, 64)

        # 选中行变化 → 通知主窗口更新详情面板
        self._table.itemSelectionChanged.connect(self._on_selection_changed)
        # 双击 → 发出 file_double_clicked 信号
        self._table.doubleClicked.connect(self._on_double_clicked)
        # 勾选状态变化 → 刷新计数标签
        self._table.itemChanged.connect(self._on_item_changed)
        # 排序后 → 按新顺序重排序号
        header.sortIndicatorChanged.connect(lambda *a: self._refresh_index_column())

        layout.addWidget(self._table, 1)

        # —— 底部计数标签 ——
        self._count_label = QLabel("共 0 个文件", self)
        self._count_label.setObjectName("fileCountLabel")
        layout.addWidget(self._count_label)

    # ============================================================
    # 加载入口（公开方法：添加/移除/清空）
    #
    # 扫描文件夹 + 读取元数据已迁移到 :class:`ui.file_load_worker.FileLoadWorker`
    # （后台线程），解析规则由 :func:`parser.metadata_resolver.resolve_file_meta`
    # 统一提供，本组件不再在 UI 线程做阻塞式标签读取。
    # ============================================================
    def add_files(self, files: list[str]) -> int:
        """添加一批文件路径到列表（自动去重、过滤非音频文件）。

        文件过滤、递归扫描与元数据读取均在后台线程
        (:class:`ui.file_load_worker.FileLoadWorker`) 完成，期间显示「加载中」
        进度对话框，避免打开大目录时 UI 卡死。

        Args:
            files: 文件路径列表。

        Returns:
            本方法为异步加载，返回 0（实际新增数量在加载完成后体现）。
        """
        items = [os.path.normpath(f) for f in files if f]
        self._start_loading(items)
        return 0

    def add_folder(self, folder: Optional[str] = None) -> int:
        """添加一个文件夹中的所有音频文件（递归）。

        若 ``folder`` 为 ``None``，弹出文件夹选择对话框。递归扫描与元数据读取
        在后台线程完成，期间显示「加载中」进度对话框，避免 UI 卡死。

        Args:
            folder: 文件夹路径，传入 ``None`` 时弹出选择对话框。

        Returns:
            本方法为异步加载，返回 0（实际新增数量在加载完成后体现）。
        """
        # folder 可能为 None（菜单/工具栏入口）或被信号误传为 bool False，
        # 统一在此归一化：仅当传入有效路径字符串时才跳过文件夹选择对话框
        if not isinstance(folder, str) or not folder:
            folder = QFileDialog.getExistingDirectory(
                self, "选择音频文件夹", "")
            if not folder:
                return 0

        self._start_loading([os.path.normpath(folder)])
        return 0

    def remove_selected(self):
        """移除当前选中的所有行。"""
        rows = sorted({idx.row() for idx in self._table.selectedIndexes()},
                      reverse=True)
        if not rows:
            return

        self._table.setSortingEnabled(False)
        self._table.blockSignals(True)
        try:
            for row in rows:
                # 通过路径删除，避免行号在删除过程中错位
                path_item = self._table.item(row, self._COL_NAME)
                if path_item is None:
                    continue
                path = path_item.data(self._PATH_ROLE)
                if path in self._path_to_row:
                    del self._path_to_row[path]
                    self._files.remove(path)
                self._table.removeRow(row)
        finally:
            # 重建行号映射（删除后行号已变化）
            self._rebuild_path_map()
            self._table.blockSignals(False)
            self._table.setSortingEnabled(True)

        self._refresh_index_column()
        self._refresh_count()
        self.files_changed.emit()

    def clear_all(self):
        """清空整个文件列表。"""
        if not self._files:
            return
        self._files.clear()
        self._path_to_row.clear()
        self._table.setRowCount(0)
        self._refresh_count()
        self.files_changed.emit()

    def get_files(self) -> list[str]:
        """返回当前列表中所有文件路径（按显示顺序，含未勾选项）。

        注意：开启排序后表格行顺序可能与插入顺序不同，此处返回当前
        表格显示顺序对应的路径列表，便于批处理按可见顺序处理。
        """
        files: list[str] = []
        for row in range(self._table.rowCount()):
            item = self._table.item(row, self._COL_NAME)
            if item is not None:
                path = item.data(self._PATH_ROLE)
                if path:
                    files.append(path)
        return files

    def get_checked_files(self) -> list[str]:
        """返回当前列表中所有「已勾选」文件路径（按显示顺序）。

        批处理入口使用此方法，仅处理用户勾选的文件（默认全选）。
        """
        files: list[str] = []
        for row in range(self._table.rowCount()):
            check_item = self._table.item(row, self._COL_CHECK)
            if check_item is not None and check_item.checkState() != Qt.CheckState.Checked:
                continue
            name_item = self._table.item(row, self._COL_NAME)
            if name_item is not None:
                path = name_item.data(self._PATH_ROLE)
                if path:
                    files.append(path)
        return files

    # ============================================================
    # 状态更新（供主窗口/批处理线程回调）
    # ============================================================
    def set_file_status(self, file_path: str, status: str):
        """更新某文件的状态（中文显示 + 状态码存储）。

        不依赖 ``_path_to_row`` 缓存（排序后视觉行号变化会导致缓存失效），
        改为直接遍历表格查找匹配的文件路径。

        Args:
            file_path: 文件完整路径。
            status: 状态码（pending/done/failed/...，见 ``_STATUS_TEXT``）。
        """
        target = os.path.normpath(file_path)
        for row in range(self._table.rowCount()):
            item = self._table.item(row, self._COL_NAME)
            if item and item.data(self._PATH_ROLE) == target:
                status_item = self._table.item(row, self._COL_STATUS)
                if status_item is None:
                    return
                status_item.setText(_STATUS_TEXT.get(status, status))
                status_item.setData(self._STATUS_ROLE, status)
                # 根据状态调整文字颜色
                color = self._status_color(status)
                if color is not None:
                    status_item.setForeground(color)
                return

    def get_selected_file(self) -> Optional[str]:
        """返回当前选中（第一行）的文件路径，无选中则返回 ``None``。"""
        rows = self._table.selectionModel().selectedRows()
        if not rows:
            return None
        item = self._table.item(rows[0].row(), self._COL_NAME)
        return item.data(self._PATH_ROLE) if item else None

    def update_file_path(self, old_path: str, new_path: str) -> bool:
        """更新列表中某文件的路径（重命名/整理后使用）。

        Args:
            old_path: 原文件路径。
            new_path: 新文件路径。

        Returns:
            成功更新返回 ``True``，原路径不存在返回 ``False``。
        """
        old_path = os.path.normpath(old_path)
        new_path = os.path.normpath(new_path)
        if old_path == new_path:
            return True
        if old_path not in self._path_to_row and old_path not in self._files:
            return False

        self._table.setSortingEnabled(False)
        try:
            # 更新内部列表
            for i, p in enumerate(self._files):
                if os.path.normpath(p) == old_path:
                    self._files[i] = new_path
                    break

            # 更新表格中的路径 data、文件名显示与 tooltip
            for row in range(self._table.rowCount()):
                item = self._table.item(row, self._COL_NAME)
                if item and item.data(self._PATH_ROLE) == old_path:
                    item.setData(self._PATH_ROLE, new_path)
                    item.setText(os.path.basename(new_path))
                    item.setToolTip(new_path)
                    break

            # 重建路径映射
            self._rebuild_path_map()
        finally:
            self._table.setSortingEnabled(True)
        return True

    # ============================================================
    # 内部辅助
    # ============================================================
    def _append_row(self, file_path: str, title: str = "", artist: str = "",
                    album: str = ""):
        """向表格追加一行（含勾选列与序号列）。

        元数据（title/artist/album）由调用方传入（来自后台 worker 已解析好的
        结果），本方法不再在 UI 线程做阻塞式标签读取。调用方需保证
        file_path 不重复且是存在的音频文件。
        """
        row = self._table.rowCount()
        self._table.insertRow(row)

        filename = os.path.basename(file_path)
        fmt = Path(file_path).suffix.lower().lstrip(".")

        # 勾选列（默认勾选，批处理仅处理已勾选项）
        check_item = QTableWidgetItem()
        check_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
        check_item.setCheckState(Qt.CheckState.Checked)
        self._table.setItem(row, self._COL_CHECK, check_item)

        # 序号列（在 add_files / 排序末尾统一刷新编号）
        index_item = QTableWidgetItem()
        index_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        self._table.setItem(row, self._COL_INDEX, index_item)

        # 文件名（携带路径 data）
        name_item = QTableWidgetItem(filename)
        name_item.setData(self._PATH_ROLE, file_path)
        name_item.setToolTip(file_path)
        self._table.setItem(row, self._COL_NAME, name_item)

        # 歌手 / 标题 / 专辑
        self._table.setItem(row, self._COL_ARTIST, QTableWidgetItem(artist))
        self._table.setItem(row, self._COL_TITLE, QTableWidgetItem(title))
        self._table.setItem(row, self._COL_ALBUM, QTableWidgetItem(album))

        # 状态（默认待处理）
        status_item = QTableWidgetItem(_STATUS_TEXT["pending"])
        status_item.setData(self._STATUS_ROLE, "pending")
        status_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        self._table.setItem(row, self._COL_STATUS, status_item)

        # 格式（大写居中）
        fmt_item = QTableWidgetItem(fmt.upper())
        fmt_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        self._table.setItem(row, self._COL_FORMAT, fmt_item)

        self._files.append(file_path)
        self._path_to_row[file_path] = row

    def _append_rows_resolved(self, entries: list) -> int:
        """批量追加已解析好的行（后台加载完成后调用，不在 UI 线程做 I/O）。

        Args:
            entries: ``[(path, title, artist, album), ...]`` 列表。

        Returns:
            实际新增的文件数量（已存在的不计入）。
        """
        added = 0
        self._table.setSortingEnabled(False)
        self._table.blockSignals(True)  # 批量插入期间屏蔽 itemChanged，避免 O(n^2)
        try:
            for entry in entries:
                if not entry:
                    continue
                path = os.path.normpath(entry[0])
                if path in self._path_to_row:
                    continue
                if not os.path.isfile(path):
                    continue
                title = entry[1] if len(entry) > 1 else ""
                artist = entry[2] if len(entry) > 2 else ""
                album = entry[3] if len(entry) > 3 else ""
                self._append_row(path, title, artist, album)
                added += 1
        finally:
            self._table.blockSignals(False)
            self._table.setSortingEnabled(True)

        if added > 0:
            self._refresh_index_column()
            self._refresh_count()
            self.files_changed.emit()
        return added

    # ============================================================
    # 后台加载（FileLoadWorker + QProgressDialog，避免大目录卡死 UI）
    # ============================================================
    def _start_loading(self, items: list):
        """启动后台加载线程，并显示「加载中」进度对话框。

        将递归扫描 + 元数据读取交给 :class:`FileLoadWorker` 在后台线程执行，
        期间主线程显示模态进度框并响应「取消」；加载完成后再批量建表。若已有
        加载任务在运行，本次请求被忽略，避免并发写入表格。

        Args:
            items: 待加载路径列表（文件与文件夹可混合）。
        """
        if not items:
            return
        if self._load_worker is not None and self._load_worker.isRunning():
            logger.debug("已有加载任务在运行，忽略本次请求")
            return

        dlg = QProgressDialog(self)
        dlg.setWindowTitle("加载中")
        dlg.setLabelText("正在扫描文件…")
        dlg.setRange(0, 0)  # 不确定模式，待获知总数后切换为确定模式
        dlg.setCancelButtonText("取消")
        dlg.setMinimumDuration(0)
        dlg.setWindowModality(Qt.WindowModality.ApplicationModal)
        self._load_dialog = dlg

        worker = FileLoadWorker(items)
        self._load_worker = worker
        worker.progress.connect(self._on_load_progress)
        worker.finished.connect(self._on_load_finished)
        worker.error.connect(self._on_load_error)
        dlg.canceled.connect(worker.request_stop)

        worker.start()
        dlg.exec()

    def _on_load_progress(self, done: int, total: int, path: str):
        """后台线程回报进度：更新进度对话框。"""
        dlg = self._load_dialog
        if dlg is None:
            return
        if dlg.maximum() != total:
            dlg.setRange(0, total)
        dlg.setLabelText(f"正在加载：{os.path.basename(path)}")
        dlg.setValue(done)

    def _on_load_finished(self, entries: list):
        """加载完成：批量建表并关闭进度框（用户取消则丢弃结果）。"""
        worker = self._load_worker
        dlg = self._load_dialog
        aborted = bool(worker and worker._stop.is_set()) if worker else False
        try:
            if not aborted and entries:
                self._append_rows_resolved(entries)
            elif aborted:
                logger.info("用户取消加载，已丢弃未完成的扫描结果")
        finally:
            if dlg is not None:
                dlg.accept()  # 关闭模态进度框
            self._cleanup_load()

    def _on_load_error(self, msg: str):
        """加载阶段出错：提示用户并关闭进度框。"""
        dlg = self._load_dialog
        if dlg is not None:
            dlg.accept()
        self._cleanup_load()
        QMessageBox.warning(self, "错误", f"加载文件时出错：\n{msg}")

    def _cleanup_load(self):
        """清理后台加载相关引用（worker 走 deleteLater，避免悬空指针）。"""
        if self._load_worker is not None:
            self._load_worker.deleteLater()
            self._load_worker = None
        self._load_dialog = None

    def _rebuild_path_map(self):
        """删除行后重建「路径 → 行号」映射。"""
        self._path_to_row.clear()
        for row in range(self._table.rowCount()):
            item = self._table.item(row, self._COL_NAME)
            if item is not None:
                self._path_to_row[item.data(self._PATH_ROLE)] = row

    def _refresh_index_column(self):
        """按当前显示顺序刷新「序号」列。"""
        for row in range(self._table.rowCount()):
            item = self._table.item(row, self._COL_INDEX)
            if item is not None:
                item.setText(str(row + 1))

    def _count_checked(self) -> int:
        """统计当前已勾选的文件数量。"""
        n = 0
        for row in range(self._table.rowCount()):
            item = self._table.item(row, self._COL_CHECK)
            if item is not None and item.checkState() == Qt.CheckState.Checked:
                n += 1
        return n

    def _refresh_count(self):
        """刷新底部文件计数标签（总数 / 已选）。"""
        total = self._table.rowCount()
        checked = self._count_checked()
        self._count_label.setText(f"共 {total} 个文件 · 已选 {checked} 个")

    def _status_color(self, status: str):
        """根据状态码返回对应的文字颜色，无特殊颜色返回 ``None``。"""
        from PyQt6.QtGui import QColor
        if status == "done":
            return QColor("#4ecca3")       # 绿
        if status == "failed":
            return QColor("#e74c3c")       # 红
        if status == "skipped":
            return QColor("#95a5a6")       # 灰
        if status in ("searching", "enriching", "writing", "organizing",
                      "parsing", "encoding"):
            return QColor("#f1c40f")       # 黄（进行中）
        return None

    # ============================================================
    # 事件处理
    # ============================================================
    def _on_add_files_clicked(self):
        """「添加文件」按钮：弹出文件选择对话框（多选）。"""
        files, _ = QFileDialog.getOpenFileNames(
            self, "选择音频文件", "",
            "音频文件 (*.mp3 *.flac *.m4a *.ogg *.wma *.ape);;所有文件 (*.*)")
        if files:
            self.add_files(files)

    def _on_item_changed(self, item: QTableWidgetItem):
        """勾选状态变化时刷新计数标签。"""
        if item.column() == self._COL_CHECK:
            self._refresh_count()

    def _on_selection_changed(self):
        """选中行变化：发射 selection_changed 信号通知主窗口。"""
        selected = self.get_selected_file()
        self.selection_changed.emit(selected or "")

    def _on_double_clicked(self, index):
        """双击行：发出 file_double_clicked 信号。"""
        item = self._table.item(index.row(), self._COL_NAME)
        if item is not None:
            path = item.data(self._PATH_ROLE)
            if path:
                self.file_double_clicked.emit(path)

    # ============================================================
    # 拖放支持
    # ============================================================
    def dragEnterEvent(self, event: QDragEnterEvent):
        """拖入时校验：含文件 URL 即接受。"""
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        """拖动移动时持续接受（保持高亮）。"""
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event: QDropEvent):
        """放下时：把所有文件/文件夹 URL 交给后台加载（文件夹递归）。"""
        if not event.mimeData().hasUrls():
            event.ignore()
            return

        items: list[str] = []
        for url in event.mimeData().urls():
            path = url.toLocalFile()
            if path:
                items.append(path)

        if items:
            self._start_loading(items)
            event.acceptProposedAction()
        else:
            event.ignore()
