"""FLAC（Vorbis Comment）格式标签写入/读取（§7.2）。"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


def write(file_path, meta, cover_data, lyrics_text):
    from mutagen.flac import FLAC, Picture
    audio = FLAC(file_path)
    if audio.tags is None:
        audio.add_tags()
    audio["TITLE"], audio["ARTIST"], audio["ALBUM"] = meta.title, meta.artist, meta.album
    if meta.release_year:
        audio["DATE"] = str(meta.release_year)
    if meta.genre:
        audio["GENRE"] = meta.genre
    if meta.track_number:
        audio["TRACKNUMBER"] = str(meta.track_number)
    if lyrics_text:
        audio["LYRICS"] = lyrics_text
    if cover_data:
        pic = Picture()
        pic.type = 3
        pic.desc = "Cover"
        pic.mime = "image/jpeg"
        pic.data = cover_data
        existing = [p for p in audio.pictures if p.type != 3]
        audio.clear_pictures()
        for p in existing:
            audio.add_picture(p)
        audio.add_picture(pic)
    audio.save()


def write_cover(file_path, cover_data):
    from mutagen.flac import FLAC, Picture
    audio = FLAC(file_path)
    if audio.tags is None:
        audio.add_tags()
    pic = Picture()
    pic.type = 3
    pic.desc = "Cover"
    pic.mime = "image/jpeg"
    pic.data = cover_data
    existing = [p for p in audio.pictures if p.type != 3]
    audio.clear_pictures()
    for p in existing:
        audio.add_picture(p)
    audio.add_picture(pic)
    audio.save()


def write_lyrics(file_path, lyrics_text):
    from mutagen.flac import FLAC
    audio = FLAC(file_path)
    if audio.tags is None:
        audio.add_tags()
    audio["LYRICS"] = lyrics_text
    audio.save()


def read_metadata(file_path) -> dict[str, Any]:
    """读取 FLAC Vorbis Comment 标签。"""
    from mutagen.flac import FLAC
    audio = FLAC(file_path)

    def _get(key):
        v = audio.get(key)
        if not v:
            return None
        return v[0] if isinstance(v, (list, tuple)) else v

    year_raw = _get("DATE")
    track_raw = _get("TRACKNUMBER")
    track_num = None
    if track_raw:
        try:
            track_num = int(str(track_raw).split("/")[0])
        except (ValueError, TypeError):
            track_num = None
    year_str = str(year_raw).strip()[:4] if year_raw else ""
    return {
        "title": str(_get("TITLE") or ""),
        "artist": str(_get("ARTIST") or ""),
        "album": str(_get("ALBUM") or ""),
        "release_year": int(year_str) if year_str.isdigit() else None,
        "genre": str(_get("GENRE") or "") or None,
        "track_number": track_num,
    }


def read_cover(file_path) -> Optional[bytes]:
    """读取 FLAC 封面（优先 type=3 前封面）。"""
    from mutagen.flac import FLAC
    audio = FLAC(file_path)
    fallback = None
    for pic in audio.pictures:
        if pic.type == 3:
            return pic.data
        if fallback is None:
            fallback = pic.data
    return fallback


def read_lyrics(file_path) -> Optional[str]:
    """读取 FLAC LYRICS 字段。"""
    from mutagen.flac import FLAC
    audio = FLAC(file_path)
    v = audio.get("LYRICS")
    if not v:
        return None
    return v[0] if isinstance(v, (list, tuple)) else str(v)
