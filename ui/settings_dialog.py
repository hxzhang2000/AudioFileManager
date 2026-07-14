"""设置对话框（§9）。

``SettingsDialog`` 是应用的配置编辑界面，覆盖开发方案 §9.2 中定义的全部配置块：

- **搜索设置**：Provider 顺序/启用状态、请求超时、总超时
- **整理设置**：输出目录、按歌手/专辑分类、删除源文件
- **文件名模板**：模板字符串、分隔符、去除数字前缀
- **编码设置**：启用/字符集
- **去重设置**：模式/哈希算法
- **冲突设置**：模式/音质优先级
- **保存模式**：标签 / 文件 / 两者

使用方式::

    dialog = SettingsDialog(parent)
    dialog.set_config(current_config_dict)   # 加载当前配置
    if dialog.exec():
        new_config = dialog.get_config()     # 获取更新后的配置

界面采用 ``QTabWidget`` 分组，深色主题样式，与主窗口风格统一。
"""

from __future__ import annotations

from typing import Any, Dict, List

from utils.logger import logger

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

# Provider 显示名称映射
_PROVIDER_DISPLAY = {
    "itunes": "iTunes Search",
    "lrclib": "LRCLIB",
    "musicbrainz": "MusicBrainz",
    "meting": "Meting (网易云等)",
}


class SettingsDialog(QDialog):
    """设置对话框 — 编辑应用全部配置。

    通过 :meth:`set_config` 加载当前配置，用户编辑后通过
    :meth:`get_config` 获取更新后的完整配置 dict。
    """

    # 全部已知 Provider（用于顺序列表与启用勾选）
    ALL_PROVIDERS = ["itunes", "lrclib", "musicbrainz", "meting"]

    def __init__(self, config: dict = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.setMinimumSize(640, 560)

        # 当前配置快照（set_config 注入，get_config 基于控件状态重建）
        self._config: Dict[str, Any] = {}

        self._build_ui()

        # 加载传入的配置
        if config is not None:
            self.set_config(config)

    # ============================================================
    # UI 构建
    # ============================================================

    def _build_ui(self):
        """构建界面：QTabWidget 分组 + 底部确定/取消按钮。"""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)

        self.tab_widget = QTabWidget()
        self.tab_widget.addTab(self._build_search_tab(), "搜索")
        self.tab_widget.addTab(self._build_organize_tab(), "整理")
        self.tab_widget.addTab(self._build_filename_tab(), "文件名")
        self.tab_widget.addTab(self._build_encoding_tab(), "编码")
        self.tab_widget.addTab(self._build_duplicate_tab(), "去重")
        self.tab_widget.addTab(self._build_conflict_tab(), "冲突")
        self.tab_widget.addTab(self._build_save_mode_tab(), "保存模式")
        self.tab_widget.addTab(self._build_logging_tab(), "日志")
        layout.addWidget(self.tab_widget)

        # 底部按钮
        self.button_box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        self.button_box.accepted.connect(self.accept)
        self.button_box.rejected.connect(self.reject)
        layout.addWidget(self.button_box)

    # ------------------------------------------------------------
    # 搜索设置 Tab
    # ------------------------------------------------------------

    def _build_search_tab(self) -> QWidget:
        """搜索设置：Provider 顺序/启用、超时。"""
        tab = QWidget()
        layout = QVBoxLayout(tab)

        # 总体开关：是否进行网络搜索
        self._chk_enable_search = QCheckBox("是否进行网络搜索")
        self._chk_enable_search.setToolTip("关闭后完全跳过在线元数据 / 封面 / 歌词匹配，仅按文件名解析。")
        self._chk_enable_search.setChecked(True)
        layout.addWidget(self._chk_enable_search)
        layout.addSpacing(8)

        # --- Provider 启用与顺序 ---
        provider_group = QGroupBox("数据源 Provider")
        provider_layout = QVBoxLayout(provider_group)

        hint = QLabel("勾选启用的数据源，使用「上移/下移」调整优先级顺序：")
        hint.setStyleSheet("color: #888; font-size: 11px;")
        provider_layout.addWidget(hint)

        # Provider 列表（显示名称 + 启用勾选）
        self.provider_list = QListWidget()
        self.provider_list.setMinimumHeight(140)
        self.provider_list.itemDoubleClicked.connect(self._on_provider_toggle)
        provider_layout.addWidget(self.provider_list)

        # 顺序调整按钮
        order_layout = QHBoxLayout()
        self.btn_provider_up = QPushButton("上移")
        self.btn_provider_down = QPushButton("下移")
        self.btn_provider_up.clicked.connect(self._on_provider_up)
        self.btn_provider_down.clicked.connect(self._on_provider_down)
        order_layout.addStretch()
        order_layout.addWidget(self.btn_provider_up)
        order_layout.addWidget(self.btn_provider_down)
        provider_layout.addLayout(order_layout)

        layout.addWidget(provider_group)

        # --- 超时设置 ---
        timeout_group = QGroupBox("超时设置")
        timeout_form = QFormLayout(timeout_group)
        self.spin_request_timeout = QSpinBox()
        self.spin_request_timeout.setRange(1, 120)
        self.spin_request_timeout.setSuffix(" 秒")
        self.spin_total_timeout = QSpinBox()
        self.spin_total_timeout.setRange(1, 300)
        self.spin_total_timeout.setSuffix(" 秒")
        timeout_form.addRow("单次请求超时:", self.spin_request_timeout)
        timeout_form.addRow("总超时:", self.spin_total_timeout)
        layout.addWidget(timeout_group)

        # --- Meting 配置 ---
        meting_group = QGroupBox("Meting 配置")
        meting_form = QFormLayout(meting_group)
        self.edit_meting_api_url = QLineEdit()
        self.edit_meting_api_url.setPlaceholderText("https://api.injahow.cn/meting/")
        self.combo_meting_server = QComboBox()
        self.combo_meting_server.setEditable(False)
        self.combo_meting_server.setToolTip("选择 Meting-API 的音源（曲库）。")
        for _val, _label in (
            ("netease", "网易云音乐 (netease)"),
            ("tencent", "QQ音乐 (tencent)"),
            ("kugou", "酷狗音乐 (kugou)"),
            ("xiami", "虾米音乐 (xiami)"),
            ("baidu", "百度音乐 (baidu)"),
        ):
            self.combo_meting_server.addItem(_label, _val)
        meting_form.addRow("API URL:", self.edit_meting_api_url)
        meting_form.addRow("Server:", self.combo_meting_server)
        layout.addWidget(meting_group)

        layout.addStretch()
        return tab

    def _on_provider_up(self):
        """上移选中的 Provider。"""
        row = self.provider_list.currentRow()
        if row > 0:
            item = self.provider_list.takeItem(row)
            self.provider_list.insertItem(row - 1, item)
            self.provider_list.setCurrentRow(row - 1)

    def _on_provider_down(self):
        """下移选中的 Provider。"""
        row = self.provider_list.currentRow()
        if 0 <= row < self.provider_list.count() - 1:
            item = self.provider_list.takeItem(row)
            self.provider_list.insertItem(row + 1, item)
            self.provider_list.setCurrentRow(row + 1)

    # ------------------------------------------------------------
    # 整理设置 Tab
    # ------------------------------------------------------------

    def _build_organize_tab(self) -> QWidget:
        """整理设置：输出目录、分类、删除源文件。"""
        tab = QWidget()
        layout = QVBoxLayout(tab)

        # 总体开关：是否进行文件整理
        self._chk_enable_organize = QCheckBox("是否进行文件整理")
        self._chk_enable_organize.setToolTip("关闭后仅填充元数据标签，不移动 / 重排音频文件。")
        self._chk_enable_organize.setChecked(True)
        layout.addWidget(self._chk_enable_organize)
        layout.addSpacing(8)

        dir_group = QGroupBox("输出目录")
        dir_layout = QHBoxLayout(dir_group)
        self.edit_output_dir = QLineEdit()
        self.edit_output_dir.setPlaceholderText("选择整理后的输出目录...")
        self.btn_browse_dir = QPushButton("浏览...")
        self.btn_browse_dir.clicked.connect(self._on_browse_output_dir)
        dir_layout.addWidget(self.edit_output_dir)
        dir_layout.addWidget(self.btn_browse_dir)
        layout.addWidget(dir_group)

        classify_group = QGroupBox("分类与整理")
        classify_layout = QFormLayout(classify_group)
        self.chk_by_artist = QCheckBox("按歌手分类")
        self.chk_by_album = QCheckBox("按专辑分类")
        self.chk_album_with_year = QCheckBox("专辑名含年份")
        self.chk_unknown_dir = QCheckBox("未知信息归入 unknown 目录")
        classify_layout.addRow(self.chk_by_artist)
        classify_layout.addRow(self.chk_by_album)
        classify_layout.addRow(self.chk_album_with_year)
        classify_layout.addRow(self.chk_unknown_dir)
        layout.addWidget(classify_group)

        source_group = QGroupBox("源文件处理")
        source_layout = QVBoxLayout(source_group)
        self.chk_delete_source = QCheckBox("整理后删除源文件")
        self.chk_delete_source.setStyleSheet("color: #ff6b6b;")
        source_layout.addWidget(self.chk_delete_source)
        layout.addWidget(source_group)

        layout.addStretch()
        return tab

    def _on_browse_output_dir(self):
        """选择输出目录。"""
        d = QFileDialog.getExistingDirectory(
            self, "选择输出目录", self.edit_output_dir.text() or ""
        )
        if d:
            self.edit_output_dir.setText(d)

    # ------------------------------------------------------------
    # 文件名模板 Tab
    # ------------------------------------------------------------

    def _build_filename_tab(self) -> QWidget:
        """文件名模板设置。"""
        tab = QWidget()
        layout = QVBoxLayout(tab)

        # 总体开关：是否进行文件名处理
        self._chk_enable_filename = QCheckBox("是否进行文件名处理（按模板重命名）")
        self._chk_enable_filename.setToolTip("关闭后仅填充元数据标签，不按模板重命名文件。")
        self._chk_enable_filename.setChecked(True)
        layout.addWidget(self._chk_enable_filename)
        layout.addSpacing(8)

        group = QGroupBox("文件名模板")
        form = QFormLayout(group)
        self.edit_filename_template = QLineEdit()
        self.edit_filename_template.setPlaceholderText("{title} - {artist}")
        form.addRow("模板:", self.edit_filename_template)

        self.edit_separator = QLineEdit()
        self.edit_separator.setPlaceholderText("-")
        form.addRow("分隔符:", self.edit_separator)

        self.chk_strip_number_prefix = QCheckBox("去除序号前缀 (如 01. / 1-)")
        form.addRow(self.chk_strip_number_prefix)

        layout.addWidget(group)

        # 模板变量说明
        hint = QLabel(
            "可用变量: {title} 标题  {artist} 歌手  {album} 专辑\n"
            "          {year} 年份  {track} 曲目号"
        )
        hint.setStyleSheet("color: #888; font-size: 11px;")
        layout.addWidget(hint)

        layout.addStretch()
        return tab

    # ------------------------------------------------------------
    # 编码设置 Tab
    # ------------------------------------------------------------

    def _build_encoding_tab(self) -> QWidget:
        """编码设置：启用/字符集。"""
        tab = QWidget()
        layout = QVBoxLayout(tab)

        group = QGroupBox("文件编码转换")
        form = QFormLayout(group)

        self.chk_encoding_enabled = QCheckBox("启用编码转换（旧文件名/标签转 UTF-8）")
        form.addRow(self.chk_encoding_enabled)

        self.combo_charset = QComboBox()
        self.combo_charset.addItems(["UTF-8", "GBK", "GB2312", "BIG5", "Shift_JIS"])
        form.addRow("目标字符集:", self.combo_charset)

        layout.addWidget(group)

        hint = QLabel(
            "启用后，整理时会自动检测并转换非 UTF-8 编码的文件名与标签。\n"
            "适用于从旧系统或特定平台导入的音乐文件。"
        )
        hint.setStyleSheet("color: #888; font-size: 11px;")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        layout.addStretch()
        return tab

    # ------------------------------------------------------------
    # 去重设置 Tab
    # ------------------------------------------------------------

    def _build_duplicate_tab(self) -> QWidget:
        """去重设置：模式/哈希。"""
        tab = QWidget()
        layout = QVBoxLayout(tab)

        group = QGroupBox("重复文件检测")
        form = QFormLayout(group)

        self.combo_duplicate_mode = QComboBox()
        self.combo_duplicate_mode.addItem("跳过 (skip)", "skip")
        self.combo_duplicate_mode.addItem("覆盖 (overwrite)", "overwrite")
        self.combo_duplicate_mode.addItem("保留两者 (keep_both)", "keep_both")
        self.combo_duplicate_mode.addItem("询问 (ask)", "ask")
        form.addRow("处理模式:", self.combo_duplicate_mode)

        self.chk_use_hash = QCheckBox("使用内容哈希检测（更精确，但较慢）")
        form.addRow(self.chk_use_hash)

        self.combo_hash_algo = QComboBox()
        self.combo_hash_algo.addItems(["sha256", "md5", "sha1"])
        form.addRow("哈希算法:", self.combo_hash_algo)

        layout.addWidget(group)

        layout.addStretch()
        return tab

    # ------------------------------------------------------------
    # 冲突设置 Tab
    # ------------------------------------------------------------

    def _build_conflict_tab(self) -> QWidget:
        """冲突设置：模式/音质优先级。"""
        tab = QWidget()
        layout = QVBoxLayout(tab)

        group = QGroupBox("文件名冲突处理")
        form = QFormLayout(group)

        self.combo_conflict_mode = QComboBox()
        self.combo_conflict_mode.addItem("保留音质最佳 (keep_best_quality)", "keep_best_quality")
        self.combo_conflict_mode.addItem("保留最新 (keep_newest)", "keep_newest")
        self.combo_conflict_mode.addItem("保留最大 (keep_largest)", "keep_largest")
        self.combo_conflict_mode.addItem("跳过 (skip)", "skip")
        self.combo_conflict_mode.addItem("重命名 (rename)", "rename")
        form.addRow("处理模式:", self.combo_conflict_mode)

        self.chk_skip_existing = QCheckBox("跳过已存在文件（不处理）")
        form.addRow(self.chk_skip_existing)

        # 音质优先级（仅 keep_best_quality 模式生效）
        quality_group = QGroupBox("音质优先级（从高到低）")
        quality_layout = QVBoxLayout(quality_group)
        hint = QLabel("拖动调整优先级顺序：")
        hint.setStyleSheet("color: #888; font-size: 11px;")
        quality_layout.addWidget(hint)

        self.quality_list = QListWidget()
        quality_items = [
            ("bitrate", "bitrate (比特率)"),
            ("sample_rate", "sample_rate (采样率)"),
            ("format", "format (格式)"),
        ]
        for key, label in quality_items:
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, key)
            self.quality_list.addItem(item)
        self.quality_list.setMinimumHeight(100)
        quality_layout.addWidget(self.quality_list)

        q_order = QHBoxLayout()
        self.btn_quality_up = QPushButton("上移")
        self.btn_quality_down = QPushButton("下移")
        self.btn_quality_up.clicked.connect(self._on_quality_up)
        self.btn_quality_down.clicked.connect(self._on_quality_down)
        q_order.addStretch()
        q_order.addWidget(self.btn_quality_up)
        q_order.addWidget(self.btn_quality_down)
        quality_layout.addLayout(q_order)

        form.addRow(quality_group)
        layout.addWidget(group)

        layout.addStretch()
        return tab

    def _on_quality_up(self):
        """上移音质优先级。"""
        row = self.quality_list.currentRow()
        if row > 0:
            item = self.quality_list.takeItem(row)
            self.quality_list.insertItem(row - 1, item)
            self.quality_list.setCurrentRow(row - 1)

    def _on_quality_down(self):
        """下移音质优先级。"""
        row = self.quality_list.currentRow()
        if 0 <= row < self.quality_list.count() - 1:
            item = self.quality_list.takeItem(row)
            self.quality_list.insertItem(row + 1, item)
            self.quality_list.setCurrentRow(row + 1)

    # ------------------------------------------------------------
    # 保存模式 Tab
    # ------------------------------------------------------------

    def _build_save_mode_tab(self) -> QWidget:
        """保存模式设置：标签/文件/两者。"""
        tab = QWidget()
        layout = QVBoxLayout(tab)

        group = QGroupBox("元数据保存方式")
        form = QFormLayout(group)

        self.combo_save_mode = QComboBox()
        self.combo_save_mode.addItem("仅写入标签 (tags)", "tags")
        self.combo_save_mode.addItem("仅写入文件名 (filename)", "filename")
        self.combo_save_mode.addItem("标签 + 文件名 (both)", "both")
        form.addRow("保存模式:", self.combo_save_mode)

        layout.addWidget(group)

        hint = QLabel(
            "选择搜索结果写入方式：\n"
            "  标签 — 写入音频文件内嵌标签（ID3/Vorbis Comment/MP4 等）\n"
            "  文件名 — 按「文件名」标签页模板重命名文件\n"
            "  两者 — 同时写入标签与重命名文件"
        )
        hint.setStyleSheet("color: #888; font-size: 11px;")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        layout.addStretch()
        return tab

    # ------------------------------------------------------------
    # 日志设置 Tab
    # ------------------------------------------------------------

    def _build_logging_tab(self) -> QWidget:
        """日志设置：文件日志开关。"""
        tab = QWidget()
        layout = QVBoxLayout(tab)

        group = QGroupBox("文件日志")
        form = QFormLayout(group)
        self.chk_logging_enabled = QCheckBox("启用文件日志（记录到 %APPDATA%/AudioFileManager/logs/）")
        self.chk_logging_enabled.setChecked(True)
        form.addRow(self.chk_logging_enabled)
        layout.addWidget(group)

        hint = QLabel(
            "启用后，程序运行中的关键操作（启动、添加文件、批处理开始/停止、\n"
            "设置变更、异常等）都会写入按日期归档的日志文件，便于排查问题。\n"
            "关闭后不再写入日志文件（控制台仍可能输出）。"
        )
        hint.setStyleSheet("color: #888; font-size: 11px;")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        layout.addStretch()
        return tab

    # ============================================================
    # 配置加载 / 获取
    # ============================================================

    def set_config(self, config: Dict[str, Any]):
        """加载当前配置到各控件。

        Args:
            config: 完整配置 dict（结构见 §9.2 ``config.json``）。
        """
        self._config = config or {}

        # --- 搜索 ---
        search = self._config.get("search", {})
        enabled = search.get("provider_enabled", {})
        order = search.get("provider_order", self.ALL_PROVIDERS)
        self._load_provider_list(order, enabled)
        self.spin_request_timeout.setValue(
            int(search.get("request_timeout", 10))
        )
        self.spin_total_timeout.setValue(
            int(search.get("total_timeout", 30))
        )
        self._chk_enable_search.setChecked(search.get("enabled", True))

        # --- Meting ---
        meting = search.get("meting", {})
        self.edit_meting_api_url.setText(meting.get("api_url", ""))
        _server = meting.get("server", "")
        _idx = self.combo_meting_server.findData(_server)
        if _idx >= 0:
            self.combo_meting_server.setCurrentIndex(_idx)
        elif _server:
            # 配置中的自定义音源不在预设列表，追加并选中，避免丢失
            self.combo_meting_server.addItem(f"{_server} (自定义)", _server)
            self.combo_meting_server.setCurrentIndex(self.combo_meting_server.count() - 1)
        else:
            self.combo_meting_server.setCurrentIndex(0)

        # --- 整理 ---
        organize = self._config.get("organize", {})
        self.edit_output_dir.setText(organize.get("output_dir", ""))
        self.chk_by_artist.setChecked(organize.get("by_artist", True))
        self.chk_by_album.setChecked(organize.get("by_album", True))
        self.chk_album_with_year.setChecked(organize.get("album_with_year", True))
        self.chk_unknown_dir.setChecked(organize.get("unknown_dir", True))
        self.chk_delete_source.setChecked(organize.get("delete_source", False))
        self._chk_enable_organize.setChecked(organize.get("enabled", True))

        # --- 文件名 ---
        filename = self._config.get("filename", {})
        self._chk_enable_filename.setChecked(filename.get("enabled", True))
        self.edit_filename_template.setText(filename.get("template", "{title} - {artist}"))
        self.edit_separator.setText(filename.get("separator", "-"))
        self.chk_strip_number_prefix.setChecked(
            filename.get("strip_number_prefix", True)
        )

        # --- 编码 ---
        encoding = self._config.get("encoding", {})
        self.chk_encoding_enabled.setChecked(encoding.get("enabled", False))
        charset = encoding.get("charset", "UTF-8")
        idx = self.combo_charset.findText(charset)
        if idx >= 0:
            self.combo_charset.setCurrentIndex(idx)

        # --- 去重 ---
        duplicate = self._config.get("duplicate", {})
        dup_mode = duplicate.get("mode", "skip")
        for i in range(self.combo_duplicate_mode.count()):
            if self.combo_duplicate_mode.itemData(i) == dup_mode:
                self.combo_duplicate_mode.setCurrentIndex(i)
                break
        self.chk_use_hash.setChecked(duplicate.get("use_hash", False))
        hash_algo = duplicate.get("hash_algorithm", "sha256")
        idx = self.combo_hash_algo.findText(hash_algo)
        if idx >= 0:
            self.combo_hash_algo.setCurrentIndex(idx)

        # --- 冲突 ---
        conflict = self._config.get("filename_conflict", {})
        conf_mode = conflict.get("mode", "keep_best_quality")
        for i in range(self.combo_conflict_mode.count()):
            if self.combo_conflict_mode.itemData(i) == conf_mode:
                self.combo_conflict_mode.setCurrentIndex(i)
                break
        self.chk_skip_existing.setChecked(conflict.get("skip_existing", False))
        # 音质优先级
        quality_priority = conflict.get(
            "quality_priority", ["bitrate", "sample_rate", "format"]
        )
        self._load_quality_list(quality_priority)

        # --- 保存模式 ---
        save_mode = self._config.get("save_mode", "tags")
        for i in range(self.combo_save_mode.count()):
            if self.combo_save_mode.itemData(i) == save_mode:
                self.combo_save_mode.setCurrentIndex(i)
                break

        # --- 日志 ---
        logging_cfg = self._config.get("logging", {})
        self.chk_logging_enabled.setChecked(logging_cfg.get("enabled", True))

    def _load_provider_list(self, order: List[str], enabled: Dict[str, bool]):
        """加载 Provider 列表（顺序 + 启用勾选）。

        在 list 中显示「✓ 显示名称」格式，data 存 provider_id。
        """
        self.provider_list.clear()
        # 按 order 顺序添加
        added = set()
        for pid in order:
            if pid in self.ALL_PROVIDERS:
                self._add_provider_item(pid, enabled.get(pid, True))
                added.add(pid)
        # 补充 order 中未列出的已知 Provider
        for pid in self.ALL_PROVIDERS:
            if pid not in added:
                self._add_provider_item(pid, enabled.get(pid, False))

    def _add_provider_item(self, pid: str, is_enabled: bool):
        """添加一个 Provider 项到列表。"""
        display = _PROVIDER_DISPLAY.get(pid, pid)
        check = "✓" if is_enabled else "✗"
        text = f"[{check}] {display}"
        item = QListWidgetItem(text)
        item.setData(Qt.ItemDataRole.UserRole, pid)
        item.setData(Qt.ItemDataRole.UserRole + 1, is_enabled)
        self.provider_list.addItem(item)

    def _load_quality_list(self, priority: List[str]):
        """加载音质优先级列表。"""
        self.quality_list.clear()
        label_map = {
            "bitrate": "bitrate (比特率)",
            "sample_rate": "sample_rate (采样率)",
            "format": "format (格式)",
        }
        added = set()
        for key in priority:
            label = label_map.get(key, key)
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, key)
            self.quality_list.addItem(item)
            added.add(key)
        # 补充未列出的
        for key, label in label_map.items():
            if key not in added:
                item = QListWidgetItem(label)
                item.setData(Qt.ItemDataRole.UserRole, key)
                self.quality_list.addItem(item)

    def get_config(self) -> Dict[str, Any]:
        """从各控件读取状态，返回更新后的完整配置 dict。

        Returns:
            完整配置 dict（结构与 §9.2 ``config.json`` 一致）。
            基于原始 ``set_config`` 注入的配置做深拷贝后覆盖修改项，
            保留未涉及的字段。
        """
        import copy
        cfg = copy.deepcopy(self._config)

        # --- 搜索 ---
        search = cfg.setdefault("search", {})
        # 从列表读取顺序与启用状态
        order, enabled = self._read_provider_list()
        search["provider_order"] = order
        search["provider_enabled"] = enabled
        search["enabled"] = self._chk_enable_search.isChecked()
        search["request_timeout"] = self.spin_request_timeout.value()
        search["total_timeout"] = self.spin_total_timeout.value()

        # --- Meting ---
        meting = search.setdefault("meting", {})
        meting["api_url"] = self.edit_meting_api_url.text().strip()
        meting["server"] = self.combo_meting_server.currentData() or ""

        # --- 整理 ---
        organize = cfg.setdefault("organize", {})
        organize["output_dir"] = self.edit_output_dir.text().strip()
        organize["by_artist"] = self.chk_by_artist.isChecked()
        organize["by_album"] = self.chk_by_album.isChecked()
        organize["album_with_year"] = self.chk_album_with_year.isChecked()
        organize["unknown_dir"] = self.chk_unknown_dir.isChecked()
        organize["delete_source"] = self.chk_delete_source.isChecked()
        organize["enabled"] = self._chk_enable_organize.isChecked()

        # --- 文件名 ---
        filename = cfg.setdefault("filename", {})
        filename["enabled"] = self._chk_enable_filename.isChecked()
        filename["template"] = self.edit_filename_template.text().strip() or "{title} - {artist}"
        filename["separator"] = self.edit_separator.text().strip() or "-"
        filename["strip_number_prefix"] = self.chk_strip_number_prefix.isChecked()

        # --- 编码 ---
        encoding = cfg.setdefault("encoding", {})
        encoding["enabled"] = self.chk_encoding_enabled.isChecked()
        encoding["charset"] = self.combo_charset.currentText()

        # --- 去重 ---
        duplicate = cfg.setdefault("duplicate", {})
        duplicate["mode"] = self.combo_duplicate_mode.currentData()
        duplicate["use_hash"] = self.chk_use_hash.isChecked()
        duplicate["hash_algorithm"] = self.combo_hash_algo.currentText()

        # --- 冲突 ---
        conflict = cfg.setdefault("filename_conflict", {})
        conflict["mode"] = self.combo_conflict_mode.currentData()
        conflict["skip_existing"] = self.chk_skip_existing.isChecked()
        conflict["quality_priority"] = self._read_quality_list()

        # --- 保存模式 ---
        cfg["save_mode"] = self.combo_save_mode.currentData()

        # --- 日志 ---
        cfg["logging"] = {"enabled": self.chk_logging_enabled.isChecked()}

        return cfg

    def _read_provider_list(self) -> tuple[List[str], Dict[str, bool]]:
        """从 Provider 列表读取顺序与启用状态。

        双击列表项可切换启用/禁用勾选。

        Returns:
            (order, enabled) — order 为 provider_id 顺序列表，
            enabled 为 {provider_id: bool} 字典。
        """
        order: List[str] = []
        enabled: Dict[str, bool] = {}
        for i in range(self.provider_list.count()):
            item = self.provider_list.item(i)
            pid = item.data(Qt.ItemDataRole.UserRole)
            is_enabled = item.data(Qt.ItemDataRole.UserRole + 1)
            order.append(pid)
            enabled[pid] = bool(is_enabled)
        return order, enabled

    def _read_quality_list(self) -> List[str]:
        """从音质优先级列表读取顺序。

        Returns:
            音质维度 key 列表（如 ``["bitrate", "sample_rate", "format"]``）。
        """
        result: List[str] = []
        for i in range(self.quality_list.count()):
            key = self.quality_list.item(i).data(Qt.ItemDataRole.UserRole)
            result.append(key or "")
        return [k for k in result if k]

    # ============================================================
    # 事件：双击 Provider 项切换启用状态
    # ============================================================

    def _on_provider_toggle(self, item: QListWidgetItem):
        """双击 Provider 项切换启用/禁用。"""
        pid = item.data(Qt.ItemDataRole.UserRole)
        is_enabled = item.data(Qt.ItemDataRole.UserRole + 1)
        new_enabled = not is_enabled
        item.setData(Qt.ItemDataRole.UserRole + 1, new_enabled)
        display = _PROVIDER_DISPLAY.get(pid, pid)
        check = "✓" if new_enabled else "✗"
        item.setText(f"[{check}] {display}")
