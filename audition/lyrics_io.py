"""歌词文件读写模块（§8B.2）。

负责 LRC 格式歌词的解析、格式化、文件加载与保存，
以及从音频文件标签读取内嵌歌词并解析为结构化行列表。

LRC 格式说明：
- 标准时间戳形如 ``[MM:SS.xx]`` 或 ``[MM:SS.xxx]``（xx 为百分秒，xxx 为毫秒）。
- 支持单行多时间戳：``[00:01.00][00:05.00]歌词文本`` 会被展开为两行
  （相同文本、不同时间），便于同一句歌词在多处出现。
- 无时间戳的纯文本行（如 ``[ti:稻香]`` 元数据行或 ``作词:周杰伦``）
  会被忽略，不进入结构化行列表。

设计要点：
- ``LyricLine`` 使用 ``dataclass``，``time`` 字段单位为毫秒（int）。
- 解析与格式化互为逆操作：``format_lrc(parse_lrc(text))`` 保持时间一致
  （格式统一为 ``[MM:SS.xx]``，即两位百分秒）。
- ``load_lyrics_from_metadata`` 委托 ``services.tag_writer.read_lyrics``
  按格式路由读取（MP3/FLAC/M4A/OGG/WMA/APE），读取失败返回空列表。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)


# ============================================================
# 数据模型
# ============================================================

@dataclass
class LyricLine:
    """单行歌词。

    Attributes:
        time: 该行起始时间戳，单位毫秒（ms）。
        text: 歌词文本（去除时间戳后的纯文本）。
    """

    time: int
    text: str = field(default="")

    def __lt__(self, other: "LyricLine") -> bool:
        # 按时间排序用（offset_all / sort 依赖）
        return self.time < other.time


# ============================================================
# LRC 解析与格式化
# ============================================================

# 匹配单个时间标签：[MM:SS.xx] 或 [MM:SS.xxx]
# 分钟为 1 位以上数字，秒为 1-2 位数字，小数部分 1-3 位
_TIME_TAG_RE = re.compile(r"\[(\d+):(\d{1,2})(?:[.:](\d{1,3}))?\]")
# 同时匹配一行中所有时间标签前缀（贪婪到所有连续标签）
_MULTI_TAG_RE = re.compile(r"^((?:\[\d+:\d{1,2}(?:[.:]\d{1,3})?\])+)(.*)$")


def _time_tag_to_ms(minutes: str, seconds: str, fraction: Optional[str]) -> int:
    """将时间标签的分/秒/小数部分转换为毫秒整数。

    Args:
        minutes: 分钟字符串（如 ``"01"``）。
        seconds: 秒字符串（如 ``"05"``）。
        fraction: 小数部分字符串（如 ``"00"``, ``"123"``），可为 ``None``。

    Returns:
        毫秒整数。小数部分按位数归一到毫秒：
        2 位 → 百分秒（×10），3 位 → 毫秒，1 位 → ×100。
    """
    ms = int(minutes) * 60_000 + int(seconds) * 1_000
    if fraction:
        # 根据小数位数归一到毫秒
        if len(fraction) == 2:
            ms += int(fraction) * 10          # 百分秒 → 毫秒
        elif len(fraction) == 3:
            ms += int(fraction)               # 已是毫秒
        elif len(fraction) == 1:
            ms += int(fraction) * 100         # 十分秒 → 毫秒
        else:
            # 超过 3 位则截断到前 3 位
            ms += int(fraction[:3])
    return ms


def parse_lrc(text: str) -> List[LyricLine]:
    """解析 LRC 格式歌词文本，返回按时间排序的行列表。

    支持以下格式：
    - ``[00:31.05]歌词文本``（标准两位百分秒）
    - ``[00:31.123]歌词文本``（三位毫秒）
    - ``[00:01.00][00:05.00]歌词文本``（多时间戳行，展开为多行）

    无时间戳的行（包括 ``[ti:...]`` 等元数据行和纯文本行）会被忽略。

    Args:
        text: LRC 格式歌词文本。

    Returns:
        按 ``time`` 升序排列的 :class:`LyricLine` 列表。
        空文本或无有效行时返回空列表。
    """
    if not text:
        return []

    lines: List[LyricLine] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        # 提取行首连续的时间标签（可能多个）
        m = _MULTI_TAG_RE.match(line)
        if not m:
            # 无时间标签前缀 → 跳过（含 [ti:] [ar:] 等元数据行、纯文本行）
            continue

        tags_part = m.group(1)
        lyric_text = m.group(2).strip()

        # 解析该行所有时间标签
        times = []
        for tag_m in _TIME_TAG_RE.finditer(tags_part):
            ms = _time_tag_to_ms(tag_m.group(1), tag_m.group(2), tag_m.group(3))
            times.append(ms)

        if not times:
            continue

        # 同一文本对应多个时间戳 → 展开为多行
        for t in times:
            lines.append(LyricLine(time=t, text=lyric_text))

    # 按时间排序（相同时间保持出现顺序，Python sort 稳定）
    lines.sort(key=lambda x: x.time)
    return lines


def format_lrc(lines: List[LyricLine]) -> str:
    """将结构化歌词行列表格式化为 LRC 文本。

    每行格式为 ``[MM:SS.xx]歌词文本``，其中 ``xx`` 为两位百分秒。
    行按 ``time`` 升序输出。

    Args:
        lines: :class:`LyricLine` 列表。

    Returns:
        LRC 格式文本（以 ``\\n`` 分隔各行，末尾无多余换行）。
        空列表返回空字符串。
    """
    if not lines:
        return ""

    sorted_lines = sorted(lines, key=lambda x: x.time)
    out = []
    for line in sorted_lines:
        minutes = line.time // 60_000
        remainder = line.time % 60_000
        seconds = remainder // 1_000
        centisec = (remainder % 1_000) // 10  # 毫秒 → 百分秒
        out.append(f"[{minutes:02d}:{seconds:02d}.{centisec:02d}]{line.text}")
    return "\n".join(out)


# ============================================================
# 文件加载与保存
# ============================================================

def load_lrc(file_path: str | Path) -> List[LyricLine]:
    """从 ``.lrc`` 文件加载歌词并解析。

    Args:
        file_path: LRC 文件路径。

    Returns:
        :class:`LyricLine` 列表。文件不存在或读取失败时返回空列表。
    """
    path = Path(file_path)
    if not path.exists():
        logger.debug(f"LRC 文件不存在: {file_path}")
        return []
    try:
        # 优先 UTF-8，回退 GBK（部分中文 LRC 使用 GBK 编码）
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = path.read_text(encoding="gbk")
        return parse_lrc(text)
    except OSError as e:
        logger.warning(f"读取 LRC 文件失败 {file_path}: {e}")
        return []


def save_lrc(file_path: str | Path, lines: List[LyricLine]) -> bool:
    """将歌词行列表保存为 ``.lrc`` 文件（UTF-8 编码）。

    Args:
        file_path: 目标 LRC 文件路径。
        lines: :class:`LyricLine` 列表。

    Returns:
        成功返回 ``True``，失败返回 ``False``。
    """
    path = Path(file_path)
    text = format_lrc(lines)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return True
    except OSError as e:
        logger.warning(f"保存 LRC 文件失败 {file_path}: {e}")
        return False


# ============================================================
# 从音频文件标签读取歌词
# ============================================================

def load_lyrics_from_metadata(file_path: str | Path) -> List[LyricLine]:
    """从音频文件标签读取内嵌歌词并解析为结构化行列表。

    委托 ``services.tag_writer.read_lyrics`` 按文件格式路由读取
    （MP3 USLT / FLAC LYRICS / M4A ©lyr / OGG LYRICS / WMA WM/Lyrics /
    APE Lyrics），读取到的文本再经 :func:`parse_lrc` 解析。

    若歌词为纯文本（无时间戳），解析结果为空列表——此时调用方可
    将原文作为整段歌词展示。

    Args:
        file_path: 音频文件路径。

    Returns:
        :class:`LyricLine` 列表。无歌词或解析失败时返回空列表。
    """
    try:
        from services.tag_writer import read_lyrics
    except ImportError:
        logger.warning("无法导入 services.tag_writer.read_lyrics")
        return []

    try:
        lrc_text = read_lyrics(str(file_path))
    except Exception as e:
        logger.warning(f"读取文件标签歌词失败 {file_path}: {e}")
        return []

    if not lrc_text:
        return []

    return parse_lrc(lrc_text)


def read_lyrics_text_from_metadata(file_path: str | Path) -> Optional[str]:
    """从音频文件标签读取原始歌词文本（不解析）。

    供需要区分「同步歌词」与「纯文本歌词」的场景使用。

    Args:
        file_path: 音频文件路径。

    Returns:
        歌词文本字符串；无歌词时返回 ``None``。
    """
    try:
        from services.tag_writer import read_lyrics
    except ImportError:
        return None
    try:
        return read_lyrics(str(file_path))
    except Exception as e:
        logger.warning(f"读取文件标签歌词失败 {file_path}: {e}")
        return None
