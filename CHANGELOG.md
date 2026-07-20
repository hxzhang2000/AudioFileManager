# Changelog

本文件遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.0.0/) 格式，
版本号遵循 [语义化版本控制 2.0.0](https://semver.org/lang/zh-CN/)。

## [1.2.2] - 2026-07-20

### Fixed

- **Release 正文未包含 CHANGELOG 内容**：`generate_release_body.py` 正则匹配在 CI 环境中失效导致回退到占位文本。新增多级匹配策略（标准日期格式 → 宽松格式 → 文本搜索），并增加完整调试日志以便排查。
- **只有发版一种模式**：workflow 中 Release 步骤新增 `if` 条件判断——tag 名不含 `-no-release` 后缀时才创建 Release。配合 release-flow skill 的发布类型选择（Release / Build-only），实现「仅构建不发版」的能力。

### Changed

- **release-flow skill**：新增第 1 步「发布类型选择」，Build-only 模式使用 `v{ver}-no-release` tag，CI 仅构建不创建 Release。
- `.github/workflows/build.yml`：Release 步骤增加 `if: ${{ !contains(github.ref_name, '-no-release') }}`。

## [1.2.1] - 2026-07-20

### Added

- **MV 视频文件支持**：批处理时同步扫描 `.mkv`/`.mp4`/`.avi` 等视频文件，按扩展名路由到简化 4 步流程（解析 → 搜索 → 重命名 → 整理），与音频共用同一线程，不再独立开线程。
- **非 MV 文件跳过保护**：文件名不匹配「歌曲名 - 歌手」模式且搜索无结果时自动跳过，避免把随机视频改名移出。
- **设置弹窗 MV 标签页**：新增「MV/视频」配置标签页，支持启用 MV 处理、扩展名列表、搜索结果封面补全开关。
- **设置弹窗 Tab 紧凑样式**：标签页文字缩小至 11px、间距收窄。
- **脚本补充**：
  - `scripts/restore_mv_from_log.py` — 从批处理日志回退已移动的 MV 文件到源目录
  - `scripts/generate_release_body.py` — 从 CHANGELOG 提取 Release 正文
- **Release 自动化**：
  - `AudioFileManager.spec` 改从 `version.py` 动态读取版本号，EXE 嵌入 Windows 版本信息（右键属性可见）
  - `.github/workflows/build.yml` 改用 `.spec` 构建，Release 自动附带 CHANGELOG 版本说明
- **全局 skill**：`release-flow` 技能，一句话触发发版提交流程

### Changed

- **MV 处理合入 BatchProcessor**：移除 `main_window.py` 中的独立 MV 菜单/按钮和独立线程 `MvProcessor`，全部在 `batch_processor.py` 的 `run()` 中处理
- `processor/file_scanner.py`、`ui/file_load_worker.py` 同步加入视频扩展名常量

## [1.1.0] - 2026-07-16

### Added

- **Web 管理服务器**：基于 Flask + waitress 的局域网 Web 服务，支持在浏览器中查看批处理状态、控制启停、浏览音频目录与加载文件。
  - REST API: `/api/status`（实时状态）、`/api/action`（控制）、`/api/files`（文件列表）、`/api/directory`（目录浏览与加载）、`/api/events`（SSE 推送）
  - 深色主题单页 Web UI，包含概览、文件列表、浏览目录、日志四页面
  - `WebBridge` 线程安全桥接器，实现 Web 服务线程与 Qt UI 线程之间的状态同步和动作队列
  - 设置对话框新增「Web 服务」标签页，可配置启用/端口/监听地址，支持运行时重启
- `requirements.txt`：新增 `flask>=3.0.0`、`waitress>=3.0.0` 依赖

## [1.0.23] - 2026-07-15

### Fixed

- **批处理后点击同一行封面消失（FILES 模式）**：`_on_batch_file_metadata_updated` 在搜索结果无封面时用 `cover_data=None` 覆盖面板已有封面；且文件列表仅监听 `itemSelectionChanged`，批处理后点击已选中的同一行不触发热加载。现修复：批处理信号中 `cover_data` 为 None 时不覆盖面板；新增 `currentItemChanged` 确保点击同列也触发重新读取文件元数据。
- **`replace_front_cover()` 缩进错误导致重复封面帧**：`audio.tags.add(APIC(...))` 被错误放置在 `for f in existing:` 循环内，每次循环都额外添加一次封面帧。现移至循环外，保证封面帧仅添加一份。

### Added

- **关于对话框升级**：从简单的 `QMessageBox.about` 替换为自定义 `QDialog`，新增 GitHub Star 按钮，用户可一键打开项目主页给予支持。
- **封面读取诊断日志**：`_cover_mp3()` 新增 try/except 和 ID3 标签缺失检测日志，便于排查封面加载失败原因。

## [1.0.22] - 2026-07-14

### Fixed

- **手动保存后封面图消失（保存模式为 FILES 时）**：`_on_save_requested` 构造 `TrackMetadata` 时未传入 `cover_data` 和 `lyrics_text`，导致 UI 更新将封面设为 `None`。现已补传。
- **切换文件再切回后封面/歌词消失（FILES 模式）**：FILES 模式将封面和歌词保存为同目录的 `cover.jpg` / `.lrc` 而非嵌入标签，`MetadataReader.read_cover()` / `read_lyrics()` 只读取标签内嵌数据，切换文件重读时返回 `None`。已添加回退逻辑：标签内无数据时分别检查同目录的 `cover.jpg` 和 `.lrc` 文件。

## [1.0.21] - 2026-07-14

### Fixed

- **mutagen 文件句柄未显式释放导致 Windows 下 rename/move 失败（WinError 32）**：手动保存标签后，批处理对该文件的后续「重命名」和「目录整理」因 `audio.save()` 后句柄未被释放而锁定。已对所有 6 个格式的 tag writer（mp3/flac/m4a/ogg/ape/wma）以及 `encoding_service.py`、`metadata_reader.py` 中的每次 `audio.save()` 和读取操作添加了 `del audio` 显式清理。

## [1.0.20] - 2026-07-14

### Added

- **详情面板「保存到文件」后同步更新主页面列表**：手动保存元数据到文件标签后，主窗口文件列表的歌手/标题/专辑等列同步刷新；若详情面板当前显示的正是该文件，面板数据也随之更新。

### Fixed

- **详情面板「网络搜索信息」未使用相似度降级逻辑**：手动搜索直接调用 `engine.search_metadata` 未传入 `similarity_fn`，仍为"首个非空即收"模式。现改为与批处理搜索一致的相似度降级匹配（`BatchProcessor._title_similarity`）。

## [1.0.19] - 2026-07-14

### Fixed

- **搜索返回同一歌手的不同歌曲覆盖正确文件名解析**：`SearchEngine` 原为「首个非空即收」模式，当 Provider 返回同歌手不同歌时直接覆盖。改为相似度降级匹配：搜索时携带 `_title_similarity` 字符级校验函数（`difflib.SequenceMatcher`），各 Provider 依次尝试，首个返回 ≥35% 相似度的结果直接采用；若全部 Provider 都返回低分结果，则自动选相似度最高者并记录 info 日志。

## [1.0.18] - 2026-07-14

### Fixed

- **空格分隔的文件名「歌曲名 歌手」解析为整串标题**：两段式解析器仅识别 `-` / `–` / `—` 作为分隔符，纯空格分隔的 `124.这首歌唱给你 王奕心&郑东.mp3` 无法切分歌手，搜索结果完全错配。增加空格分隔兜底逻辑：两段式短横匹配失败后，用 `rsplit(maxsplit=1)` 取最后一段，通过 `_looks_like_artist()`（含 `&`、命中艺人库、中文姓氏）判断是否为歌手。
- **繁体→简体（T2S）转换实际未生效**：`hanziconv` 已在 `requirements.txt` 声明但未安装（`ImportError` 被静默捕获），`_t2s()` 降级为原样返回。现已安装，`鄭中基` → `郑中基` 等转换正常工作。

## [1.0.17] - 2026-07-14

### Fixed

- **打开带内嵌封面的 MP3 试听弹窗时卡死**：QMediaPlayer (Windows Media Foundation) 将内嵌封面（APIC/mjpeg）识别为视频流，`setSource` 在构造函数中同步初始化视频解码管道，阻塞 UI 线程。改为 `QTimer.singleShot(0, ...)` 延迟到对话框构建完成、事件循环就绪后再调用 `setSource`，消除启动卡死（c.f. QMediaPlayer on Windows + embedded cover known issue）。
- **关闭试听弹窗时 `RuntimeError: wrapped C/C++ object of type _LyricsSearchWorker has been deleted` 崩溃**：`closeEvent` → `_stop_lyrics_worker` 中访问已自然结束并被 `deleteLater` 销毁的 worker 对象。用 `try/except RuntimeError` 安全处理 worker 已销毁的场景。

## [1.0.16] - 2026-07-14

### Fixed

- **冲突解决「跳过」时 UI 日志误标为「完成」**：`_organize_file` 中的 skip 路径原返回 `None`，导致 `_process_one` 正常结束、UI 显示"✓ 完成"。改为抛出 `DuplicateFileException`，让 `run()` 的现有 `except` 分支捕获并发射 `file_finished(file, "skipped")`，底部日志正确显示"⊘ 已跳过"。

## [1.0.15] - 2026-07-14

### Changed

- **试听弹窗保存歌词遵循配置**：`_save_to_file` 不再硬编码同时写入文件标签 + LRC，改为读取配置的 `save_mode`（`tags`/`files`/`both`），与批处理和手动保存行为一致。弹窗新增 `save_mode` 参数，主窗口创建时传入当前配置值。

### Fixed

- **试听弹窗保存后关闭崩溃**：`_save_to_file` 写入歌词前先调用 `player.stop()` 释放文件句柄，避免 Windows Media Foundation 管道因底层文件被 mutagen 修改而状态不一致，导致关闭弹窗时 `closeEvent` 里的 `player.stop()` 崩溃。
- **批处理后文件列表元数据未刷新**：`BatchProcessor` 新增 `file_metadata_updated` 信号，`_process_one` 处理完每个文件后发射已解析的元数据字典（歌手/标题/专辑/年份/流派/曲目号/封面/歌词）。主窗口连接该信号，自动更新文件列表的歌手/标题/专辑列，若详情面板当前显示该文件则同步刷新面板，修复批处理完成后列表仍显示旧数据的问题。

## [1.0.14] - 2026-07-14

### Added

- **批处理暂停/继续**：进度条区域新增「暂停」按钮（位于进度条右侧、停止按钮左侧）。点击一次暂停处理（当前文件完成后生效），再次点击继续；按钮文本在「暂停 / 继续」间切换，状态栏与日志同步提示。暂停基于工作线程阻塞实现，不卡界面；暂停期间点击「停止」可正常退出。

## [1.0.13] - 2026-07-14

### Added

- **繁体→简体转换开关**：「设置 → 编码」标签页新增「启用繁体→简体转换（如 費翔 → 费翔）」勾选框，对应 `encoding.traditional_to_simplified`（默认 `false`）。开启后，批处理在写入标签前（以及编码统一步骤中）将元数据文本中的繁体中文字符转为简体，使同一艺人的不同写法（如 費翔 / 费翔）在标签与去重中保持一致；去重归一化键也走同一转换。依赖 `hanziconv`（纯 Python，已加入 `requirements.txt`），缺失时自动降级为不转换。

## [1.0.12] - 2026-07-14

### Added

- **批处理剩余时间预估（ETA）**：进度条文本在总进度后追加「预计剩余 X 分 Y 秒」，基于已用时间与已处理文件数估算单文件平均耗时并外推剩余时长；开局数据不足时显示「计算中…」，批处理结束后复位。

## [1.0.11] - 2026-07-14

### Fixed

- **打开大目录时界面卡死**：目录扫描与逐文件标签读取原在主线程同步执行，文件很多时界面长时间无响应。改为后台 `QThread`（`FileLoadWorker`）执行扫描与元数据解析，并显示模态「加载中」进度对话框（带取消按钮），扫描结果批量回填列表。

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
