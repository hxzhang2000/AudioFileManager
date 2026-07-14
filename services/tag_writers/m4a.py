"""M4A（iTunes MP4）格式标签写入/读取（§7.2）。"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


def write(file_path, meta, cover_data, lyrics_text):
    from mutagen.mp4 import MP4, MP4Cover
    audio = MP4(file_path)
    if audio.tags is None:
        audio.add_tags()
    audio["\xa9nam"], audio["\xa9ART"], audio["\xa9alb"] = meta.title, meta.artist, meta.album
    if meta.release_year:
        audio["\xa9day"] = str(meta.release_year)
    if meta.genre:
        audio["\xa9gen"] = meta.genre
    if meta.track_number:
        try:
            audio["trkn"] = [(int(meta.track_number), 0)]
        except ValueError:
            pass
    if lyrics_text:
        audio["\xa9lyr"] = lyrics_text
    if cover_data:
        audio["covr"] = [MP4Cover(cover_data, imageformat=MP4Cover.FORMAT_JPEG)]
    audio.save()
    del audio


def write_cover(file_path, cover_data):
    from mutagen.mp4 import MP4, MP4Cover
    audio = MP4(file_path)
    if audio.tags is None:
        audio.add_tags()
    audio["covr"] = [MP4Cover(cover_data, imageformat=MP4Cover.FORMAT_JPEG)]
    audio.save()
    del audio


def write_lyrics(file_path, lyrics_text):
    from mutagen.mp4 import MP4
    audio = MP4(file_path)
    if audio.tags is None:
        audio.add_tags()
    audio["\xa9lyr"] = lyrics_text
    audio.save()
    del audio


def read_metadata(file_path) -> dict[str, Any]:
    """读取 M4A iTunes MP4 标签。"""
    from mutagen.mp4 import MP4
    audio = MP4(file_path)

    def _get(key):
        v = audio.get(key)
        if not v:
            return None
        return v[0] if isinstance(v, (list, tuple)) else v

    year_raw = _get("\xa9day")
    trkn = audio.get("trkn")
    track_num = None
    if trkn:
        try:
            track_num = int(trkn[0][0]) if trkn[0] else None
        except (IndexError, ValueError, TypeError):
            track_num = None
    year_str = str(year_raw).strip()[:4] if year_raw else ""
    del audio
    return {
        "title": str(_get("\xa9nam") or ""),
        "artist": str(_get("\xa9ART") or ""),
        "album": str(_get("\xa9alb") or ""),
        "release_year": int(year_str) if year_str.isdigit() else None,
        "genre": str(_get("\xa9gen") or "") or None,
        "track_number": track_num,
    }


def read_cover(file_path) -> Optional[bytes]:
    """读取 M4A covr 封面。"""
    from mutagen.mp4 import MP4
    audio = MP4(file_path)
    covr = audio.get("covr")
    if not covr:
        return None
    first = covr[0]
    result = bytes(first) if first is not None else None
    del audio
    return result


def read_lyrics(file_path) -> Optional[str]:
    """读取 M4A ©lyr 歌词字段。"""
    from mutagen.mp4 import MP4
    audio = MP4(file_path)
    v = audio.get("\xa9lyr")
    if not v:
        del audio
        return None
    result = v[0] if isinstance(v, (list, tuple)) else str(v)
    del audio
    return result
