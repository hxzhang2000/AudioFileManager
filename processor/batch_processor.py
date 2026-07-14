"""批处理调度器（§10.1, §10.2, §10.7.5, §10.9）。

在 QThread 工作线程中逐文件处理音频文件，完整流程（§10.3）：

1. **文件名解析**（离线）→ 提取 ``(title, artist, album)``，优先读取已有 ID3 标签
2. **元数据搜索**（在线）→ iTunes → MusicBrainz → Meting 降级链
3. **封面+歌词补全**（在线）→ ``enrich_cover_and_lyrics`` 统一补全，优先复用已有 cover_url
4. **编码统一**（离线，如启用）→ 统一标签文本编码
5. **标签写入**（离线）→ ``MetadataSaver.save_metadata_to_tags`` 按 save_mode 路由
6. **目录整理**（离线）→ 按歌手/专辑分目录 + 文件名冲突处理

支持断点续传（§10.4）、停止控制（§10.4.2）、离线模式（§10.9）。

执行模型（v5.7 统一）：同步模型。网络 I/O 使用同步 ``httpx``，速率控制
使用 ``threading``（§10.7 RateLimiter）。运行在 QThread 工作线程中，
同步阻塞不卡 GUI 主线程。
"""

import os
import threading
import logging
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QThread, pyqtSignal

from parser.filename_parser import FileNameParser
from parser.artist_db import ArtistDB
from parser.metadata_reader import MetadataReader
from search.provider import SearchEngine, TrackMetadata
from search.config import SearchConfig
from services.metadata_saver import MetadataSaver
from services.encoding_service import EncodingService
from processor.batch_state import BatchStateManager
from processor.file_renamer import FileRenamer
from processor.file_scanner import AudioFileEntry
from processor.conflict_resolver import DuplicateDetector, ConflictResolver

logger = logging.getLogger(__name__)


class DuplicateFileException(Exception):
    """重复文件异常，用于在 _process_one() 中通知 run() 跳过该文件。"""
    pass


class BatchProcessor(QThread):
    """批处理调度器 — 在 QThread 工作线程中逐文件处理音频文件。

    信号：

    - ``progress_updated(int, int, str, int, int)``：
        ``(当前文件索引, 文件总数, 子步骤名称, 子步骤序号, 子步骤总数)``
    - ``file_finished(str, str)``：
        ``(文件路径, 状态)`` — 状态为 ``"done"`` 或 ``"failed"``
    - ``batch_finished(int, int)``：
        ``(成功数, 失败数)``
    - ``error_occurred(str)``：
        严重错误信息（如离线模式提示）

    子步骤名称（§10.7.5）：
    ``"parsing"`` → ``"searching"`` → ``"enriching"`` →
    ``"encoding"`` → ``"writing"`` → ``"organizing"``
    """

    # —— 信号定义 ——
    # 进度信号（细粒度）：(current_file_index, total_files, sub_step_name, sub_step_index, sub_step_total)
    progress_updated = pyqtSignal(int, int, str, int, int)
    # 单文件完成信号：file_path, status("done" / "failed")
    file_finished = pyqtSignal(str, str)
    # 文件路径变更信号（重命名/整理后）：(old_path, new_path)
    file_renamed = pyqtSignal(str, str)
    # 批处理完成信号：success_count, failed_count
    batch_finished = pyqtSignal(int, int)
    # 严重错误信号：error_message
    error_occurred = pyqtSignal(str)

    # 子步骤名称列表（§10.7.5），共 6 步
    _SUB_STEPS = ["parsing", "searching", "enriching", "encoding", "writing", "organizing"]

    def __init__(self, files: list[str], config: dict, state_file: str):
        """初始化批处理调度器。

        Args:
            files: 待处理的音频文件路径列表。
            config: 完整配置字典（对应 config.json 的内容）。
            state_file: 断点续传状态文件路径。
        """
        super().__init__()
        # 线程安全的停止信号（§10.4.2）
        self._stop_event = threading.Event()
        self.files = files
        self.config = config
        self.state_file = state_file
        self._offline_mode = False

        # —— 断点续传状态管理器（§10.4）——
        self.state_manager = BatchStateManager(state_file)

        # —— 文件名解析器（需要 ArtistDB，§5.1）——
        self.artist_db = ArtistDB()
        self.parser = FileNameParser(self.artist_db)

        # —— 现有 ID3 标签读取器（§5.1.3，优先于文件名解析）——
        self.metadata_reader = MetadataReader()

        # —— 搜索引擎（§3.2，按配置注册各 Provider）——
        self.engine = self._build_search_engine(config)

        # —— 元数据保存器（§7.5，按 save_mode 路由）——
        self.saver = MetadataSaver(
            save_mode=config.get("save_mode", "tags"),
            encoding_config=config.get("encoding", {"enabled": False, "charset": "UTF-8"}),
        )

        # —— 编码统一服务（§7.6）——
        encoding_cfg = config.get("encoding", {})
        self.encoder = EncodingService(
            enabled=encoding_cfg.get("enabled", False),
            target_charset=encoding_cfg.get("charset", "UTF-8"),
            traditional_to_simplified=encoding_cfg.get("traditional_to_simplified", False),
        )

        # —— 目录整理器（§6，延迟加载：模块可能尚未实现）——
        self.organizer = None
        try:
            from processor.file_organizer import FileOrganizer
            self.organizer = FileOrganizer(config.get("organize", {}))
        except ImportError:
            logger.warning("file_organizer 模块未实现，目录整理步骤将被跳过")

        # —— 文件重命名器（§6.3，按模板重命名）——
        self.file_renamer = FileRenamer(config)

        # —— 重复检测器（§10.5.1，L1 名称+大小 / L2 哈希）——
        self.duplicate_detector = DuplicateDetector(config.get("duplicate", {}))

        # —— 文件名冲突解决器（§10.5.2，音质比较等）——
        self.conflict_resolver = ConflictResolver(config.get("filename_conflict", {}))

    # ============================================================
    # 停止控制（§10.4.2）
    # ============================================================

    @property
    def _is_stopped(self) -> bool:
        """停止信号是否已触发（线程安全读取）。"""
        return self._stop_event.is_set()

    def stop(self):
        """外部调用：点击「停止」按钮触发。

        设置停止信号，当前正在处理的一个文件允许完成后停止，
        剩余文件全部保留为 pending 状态，状态文件保存当前进度。
        """
        self._stop_event.set()

    # ============================================================
    # 网络检测（§10.9 离线模式）
    # ============================================================

    def _check_network(self) -> bool:
        """检测网络连通性。

        尝试连接阿里 DNS (223.5.5.5:53)，3 秒超时。
        使用国内可达的地址，避免在大陆网络环境下因 8.8.8.8 被墙而误判离线。
        连接成功返回 ``True``，失败返回 ``False``（进入离线模式）。
        """
        import socket
        try:
            socket.create_connection(("223.5.5.5", 53), timeout=3)
            return True
        except OSError:
            return False

    # ============================================================
    # 主循环（§10.1 完整处理流程）
    # ============================================================

    def run(self):
        """批处理主循环（在 QThread 工作线程中执行）。

        流程：
        1. 网络检测 → 决定是否进入离线模式（§10.9）
        2. 逐文件处理，断点续传跳过已完成的文件（§10.4）
        3. 每处理完一个文件更新状态文件
        4. 用户点击停止时，当前文件完成后停止（§10.4.2）
        5. 全部完成后发射 batch_finished 信号
        """
        total = len(self.files)

        # —— 网络检测（§10.9 离线模式）——
        self._offline_mode = not self._check_network()
        if self._offline_mode:
            logger.warning("网络不可用，进入离线模式：仅基于已有 ID3 和文件名处理")
            self.error_occurred.emit("网络不可用，进入离线模式：仅基于已有 ID3 和文件名处理")

        # —— 记录源目录到状态文件 ——
        source_dir = self.config.get("last_input_dir", "")
        if not source_dir and self.files:
            source_dir = str(Path(self.files[0]).parent)
        self.state_manager.get_state()["source_dir"] = source_dir

        success_count = 0
        failed_count = 0

        try:
            for i, file_path in enumerate(self.files):
                # 检查停止信号（处理新文件前）
                if self._is_stopped:
                    logger.info(f"用户手动停止批处理，已处理 {i}/{total} 个文件")
                    break

                # 断点续传：跳过已完成的文件（§10.4.1）
                if self.state_manager.is_done(file_path):
                    logger.debug(f"断点续传跳过已完成文件: {file_path}")
                    # 跳过时也发射进度信号，使 UI 进度条能正确反映
                    self.progress_updated.emit(i, total, "skipped", 0, len(self._SUB_STEPS))
                    self.file_finished.emit(file_path, "skipped")
                    success_count += 1
                    continue

                try:
                    final_path, result = self._process_one(file_path, i, total)
                    self._save_state(final_path, "done", result)
                    self.file_finished.emit(file_path, "done")
                    if final_path != file_path:
                        self.file_renamed.emit(file_path, final_path)
                    success_count += 1
                except DuplicateFileException as e:
                    logger.info(str(e))
                    self._save_state(file_path, "done", "skipped_duplicate")
                    self.progress_updated.emit(i, total, "skipped", 0, len(self._SUB_STEPS))
                    self.file_finished.emit(file_path, "skipped")
                    success_count += 1
                    continue
                except Exception as e:
                    error_msg = str(e)
                    logger.error(f"处理文件失败 {file_path}: {error_msg}", exc_info=True)
                    self._save_state(file_path, "failed", error_msg)
                    self.file_finished.emit(file_path, "failed")
                    failed_count += 1

                # 处理完一个文件后检查停止信号（当前文件允许完成后停止）
                if self._is_stopped:
                    logger.info("用户手动停止批处理，当前文件已完成，剩余文件保留为 pending")
                    break
        finally:
            # 清理搜索引擎连接池资源
            try:
                self.engine.close()
            except Exception as e:
                logger.debug(f"搜索引擎关闭异常: {e}")

        # 发射批处理完成信号
        self.batch_finished.emit(success_count, failed_count)

    # ============================================================
    # 单文件处理（§10.3 元数据搜索与写入顺序，§10.7.5 请求节奏）
    # ============================================================

    def _process_one(self, file_path: str, index: int, total: int) -> tuple[str, str]:
        """处理单个文件（从解析到整理），支持子步骤进度上报。

        处理顺序（§10.3）：
        1. 文件名解析（离线）→ 提取 (title, artist, album)，优先读取已有 ID3
        2. 元数据搜索（在线）→ 搜索引擎降级链
        3. 封面+歌词补全（在线）→ enrich_cover_and_lyrics，优先复用已有 cover_url
        4. 编码统一（离线，如启用）
        5. 标签写入（离线）→ MetadataSaver 按 save_mode 路由
        6. 目录整理（离线）→ FileOrganizer 按歌手/专辑分目录

        注意（§10.7.5）：将文件的多个网络请求分散在时间线上，
        避免短时间内对同一个 Provider 连续请求。每个请求内部走 RateLimiter。

        Args:
            file_path: 音频文件完整路径。
            index: 当前文件在批处理列表中的索引（从 0 起）。
            total: 批处理文件总数。

        Returns:
            ``(最终文件路径, 处理结果描述字符串)``。
            若发生重命名或目录整理，最终路径可能与 ``file_path`` 不同。

        Raises:
            Exception: 处理过程中的任何异常（由 run() 捕获并标记为 failed）。
        """
        total_steps = len(self._SUB_STEPS)  # 6

        # —— 步骤 1：文件名解析 + 重复检测（离线）——
        self.progress_updated.emit(index, total, "parsing", 1, total_steps)
        title, artist, album = self._parse_and_merge_metadata(file_path)
        self._check_duplicate(file_path, title, artist)

        # —— 步骤 2：元数据搜索（在线，受 search.enabled 控制）——
        search_cfg = self.config.get("search", {})
        search_enabled = search_cfg.get("enabled", True)
        self.progress_updated.emit(index, total, "searching", 2, total_steps)
        if search_enabled and not self._offline_mode:
            metadata = self._search_metadata(title, artist, album)
        else:
            # 关闭网络搜索：仅基于文件名 / 已有标签构建本地元数据
            metadata = TrackMetadata(title=title, artist=artist or "未知艺人")
            if album:
                metadata.album = album

        # —— 步骤 3：封面+歌词补全（在线，受 search.enabled 控制）——
        self.progress_updated.emit(index, total, "enriching", 3, total_steps)
        if search_enabled and not self._offline_mode:
            self._enrich_cover_and_lyrics(metadata, title, artist, album)

        # —— 步骤 4：编码统一（离线，如启用或开启繁→简）——
        self.progress_updated.emit(index, total, "encoding", 4, total_steps)
        if self.encoder.enabled or self.encoder.t2s_enabled:
            self.encoder.normalize_file(file_path)

        # —— 步骤 5：标签写入（离线）——
        self.progress_updated.emit(index, total, "writing", 5, total_steps)
        # 繁→简转换（如启用）：写入前统一元数据文本，使 費翔 → 费翔 等保持一致
        self._apply_t2s_to_metadata(metadata)
        self.saver.save_metadata_to_tags(
            file_path,
            metadata,
            cover_data=metadata.cover_data,
            lyrics_text=metadata.lyrics_text,
        )

        # —— 步骤 5.5：文件名重命名（按模板，§6.3，受 filename.enabled 控制）——
        # 记录重命名前的原始路径，用于后续整理时定位同名的 .lrc / 封面图
        original_path = file_path
        filename_cfg = self.config.get("filename", {})
        if filename_cfg.get("enabled", True):
            file_path = self._rename_file(file_path, metadata)

        # —— 步骤 6：目录整理（离线，受 organize.enabled 控制）——
        organize_cfg = self.config.get("organize", {})
        self.progress_updated.emit(index, total, "organizing", 6, total_steps)
        if organize_cfg.get("enabled", True):
            file_path = (
                self._organize_file(file_path, metadata, original_path=original_path)
                or file_path
            )

        return file_path, f"已处理: {metadata.title} - {metadata.artist}"

    # ------------------------------------------------------------------
    # _process_one 内联子步骤
    # ------------------------------------------------------------------

    def _parse_and_merge_metadata(self, file_path: str) -> tuple[str, str, str]:
        """解析文件名，并用已有 ID3 标签补全。

        Returns:
            ``(title, artist, album)`` 三元组。
        """
        filename = Path(file_path).name
        title, artist, album, uncertain = self.parser.parse(filename)
        if uncertain:
            logger.info(f"三段式解析不确定: {file_path}，将在 UI 标记警告")

        # 优先从已有 ID3 标签读取已知信息（§10.1 步骤 2）
        existing_meta = self.metadata_reader.read(file_path)
        if existing_meta.get("title"):
            title = existing_meta["title"]
        if existing_meta.get("artist"):
            artist = existing_meta["artist"]
        if existing_meta.get("album"):
            album = existing_meta["album"]
        return title, artist, album

    def _check_duplicate(self, file_path: str, title: str, artist: str):
        """基于解析后的 title + artist 进行 L1 去重（§10.5.1）。

        Raises:
            DuplicateFileException: 跳过模式下的重复文件。
        """
        entry = AudioFileEntry(
            file_path=file_path,
            relative_path=os.path.basename(file_path),
            dir_name=os.path.basename(os.path.dirname(file_path)) or "",
            size=0,
            ext=Path(file_path).suffix.lower().lstrip("."),
        )
        try:
            entry.size = os.path.getsize(file_path)
        except OSError:
            pass
        normalized = self.encoder.convert_text(title).lower().strip() if title else ""
        if artist:
            normalized = f"{self.encoder.convert_text(title)} - {self.encoder.convert_text(artist)}".lower().strip()
        entry.name_normalized = normalized
        entry = self.duplicate_detector.check(entry)
        if not entry.is_duplicate:
            return

        dup_mode = self.config.get("duplicate", {}).get("mode", "skip")
        if dup_mode == "skip":
            raise DuplicateFileException(
                f"跳过重复文件: {file_path}（重复于 {entry.duplicate_of}）"
            )
        elif dup_mode == "replace":
            logger.warning(
                f"重复文件（replace 模式）: {file_path}（重复于 {entry.duplicate_of}）"
            )
        else:
            logger.warning(
                f"未知重复处理模式 '{dup_mode}': {file_path}（重复于 {entry.duplicate_of}）"
            )

    def _search_metadata(self, title: str, artist: str, album: str) -> TrackMetadata:
        """执行元数据搜索，失败时返回基于文件名的空 TrackMetadata。"""
        metadata: Optional[TrackMetadata] = None
        if not self._offline_mode:
            metadata = self.engine.search_metadata(title, artist or "")

        if metadata is None:
            metadata = TrackMetadata(title=title, artist=artist or "未知艺人")
        else:
            if not metadata.title:
                metadata.title = title
            if not metadata.artist:
                metadata.artist = artist or "未知艺人"

        if album and not metadata.album:
            metadata.album = album
        return metadata

    def _enrich_cover_and_lyrics(
        self, metadata: TrackMetadata, title: str, artist: str, album: str
    ):
        """在线补全封面和歌词（受配置开关控制）。"""
        fetch_cover = self.config.get("fetch_cover", True)
        fetch_lyrics = self.config.get("fetch_lyrics", True)

        if not (fetch_cover and not metadata.cover_data) and not (
            fetch_lyrics and not metadata.lyrics_text
        ):
            return

        metadata = self.engine.enrich_cover_and_lyrics(
            metadata, title, artist or "", album or metadata.album
        )
        if not fetch_lyrics:
            metadata.lyrics_text = None
            metadata.lyrics = None
        if not fetch_cover:
            metadata.cover_data = None

    def _apply_t2s_to_metadata(self, metadata: TrackMetadata):
        """繁→简转换（如启用）：写入标签前统一元数据的文本字段。

        使 費翔 → 费翔 等同一艺人的不同写法在标签与文件名中保持一致。
        开关关闭时 ``convert_text`` 原样返回，无副作用。
        """
        if not self.encoder.t2s_enabled:
            return
        metadata.title = self.encoder.convert_text(metadata.title)
        metadata.artist = self.encoder.convert_text(metadata.artist)
        if metadata.album:
            metadata.album = self.encoder.convert_text(metadata.album)

    def _rename_file(self, file_path: str, metadata: TrackMetadata) -> str:
        """按模板重命名文件（§6.3）；失败时返回原路径。"""
        template_cfg = self.config.get("filename", {}).get("template")
        if not template_cfg:
            return file_path

        template = self.config.get("filename", {}).get("template", "{title} - {artist}")
        try:
            new_path = self.file_renamer.rename(file_path, metadata, template)
            return new_path
        except Exception as e:
            logger.warning(f"文件重命名失败 {file_path}: {e}")
            return file_path

    # ============================================================
    # 目录整理（§6 目录化整理）
    # ============================================================

    def _organize_file(
        self,
        file_path: str,
        metadata: TrackMetadata,
        original_path: Optional[str] = None,
    ) -> Optional[str]:
        """目录整理：按歌手/专辑分目录 + 文件名冲突处理。

        委托给 ``FileOrganizer.organize()`` 实现。若 ``file_organizer``
        模块尚未实现，跳过此步骤并记录日志。

        在调用 organize 之前，先推断目标路径并使用 ``ConflictResolver``
        检测文件名冲突（§10.5.2），按配置的策略决定跳过 / 覆盖 / 重命名。

        Args:
            file_path: 源音频文件路径。
            metadata: 已写入标签的元数据（含 title/artist/album/release_year）。

        Returns:
            整理后的最终文件路径；若未执行整理则返回 ``None``。
        """
        if self.organizer is None:
            logger.debug("目录整理器未初始化，跳过目录整理步骤")
            return

        organize_config = self.config.get("organize", {})
        # 未设置输出目录时跳过目录整理
        if not organize_config.get("output_dir"):
            logger.debug("未设置输出目录，跳过目录整理步骤")
            return

        # —— 冲突检测（§10.5.2）：推断目标路径，检查是否存在同名文件 ——
        try:
            target_dir = self.organizer.predict_target_path(metadata, organize_config)
            target_filename = Path(file_path).name
            target_path = Path(target_dir) / target_filename
        except Exception:
            target_path = None

        if target_path is not None and target_path.exists():
            try:
                if target_path.resolve() != Path(file_path).resolve():
                    action, resolved_path, reason = self.conflict_resolver.resolve(
                        new_file=file_path,
                        existing_file=str(target_path),
                    )
                    if action == "skip":
                        logger.info(
                            f"文件名冲突解决: 跳过 {Path(file_path).name} - {reason}"
                        )
                        return
                    elif action == "overwrite":
                        logger.info(
                            f"文件名冲突解决: 覆盖 {Path(file_path).name} - {reason}"
                        )
                    elif action == "rename_new":
                        logger.info(
                            f"文件名冲突解决: 重命名 {Path(file_path).name} - {reason}"
                        )
                    elif action == "ask":
                        logger.warning(
                            f"文件名冲突解决: 需用户确认，跳过 {Path(file_path).name}"
                        )
                        return
            except OSError:
                pass  # resolve 失败时跳过冲突检测，继续正常整理

        # FileOrganizer.organize 接口：
        #   organize(file_path, metadata, organize_config)
        #   - file_path: 源文件路径
        #   - metadata: TrackMetadata（含歌手/专辑/年份，用于确定目标目录）
        #   - organize_config: organize 配置段（output_dir / by_artist / delete_source 等）
        return self.organizer.organize(
            file_path, metadata, organize_config, original_path=original_path
        )

    # ============================================================
    # 状态管理委托（§10.4）
    # ============================================================

    def _save_state(self, file_path: str, status: str, result_or_error: str):
        """保存单个文件的处理状态（委托给 BatchStateManager）。

        Args:
            file_path: 文件完整路径。
            status: ``"done"`` 或 ``"failed"``。
            result_or_error: 成功时为结果描述，失败时为错误信息。
        """
        if status == "done":
            self.state_manager.mark_done(file_path, result_or_error)
        elif status == "failed":
            self.state_manager.mark_failed(file_path, result_or_error)

    def _load_state(self) -> dict:
        """加载完整状态字典（委托给 BatchStateManager）。

        Returns:
            完整状态字典，包含 version / source_dir / files 等字段。
        """
        return self.state_manager.get_state()

    # ============================================================
    # 搜索引擎构建（§3.2 插件式搜索架构）
    # ============================================================

    @staticmethod
    def _build_search_engine(config: dict) -> SearchEngine:
        """从配置构建搜索引擎，按配置注册各 Provider。

        各 Provider 的注册失败不影响其他 Provider，仅记录警告。
        速率控制由各 Provider 内部的 RateLimiter 自动处理（§10.7）。

        Args:
            config: 完整配置字典。

        Returns:
            已注册各 Provider 的 SearchEngine 实例。
        """
        search_config = SearchConfig.from_dict(config.get("search", {}))
        engine = SearchEngine(search_config)

        # iTunes Provider（首选：元数据 + 封面 + 试听）
        try:
            from search.itunes_provider import iTunesSearchProvider
            engine.register(iTunesSearchProvider())
        except Exception as e:
            logger.warning(f"iTunes Provider 注册失败: {e}")

        # LRCLIB Provider（歌词）
        try:
            from search.lrclib_provider import LRCLIBProvider
            engine.register(LRCLIBProvider())
        except Exception as e:
            logger.warning(f"LRCLIB Provider 注册失败: {e}")

        # MusicBrainz Provider（降级备选：元数据 + 封面）
        try:
            from search.musicbrainz_provider import MusicBrainzProvider
            engine.register(MusicBrainzProvider())
        except Exception as e:
            logger.warning(f"MusicBrainz Provider 注册失败: {e}")

        # Meting Provider（中文深度补充，需要额外配置）
        if search_config.is_enabled("meting"):
            try:
                from search.meting_provider import MetingProvider
                engine.register(MetingProvider(
                    api_url=search_config.meting_api_url,
                    server=search_config.meting_server,
                ))
            except Exception as e:
                logger.warning(f"Meting Provider 注册失败: {e}")

        return engine
