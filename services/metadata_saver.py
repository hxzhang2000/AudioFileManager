"""元数据保存器（§7.5）。

根据设置中的 ``save_mode`` 配置，决定元数据（歌词/封面/标签）的保存目标：
- TAGS：仅写入文件标签（ID3/Vorbis/MP4/ASF/APEv2）
- FILES：仅保存为同目录独立文件（.lrc / cover.jpg）
- BOTH：两者都保存（标签 + 独立文件）

所有写入都需经过此模块，确保行为一致。
"""

from __future__ import annotations

import copy
import logging
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    # 仅用于类型检查，运行时不导入，避免对尚未实现的 search 模型产生硬依赖
    from search.provider import TrackMetadata  # noqa: F401

logger = logging.getLogger(__name__)


class SaveMode(str, Enum):
    """元数据保存方式枚举。"""

    TAGS = "tags"    # 仅写入文件标签（ID3/Vorbis/MP4/ASF/APEv2）
    FILES = "files"  # 仅保存为同目录独立文件
    BOTH = "both"    # 两者都保存


class MetadataSaver:
    """元数据保存器 — 根据配置决定歌词和封面的保存位置。

    所有写入都需经过此模块，确保行为一致。
    """

    def __init__(self, save_mode: SaveMode, encoding_config: dict):
        """
        Args:
            save_mode: 保存方式（tags/files/both），支持传入枚举或字符串。
            encoding_config: 编码设置（见 §7.6），形如
                ``{"enabled": bool, "charset": "UTF-8"}``。
        """
        self.save_mode = SaveMode(save_mode)
        self.encoding = encoding_config  # 编码设置见 §7.6

    # ------------------------------------------------------------
    # 歌词保存
    # ------------------------------------------------------------
    def save_lyrics(self, file_path: str, lyrics_text: str):
        """保存歌词，根据 ``save_mode`` 选择写入方式。"""
        if self.save_mode in (SaveMode.TAGS, SaveMode.BOTH):
            # 写入文件标签（ID3 USLT / Vorbis Comment）
            self._write_lyrics_to_tags(file_path, lyrics_text)

        if self.save_mode in (SaveMode.FILES, SaveMode.BOTH):
            # 保存同目录独立 LRC 文件
            lrc_path = Path(file_path).with_suffix(".lrc")
            # 应用编码设置后再写入
            encoded_text = self._apply_encoding(lyrics_text)
            lrc_path.write_text(
                encoded_text,
                encoding=self.encoding.get("charset", "utf-8"),
            )

    # ------------------------------------------------------------
    # 封面保存
    # ------------------------------------------------------------
    def save_cover(self, file_path: str, cover_data: bytes):
        """保存封面，根据 ``save_mode`` 选择写入方式。"""
        if self.save_mode in (SaveMode.TAGS, SaveMode.BOTH):
            self._write_cover_to_tags(file_path, cover_data)

        if self.save_mode in (SaveMode.FILES, SaveMode.BOTH):
            cover_path = Path(file_path).parent / "cover.jpg"
            cover_path.write_bytes(cover_data)

    # ------------------------------------------------------------
    # 标签写入辅助
    # ------------------------------------------------------------
    def _write_lyrics_to_tags(self, file_path: str, lyrics_text: str):
        """把歌词写入文件标签（按格式路由到 tag_writer）。"""
        # 导入路径修正：使用 services.tag_writer（§7.5 原文为 from tag_writer）
        from services.tag_writer import write_lyrics
        write_lyrics(file_path, lyrics_text)

    def _write_cover_to_tags(self, file_path: str, cover_data: bytes):
        """把封面写入文件标签（按格式路由到 tag_writer）。"""
        from services.tag_writer import write_cover
        write_cover(file_path, cover_data)

    # ------------------------------------------------------------
    # 手动保存主入口
    # ------------------------------------------------------------
    def save_metadata_to_tags(
        self,
        file_path: str,
        meta: "TrackMetadata",
        cover_data: Optional[bytes] = None,
        lyrics_text: Optional[str] = None,
    ):
        """用户手动保存时：将当前输入框内容写入文件标签。

        流程：
        1. 若启用编码统一，先对 meta 的文本字段做编码转换；
        2. 复用 §7.2 的 ``write_metadata()`` 一次性写入标签（含歌词+封面）；
        3. 若 ``save_mode`` 要求同时保存独立文件（FILES/BOTH），
           仅落盘 LRC/cover.jpg，**不重复写标签**（标签已在第 2 步写入）。
        """
        from services.tag_writer import write_metadata

        # 对每个文本字段做编码转换
        if self.encoding.get("enabled", False):
            meta = self._convert_encoding(meta)

        # ① 写入标签（含歌词+封面，由 write_metadata 一次性完成）
        # FILES 模式语义为"仅保存为独立文件"，不应写入标签中的封面/歌词，
        # 故在该模式下不把 cover_data / lyrics_text 传给 write_metadata
        # （基础文本标签字段仍正常写入）。
        tag_cover = cover_data if self.save_mode != SaveMode.FILES else None
        tag_lyrics = lyrics_text if self.save_mode != SaveMode.FILES else None
        write_metadata(file_path, meta, tag_cover, tag_lyrics)

        # ② 若 save_mode 要求同时保存独立文件，仅落盘文件（不重复写标签）
        if self.save_mode in (SaveMode.FILES, SaveMode.BOTH):
            if lyrics_text:
                lrc_path = Path(file_path).with_suffix(".lrc")
                encoded_text = self._apply_encoding(lyrics_text)
                self._write_with_retry(
                    lambda: lrc_path.write_text(
                        encoded_text,
                        encoding=self.encoding.get("charset", "utf-8"),
                    )
                )
            if cover_data:
                cover_path = Path(file_path).parent / "cover.jpg"
                self._write_with_retry(
                    lambda: cover_path.write_bytes(cover_data)
                )

    # ------------------------------------------------------------
    # 编码转换辅助
    # ------------------------------------------------------------
    @staticmethod
    def _write_with_retry(write_fn, max_attempts: int = 3, delay: float = 0.5):
        """带重试的文件写入封装，处理 PermissionError 等文件锁定情况。

        Args:
            write_fn: 无参可调用对象，执行实际写入操作。
            max_attempts: 最大重试次数。
            delay: 重试间隔（秒）。
        """
        import time
        last_exc = None
        for attempt in range(max_attempts):
            try:
                write_fn()
                return
            except PermissionError as e:
                last_exc = e
                if attempt < max_attempts - 1:
                    logger.warning(
                        f"文件写入被锁定 (第 {attempt+1}/{max_attempts} 次): {e}"
                    )
                    time.sleep(delay)
        raise last_exc

    def _apply_encoding(self, text: str) -> str:
        """尝试修复被 mutagen 误读为 Latin-1 的源编码文本。

        部分历史音频文件的标签使用 GBK 等非 UTF-8 编码，mutagen 在读取时
        可能按 Latin-1 逐字节解码，导致 Python 字符串呈现乱码。本方法通过
        ``latin-1`` 还原原始字节，再用用户指定的 ``charset`` 解码，恢复
        正确文本。

        当编码统一未启用、目标编码为 UTF-8、或无法还原时，直接返回原文本。
        """
        if not self.encoding.get("enabled", False):
            return text
        if not text:
            return text
        charset = self.encoding.get("charset", "UTF-8")
        if charset.lower() in ("utf-8", "utf8"):
            return text
        try:
            return text.encode("latin-1").decode(charset, errors="replace")
        except (UnicodeEncodeError, UnicodeDecodeError):
            return text

    def _convert_encoding(self, meta: "TrackMetadata") -> "TrackMetadata":
        """对 TrackMetadata 的所有文本字段做编码转换。

        返回 meta 的浅拷贝，不修改原对象。
        """
        m = copy.copy(meta)
        for field in ["title", "artist", "album", "genre"]:
            val = getattr(m, field, None)
            if val:
                setattr(m, field, self._apply_encoding(val))
        return m
