"""WMA（ASF）格式标签写入/读取（§7.2）。

封面写入在 §7.1 中标记为不支持（❌），故 ``tag_writer`` 的路由表不含 WMA
封面写入项。封面读取同样不支持（``_READ_COVER_DISPATCH`` 不含 ``wma``）。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


def write(file_path, meta, cover_data, lyrics_text):
    from mutagen.asf import ASF
    audio = ASF(file_path)
    # ASF.load 内部始终执行 self.tags = ASFTags()，故 audio.tags 永不为 None
    audio["Title"], audio["Author"], audio["WM/AlbumTitle"] = meta.title, meta.artist, meta.album
    if meta.release_year:
        audio["WM/Year"] = str(meta.release_year)
    if meta.genre:
        audio["WM/Genre"] = meta.genre
    if meta.track_number:
        audio["WM/TrackNumber"] = str(meta.track_number)
    if lyrics_text:
        audio["WM/Lyrics"] = lyrics_text
    audio.save()


def write_lyrics(file_path, lyrics_text):
    from mutagen.asf import ASF
    audio = ASF(file_path)
    audio["WM/Lyrics"] = lyrics_text
    audio.save()


def read_metadata(file_path) -> dict[str, Any]:
    """读取 WMA ASF 标签。"""
    from mutagen.asf import ASF
    audio = ASF(file_path)

    def _get(key):
        v = audio.get(key)
        if not v:
            return None
        return v[0] if isinstance(v, (list, tuple)) else v

    year_raw = _get("WM/Year")
    track_raw = _get("WM/TrackNumber")
    track_num = None
    if track_raw:
        try:
            track_num = int(str(track_raw).split("/")[0])
        except (ValueError, TypeError):
            track_num = None
    year_str = str(year_raw).strip()[:4] if year_raw else ""
    return {
        "title": str(_get("Title") or ""),
        "artist": str(_get("Author") or ""),
        "album": str(_get("WM/AlbumTitle") or ""),
        "release_year": int(year_str) if year_str.isdigit() else None,
        "genre": str(_get("WM/Genre") or "") or None,
        "track_number": track_num,
    }


def read_lyrics(file_path) -> Optional[str]:
    """读取 WMA WM/Lyrics 字段。"""
    from mutagen.asf import ASF
    audio = ASF(file_path)
    v = audio.get("WM/Lyrics")
    if not v:
        return None
    return v[0] if isinstance(v, (list, tuple)) else str(v)
