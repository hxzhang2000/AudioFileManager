"""APE（APEv2）格式标签写入/读取（§7.2）。"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


def write(file_path, meta, cover_data, lyrics_text):
    from mutagen.apev2 import APEv2, APEBinaryValue, APENoHeaderError
    try:
        audio = APEv2(file_path)
    except APENoHeaderError:
        audio = APEv2()
    audio["Title"], audio["Artist"], audio["Album"] = meta.title, meta.artist, meta.album
    if meta.release_year:
        audio["Year"] = str(meta.release_year)
    if meta.genre:
        audio["Genre"] = meta.genre
    if meta.track_number:
        audio["Track"] = str(meta.track_number)
    if lyrics_text:
        audio["Lyrics"] = lyrics_text
    if cover_data:
        audio["Cover Art (Front)"] = APEBinaryValue(b"cover.jpg\x00" + cover_data)
    audio.save(file_path)


def write_cover(file_path, cover_data):
    from mutagen.apev2 import APEv2, APEBinaryValue, APENoHeaderError
    try:
        audio = APEv2(file_path)
    except APENoHeaderError:
        audio = APEv2()
    audio["Cover Art (Front)"] = APEBinaryValue(b"cover.jpg\x00" + cover_data)
    audio.save(file_path)


def write_lyrics(file_path, lyrics_text):
    from mutagen.apev2 import APEv2, APENoHeaderError
    try:
        audio = APEv2(file_path)
    except APENoHeaderError:
        audio = APEv2()
    audio["Lyrics"] = lyrics_text
    audio.save(file_path)


def read_metadata(file_path) -> dict[str, Any]:
    """读取 APE APEv2 标签。"""
    from mutagen.apev2 import APEv2, APENoHeaderError
    try:
        audio = APEv2(file_path)
    except APENoHeaderError:
        return {}

    def _get(key):
        v = audio.get(key)
        if v is None:
            return None
        return str(v)

    year_raw = _get("Year")
    track_raw = _get("Track")
    track_num = None
    if track_raw:
        try:
            track_num = int(str(track_raw).split("/")[0])
        except (ValueError, TypeError):
            track_num = None
    year_str = str(year_raw).strip()[:4] if year_raw else ""
    return {
        "title": _get("Title") or "",
        "artist": _get("Artist") or "",
        "album": _get("Album") or "",
        "release_year": int(year_str) if year_str.isdigit() else None,
        "genre": _get("Genre") or None,
        "track_number": track_num,
    }


def read_cover(file_path) -> Optional[bytes]:
    """读取 APE Cover Art (Front) 封面。

    APE 的 ``Cover Art (Front)`` 字段格式为 ``文件名\\x00<二进制数据>``，
    需要跳过开头到第一个 null 字节的部分。
    """
    from mutagen.apev2 import APEv2, APENoHeaderError
    try:
        audio = APEv2(file_path)
    except APENoHeaderError:
        return None
    val = audio.get("Cover Art (Front)")
    if val is None:
        return None
    data = bytes(val)
    sep = data.find(b"\x00")
    if sep >= 0:
        return data[sep + 1:]
    return data


def read_lyrics(file_path) -> Optional[str]:
    """读取 APE Lyrics 字段。"""
    from mutagen.apev2 import APEv2, APENoHeaderError
    try:
        audio = APEv2(file_path)
    except APENoHeaderError:
        return None
    val = audio.get("Lyrics")
    return str(val) if val is not None else None
