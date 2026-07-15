"""MP3（ID3v2.4）格式标签写入/读取（§7.2）。"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


def replace_front_cover(audio, cover_data):
    """替换 MP3 的前封面（type=3），保留其他图片类型。

    mutagen 对 APIC 这类多值帧，``audio["APIC"] = frame`` 会新增而非替换，
    故需先显式移除已有的 type=3 APIC 帧，再添加新封面（与 FLAC 行为一致）。
    """
    from mutagen.id3 import APIC, Encoding
    if audio.tags is None:
        audio.add_tags()
    existing = [f for f in audio.tags.getall("APIC") if f.type != 3]
    audio.tags.delall("APIC")
    for f in existing:
        audio.tags.add(f)
    audio.tags.add(APIC(
        encoding=Encoding.UTF8, mime="image/jpeg", type=3,
        desc="Cover", data=cover_data,
    ))


def replace_lyrics(audio, lyrics_text):
    """替换 MP3 的歌词帧（desc=\\"Lyrics"），保留其他语言的歌词帧。

    与封面同理：USLT 是多值帧，直接赋值会累积重复帧，需先移除再添加。
    """
    from mutagen.id3 import USLT, Encoding
    if audio.tags is None:
        audio.add_tags()
    existing = [f for f in audio.tags.getall("USLT") if f.desc != "Lyrics"]
    audio.tags.delall("USLT")
    for f in existing:
        audio.tags.add(f)
    audio.tags.add(USLT(
        encoding=Encoding.UTF8, lang="chi", desc="Lyrics", text=lyrics_text,
    ))


def write(file_path, meta, cover_data, lyrics_text):
    from mutagen.id3 import ID3, TIT2, TPE1, TALB, TDRC, TCON, TRCK, Encoding
    from mutagen.mp3 import MP3
    audio = MP3(file_path, ID3=ID3)
    if audio.tags is None:
        audio.add_tags()
    audio["TIT2"] = TIT2(encoding=Encoding.UTF8, text=[meta.title])
    audio["TPE1"] = TPE1(encoding=Encoding.UTF8, text=[meta.artist])
    audio["TALB"] = TALB(encoding=Encoding.UTF8, text=[meta.album])
    if meta.release_year:
        audio["TDRC"] = TDRC(encoding=Encoding.UTF8, text=[str(meta.release_year)])
    if meta.genre:
        audio["TCON"] = TCON(encoding=Encoding.UTF8, text=[meta.genre])
    if meta.track_number:
        audio["TRCK"] = TRCK(encoding=Encoding.UTF8, text=[str(meta.track_number)])
    if cover_data:
        replace_front_cover(audio, cover_data)
    if lyrics_text:
        replace_lyrics(audio, lyrics_text)
    audio.save(v2_version=4)
    del audio


def write_cover(file_path, cover_data):
    from mutagen.mp3 import MP3
    from mutagen.id3 import ID3
    audio = MP3(file_path, ID3=ID3)
    replace_front_cover(audio, cover_data)
    audio.save(v2_version=4)
    del audio


def write_lyrics(file_path, lyrics_text):
    from mutagen.mp3 import MP3
    from mutagen.id3 import ID3
    audio = MP3(file_path, ID3=ID3)
    replace_lyrics(audio, lyrics_text)
    audio.save(v2_version=4)
    del audio


def read_metadata(file_path) -> dict[str, Any]:
    """读取 MP3 ID3v2 标签。"""
    from mutagen.mp3 import MP3
    from mutagen.id3 import ID3
    audio = MP3(file_path, ID3=ID3)
    tags = audio.tags or {}

    def _first(frame, attr="text"):
        f = tags.get(frame)
        if f is None:
            return None
        val = getattr(f, attr, None)
        if isinstance(val, (list, tuple)):
            return val[0] if val else None
        return val

    year_raw = _first("TDRC")
    track_raw = _first("TRCK")
    track_num = None
    if track_raw:
        try:
            track_num = int(str(track_raw).split("/")[0])
        except (ValueError, TypeError):
            track_num = None
    year_str = str(year_raw).strip()[:4] if year_raw else ""
    del audio
    return {
        "title": str(_first("TIT2") or ""),
        "artist": str(_first("TPE1") or ""),
        "album": str(_first("TALB") or ""),
        "release_year": int(year_str) if year_str.isdigit() else None,
        "genre": str(_first("TCON") or "") or None,
        "track_number": track_num,
    }


def read_cover(file_path) -> Optional[bytes]:
    """读取 MP3 APIC 封面（优先 type=3 前封面）。"""
    from mutagen.mp3 import MP3
    from mutagen.id3 import ID3, APIC
    audio = MP3(file_path, ID3=ID3)
    tags = audio.tags or {}
    fallback = None
    for frame in tags.values():
        if isinstance(frame, APIC):
            if frame.type == 3:
                del audio
                return frame.data
            if fallback is None:
                fallback = frame.data
    del audio
    return fallback


def read_lyrics(file_path) -> Optional[str]:
    """读取 MP3 USLT 歌词。"""
    from mutagen.mp3 import MP3
    from mutagen.id3 import ID3, USLT
    audio = MP3(file_path, ID3=ID3)
    tags = audio.tags or {}
    for frame in tags.values():
        if isinstance(frame, USLT):
            text = frame.text or None
            del audio
            return text
    del audio
    return None
