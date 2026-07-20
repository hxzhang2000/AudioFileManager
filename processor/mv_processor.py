"""MV 视频文件整理处理器。

扫描源目录中的 MV 视频文件（MP4 / MKV 等），按文件名解析歌手和歌曲名，
根据用户配置的模板进行重命名，并按歌手目录结构复制或移动到目标目录。

流程（与音频批处理相比跳过编码/标签写入，但复用元数据搜索获取专辑信息）：
1. 递归扫描源目录 → 匹配视频扩展名
2. 文件名解析 → 提取 (title, artist)，复用 FileNameParser
3. 元数据搜索 → 复用 SearchEngine，获取 album / year（可选，受 search_metadata 控制）
4. 重命名 → 按配置模板（如 ``{title} - {artist}``）
5. 整理 → 复用 FileOrganizer，与音乐文件进入同一 Artist/Album 目录

使用方式::

    processor = MvProcessor(config)
    processor.run()
    # 或通过 QThread 异步运行:
    thread = MvProcessor(config)
    thread.files_scanned.connect(on_scanned)
    thread.file_finished.connect(on_finished)
    thread.batch_finished.connect(on_done)
    thread.start()
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QThread, pyqtSignal

from parser.filename_parser import FileNameParser
from parser.artist_db import ArtistDB
from processor.file_renamer import FileRenamer
from processor.file_organizer import FileOrganizer, UNKNOWN_ARTIST_DIR
from search.provider import SearchEngine, TrackMetadata
from search.config import SearchConfig
from utils.helpers import ensure_dir

logger = logging.getLogger(__name__)

# 默认支持的 MV 视频扩展名
DEFAULT_VIDEO_EXTENSIONS = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".webm", ".m4v"}

# 需要跳过的系统隐藏文件夹（与 file_scanner 一致）
SKIP_DIRS = {"$RECYCLE.BIN", "System Volume Information", "lost+found"}


@dataclass
class MvFileEntry:
    """单个 MV 视频文件的扫描结果条目。"""

    file_path: str               # 源文件完整路径
    relative_path: str           # 相对于源目录的路径
    dir_name: str                # 所在子文件夹名
    size: int                    # 文件大小（字节）
    ext: str                     # 扩展名
    title: str = ""              # 解析出的歌曲名
    artist: str = ""             # 解析出的歌手
    album: str = ""              # 专辑名（元数据搜索后填充）
    year: Optional[int] = None   # 发行年份（元数据搜索后填充）
    status: str = "pending"      # pending | parsed | searched | renamed | organized | done | failed
    new_path: str = ""           # 重命名后的路径（与 file_path 不同时表示已改名）
    target_path: str = ""        # 整理后的最终路径


class MvProcessor(QThread):
    """MV 视频文件整理处理器 — 在 QThread 中异步执行。

    信号：
    - ``files_scanned(int)``: 扫描完成，参数为文件总数
    - ``progress_updated(int, int, str)``: (当前索引, 总数, 步骤名称)
    - ``file_finished(str, str)``: (文件路径, 状态 "done"/"failed"/"skipped")
    - ``batch_finished(int, int)``: (成功数, 失败数)
    - ``log_message(str)``: 日志消息（供 UI 显示）
    """

    files_scanned = pyqtSignal(int)
    progress_updated = pyqtSignal(int, int, str)
    file_finished = pyqtSignal(str, str)
    batch_finished = pyqtSignal(int, int)
    log_message = pyqtSignal(str)

    def __init__(self, config: dict, source_dir: str = "", parent=None):
        """初始化 MV 整理处理器。

        Args:
            config: 完整配置字典。
            source_dir: 要扫描的源目录路径（为空时回退到 config.mv.source_dir）。
        """
        super().__init__(parent)
        self.config = config
        mv_cfg = config.get("mv", {})

        # 视频扩展名集合（MV 独有配置）
        exts = mv_cfg.get("video_extensions", [])
        self.video_extensions = {ext.lower() if ext.startswith(".") else f".{ext.lower()}"
                                 for ext in exts} if exts else DEFAULT_VIDEO_EXTENSIONS

        # 元数据搜索开关
        self.search_metadata_enabled = mv_cfg.get("search_metadata", True)

        # 搜索引擎（延迟初始化）
        self._search_engine: Optional[SearchEngine] = None

        # 源目录（外部传入，为空时也无需降级）
        self.source_dir = source_dir or mv_cfg.get("source_dir", "")

        # 整理设置 — 复用「整理」页面配置
        org_cfg = config.get("organize", {})
        self.output_dir = org_cfg.get("output_dir", "")
        self.by_artist = org_cfg.get("by_artist", True)
        self.by_album = org_cfg.get("by_album", True)
        self.unknown_dir = org_cfg.get("unknown_dir", True)
        self.delete_source = org_cfg.get("delete_source", False)

        # 文件名设置 — 复用「文件名」页面配置
        fn_cfg = config.get("filename", {})
        self.file_renamer = FileRenamer({"filename": fn_cfg})

        # 目录整理器（与音乐整理使用相同的分类配置）
        organize_cfg = {
            "by_artist": self.by_artist,
            "by_album": self.by_album,
            "unknown_dir": self.unknown_dir,
            "delete_source": self.delete_source,
            "output_dir": self.output_dir,
        }
        self.organizer = FileOrganizer(organize_cfg)

        # 文件名解析器
        self.artist_db = ArtistDB()
        self.parser = FileNameParser(self.artist_db)

        # 结果统计
        self._entries: list[MvFileEntry] = []
        self._success_count = 0
        self._failed_count = 0

    # ============================================================
    # 停止控制
    # ============================================================

    def stop(self):
        """请求停止处理（当前文件完成后退出）。"""
        self._stop_requested = True

    # ============================================================
    # 搜索引擎构建（§3.2 插件式搜索架构，与 BatchProcessor 一致）
    # ============================================================

    @staticmethod
    def _build_search_engine(config: dict) -> SearchEngine:
        """从配置构建搜索引擎，按配置注册各 Provider。

        Args:
            config: 完整配置字典。

        Returns:
            已注册各 Provider 的 SearchEngine 实例。
        """
        search_config = SearchConfig.from_dict(config.get("search", {}))
        engine = SearchEngine(search_config)

        try:
            from search.itunes_provider import iTunesSearchProvider
            engine.register(iTunesSearchProvider())
        except Exception as e:
            logger.warning(f"iTunes Provider 注册失败: {e}")

        try:
            from search.lrclib_provider import LRCLIBProvider
            engine.register(LRCLIBProvider())
        except Exception as e:
            logger.warning(f"LRCLIB Provider 注册失败: {e}")

        try:
            from search.musicbrainz_provider import MusicBrainzProvider
            engine.register(MusicBrainzProvider())
        except Exception as e:
            logger.warning(f"MusicBrainz Provider 注册失败: {e}")

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

    def _get_search_engine(self) -> SearchEngine:
        """获取搜索引擎（延迟初始化 + 缓存）。"""
        if self._search_engine is None:
            self._search_engine = self._build_search_engine(self.config)
        return self._search_engine

    # ============================================================
    # 元数据搜索
    # ============================================================

    def _search_metadata(self, title: str, artist: str) -> Optional[TrackMetadata]:
        """搜索元数据，获取 album / year 等专辑信息。

        Args:
            title: 歌曲名。
            artist: 歌手。

        Returns:
            搜索到的 TrackMetadata，失败返回 ``None``。
        """
        try:
            engine = self._get_search_engine()
            return engine.search_metadata(title, artist)
        except Exception as e:
            logger.warning(f"MV 元数据搜索失败 {title} - {artist}: {e}")
            return None

    # ============================================================
    # 扫描
    # ============================================================

    def scan_source(self) -> list[MvFileEntry]:
        """递归扫描源目录，收集所有 MV 视频文件。

        Returns:
            符合条件的文件条目列表。
        """
        entries: list[MvFileEntry] = []
        src = Path(self.source_dir)

        if not src.exists():
            self.log_message.emit(f"源目录不存在: {self.source_dir}")
            return entries

        for root, dirs, files in os.walk(src, topdown=True):
            # 跳过系统隐藏文件夹
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith('.')]

            for f in sorted(files):
                ext = Path(f).suffix.lower()
                if ext not in self.video_extensions:
                    continue

                full_path = Path(root) / f
                try:
                    size = full_path.stat().st_size
                except OSError:
                    size = 0

                rel_path = full_path.relative_to(src)
                entry = MvFileEntry(
                    file_path=str(full_path),
                    relative_path=str(rel_path),
                    dir_name=Path(root).name,
                    size=size,
                    ext=ext,
                )
                entries.append(entry)

        return entries

    # ============================================================
    # 文件名解析
    # ============================================================

    def _parse_filename(self, entry: MvFileEntry) -> tuple[str, str]:
        """解析文件名，提取歌曲名和歌手。

        Returns:
            ``(title, artist)`` 元组。
        """
        filename = Path(entry.file_path).name
        title, artist, _, _ = self.parser.parse(filename)
        return title or Path(entry.file_path).stem, artist or ""

    # ============================================================
    # 构建简化的元数据对象
    # ============================================================

    def _build_metadata(self, entry: MvFileEntry) -> dict:
        """构造供 FileRenamer / FileOrganizer 使用的元数据 dict。

        Args:
            entry: MV 文件条目（需已设置 title / artist / album / year）。

        Returns:
            包含 title / artist / album / release_year 的 dict。
        """
        meta = {
            "title": entry.title,
            "artist": entry.artist or UNKNOWN_ARTIST_DIR,
        }
        if entry.album:
            meta["album"] = entry.album
        if entry.year:
            meta["release_year"] = entry.year
        return meta

    # ============================================================
    # 重命名
    # ============================================================

    def _rename_file(self, entry: MvFileEntry) -> str:
        """按配置模板重命名 MV 文件。

        Args:
            entry: MV 文件条目（需已设置 title / artist）。
            metadata: 元数据字典（含 album / year 等）。

        Returns:
            重命名后的文件完整路径；若未命名则返回原路径。
        """
        metadata = self._build_metadata(entry)
        try:
            return self.file_renamer.rename(entry.file_path, metadata)
        except Exception as e:
            logger.warning(f"MV 重命名失败 {entry.file_path}: {e}")
            return entry.file_path

    # ============================================================
    # 整理（复制/移动到歌手目录）
    # ============================================================

    def _organize_file(self, entry: MvFileEntry) -> Optional[str]:
        """将 MV 文件整理到目标目录（按歌手/专辑分类）。

        Args:
            entry: MV 文件条目（需已设置 title / artist / album）。

        Returns:
            整理后的最终路径；失败返回 ``None``。
        """
        metadata = self._build_metadata(entry)
        try:
            current_path = entry.new_path or entry.file_path
            # 使用 FileOrganizer.predict_target_path 确定目标目录
            organize_config = {
                "by_artist": self.by_artist,
                "by_album": self.by_album and bool(entry.album),
                "output_dir": self.output_dir,
            }
            target_dir = self.organizer.predict_target_path(metadata, organize_config)
            ensure_dir(target_dir)

            target = self.organizer.copy_or_move(
                current_path, target_dir,
                delete_source=self.delete_source,
            )
            return target
        except Exception as e:
            logger.warning(f"MV 整理失败 {entry.file_path}: {e}")
            return None

    # ============================================================
    # 主流程
    # ============================================================

    def run(self):
        """整理主流程：扫描 → 解析 → 重命名 → 整理。"""
        self._stop_requested = False

        # 1. 检查配置
        if not self.source_dir:
            self.log_message.emit("错误：未设置 MV 源目录，请在设置中配置。")
            self.batch_finished.emit(0, 0)
            return

        if not self.output_dir:
            self.log_message.emit("错误：未设置 MV 输出目录，请在设置中配置。")
            self.batch_finished.emit(0, 0)
            return

        # 2. 扫描
        self.log_message.emit(f"正在扫描源目录: {self.source_dir}")
        self._entries = self.scan_source()
        total = len(self._entries)

        if total == 0:
            self.log_message.emit(f"未在 {self.source_dir} 中发现视频文件")
            self.batch_finished.emit(0, 0)
            return

        self.log_message.emit(f"扫描完成，共发现 {total} 个 MV 视频文件")
        self.files_scanned.emit(total)

        # 3. 逐文件处理
        self._success_count = 0
        self._failed_count = 0

        for i, entry in enumerate(self._entries):
            if getattr(self, "_stop_requested", False):
                self.log_message.emit("用户手动停止 MV 整理")
                break

            file_name = os.path.basename(entry.file_path)

            try:
                # 步骤 1：文件名解析
                self.progress_updated.emit(i, total, "解析")
                entry.title, entry.artist = self._parse_filename(entry)
                entry.status = "parsed"
                self.log_message.emit(f"  {file_name} → 歌曲: {entry.title}, 歌手: {entry.artist or '(未知)'}")

                if getattr(self, "_stop_requested", False):
                    break

                # 步骤 2：元数据搜索（在线，获取 album / year）
                if self.search_metadata_enabled:
                    self.progress_updated.emit(i, total, "搜索")
                    result = self._search_metadata(entry.title, entry.artist)
                    if result:
                        entry.album = result.album or ""
                        entry.year = result.release_year
                        if result.title:
                            entry.title = result.title
                        if result.artist and result.artist != "未知艺人":
                            entry.artist = result.artist
                        entry.status = "searched"
                        self.log_message.emit(
                            f"  → 搜索: {entry.title} - {entry.artist}"
                            f"{' | ' + entry.album if entry.album else ''}"
                        )
                    else:
                        self.log_message.emit(f"  → 搜索无结果，使用文件名解析信息")

                if getattr(self, "_stop_requested", False):
                    break

                # 步骤 3：重命名
                self.progress_updated.emit(i, total, "重命名")
                fn_cfg = self.config.get("filename", {})
                if fn_cfg.get("enabled", True):
                    new_path = self._rename_file(entry)
                    if new_path != entry.file_path:
                        entry.new_path = new_path
                        entry.status = "renamed"
                        self.log_message.emit(f"  → 重命名: {os.path.basename(new_path)}")

                if getattr(self, "_stop_requested", False):
                    break

                # 步骤 4：整理（复制/移动）
                self.progress_updated.emit(i, total, "整理")
                target = self._organize_file(entry)
                if target:
                    entry.target_path = target
                    entry.status = "organized"
                    self.log_message.emit(
                        f"  → 整理完成: {target}"
                    )
                else:
                    # 整理失败但文件可能已改名，标记为 done 而非 failed
                    entry.status = "done" if entry.new_path else "failed"

                entry.status = "done"
                self._success_count += 1
                self.file_finished.emit(entry.file_path, "done")

            except Exception as e:
                logger.error(f"MV 处理失败 {file_name}: {e}", exc_info=True)
                entry.status = "failed"
                self._failed_count += 1
                self.file_finished.emit(entry.file_path, "failed")
                self.log_message.emit(f"  ✗ 失败: {file_name} - {e}")

        # 4. 完成
        self.log_message.emit(
            f"MV 整理完成：成功 {self._success_count}，失败 {self._failed_count}"
        )
        self.batch_finished.emit(self._success_count, self._failed_count)
