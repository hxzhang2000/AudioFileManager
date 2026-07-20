"""递归文件扫描器（§10.1.1）。

负责扫描用户选择的源文件夹，递归收集所有支持的音频文件，
并预读已有 ID3/标签信息，供后续文件名解析与搜索补全使用。

扫描流程（§10.1）：
1. 使用 ``os.walk()`` 递归遍历所有子文件夹
2. 过滤支持的音频扩展名 (.mp3/.flac/.m4a/.ogg/.wma/.ape)
3. 跳过系统隐藏文件夹（如 $RECYCLE.BIN）
4. 统计子文件夹数 + 音频文件总数
5. 读取现有 ID3 标签，填充 title/artist（优先于文件名解析）
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# 支持的音频格式
AUDIO_EXTENSIONS = {".mp3", ".flac", ".m4a", ".ogg", ".wma", ".ape"}
# MV 视频文件扩展名（与 batch_processor.DEFAULT_VIDEO_EXTENSIONS 保持一致）
DEFAULT_VIDEO_EXTENSIONS = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".webm", ".m4v"}
# 所有支持的扩展名（音频 + 视频）
SUPPORTED_EXTENSIONS = AUDIO_EXTENSIONS | DEFAULT_VIDEO_EXTENSIONS

# 需要跳过的系统隐藏文件夹
SKIP_DIRS = {"$RECYCLE.BIN", "System Volume Information", "lost+found"}


@dataclass
class AudioFileEntry:
    """单个音频文件的扫描结果条目。"""

    file_path: str               # 文件完整路径
    relative_path: str           # 相对于源目录的路径（用于分组显示）
    dir_name: str                # 所在子文件夹名（如 "魔杰座"）
    size: int                    # 文件大小（字节）
    ext: str                     # 扩展名
    title: str = ""              # 解析出的歌曲名
    artist: str = ""             # 解析出的歌手
    status: str = "pending"      # pending | parsed | searching | done | failed
    name_normalized: str = ""    # 归一化后的「歌曲名-歌手」（去重用）
    file_hash: Optional[str] = None  # SHA-256（搜索阶段前计算，L3 去重用）
    is_duplicate: bool = False   # 是否被判定为重复文件
    duplicate_of: Optional[str] = None  # 指向重复源文件的路径


class _DefaultMetadataReader:
    """默认标签读取器：委托给 services.tag_writer.read_metadata。

    提供与 §10.1.1 中 ``metadata_reader.read(path)`` 一致的接口。
    """

    def read(self, file_path: str) -> Optional[dict]:
        from services.tag_writer import read_metadata
        try:
            return read_metadata(file_path)
        except Exception as e:
            logger.warning(f"默认标签读取失败: {file_path}: {e}")
            return None


class FileScanner:
    """递归文件扫描器。"""

    def __init__(self, base_dir: str):
        self.base_dir = Path(base_dir)

    def scan(self) -> list[AudioFileEntry]:
        """递归扫描 ``base_dir`` 下的所有子文件夹，收集音频文件。

        返回按目录排序的文件列表（同一子文件夹的文件连续排列）。
        跳过 ``SKIP_DIRS`` 中的系统隐藏文件夹及以 ``.`` 开头的隐藏目录。
        """
        entries: list[AudioFileEntry] = []
        for root, dirs, files in os.walk(self.base_dir, topdown=True):
            # 跳过系统隐藏文件夹（topdown=True 时修改 dirs[:] 影响后续递归）
            dirs[:] = [
                d for d in dirs
                if d not in SKIP_DIRS and not d.startswith('.')
            ]

            for f in sorted(files):
                ext = Path(f).suffix.lower()
                if ext not in AUDIO_EXTENSIONS:
                    continue

                full_path = Path(root) / f
                rel_path = full_path.relative_to(self.base_dir)

                # 文件大小（被锁定/无权限时降级为 0）
                try:
                    size = full_path.stat().st_size
                except OSError:
                    size = 0

                entry = AudioFileEntry(
                    file_path=str(full_path),
                    relative_path=str(rel_path),
                    dir_name=Path(root).name,
                    size=size,
                    ext=ext,
                )
                entries.append(entry)

        return entries

    def pre_read_id3(
        self,
        entries: list[AudioFileEntry],
        metadata_reader=None,
    ) -> list[AudioFileEntry]:
        """扫描后对每个文件尝试读取已有 ID3 标签，优先填充 title/artist。

        - 有 ID3 标签（且含 title）→ 直接填充，状态置为 ``parsed``
        - 无 ID3 标签 → 留空，状态保持 ``pending``，交给 FileNameParser 解析
        - 读取异常（如文件锁定）→ 状态置为 ``failed``

        调用时机：扫描完成后、FileNameParser 解析前。

        Args:
            entries: ``scan()`` 返回的条目列表。
            metadata_reader: 提供 ``read(file_path) -> dict`` 接口的标签读取器；
                为 ``None`` 时使用默认读取器（委托 ``services.tag_writer``）。
        """
        if metadata_reader is None:
            metadata_reader = _DefaultMetadataReader()

        for entry in entries:
            try:
                id3_meta = metadata_reader.read(entry.file_path)
                if id3_meta and id3_meta.get("title"):
                    entry.title = id3_meta["title"]
                    entry.artist = id3_meta.get("artist", "") or ""
                    entry.status = "parsed"
                else:
                    # 无可用标签，交给 FileNameParser 解析
                    entry.status = "pending"
            except OSError as e:
                entry.status = "failed"
                logger.warning(f"无法读取 ID3: {entry.file_path}: {e}")
            except Exception as e:
                # 其他异常不阻断扫描，仅标记失败
                entry.status = "failed"
                logger.warning(f"读取标签异常: {entry.file_path}: {e}")
        return entries

    @staticmethod
    def get_scan_summary(base_dir: str, entries: list[AudioFileEntry]) -> dict:
        """返回扫描统计信息（用于工具栏显示）。

        含：总文件数、子文件夹数、总大小（MB）、源目录。
        """
        subdirs: set[str] = set()
        for entry in entries:
            # 获取相对于 base_dir 的父目录
            rel = Path(entry.relative_path).parent
            if str(rel) != ".":
                subdirs.add(str(rel))

        return {
            "total_files": len(entries),
            "subfolder_count": len(subdirs),
            "total_size_mb": sum(e.size for e in entries) / (1024 * 1024),
            "base_dir": base_dir,
        }
