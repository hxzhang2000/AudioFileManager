"""主窗口（§8A.1）。

AudioFileManager 的主界面，组装文件列表面板、文件详情面板与进度组件，
并连接菜单栏、工具栏、状态栏与批处理线程。

布局：
::

    ┌─────────────────────────────────────────────────────┐
    │  菜单栏（文件 / 操作 / 帮助） + 工具栏                │
    ├──────────────────────────┬──────────────────────────┤
    │  FileListWidget（左侧）   │  FileDetailPanel（右侧）  │
    │                          │                          │
    ├──────────────────────────┴──────────────────────────┤
    │  ProgressWidget（底部）                              │
    ├─────────────────────────────────────────────────────┤
    │  状态栏                                              │
    └─────────────────────────────────────────────────────┘

左侧与右侧用水平 QSplitter 分隔（右侧约占 35%），上下用垂直 QSplitter
分隔，窗口几何与分隔条状态通过 QSettings 持久化。

批处理（§10）由 :class:`processor.batch_processor.BatchProcessor` 在
QThread 工作线程中执行，主窗口负责把其信号转发到进度组件与文件列表。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import Qt, QSettings, QSize, QThread, pyqtSignal
from PyQt6.QtGui import QAction, QKeySequence
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QStatusBar,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from utils.logger import logger, set_file_logging_enabled
from version import APP_NAME, APP_VERSION, APP_GITHUB_URL, version_display

# —— 配置与处理模块（懒导入以降低启动耦合）——
from config.settings import get_config, get_config_dir

# —— Web 管理服务器 ——
from services.web_server import WebServer, get_bridge

from ui.file_list_widget import FileListWidget
from ui.file_detail_panel import FileDetailPanel
from ui.progress_widget import ProgressWidget


def _load_qss() -> str:
    """从 ``ui/dark_theme.qss`` 文件读取深色主题样式表。

    放置路径相对于本模块所在目录。文件不存在时回退到空字符串（保持默认 Qt 主题），
    不阻塞启动流程。
    """
    qss_path = Path(__file__).resolve().parent / "dark_theme.qss"
    try:
        return qss_path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError) as e:
        logger.warning(f"深色主题文件未找到 ({qss_path}): {e}，使用默认主题")
        return ""


def apply_dark_theme(app: QApplication) -> None:
    """从外部 QSS 文件加载深色主题并应用到 QApplication。"""
    qss = _load_qss()
    if qss:
        app.setStyleSheet(qss)


# ============================================================
# 主窗口
# ============================================================
class MainWindow(QMainWindow):
    """主窗口（§8A.1）。

    组装文件列表 / 详情 / 进度三大组件，提供菜单、工具栏、状态栏，
    并驱动批处理线程。
    """

    # 窗口默认尺寸
    _DEFAULT_SIZE = QSize(1200, 760)

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setObjectName("MainWindow")
        self.setWindowTitle(version_display())

        # —— 运行态对象 ——
        self._batch = None              # 当前批处理线程
        self._search_thread = None      # 当前手动搜索线程
        self._search_engine = None      # 搜索引擎（懒加载缓存）
        self._metadata_reader = None    # 元数据读取器（懒加载）
        self._audition_dialog = None    # 试听弹窗引用（非模态单实例）

        # 应用深色主题
        app = QApplication.instance()
        if app is not None:
            apply_dark_theme(app)

        self._setup_ui()
        self._setup_menu()
        self._setup_toolbar()
        self._setup_statusbar()
        self._connect_signals()

        # 恢复窗口几何与分隔条状态
        self._restore_geometry()

        # —— 启动 Web 管理服务器 ——
        self._mv_batch = None          # MV 整理线程
        self._web_server: WebServer | None = None
        self._init_web_server()

    # ------------------------------------------------------------
    # Web 管理服务器
    # ------------------------------------------------------------

    def _init_web_server(self) -> None:
        """初始化并启动 Web 管理服务器。"""
        try:
            config = get_config()
            self._web_server = WebServer(config)
            # 绑定桥接器
            bridge = get_bridge()
            bridge.bind(
                main_window=self,
                file_list_widget=self.file_list,
            )
            # 注册文件列表动作
            bridge.register_action("add_folder", self._on_web_add_folder)

            if config.get("web", {}).get("enabled", False):
                self._web_server.start()

            # 启动定时更新桥状态（每 500ms）
            from PyQt6.QtCore import QTimer
            self._web_timer = QTimer(self)
            self._web_timer.timeout.connect(self._update_web_bridge)
            self._web_timer.start(500)
        except Exception as e:
            logger.warning(f"Web 管理服务器初始化失败: {e}")
            self._web_server = None

    def _update_web_bridge(self) -> None:
        """定时更新 WebBridge 状态快照。"""
        bridge = get_bridge()
        batch = self._batch
        files = self.file_list.get_files()
        checked = self.file_list.get_checked_files()
        total = len(files)

        if batch is not None and batch.isRunning():
            # 批处理运行中：统计来自进度相关属性
            done = 0
            skipped = 0
            failed = 0
            # 通过文件列表状态统计
            if hasattr(self.file_list, "get_file_status"):
                for f in files:
                    st = self.file_list.get_file_status(f)
                    if st == "done":
                        done += 1
                    elif st == "skipped":
                        skipped += 1
                    elif st and st != "" and st != "waiting":
                        failed += 1
            cur_file = self.progress_widget._current_file if hasattr(self.progress_widget, "_current_file") else ""
            bridge.update_status(
                batch_running=True,
                batch_paused=batch.is_paused,
                current_file=cur_file,
                done_count=done,
                skipped_count=skipped,
                failed_count=failed,
                total_files=total,
                checked_files=len(checked),
            )
        else:
            # 空闲状态
            bridge.update_status(
                batch_running=False,
                batch_paused=False,
                current=0,
                total=0,
                current_file="",
                step_name="",
                done_count=0,
                skipped_count=0,
                failed_count=0,
                total_files=total,
                checked_files=len(checked),
            )

        # 缓存文件列表快照供 Web API 读取
        file_cache: list[dict] = []
        for p in files:
            try:
                name = os.path.basename(p)
                size = os.path.getsize(p)
                suffix = os.path.splitext(p)[1].lower()
                file_cache.append({
                    "path": p,
                    "name": name,
                    "size": size,
                    "ext": suffix,
                    "status": "",
                })
            except Exception:
                file_cache.append({"path": p, "name": os.path.basename(p), "size": 0, "ext": "", "status": ""})
        bridge.set_cached_files(file_cache)

        # 处理来自 Web 的动作队列
        for action in bridge.drain_actions():
            self._process_web_action(action)

    def _process_web_action(self, action: dict) -> None:
        """处理从 Web 页面发来的动作。"""
        name = action.get("action", "")
        params = action.get("params", {})

        if name == "batch_start":
            self._on_start_batch()
        elif name == "batch_pause":
            self._on_pause_batch()
        elif name == "batch_stop":
            self._on_stop_batch()
        elif name == "add_folder":
            folder = params.get("folder", "")
            if folder:
                self.file_list.add_folder(folder)

    def _on_web_add_folder(self, folder: str) -> dict:
        """Web 触发的加载目录动作（在 Web 线程中执行）。"""
        # 通过 invokeMethod 异步执行
        from PyQt6.QtCore import QMetaObject, Qt, Q_ARG
        if folder:
            QMetaObject.invokeMethod(
                self.file_list, "add_folder",
                Qt.ConnectionType.QueuedConnection,
                Q_ARG(str, folder),
            )
            return {"ok": True, "path": folder}
        return {"error": "empty path"}

    def _web_start_server(self, web_cfg: dict) -> None:
        """启动 Web 服务器。

        Args:
            web_cfg: 来自对话框的 web 配置（尚未持久化）。
        """
        if self._web_server is None:
            self._init_web_server()
        if self._web_server is not None:
            config = get_config()
            config["web"] = web_cfg
            success = self._web_server.restart(config)
            if success:
                self._status.showMessage(f"Web 服务已启动（端口 {web_cfg.get('port', 8080)}）", 3000)

    def _web_stop_server(self) -> None:
        """停止 Web 服务器。"""
        if self._web_server is not None:
            self._web_server.stop()
            self._status.showMessage("Web 服务已停止。", 3000)

    def _web_restart_server(self, web_cfg: dict) -> None:
        """重启 Web 服务器（应用新端口/地址配置）。

        Args:
            web_cfg: 来自对话框的 web 配置（尚未持久化）。
        """
        if self._web_server is not None:
            config = get_config()
            config["web"] = web_cfg
            success = self._web_server.restart(config)
            if success:
                self._status.showMessage(f"Web 服务已重启（端口 {web_cfg.get('port', 8080)}）", 3000)
        else:
            self._init_web_server()

    # ============================================================
    # UI 构建
    # ============================================================
    def _setup_ui(self):
        """构建中央部件：上=水平分隔(列表+详情)，下=进度。"""
        # 水平分隔：左侧文件列表 + 右侧详情面板
        h_splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.file_list = FileListWidget(h_splitter)
        self.detail_panel = FileDetailPanel(h_splitter)
        h_splitter.addWidget(self.file_list)
        h_splitter.addWidget(self.detail_panel)
        # 右侧约占 35%
        h_splitter.setStretchFactor(0, 65)
        h_splitter.setStretchFactor(1, 35)
        h_splitter.setSizes([780, 420])
        self._h_splitter = h_splitter

        # 垂直分隔：上方内容 + 底部进度
        v_splitter = QSplitter(Qt.Orientation.Vertical, self)
        v_splitter.addWidget(h_splitter)
        self.progress_widget = ProgressWidget(v_splitter)
        v_splitter.addWidget(self.progress_widget)
        v_splitter.setStretchFactor(0, 1)
        v_splitter.setStretchFactor(1, 0)
        v_splitter.setSizes([560, 180])
        self._v_splitter = v_splitter

        self.setCentralWidget(v_splitter)
        self.resize(self._DEFAULT_SIZE)

    def _setup_menu(self):
        """构建菜单栏：文件 / 操作 / 帮助。"""
        menubar = self.menuBar()

        # —— 文件菜单 ——
        file_menu = menubar.addMenu("文件")

        self._act_open_folder = QAction("打开文件夹…", self)
        self._act_open_folder.setShortcut(QKeySequence("Ctrl+O"))
        self._act_open_folder.triggered.connect(self._on_open_folder)
        file_menu.addAction(self._act_open_folder)

        self._act_open_files = QAction("打开文件…", self)
        self._act_open_files.setShortcut(QKeySequence("Ctrl+Shift+O"))
        self._act_open_files.triggered.connect(self._on_open_files)
        file_menu.addAction(self._act_open_files)

        file_menu.addSeparator()

        self._act_quit = QAction("退出", self)
        self._act_quit.setShortcut(QKeySequence("Ctrl+Q"))
        self._act_quit.triggered.connect(self.close)
        file_menu.addAction(self._act_quit)

        # —— 操作菜单 ——
        op_menu = menubar.addMenu("操作")

        self._act_start = QAction("开始批处理", self)
        self._act_start.setShortcut(QKeySequence("F5"))
        self._act_start.triggered.connect(self._on_start_batch)
        op_menu.addAction(self._act_start)

        self._act_stop = QAction("停止", self)
        self._act_stop.setShortcut(QKeySequence("Shift+F5"))
        self._act_stop.triggered.connect(self._on_stop_batch)
        self._act_stop.setEnabled(False)
        op_menu.addAction(self._act_stop)

        op_menu.addSeparator()

        self._act_settings = QAction("设置…", self)
        self._act_settings.setShortcut(QKeySequence("Ctrl+,"))
        self._act_settings.triggered.connect(self._on_settings)
        op_menu.addAction(self._act_settings)

        op_menu.addSeparator()

        self._act_mv = QAction("MV 整理…", self)
        self._act_mv.triggered.connect(self._on_mv_organize)
        op_menu.addAction(self._act_mv)

        # —— 帮助菜单 ——
        help_menu = menubar.addMenu("帮助")
        self._act_about = QAction("关于", self)
        self._act_about.triggered.connect(self._on_about)
        help_menu.addAction(self._act_about)

    def _setup_toolbar(self):
        """构建工具栏：快速操作按钮。"""
        self._toolbar = QToolBar("快速操作", self)
        self._toolbar.setMovable(False)
        self._toolbar.setObjectName("mainToolbar")
        self.addToolBar(self._toolbar)

        self._toolbar.addAction(self._act_open_folder)
        self._toolbar.addAction(self._act_open_files)
        self._toolbar.addSeparator()
        self._toolbar.addAction(self._act_start)
        self._toolbar.addAction(self._act_stop)
        self._toolbar.addSeparator()
        self._toolbar.addAction(self._act_settings)
        self._toolbar.addSeparator()
        self._toolbar.addAction(self._act_mv)

    def _setup_statusbar(self):
        """构建状态栏。"""
        self._status = QStatusBar(self)
        self.setStatusBar(self._status)
        self._status.showMessage("就绪。请添加音频文件夹或文件。")

    def _connect_signals(self):
        """连接子组件信号到主窗口处理槽。"""
        # 文件列表变化 → 更新状态栏计数与批处理可用性
        self.file_list.files_changed.connect(self._on_files_changed)
        # 双击文件 → 打开试听弹窗
        self.file_list.file_double_clicked.connect(self._on_file_double_clicked)
        # 选中行变化 → 更新详情面板
        self.file_list.selection_changed.connect(self._on_file_selected)

        # 进度组件的 开始/停止 按钮
        self.progress_widget.start_requested.connect(self._on_start_batch)
        self.progress_widget.stop_requested.connect(self._on_stop_batch)
        self.progress_widget.pause_requested.connect(self._on_pause_batch)

        # 详情面板的 搜索/保存 信号
        self.detail_panel.search_requested.connect(self._on_search_requested)
        self.detail_panel.save_requested.connect(self._on_save_requested)

    # ============================================================
    # 懒加载依赖
    # ============================================================
    def _get_metadata_reader(self):
        """懒加载元数据读取器（parser.metadata_reader.MetadataReader）。"""
        if self._metadata_reader is None:
            try:
                from parser.metadata_reader import MetadataReader
                self._metadata_reader = MetadataReader()
            except Exception as e:
                logger.error(f"MetadataReader 加载失败: {e}")
                self._metadata_reader = False
        return self._metadata_reader if self._metadata_reader is not False else None

    def _get_search_engine(self):
        """懒加载并缓存搜索引擎。

        复用 :meth:`BatchProcessor._build_search_engine` 的 Provider 注册逻辑，
        避免在 UI 层重复维护一份注册代码。该方法内部不使用 ``self``，故可
        作为非绑定方法调用（``self`` 传 ``None``）。
        """
        if self._search_engine is None:
            try:
                from processor.batch_processor import BatchProcessor
                config = get_config()
                # _build_search_engine 是 @staticmethod，不依赖实例状态
                self._search_engine = BatchProcessor._build_search_engine(config)
            except Exception as e:
                logger.error(f"搜索引擎构建失败: {e}")
                self._search_engine = False
        return self._search_engine if self._search_engine is not False else None

    # ============================================================
    # 菜单 / 工具栏 槽
    # ============================================================
    def _on_open_folder(self):
        """文件→打开文件夹：调用文件列表的 add_folder。"""
        logger.info("用户打开文件夹")
        try:
            self.file_list.add_folder(None)
        except Exception as e:
            logger.error(f"打开文件夹失败: {e}", exc_info=True)
            QMessageBox.warning(self, "打开文件夹", f"读取文件夹失败：{e}")

    def _on_open_files(self):
        """文件→打开文件：弹出多选对话框并添加。"""
        files, _ = QFileDialog.getOpenFileNames(
            self, "选择音频文件", "",
            "音频文件 (*.mp3 *.flac *.m4a *.ogg *.wma *.ape);;所有文件 (*.*)")
        if files:
            self.file_list.add_files(files)

    def _on_settings(self):
        """操作→设置：打开设置对话框。

        设置对话框 :mod:`ui.settings_dialog` 尚未实现时，回退为提示信息。
        """
        try:
            from ui.settings_dialog import SettingsDialog
        except ImportError:
            QMessageBox.information(
                self, "设置",
                "设置对话框尚未实现（ui/settings_dialog.py）。\n"
                "当前可手动编辑配置文件：\n"
                f"{get_config_dir() / 'config.json'}")
            return

        try:
            dialog = SettingsDialog(get_config(), self)
            # 连接 Web 服务控制信号
            dialog.web_start_requested.connect(self._web_start_server)
            dialog.web_stop_requested.connect(self._web_stop_server)
            dialog.web_restart_requested.connect(self._web_restart_server)
            if dialog.exec():
                # 对话框 accepted：获取用户修改并保存
                new_config = dialog.get_config()
                from config.settings import save_config, reload_config
                save_config(new_config)
                reload_config()
                # 应用日志文件记录开关（运行时启停文件日志）
                logging_enabled = new_config.get("logging", {}).get("enabled", True)
                set_file_logging_enabled(logging_enabled)
                logger.info(f"配置已更新，文件日志记录{'已启用' if logging_enabled else '已禁用'}。")
                self._status.showMessage("配置已更新。", 3000)
                self.progress_widget.log_message("配置已更新。")
        except Exception as e:
            logger.error(f"打开设置对话框失败: {e}", exc_info=True)
            QMessageBox.warning(self, "设置", f"打开设置失败：{e}")

    def _on_mv_organize(self):
        """操作→MV 整理：执行 MV 视频文件整理。

        源目录默认为当前工作目录（last_input_dir），输出目录复用「整理」页面设置。
        """
        from processor.mv_processor import MvProcessor
        config = get_config()

        # 源目录：默认使用 last_input_dir，为空时让用户选择
        src = config.get("last_input_dir", "")
        if not src or not os.path.isdir(src):
            src = QFileDialog.getExistingDirectory(
                self, "选择 MV 源目录（包含视频文件的文件夹）", ""
            )
            if not src:
                return

        # 输出目录：复用「整理」页面设置
        out = config.get("organize", {}).get("output_dir", "")
        if not out:
            QMessageBox.warning(
                self, "MV 整理",
                "请先在「设置→整理」中配置输出目录。"
            )
            return

        # 检查是否有线程已在运行
        if self._mv_batch is not None and self._mv_batch.isRunning():
            QMessageBox.information(self, "MV 整理", "MV 整理已在运行中。")
            return

        self._act_mv.setEnabled(False)
        self._status.showMessage("MV 视频文件整理中…")

        # 创建运行线程，传入源目录
        self._mv_batch = MvProcessor(config, source_dir=src)
        self._mv_batch.files_scanned.connect(self._on_mv_scanned)
        self._mv_batch.progress_updated.connect(self.progress_widget.update_progress)
        self._mv_batch.file_finished.connect(self._on_mv_file_finished)
        self._mv_batch.batch_finished.connect(self._on_mv_batch_finished)
        self._mv_batch.log_message.connect(self.progress_widget.log_message)
        self.progress_widget.log_message("MV 整理开始…")
        self._mv_batch.start()

    def _on_mv_scanned(self, total: int):
        """MV 整理扫描完成回调。"""
        self._status.showMessage(f"MV 整理：发现 {total} 个视频文件")
        self.progress_widget.set_max(total)

    def _on_mv_file_finished(self, file_path: str, status: str):
        """单个 MV 文件处理完成回调。"""
        label = {None: "", "done": "✓", "failed": "✗", "skipped": "⏭"}
        self.progress_widget.file_finished(file_path, label.get(status, status))

    def _on_mv_batch_finished(self, success: int, failed: int):
        """MV 整理全部完成回调。"""
        self._act_mv.setEnabled(True)
        self._mv_batch.deleteLater()
        self._mv_batch = None
        msg = f"MV 整理完成：成功 {success}，失败 {failed}"
        self._status.showMessage(msg)
        self.progress_widget.log_message(msg)

    def _on_about(self):
        """帮助→关于：显示关于对话框（含 GitHub Star 按钮）。"""
        dialog = QDialog(self)
        dialog.setWindowTitle(f"关于 {APP_NAME}")
        dialog.setFixedSize(480, 320)
        dialog.setStyleSheet(self._about_dialog_style())

        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(12)

        # 标题
        title = QLabel(f"<h2 style='margin:0;'>{APP_NAME}</h2>")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)

        # 版本号
        ver = QLabel(f"<p style='color:#aaa;font-size:13px;'>v{APP_VERSION}</p>")
        ver.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(ver)

        layout.addSpacing(4)

        # 描述
        desc = QLabel(
            "<p style='font-size:13px;line-height:1.6;'>"
            "音频文件管理工具：自动识别元数据、补全封面与歌词、整理目录结构。</p>"
            "<p style='color:#999;font-size:12px;'>技术栈：PyQt6 + mutagen + httpx</p>"
        )
        desc.setWordWrap(True)
        desc.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(desc)

        layout.addStretch()

        # Star 提示行 + 按钮
        star_layout = QHBoxLayout()
        star_layout.setSpacing(8)
        star_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        hint = QLabel(
            "<span style='font-size:13px;'>如果它解决了你的问题，请不要吝啬你的 Star 哟！</span>"
        )
        star_layout.addWidget(hint)

        arrow = QLabel("<span style='font-size:16px;'>👉</span>")
        star_layout.addWidget(arrow)

        star_btn = QPushButton("⭐ Star on GitHub")
        star_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        star_btn.setStyleSheet("""
            QPushButton {
                background-color: #238636;
                color: #fff;
                border: 1px solid #2ea043;
                border-radius: 6px;
                padding: 8px 18px;
                font-size: 13px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #2ea043;
            }
            QPushButton:pressed {
                background-color: #196c2e;
            }
        """)
        star_btn.clicked.connect(
            lambda: __import__("webbrowser").open(APP_GITHUB_URL)
        )
        star_layout.addWidget(star_btn)

        layout.addLayout(star_layout)
        layout.addSpacing(8)

        # 底部关闭按钮
        close_layout = QHBoxLayout()
        close_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)
        close_btn = QPushButton("关闭")
        close_btn.setFixedWidth(100)
        close_btn.clicked.connect(dialog.accept)
        close_layout.addWidget(close_btn)
        layout.addLayout(close_layout)

        dialog.exec()

    @staticmethod
    def _about_dialog_style() -> str:
        """关于对话框的基础样式表。"""
        return """
            QDialog {
                background-color: #1e1e1e;
            }
            QLabel {
                color: #d4d4d4;
                background: transparent;
            }
            QPushButton {
                background-color: #333;
                color: #d4d4d4;
                border: 1px solid #555;
                border-radius: 4px;
                padding: 6px 16px;
                font-size: 12px;
            }
            QPushButton:hover {
                background-color: #444;
            }
        """

    # ============================================================
    # 文件列表变化槽
    # ============================================================
    def _on_files_changed(self):
        """文件列表变化：更新状态栏计数与开始按钮可用性。"""
        files = self.file_list.get_files()
        count = len(files)
        self._status.showMessage(f"当前列表：{count} 个音频文件")
        # 有文件且未运行时才允许开始
        self._act_start.setEnabled(count > 0 and not self.progress_widget.is_running())

    # ============================================================
    # 文件选中槽 → 更新详情面板
    # ============================================================
    def _on_file_selected(self, file_path: str):
        """文件列表选中行变化：读取元数据并填充详情面板。

        Args:
            file_path: 当前选中的文件路径；无选中时为空字符串。
        """
        if not file_path:
            self.detail_panel.clear_panel()
            return

        reader = self._get_metadata_reader()
        if reader is None:
            # 读取器不可用：仅显示文件路径
            self.detail_panel.set_metadata({"file_path": file_path})
            return

        try:
            meta = reader.read(file_path)          # 标签字段
            cover = reader.read_cover(file_path)    # 封面二进制
            lyrics = reader.read_lyrics(file_path)  # 歌词文本
        except Exception as e:
            logger.warning(f"读取文件元数据失败 {file_path}: {e}")
            self._status.showMessage(f"读取元数据失败：{e}", 5000)
            self.detail_panel.set_metadata({"file_path": file_path})
            return

        # 统一为详情面板期望的字典结构
        metadata = {
            "file_path": file_path,
            "title": meta.get("title") or "",
            "artist": meta.get("artist") or "",
            "album": meta.get("album") or "",
            "year": meta.get("year"),
            "genre": meta.get("genre") or "",
            "track_number": meta.get("track_number"),
            "duration": meta.get("duration"),
            "bitrate": meta.get("bitrate"),
            "sample_rate": meta.get("sample_rate"),
            "cover_data": cover,
            "lyrics_text": lyrics or "",
        }
        self.detail_panel.set_metadata(metadata)
        self._status.showMessage(f"已选中：{os.path.basename(file_path)}")

    # ============================================================
    # 双击文件槽 → 打开试听弹窗
    # ============================================================
    def _on_file_double_clicked(self, file_path: str):
        """双击文件：打开试听弹窗（§8B.1 AuditionDialog）。

        试听弹窗 :mod:`audition.audition_dialog` 尚未实现时，回退为提示。
        """
        try:
            from audition.audition_dialog import AuditionDialog
        except ImportError:
            QMessageBox.information(
                self, "试听",
                f"试听弹窗尚未实现（audition/audition_dialog.py）。\n"
                f"文件：{file_path}")
            return

        try:
            # 关闭之前的试听弹窗（非模态单实例，防止多实例并发与 GC 悬空）
            if self._audition_dialog is not None:
                self._audition_dialog.close()
                self._audition_dialog.deleteLater()
                self._audition_dialog = None

            # 读取元数据传给试听弹窗
            reader = self._get_metadata_reader()
            meta = reader.read(file_path) if reader else {}
            cover = reader.read_cover(file_path) if reader else None
            lyrics = None
            try:
                lyrics = reader.read_lyrics(file_path) if reader else None
            except Exception:
                pass
            from search.provider import TrackMetadata
            track = TrackMetadata(
                title=meta.get("title") or os.path.basename(file_path),
                artist=meta.get("artist") or "未知艺人",
                album=meta.get("album") or "",
                release_year=meta.get("year"),
                genre=meta.get("genre"),
                cover_data=cover,
                lyrics_text=lyrics,
            )
            search_engine = self._get_search_engine()
            from config.settings import get_config
            save_mode = get_config().get("save_mode", "tags")
            dialog = AuditionDialog(file_path, track, self,
                                    search_engine=search_engine,
                                    save_mode=save_mode)
            self._audition_dialog = dialog
            # 弹窗销毁时清空引用，防止 GC 悬空引用
            dialog.destroyed.connect(lambda: setattr(self, '_audition_dialog', None))
            dialog.show()
        except Exception as e:
            logger.error(f"打开试听弹窗失败: {e}", exc_info=True)
            QMessageBox.warning(self, "试听", f"打开试听弹窗失败：{e}")

    # ============================================================
    # 批处理槽
    # ============================================================
    def _on_start_batch(self):
        """开始批处理：创建 BatchProcessor 线程并启动（§10）。"""
        if self.progress_widget.is_running():
            return

        files = self.file_list.get_checked_files()
        if not files:
            QMessageBox.information(self, "批处理", "没有勾选任何文件，请先在列表中选择要处理的音频文件。")
            return
        logger.info(f"开始批处理：共勾选 {len(files)} 个文件")

        try:
            from processor.batch_processor import BatchProcessor
        except Exception as e:
            QMessageBox.critical(self, "批处理", f"批处理模块加载失败：{e}")
            return

        config = get_config()
        state_file = str(get_config_dir() / "batch_state.json")

        # 创建批处理线程
        self._batch = BatchProcessor(files, config, state_file)
        # 保存批处理启动时的文件列表快照（避免排序后索引错位）
        self._batch_files = list(files)

        # 连接信号
        self._batch.progress_updated.connect(self._on_batch_progress)
        self._batch.file_finished.connect(self._on_batch_file_finished)
        self._batch.file_renamed.connect(self._on_file_renamed)
        self._batch.batch_finished.connect(self._on_batch_finished)
        self._batch.error_occurred.connect(self._on_batch_error)
        self._batch.pause_state_changed.connect(self.progress_widget.set_paused)
        self._batch.file_metadata_updated.connect(self._on_batch_file_metadata_updated)

        # 切换 UI 为运行态
        self.progress_widget.reset()
        self.progress_widget.set_running(True)
        self._act_start.setEnabled(False)
        self._act_stop.setEnabled(True)
        self._status.showMessage(f"批处理开始：共 {len(files)} 个文件")
        self.progress_widget.log_message(f"批处理开始：共 {len(files)} 个文件")

        # 启动线程
        self._batch.start()

    def _on_stop_batch(self):
        """停止批处理：触发停止信号（当前文件完成后停止，§10.4.2）。"""
        if self._batch is None or not self._batch.isRunning():
            return
        self._batch.stop()
        self._status.showMessage("正在停止批处理（当前文件完成后停止）…")
        self.progress_widget.log_message("用户请求停止批处理，等待当前文件完成…")
        self._act_stop.setEnabled(False)

    def _on_pause_batch(self):
        """暂停/继续批处理：根据当前状态切换（§10.4.3）。

        点击一次暂停（当前文件完成后生效），再次点击继续；
        进度组件按钮文本与状态栏/日志同步。
        """
        if self._batch is None or not self._batch.isRunning():
            return
        if self._batch.is_paused:
            self._batch.resume()
            self.progress_widget.set_paused(False)
            self._status.showMessage("继续批处理…")
            self.progress_widget.log_message("用户请求继续批处理")
        else:
            self._batch.pause()
            self.progress_widget.set_paused(True)
            self._status.showMessage("已暂停批处理（当前文件完成后暂停）…")
            self.progress_widget.log_message("用户请求暂停批处理，等待当前文件完成…")

    def _on_batch_progress(self, current, total, step_name, step_index, step_total):
        """批处理进度信号 → 进度组件。"""
        # 使用批处理启动时的快照列表（按原始顺序索引），避免表格排序后的
        # 显示顺序与处理顺序不一致导致状态更新到错误文件（issue #2 fix）。
        batch_files = getattr(self, "_batch_files", self.file_list.get_files())
        if 0 <= current < len(batch_files):
            self.progress_widget.set_current_file(batch_files[current])
            # 同步更新文件列表的状态为当前子步骤
            self.file_list.set_file_status(batch_files[current], step_name)
        self.progress_widget.update_progress(current, total, step_name, step_index, step_total)

    def _on_batch_file_finished(self, file_path: str, status: str):
        """单文件完成：更新文件列表状态并记录日志。"""
        self.file_list.set_file_status(file_path, status)
        name = os.path.basename(file_path)
        if status == "done":
            self.progress_widget.log_message(f"  ✓ 完成：{name}")
        elif status == "skipped":
            self.progress_widget.log_message(f"  ⊘ 已跳过：{name}")
        else:
            self.progress_widget.log_message(f"  ✗ 失败：{name}")

    def _on_file_renamed(self, old_path: str, new_path: str):
        """文件重命名/整理后路径变更：同步更新文件列表与批处理快照。"""
        if self.file_list.update_file_path(old_path, new_path):
            self.progress_widget.log_message(
                f"  → 路径变更：{os.path.basename(old_path)} → {new_path}"
            )
            # 同步更新快照（保持索引对应关系）
            batch_files = getattr(self, "_batch_files", None)
            if batch_files:
                for i, p in enumerate(batch_files):
                    if os.path.normpath(p) == os.path.normpath(old_path):
                        batch_files[i] = new_path
                        break

    def _on_batch_file_metadata_updated(self, file_path: str, metadata: dict):
        """单文件元数据更新：同步到文件列表与详情面板（若当前显示该文件）。"""
        self.file_list.update_file_metadata(file_path, metadata)
        # 如果详情面板当前显示的是该文件，同步更新
        if self.detail_panel.get_file_path() == file_path:
            meta = {
                "file_path": file_path,
                "title": metadata.get("title") or "",
                "artist": metadata.get("artist") or "",
                "album": metadata.get("album") or "",
                "year": metadata.get("release_year"),
                "genre": metadata.get("genre") or "",
                "track_number": metadata.get("track_number"),
                # cover_data 为 None 时不覆盖面板已有封面（FILES 模式下搜索未找到新封面时
                # cover_data 为 None，但我们不应因此清空面板上来自嵌入式标签的已有封面）
                "cover_data": metadata.get("cover_data") or self.detail_panel.get_cover_data(),
                "lyrics_text": metadata.get("lyrics_text") or "",
            }
            self.detail_panel.set_metadata(meta)

    def _on_batch_finished(self, success_count: int, failed_count: int):
        """批处理全部完成：复位 UI、清理线程引用。"""
        total = success_count + failed_count
        msg = f"批处理完成：成功 {success_count}，失败 {failed_count}，共 {total}"
        self.progress_widget.log_message(msg)
        self._status.showMessage(msg)
        self.progress_widget.set_running(False)
        self._act_start.setEnabled(len(self.file_list.get_files()) > 0)
        self._act_stop.setEnabled(False)

        # 清理线程引用（确保 finished 信号断开后对象可回收）
        if self._batch is not None:
            self._batch.deleteLater()
            self._batch = None

    def _on_batch_error(self, error_msg: str):
        """批处理严重错误（如离线模式提示）。"""
        self.progress_widget.log_message(f"[错误] {error_msg}")
        self._status.showMessage(error_msg, 5000)

    # ============================================================
    # 详情面板槽：搜索 / 保存
    # ============================================================
    def _on_search_requested(self, artist: str, title: str):
        """详情面板搜索按钮：在工作线程中执行手动搜索（§7.4）。"""
        engine = self._get_search_engine()
        if engine is None:
            QMessageBox.warning(self, "搜索", "搜索引擎不可用，无法执行网络搜索。")
            return

        self._status.showMessage(f"正在搜索：{artist} - {title} …")
        self.progress_widget.log_message(f"手动搜索：{artist} - {title}")

        try:
            from processor.batch_processor import BatchProcessor
            from services.manual_search import ManualSearchService

            service = ManualSearchService(
                engine,
                similarity_fn=BatchProcessor._title_similarity,
            )

            # 用独立 QThread worker 执行同步搜索，避免阻塞 UI
            # _SearchWorker 定义在本模块末尾（信号需在类层级注册）
            worker = _SearchWorker(service, title, artist)
            worker.results_ready.connect(self._on_search_results)
            worker.error_occurred.connect(self._on_search_error)
            worker.finished.connect(worker.deleteLater)
            self._search_thread = worker
            worker.start()
        except Exception as e:
            logger.error(f"启动搜索失败: {e}", exc_info=True)
            QMessageBox.warning(self, "搜索", f"启动搜索失败：{e}")

    def _on_search_results(self, results: list):
        """搜索结果到达：把第一条填入详情面板。"""
        self._search_thread = None
        if not results:
            self._status.showMessage("未找到匹配结果", 4000)
            self.progress_widget.log_message("  未找到匹配结果")
            QMessageBox.information(self, "搜索", "未找到匹配结果。")
            return

        # 填入第一条结果（多候选预览由详情面板后续扩展）
        first = results[0]
        self.detail_panel.fill_from_search_result({
            "title": first.title,
            "artist": first.artist,
            "album": first.album,
            "release_year": first.release_year,
            "genre": first.genre,
            "track_number": first.track_number,
            "cover_data": first.cover_data,
            "lyrics_text": first.lyrics_text,
        })
        src = first.source or "网络"
        self._status.showMessage(f"搜索完成：来自 {src}（共 {len(results)} 条）", 4000)
        self.progress_widget.log_message(
            f"  搜索完成：来自 {src}，共 {len(results)} 条候选，已填入第 1 条")

    def _on_search_error(self, error_msg: str):
        """搜索失败：记录日志并提示。"""
        self._search_thread = None
        logger.warning(f"手动搜索失败: {error_msg}")
        self._status.showMessage(f"搜索失败：{error_msg}", 5000)
        self.progress_widget.log_message(f"  搜索失败：{error_msg}")

    def _on_save_requested(self, file_path: str, metadata: dict):
        """详情面板保存按钮：把输入框内容写入文件标签（§7.5）。"""
        try:
            from search.provider import TrackMetadata
            from services.metadata_saver import MetadataSaver

            # 构造 TrackMetadata
            year_raw = metadata.get("year")
            year = int(year_raw) if (year_raw and str(year_raw).strip().isdigit()) else None
            track_raw = metadata.get("track_number")
            track_num = int(track_raw) if (track_raw and str(track_raw).strip().isdigit()) else 0

            track = TrackMetadata(
                title=metadata.get("title") or "",
                artist=metadata.get("artist") or "未知艺人",
                album=metadata.get("album") or "",
                release_year=year,
                genre=metadata.get("genre") or None,
                track_number=track_num,
                cover_data=metadata.get("cover_data"),
                lyrics_text=metadata.get("lyrics_text"),
            )

            config = get_config()
            saver = MetadataSaver(
                save_mode=config.get("save_mode", "tags"),
                encoding_config=config.get("encoding", {"enabled": False, "charset": "UTF-8"}),
            )
            saver.save_metadata_to_tags(
                file_path, track,
                cover_data=metadata.get("cover_data"),
                lyrics_text=metadata.get("lyrics_text"),
            )

            # 同步更新主页面文件列表与详情面板
            self.file_list.update_file_metadata(file_path, {
                "title": track.title,
                "artist": track.artist,
                "album": track.album,
                "release_year": track.release_year,
                "genre": track.genre,
                "track_number": track.track_number,
                "cover_data": track.cover_data,
                "lyrics_text": track.lyrics_text,
            })
            if self.detail_panel.get_file_path() == file_path:
                self.detail_panel.set_metadata({
                    "file_path": file_path,
                    "title": track.title,
                    "artist": track.artist,
                    "album": track.album,
                    "year": track.release_year,
                    "genre": track.genre,
                    "track_number": track.track_number,
                    "cover_data": track.cover_data,
                    "lyrics_text": track.lyrics_text,
                })

            name = os.path.basename(file_path)
            self._status.showMessage(f"已保存：{name}", 4000)
            self.progress_widget.log_message(f"  💾 已保存元数据到文件标签：{name}")
            QMessageBox.information(self, "保存", f"已保存到：\n{file_path}")
        except Exception as e:
            logger.error(f"保存元数据失败 {file_path}: {e}", exc_info=True)
            QMessageBox.warning(self, "保存失败", f"保存元数据失败：{e}")

    # ============================================================
    # 窗口几何持久化
    # ============================================================
    def _restore_geometry(self):
        """从 QSettings 恢复窗口几何与分隔条状态。"""
        settings = QSettings("AudioFileManager", "MainWindow")
        geo = settings.value("geometry")
        if geo is not None:
            self.restoreGeometry(geo)
        state = settings.value("windowState")
        if state is not None:
            self.restoreState(state)
        h_sizes = settings.value("hSplitterSizes")
        if h_sizes is not None:
            try:
                self._h_splitter.setSizes([int(x) for x in h_sizes])
            except (TypeError, ValueError):
                pass
        v_sizes = settings.value("vSplitterSizes")
        if v_sizes is not None:
            try:
                self._v_splitter.setSizes([int(x) for x in v_sizes])
            except (TypeError, ValueError):
                pass

    def _save_geometry(self):
        """保存窗口几何与分隔条状态到 QSettings。"""
        settings = QSettings("AudioFileManager", "MainWindow")
        settings.setValue("geometry", self.saveGeometry())
        settings.setValue("windowState", self.saveState())
        settings.setValue("hSplitterSizes", self._h_splitter.sizes())
        settings.setValue("vSplitterSizes", self._v_splitter.sizes())

    # ============================================================
    # 退出处理
    # ============================================================
    def closeEvent(self, event):
        """关闭窗口：优雅停止批处理线程后保存几何。"""
        self.cleanup()
        self._save_geometry()
        event.accept()

    def cleanup(self):
        """清理资源：停止批处理线程、搜索线程等。供 main.py 在 aboutToQuit 时调用。"""
        # 关闭试听弹窗
        if self._audition_dialog is not None:
            self._audition_dialog.close()
            self._audition_dialog.deleteLater()
            self._audition_dialog = None

        # 停止批处理线程（若正在运行）
        if self._batch is not None and self._batch.isRunning():
            self._batch.stop()
            self._status.showMessage("等待批处理线程退出…")
            # 最多等待 5 秒
            self._batch.wait(5000)
            if self._batch.isRunning():
                logger.warning("批处理线程未在超时内退出，强制终止")
                self._batch.terminate()
                self._batch.wait(2000)
            self._batch = None

        # 停止搜索线程（若正在运行）
        if self._search_thread is not None and self._search_thread.isRunning():
            self._search_thread.quit()
            self._search_thread.wait(3000)
            self._search_thread = None

        # 停止 MV 整理线程（若正在运行）
        if self._mv_batch is not None and self._mv_batch.isRunning():
            self._mv_batch.stop()
            self._mv_batch.wait(3000)
            if self._mv_batch.isRunning():
                self._mv_batch.terminate()
                self._mv_batch.wait(2000)
            self._mv_batch.deleteLater()
            self._mv_batch = None

        # 停止 Web 管理服务器
        if self._web_server is not None:
            self._web_server.stop()
            self._web_server = None

        # 停止 Web 状态定时器
        if hasattr(self, "_web_timer") and self._web_timer is not None:
            self._web_timer.stop()


# ============================================================
# 手动搜索工作线程（模块级定义，确保信号在类层级正确注册）
# ============================================================


class _SearchWorker(QThread):
    """在独立线程中执行 ManualSearchService.search，避免阻塞 UI。"""

    # 搜索结果列表（List[SearchResult]）
    results_ready = pyqtSignal(list)
    # 错误信息
    error_occurred = pyqtSignal(str)

    def __init__(self, service, title: str, artist: str,
                 parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._service = service
        self._title = title
        self._artist = artist

    def run(self):
        """线程入口：执行搜索并发射结果/错误信号。"""
        try:
            results = self._service.search(self._title, self._artist)
            self.results_ready.emit(results)
        except Exception as e:
            logger.error(f"搜索线程异常: {e}", exc_info=True)
            self.error_occurred.emit(str(e))
