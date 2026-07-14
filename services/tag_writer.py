"""多格式音频标签写入/读取服务（§7.2）— 路由层。

格式专用的写入/读取实现位于 :mod:`services.tag_writers` 子包下：

- :mod:`services.tag_writers.mp3` — ID3v2.4
- :mod:`services.tag_writers.flac` — Vorbis Comment
- :mod:`services.tag_writers.m4a` — iTunes MP4
- :mod:`services.tag_writers.ogg` — Vorbis Comment（OGG 容器）
- :mod:`services.tag_writers.wma` — ASF
- :mod:`services.tag_writers.ape` — APEv2

本模块只负责四件事：
1. 提供 ``write_metadata`` / ``write_cover`` / ``write_lyrics`` /
   ``read_metadata`` / ``read_cover`` / ``read_lyrics`` 六个公共 API。
2. 按文件扩展名路由到对应格式模块。
3. 对写入操作施加 ``@retry_on_locked`` 重试保护。
4. 为读取操作添加统一的异常容错（失败返回 None/空字典）。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from utils.retry_on_locked import retry_on_locked
from services.tag_writers import mp3, flac, m4a, ogg, wma, ape

if TYPE_CHECKING:
    from search.provider import TrackMetadata  # noqa: F401

logger = logging.getLogger(__name__)


# ============================================================
# 路由表
# ============================================================
_WRITE_DISPATCH = {
    "mp3": mp3.write, "flac": flac.write, "m4a": m4a.write,
    "ogg": ogg.write, "wma": wma.write, "ape": ape.write,
}
_COVER_DISPATCH = {
    "mp3": mp3.write_cover, "flac": flac.write_cover, "m4a": m4a.write_cover,
    "ogg": ogg.write_cover, "ape": ape.write_cover,  # 不含 wma（§7.1 封面 ❌）
}
_LYRICS_DISPATCH = {
    "mp3": mp3.write_lyrics, "flac": flac.write_lyrics, "m4a": m4a.write_lyrics,
    "ogg": ogg.write_lyrics, "wma": wma.write_lyrics, "ape": ape.write_lyrics,
}
_READ_DISPATCH = {
    "mp3": mp3.read_metadata, "flac": flac.read_metadata, "m4a": m4a.read_metadata,
    "ogg": ogg.read_metadata, "wma": wma.read_metadata, "ape": ape.read_metadata,
}
_READ_COVER_DISPATCH = {
    "mp3": mp3.read_cover, "flac": flac.read_cover, "m4a": m4a.read_cover,
    "ogg": ogg.read_cover, "ape": ape.read_cover,  # 不含 wma
}
_READ_LYRICS_DISPATCH = {
    "mp3": mp3.read_lyrics, "flac": flac.read_lyrics, "m4a": m4a.read_lyrics,
    "ogg": ogg.read_lyrics, "wma": wma.read_lyrics, "ape": ape.read_lyrics,
}


# ============================================================
# 主入口（写入）
# ============================================================
@retry_on_locked(max_attempts=3, delay=0.5)
def write_metadata(file_path: str, meta: "TrackMetadata",
                   cover_data: Optional[bytes] = None,
                   lyrics_text: Optional[str] = None):
    """按文件格式写入标签（MP3/FLAC/M4A/OGG/WMA/APE）。

    字段映射：标题/歌手/专辑/年份/流派/曲目号/封面/歌词。
    各格式标签容器不同（ID3 / Vorbis Comment / MP4 / ASF / APEv2），
    由 ``_WRITE_DISPATCH`` 依据扩展名路由到对应格式模块。

    未知扩展名（如 .wav）不在路由表中，记录警告并跳过，
    避免回退到 MP3 写入导致 mutagen 抛异常。
    """
    suffix = Path(file_path).suffix.lower().lstrip(".")
    handler = _WRITE_DISPATCH.get(suffix)
    if handler is None:
        logger.warning(f"标签写入不支持该格式，跳过: {file_path}")
        return
    handler(file_path, meta, cover_data, lyrics_text)


@retry_on_locked(max_attempts=3, delay=0.5)
def write_cover(file_path: str, cover_data: bytes):
    """仅写入封面（按格式路由）。

    WMA 在 §7.1 中封面写入标记为不支持，此处跳过（无对应路由项）。
    未知扩展名同样记录警告并跳过。
    """
    suffix = Path(file_path).suffix.lower().lstrip(".")
    handler = _COVER_DISPATCH.get(suffix)
    if handler is None:
        logger.warning(f"封面写入不支持该格式，跳过: {file_path}")
        return
    handler(file_path, cover_data)


@retry_on_locked(max_attempts=3, delay=0.5)
def write_lyrics(file_path: str, lyrics_text: str):
    """仅写入歌词（按格式路由）。

    未知扩展名不在路由表中，记录警告并跳过，
    避免回退到 MP3 写入导致 mutagen 抛异常。
    """
    suffix = Path(file_path).suffix.lower().lstrip(".")
    handler = _LYRICS_DISPATCH.get(suffix)
    if handler is None:
        logger.warning(f"歌词写入不支持该格式，跳过: {file_path}")
        return
    handler(file_path, lyrics_text)


# ============================================================
# 主入口（读取）
# ============================================================
def read_metadata(file_path: str) -> dict[str, Any]:
    """读取文件标签，返回字段字典。

    返回结构（缺失字段为空串/None）::

        {
            "title": str,
            "artist": str,
            "album": str,
            "release_year": Optional[int],
            "genre": Optional[str],
            "track_number": Optional[int],
        }

    无法识别的扩展名返回空字典。
    """
    suffix = Path(file_path).suffix.lower().lstrip(".")
    handler = _READ_DISPATCH.get(suffix)
    if handler is None:
        return {}
    try:
        return handler(file_path)
    except Exception as e:
        logger.warning(f"读取标签失败: {file_path}: {e}")
        return {}


def read_cover(file_path: str) -> Optional[bytes]:
    """读取封面二进制数据，无封面或不支持则返回 ``None``。"""
    suffix = Path(file_path).suffix.lower().lstrip(".")
    handler = _READ_COVER_DISPATCH.get(suffix)
    if handler is None:
        return None
    try:
        return handler(file_path)
    except Exception as e:
        logger.warning(f"读取封面失败: {file_path}: {e}")
        return None


def read_lyrics(file_path: str) -> Optional[str]:
    """读取歌词文本，无歌词则返回 ``None``。"""
    suffix = Path(file_path).suffix.lower().lstrip(".")
    handler = _READ_LYRICS_DISPATCH.get(suffix)
    if handler is None:
        return None
    try:
        return handler(file_path)
    except Exception as e:
        logger.warning(f"读取歌词失败: {file_path}: {e}")
        return None
