# Changelog

本文件遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.0.0/) 格式，
版本号遵循 [语义化版本控制 2.0.0](https://semver.org/lang/zh-CN/)。

## [1.0.10] - 2026-07-14

### Added

- **运行时日志开关**：新增「设置 → 日志」标签页，可启用/禁用文件日志（运行时即时生效，无需重启）；`config.json` 新增 `logging.enabled` 键，默认 `true`。
- **主列表勾选列**：文件列表新增「选择」复选列，批处理仅处理已勾选文件（默认全勾），其余保持原状。
- **主列表序号列**：文件列表新增「序号」列，随勾选/增删自动重排。
- **网络搜索总开关**：「设置 → 搜索」标签页顶部新增「是否进行网络搜索」勾选框，对应 `search.enabled`（默认 `true`）；关闭后批处理完全跳过在线元数据 / 封面 / 歌词匹配，仅按文件名与已有标签构建本地元数据。
- **文件整理总开关**：「设置 → 整理」标签页顶部新增「是否进行文件整理」勾选框，对应 `organize.enabled`（默认 `true`）；关闭后批处理仅填充元数据标签，不移动 / 重排音频文件（文件名重命名仍按「文件名」标签页模板执行）。
- **文件名处理总开关**：「设置 → 文件名」标签页顶部新增「是否进行文件名处理（按模板重命名）」勾选框，对应 `filename.enabled`（默认 `true`）；关闭后批处理仅填充元数据标签，完全不按模板重命名文件（#1）。
- **Meting 音源下拉**：「设置 → 搜索 → Meting 配置」的 Server 字段由文本框改为下拉列表，预设 `netease`/`tencent`/`kugou`/`xiami`/`baidu` 五种音源（显示中文名、存储英文值）；配置中的自定义音源不在预设时自动追加为「（自定义）」项并选中，避免丢失。
- **整理时迁移伴随文件**：目录整理（复制/移动到输出目录）现在会一并迁移同目录的 `.lrc` 歌词与封面图——`<原名>.lrc` 随音频改名、`cover.<ext>` 专辑级封面保留原名、`<原名>.<img>` 同名封面随音频改名；目标已存在同名封面时跳过避免重复 `cover (2).jpg`。兼容「先改名后整理」流程（按重命名前的原名定位伴随文件）。
- **列表列宽可手动调整**：文件列表所有列改为 `Interactive` 模式，支持拖拽调整列宽；「选择」列与「序号」列默认收窄至 44/50px，减少空间占用。

### Fixed

- **主列表「专辑」列空白**：文件列表此前仅按文件名推断元数据，且 `_guess_meta` 始终返回空专辑，导致「专辑」列永远为空。新增 `_resolve_meta`：在文件名解析基础上读取音频文件已有标签（歌手/标题/专辑），标签真实值优先于文件名推断（与 `batch_processor._parse_and_merge_metadata` 逻辑一致），列表加载时即正确填充「专辑」列。
- **打开文件夹崩溃（真实根因）**：工具栏「添加文件夹」按钮的 `QAction.triggered` 信号会附带一个 `bool` 参数，直接连到 `add_folder` 导致调用变成 `add_folder(False)`，绕过「`folder is None` 才弹对话框」的判断后把 `False` 传入 `os.walk(False)` 而抛出 `TypeError`。改为用 `lambda` 丢弃该参数（`triggered.connect(lambda: self.add_folder())`），并在 `add_folder` 入口对 `folder` 做类型归一化（仅当传入有效路径字符串时才跳过选择对话框）；同时保留 `os.walk(onerror=...)` 对无权限目录（`$RECYCLE.BIN`、`System Volume Information`、`lost+found`）的健壮性处理。

## [1.0.3] - 2026-07-14

### Fixed

- **试听弹窗关闭崩溃**：`audition/audition_dialog.py` 的 `closeEvent()` 在音频播放中关闭弹窗（X/ESC）时崩溃；改为先断开播放器信号连接（`positionChanged`/`playbackStateChanged`/`durationChanged`/`sliderMoved`）再执行 `player.stop()`，并释放音频输出，避免回调访问已销毁的 UI 控件。
- **重复搜索歌词崩溃**：连续两次点击"搜索歌词"（首次搜索仍在进行时）触发 `QThread.terminate()` 强制杀掉正在执行网络 I/O 的原生线程，导致进程崩溃。改为：再次搜索时仅设置停止事件并断开旧 worker 的结果回调（非阻塞、不卡 UI）；彻底移除 `terminate()`，旧线程自行结束后 `deleteLater` 清理；`_on_lyrics_search_done` 增加 `sender()` 守卫，忽略已被取代的旧搜索结果。

## [1.0.2] - 2026-07-14

### Added

- 版本号统一管理模块 `version.py`，作为应用名称与版本号的单一来源。

### Fixed

- **用户代理合规**：`search/musicbrainz_provider.py` 使用 `APP_NAME/APP_VERSION` 替换硬编码占位邮箱，符合 MusicBrainz 官方要求。
- **URL 校验**：`search/meting_provider.py` 新增 `_validate_url()` 校验 scheme 与网络地址完整性，并做 SSRF 防护（拒绝 localhost/私有 IP）。
- **日志可见性**：`search/rate_limiter.py` 在 `get_provider_config()` 匹配失败时输出 `logger.warning`，避免静默回退。
- **超时保护**：`search/provider.py` 提取 `_new_client()` 工厂方法，设置 `httpx.Timeout(10.0)` 作为默认超时。
- **QSS 分离**：`ui/dark_theme.qss` 从 Python 内联字符串提取为独立样式文件，`ui/main_window.py` 改为运行时加载，支持后续用户自定义主题。

### Changed

- `processor/batch_processor.py`：将 147 行的 `_process_one()` 拆分为 5 个职责单一的内联方法，编排逻辑从 147 行缩至 60 行。
- `services/tag_writer.py`：将 843 行的标签写入模块拆分为 `services/tag_writers/` 子包（mp3/flac/m4a/ogg/wma/ape 各约 60–100 行），原文件保留公共 API 与路由分发。
- 项目根目录新增 `start.bat` Windows 启动脚本。

## [1.0.1] - 2026-07-14

### Fixed

- **并发控制**：`search/rate_limiter.py` 中 Provider 封禁等待不再持有全局锁，避免单个 Provider 封禁阻塞所有 Provider 并发。
- **路径同步**：`processor/batch_processor.py` 现在正确跟踪文件重命名/目录整理后的最终路径；新增 `file_renamed` 信号同步更新 UI 列表与断点续传状态。
- **编码修复**：`services/metadata_saver.py` 的 `_apply_encoding` 改为通过 Latin-1 还原原始字节后按源 charset 解码，真正修复中文乱码。
- **线程清理**：`audition/audition_dialog.py` 的歌词搜索 Worker 新增停止事件，关闭弹窗时可在超时后安全终止线程。
- **封装修复**：`ui/file_list_widget.py` 暴露公开 `selection_changed` 信号，主窗口不再直接访问 `_table` 私有属性。
- **重名冲突**：`utils/helpers.py` 新增 `make_unique_path()`，`FileOrganizer` 与 `FileRenamer` 统一调用，避免重复实现。
- **设置对话框**：音质优先级列表使用 `UserRole` 存储 key；Provider 列表双击信号只在 UI 构建时连接一次。
- **模式别名**：`processor/conflict_resolver.py` 将 `replace` 作为 `overwrite` 的别名，修复命名不一致。
- **APE 读取**：`services/tag_writer.py` 中 APE 标签/封面/歌词读取增加 `APENoHeaderError` 保护。

### Changed

- `processor/file_organizer.py` 新增公开 `predict_target_path()` 方法，替代 `_build_target_path()` 的私有调用。
- `processor/batch_processor.py` 的进度处理改为使用实时文件列表，并做越界保护。

## [1.0.0] - 2026-07-12

### Added

- 初始版本：完整的音频文件元数据管理工具。
- 支持 MP3 / FLAC / M4A / OGG / WMA / APE 格式的标签读取与写入。
- 多 Provider 搜索架构：iTunes、MusicBrainz、LRCLIB、Meting。
- 批量处理工作流：解析 → 搜索 → 补全封面/歌词 → 编码统一 → 写入标签 → 目录整理。
- 试听弹窗：基于 QMediaPlayer 的音频播放与 LRC 歌词同步编辑。
- 深色主题 Qt 界面，支持拖放添加文件。
- 配置持久化与版本迁移机制。
- 断点续传状态管理。
