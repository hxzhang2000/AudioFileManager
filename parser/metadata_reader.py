"""现有 ID3/标签读取器 — §5.1.3

使用 mutagen 读取各音频格式的标签，返回统一的元数据字典。

支持格式：MP3（ID3v2）/ FLAC（Vorbis Comment）/ M4A（iTunes MP4）
          / OGG（Vorbis Comment）/ WMA（ASF）/ APE（APEv2）。

字段映射与 §7.2 的 tag_writer 写入端保持一致。

关于文件锁定重试：mutagen 会将文件 IO 错误（含锁定 PermissionError）
包装为 ``MutagenError``，原始异常保存在 ``__cause__`` 中。本模块在
``_read_with_retry`` 中识别锁定类错误并重新抛出原始 ``PermissionError``，
以触发 ``utils.retry_on_locked`` 装饰器重试；非锁定错误（标签损坏 / 文件
缺失等）由 ``read`` / ``read_cover`` / ``read_lyrics`` 统一降级处理，
返回空值，保证批处理扫描连续运行。
"""

import logging
from pathlib import Path
from typing import Optional

from utils.retry_on_locked import retry_on_locked

logger = logging.getLogger(__name__)

# mutagen 将文件 IO 错误包装为 MutagenError（其 __cause__ 为原始异常）。
# 此处做可选导入：mutagen 未安装时置 None，读取时由调用方统一降级处理。
try:
    from mutagen import MutagenError
except ImportError:  # mutagen 未安装
    MutagenError = None


# ============================================================
# 各格式字段读取辅助函数
# ============================================================

def _empty_meta() -> dict:
    """返回空元数据字典（字段统一为 None）。"""
    return {
        "title": None,
        "artist": None,
        "album": None,
        "year": None,
        "genre": None,
        "track_number": None,
        "duration": None,        # 秒
        "bitrate": None,         # kbps
        "sample_rate": None,     # Hz
        "channels": None,
    }


def _first(values):
    """取列表第一个元素，空则返回 None。"""
    if values:
        return values[0]
    return None


def _id3_get(tags, key: str) -> Optional[str]:
    """从 ID3 标签读取某帧文本（兼容多值，返回字符串）。"""
    if key in tags:
        frame = tags[key]
        text = getattr(frame, "text", None)
        if text:
            # 多值用 ' / ' 连接（如多歌手）
            return " / ".join(str(t) for t in text)
        return str(frame) if frame else None
    return None


def _vc_get(audio, key: str) -> Optional[str]:
    """从 Vorbis Comment（FLAC/OGG）读取字段。"""
    if key in audio and audio[key]:
        return str(audio[key][0])
    return None


def _mp4_get(audio, key: str) -> Optional[str]:
    """从 MP4（M4A）标签读取字段。"""
    if key in audio and audio[key]:
        return str(audio[key][0])
    return None


def _asf_get(audio, key: str) -> Optional[str]:
    """从 ASF（WMA）标签读取字段。"""
    if key in audio and audio[key]:
        return str(audio[key][0].value)
    return None


def _ape_get(audio, key: str) -> Optional[str]:
    """从 APEv2 标签读取字段（键名大小写不敏感）。"""
    # APEv2 键名大小写不敏感，遍历查找
    try:
        val = audio[key]
        return str(val) if val else None
    except KeyError:
        return None


def _fill_audio_info(meta: dict, info):
    """填充音频流信息（时长/比特率/采样率/声道数）。"""
    meta["duration"] = getattr(info, "length", None)
    bitrate = getattr(info, "bitrate", None)
    meta["bitrate"] = bitrate // 1000 if bitrate else None
    meta["sample_rate"] = getattr(info, "sample_rate", None)
    meta["channels"] = getattr(info, "channels", None)


# ============================================================
# 各格式标签读取函数（返回统一元数据字典）
# ============================================================

def _read_mp3(file_path: str) -> dict:
    """读取 MP3（ID3v2）标签。"""
    from mutagen.mp3 import MP3
    audio = MP3(file_path)
    meta = _empty_meta()
    _fill_audio_info(meta, audio.info)
    tags = audio.tags  # ID3 实例或 None
    if tags:
        meta["title"] = _id3_get(tags, "TIT2")
        meta["artist"] = _id3_get(tags, "TPE1")
        meta["album"] = _id3_get(tags, "TALB")
        # 年份：优先 TDRC（ID3v2.4），回退 TYER（ID3v2.3）
        meta["year"] = _id3_get(tags, "TDRC") or _id3_get(tags, "TYER")
        meta["genre"] = _id3_get(tags, "TCON")
        meta["track_number"] = _id3_get(tags, "TRCK")
    del audio
    return meta


def _read_flac(file_path: str) -> dict:
    """读取 FLAC（Vorbis Comment）标签。"""
    from mutagen.flac import FLAC
    audio = FLAC(file_path)
    meta = _empty_meta()
    _fill_audio_info(meta, audio.info)
    meta["title"] = _vc_get(audio, "TITLE")
    meta["artist"] = _vc_get(audio, "ARTIST")
    meta["album"] = _vc_get(audio, "ALBUM")
    meta["year"] = _vc_get(audio, "DATE") or _vc_get(audio, "YEAR")
    meta["genre"] = _vc_get(audio, "GENRE")
    meta["track_number"] = _vc_get(audio, "TRACKNUMBER") or _vc_get(audio, "TRACK")
    del audio
    return meta


def _read_m4a(file_path: str) -> dict:
    """读取 M4A（iTunes MP4）标签。"""
    from mutagen.mp4 import MP4
    audio = MP4(file_path)
    meta = _empty_meta()
    _fill_audio_info(meta, audio.info)
    meta["title"] = _mp4_get(audio, "\xa9nam")
    meta["artist"] = _mp4_get(audio, "\xa9ART")
    meta["album"] = _mp4_get(audio, "\xa9alb")
    meta["year"] = _mp4_get(audio, "\xa9day")
    meta["genre"] = _mp4_get(audio, "\xa9gen")
    # trkn 为 [(track, total), ...] 列表
    trkn = audio.get("trkn")
    if trkn:
        track = _first(trkn)
        if track and track[0]:
                meta["track_number"] = str(track[0])
    del audio
    return meta


def _read_ogg(file_path: str) -> dict:
    """读取 OGG（Vorbis Comment）标签。"""
    from mutagen.oggvorbis import OggVorbis
    audio = OggVorbis(file_path)
    meta = _empty_meta()
    _fill_audio_info(meta, audio.info)
    meta["title"] = _vc_get(audio, "TITLE")
    meta["artist"] = _vc_get(audio, "ARTIST")
    meta["album"] = _vc_get(audio, "ALBUM")
    meta["year"] = _vc_get(audio, "DATE") or _vc_get(audio, "YEAR")
    meta["genre"] = _vc_get(audio, "GENRE")
    meta["track_number"] = _vc_get(audio, "TRACKNUMBER") or _vc_get(audio, "TRACK")
    del audio
    return meta



def _read_wma(file_path: str) -> dict:
    """读取 WMA（ASF）标签。"""
    from mutagen.asf import ASF
    audio = ASF(file_path)
    meta = _empty_meta()
    _fill_audio_info(meta, audio.info)
    meta["title"] = _asf_get(audio, "Title")
    meta["artist"] = _asf_get(audio, "Author")
    meta["album"] = _asf_get(audio, "WM/AlbumTitle")
    meta["year"] = _asf_get(audio, "WM/Year")
    meta["genre"] = _asf_get(audio, "WM/Genre")
    meta["track_number"] = _asf_get(audio, "WM/TrackNumber")
    del audio
    return meta


def _read_ape(file_path: str) -> dict:
    """读取 APE（APEv2）标签。"""
    # MonkeysAudio 同时提供音频流信息与 APEv2 标签
    from mutagen.monkeysaudio import MonkeysAudio
    audio = MonkeysAudio(file_path)
    meta = _empty_meta()
    _fill_audio_info(meta, audio.info)
    meta["title"] = _ape_get(audio, "Title")
    meta["artist"] = _ape_get(audio, "Artist")
    meta["album"] = _ape_get(audio, "Album")
    meta["year"] = _ape_get(audio, "Year")
    meta["genre"] = _ape_get(audio, "Genre")
    meta["track_number"] = _ape_get(audio, "Track")
    del audio
    return meta


# ============================================================
# 各格式封面读取函数（均只接收 file_path 单参数，便于分发）
# ============================================================

def _cover_mp3(file_path: str) -> Optional[bytes]:
    """读取 MP3 封面（APIC 帧）。"""
    from mutagen.mp3 import MP3
    try:
        audio = MP3(file_path)
    except Exception as e:
        logger.warning(f"_cover_mp3: 无法打开 MP3 文件 {file_path}: {e}")
        return None
    tags = audio.tags
    if tags:
        for key in tags:
            if key.startswith("APIC"):
                data = tags[key].data
                del audio
                return data
    else:
        logger.debug(f"_cover_mp3: {file_path} 没有 ID3 标签")
    del audio
    return None


def _cover_flac(file_path: str) -> Optional[bytes]:
    """读取 FLAC 封面（Picture，优先 type=3 正面封面）。"""
    from mutagen.flac import FLAC
    audio = FLAC(file_path)
    for pic in audio.pictures:
        if pic.type == 3:  # front cover
            data = pic.data
            del audio
            return data
    # 无 front cover 时取第一张图
    if audio.pictures:
        data = audio.pictures[0].data
        del audio
        return data
    del audio
    return None


def _cover_m4a(file_path: str) -> Optional[bytes]:
    """读取 M4A 封面（covr）。"""
    from mutagen.mp4 import MP4
    audio = MP4(file_path)
    covr = audio.get("covr")
    if covr:
        result = covr[0]
        del audio
        return result
    del audio
    return None


def _cover_ogg(file_path: str) -> Optional[bytes]:
    """读取 OGG 封面（METADATA_BLOCK_PICTURE，base64 编码）。"""
    import base64
    from mutagen.oggvorbis import OggVorbis
    from mutagen.flac import Picture
    audio = OggVorbis(file_path)
    pics = audio.get("METADATA_BLOCK_PICTURE", [])
    if pics:
        pic = Picture(base64.b64decode(pics[0]))
        data = pic.data
        del audio
        return data
    del audio
    return None


def _cover_ape(file_path: str) -> Optional[bytes]:
    """读取 APE 封面（Cover Art (Front) 等）。

    APEv2 的 ``Cover Art (Front)`` 字段格式为 ``文件名\\x00<二进制数据>``，
    需要跳过开头到第一个 null 字节的部分，与 tag_writer._read_cover_ape 保持一致。
    """
    from mutagen.apev2 import APEv2, APEBinaryValue
    audio = APEv2(file_path)
    for key in audio:
        if key.lower().startswith("cover art"):
            val = audio[key]
            if isinstance(val, APEBinaryValue):
                data = bytes(val.value)
            else:
                data = bytes(val)
            # 剥离 filename\x00 前缀
            null_pos = data.find(b"\x00")
            if null_pos >= 0:
                data = data[null_pos + 1:]
            del audio
            return data
    del audio
    return None


# ============================================================
# 各格式歌词读取函数（均只接收 file_path 单参数，便于分发）
# ============================================================

def _lyrics_mp3(file_path: str) -> Optional[str]:
    """读取 MP3 歌词（USLT 帧）。"""
    from mutagen.mp3 import MP3
    audio = MP3(file_path)
    tags = audio.tags
    if tags:
        for key in tags:
            if key.startswith("USLT"):
                text = tags[key].text or None
                del audio
                return text
    del audio
    return None


def _lyrics_flac(file_path: str) -> Optional[str]:
    """读取 FLAC 歌词（LYRICS / UNSYNCEDLYRICS）。"""
    from mutagen.flac import FLAC
    audio = FLAC(file_path)
    if "LYRICS" in audio and audio["LYRICS"]:
        result = str(audio["LYRICS"][0])
        del audio
        return result
    if "UNSYNCEDLYRICS" in audio and audio["UNSYNCEDLYRICS"]:
        result = str(audio["UNSYNCEDLYRICS"][0])
        del audio
        return result
    del audio
    return None


def _lyrics_ogg(file_path: str) -> Optional[str]:
    """读取 OGG 歌词（LYRICS / UNSYNCEDLYRICS）。"""
    from mutagen.oggvorbis import OggVorbis
    audio = OggVorbis(file_path)
    if "LYRICS" in audio and audio["LYRICS"]:
        result = str(audio["LYRICS"][0])
        del audio
        return result
    if "UNSYNCEDLYRICS" in audio and audio["UNSYNCEDLYRICS"]:
        result = str(audio["UNSYNCEDLYRICS"][0])
        del audio
        return result
    del audio
    return None


def _lyrics_m4a(file_path: str) -> Optional[str]:
    """读取 M4A 歌词（©lyr）。"""
    from mutagen.mp4 import MP4
    audio = MP4(file_path)
    lyr = audio.get("\xa9lyr")
    if lyr:
        result = str(lyr[0])
        del audio
        return result
    del audio
    return None


def _lyrics_wma(file_path: str) -> Optional[str]:
    """读取 WMA 歌词（WM/Lyrics）。"""
    from mutagen.asf import ASF
    audio = ASF(file_path)
    if "WM/Lyrics" in audio and audio["WM/Lyrics"]:
        result = str(audio["WM/Lyrics"][0].value)
        del audio
        return result
    del audio
    return None


def _lyrics_ape(file_path: str) -> Optional[str]:
    """读取 APE 歌词（Lyrics / LYRICS / Unsynced Lyrics）。"""
    from mutagen.apev2 import APEv2
    audio = APEv2(file_path)
    for key in ("Lyrics", "LYRICS", "Unsynced Lyrics"):
        try:
            result = str(audio[key])
            del audio
            return result
        except KeyError:
            continue
    del audio
    return None


# ============================================================
# MetadataReader 主类
# ============================================================

class MetadataReader:
    """
    元数据读取器 — 按文件格式路由到对应的 mutagen 读取函数。

    使用 utils.retry_on_locked 装饰 _read_with_retry，文件被锁定时自动重试。
    """

    # 扩展名 → 标签读取函数 的分发表
    _READ_DISPATCH = {
        "mp3": _read_mp3,
        "flac": _read_flac,
        "m4a": _read_m4a,
        "ogg": _read_ogg,
        "wma": _read_wma,
        "ape": _read_ape,
    }

    # 扩展名 → 封面读取函数（WMA 按 §7.1 约定不支持封面，故不含 wma）
    _COVER_DISPATCH = {
        "mp3": _cover_mp3,
        "flac": _cover_flac,
        "m4a": _cover_m4a,
        "ogg": _cover_ogg,
        "ape": _cover_ape,
    }

    # 扩展名 → 歌词读取函数
    _LYRICS_DISPATCH = {
        "mp3": _lyrics_mp3,
        "flac": _lyrics_flac,
        "m4a": _lyrics_m4a,
        "ogg": _lyrics_ogg,
        "wma": _lyrics_wma,
        "ape": _lyrics_ape,
    }

    @retry_on_locked(max_attempts=3, delay=0.5)
    def _read_with_retry(self, file_path: str, reader_func):
        """
        执行实际的 mutagen 读取，文件锁定时由装饰器重试。

        mutagen 会把文件 IO 错误（含锁定 PermissionError）包装为
        ``MutagenError``，原始异常保存在 ``__cause__`` 中。本方法识别
        锁定类错误并重新抛出原始 ``PermissionError``，以触发
        ``retry_on_locked`` 装饰器重试；非锁定错误向上抛出，由调用方
        （read / read_cover / read_lyrics）统一降级处理。
        """
        try:
            return reader_func(file_path)
        except Exception as e:
            # mutagen 包装的文件 IO 错误
            if MutagenError is not None and isinstance(e, MutagenError):
                cause = e.__cause__
                if isinstance(cause, PermissionError):
                    # 文件锁定：重新抛出原始 PermissionError，触发装饰器重试
                    raise cause
                # 标签损坏 / 文件缺失等：向上抛出，由调用方降级（不重试）
                raise
            # 非 mutagen 抛出的锁定/权限错误：交给装饰器重试
            if isinstance(e, (PermissionError, OSError)):
                raise
            # 其他异常（如 mutagen 未安装时的 ModuleNotFoundError）：向上抛出
            raise

    def read(self, file_path: str) -> dict:
        """
        读取音频文件的标签元数据。

        Args:
            file_path: 音频文件路径。

        Returns:
            统一格式的元数据字典，包含以下键（缺失字段为 None）：
            - title / artist / album / year / genre / track_number
            - duration（秒）/ bitrate（kbps）/ sample_rate（Hz）/ channels

            不支持的格式、文件损坏或读取失败时返回空字段字典
            （不抛异常，便于批处理扫描连续运行）。
        """
        suffix = Path(file_path).suffix.lower().lstrip(".")
        reader = self._READ_DISPATCH.get(suffix)
        if reader is None:
            logger.debug(f"不支持的音频格式: {file_path}")
            return _empty_meta()
        try:
            return self._read_with_retry(file_path, reader)
        except Exception as e:
            # 重试耗尽仍锁定 / 标签损坏 / 缺失 / 格式异常：记录并返回空字典
            logger.warning(f"读取标签失败 {file_path}: {e}")
            return _empty_meta()

    def read_cover(self, file_path: str) -> Optional[bytes]:
        """
        读取封面图片二进制数据。

        优先级：
        1. 标签内嵌封面（按格式路由）
        2. 同目录下的 ``cover.jpg``（FILES 保存模式落盘的独立封面文件）

        WMA 按 §7.1 约定不支持标签封面，但仍会回退检查 cover.jpg。
        读取失败时返回 None（不抛异常）。
        """
        suffix = Path(file_path).suffix.lower().lstrip(".")
        cover_fn = self._COVER_DISPATCH.get(suffix)
        if cover_fn is not None:
            try:
                result = self._read_with_retry(file_path, cover_fn)
                if result is not None:
                    return result
            except Exception as e:
                logger.warning(f"读取标签封面失败 {file_path}: {e}")

        # —— 回退：检查同目录 cover.jpg （FILES 保存模式）——
        cover_jpg = Path(file_path).parent / "cover.jpg"
        if cover_jpg.is_file():
            try:
                return cover_jpg.read_bytes()
            except Exception as e:
                logger.warning(f"读取独立封面文件失败 {cover_jpg}: {e}")

        return None

    def read_lyrics(self, file_path: str) -> Optional[str]:
        """
        读取歌词文本。

        优先级：
        1. 标签内嵌歌词（按格式路由）
        2. 同目录下的 ``.lrc`` 文件（FILES 保存模式落盘的独立歌词文件）

        读取失败时返回 None（不抛异常）。
        """
        suffix = Path(file_path).suffix.lower().lstrip(".")
        lyrics_fn = self._LYRICS_DISPATCH.get(suffix)
        if lyrics_fn is not None:
            try:
                result = self._read_with_retry(file_path, lyrics_fn)
                if result is not None:
                    return result
            except Exception as e:
                logger.warning(f"读取标签歌词失败 {file_path}: {e}")

        # —— 回退：检查同目录 .lrc 文件（FILES 保存模式）——
        lrc_path = Path(file_path).with_suffix(".lrc")
        if lrc_path.is_file():
            try:
                return lrc_path.read_text(encoding="utf-8", errors="replace")
            except Exception as e:
                logger.warning(f"读取独立歌词文件失败 {lrc_path}: {e}")

        return None
