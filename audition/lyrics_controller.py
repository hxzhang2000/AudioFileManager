"""歌词同步/编辑控制器（§8B.2）。

``LyricsController`` 是歌词数据与 UI 之间的中间层，负责：
- 维护结构化歌词行列表（``list[LyricLine]``）；
- 根据播放进度计算当前应高亮的行索引；
- 提供行的增删改、整体偏移、清空、批量设置等编辑操作；
- 在 LRC 文本与结构化行列表之间互转。

与 ``audition/lyrics_io.py`` 的关系：
- 本控制器不直接做文件 I/O，而是复用 ``lyrics_io.parse_lrc`` /
  ``format_lrc`` 进行文本解析与格式化。
- 文件读写由 ``lyrics_io.load_lrc`` / ``save_lrc`` /
  ``load_lyrics_from_metadata`` 负责，调用方加载后通过
  :meth:`LyricsController.set_lyrics` 注入。
"""

from __future__ import annotations

import copy
import logging
from typing import List, Union

from audition.lyrics_io import LyricLine, format_lrc, parse_lrc

logger = logging.getLogger(__name__)


class LyricsController:
    """歌词同步与编辑控制器。

    职责：
    1. **同步高亮**：:meth:`update_position` 根据播放进度返回当前行索引。
    2. **行编辑**：:meth:`add_line` / :meth:`remove_line` / :meth:`update_line`
       支持单行增删改，操作后自动按时间重排。
    3. **批量操作**：:meth:`offset` 整体偏移、:meth:`clear` 清空、
       :meth:`set_lyrics` 批量替换。
    4. **文本互转**：:meth:`get_text` 输出 LRC 文本，:meth:`set_lyrics`
       接受文本或行列表。

    所有时间单位均为毫秒（int）。
    """

    def __init__(self):
        # 结构化歌词行列表（按 time 升序）
        self.lines: List[LyricLine] = []
        # 原始 LRC 文本快照（用于「重置」操作）
        self._original_text: str = ""

    # ============================================================
    # 同步高亮
    # ============================================================

    def update_position(self, ms: int) -> int:
        """根据播放位置返回当前应高亮的歌词行索引。

        查找逻辑：返回 ``time <= ms`` 的最后一行索引；
        若没有任何行的 ``time <= ms``（播放进度在第一行之前），
        返回 ``-1``。

        Args:
            ms: 当前播放位置（毫秒）。

        Returns:
            当前行索引（0-based），无匹配时返回 ``-1``。
        """
        if not self.lines:
            return -1
        # 从后往前找到第一个 time <= ms 的行
        for i in range(len(self.lines) - 1, -1, -1):
            if self.lines[i].time <= ms:
                return i
        return -1

    # ============================================================
    # 行编辑
    # ============================================================

    def add_line(self, time: int, text: str) -> int:
        """添加一行歌词，并按时间重排。

        Args:
            time: 时间戳（毫秒）。
            text: 歌词文本。

        Returns:
            新增行在重排后的列表索引。
        """
        new_line = LyricLine(time=max(0, time), text=text)
        self.lines.append(new_line)
        self.lines.sort(key=lambda x: x.time)
        return self.lines.index(new_line)

    def remove_line(self, index: int) -> bool:
        """删除指定索引的歌词行。

        Args:
            index: 行索引（0-based）。

        Returns:
            成功返回 ``True``，索引越界返回 ``False``。
        """
        if 0 <= index < len(self.lines):
            del self.lines[index]
            return True
        return False

    def update_line(self, index: int, time: int, text: str) -> bool:
        """更新指定行的时间与文本，并按时间重排。

        Args:
            index: 行索引（0-based）。
            time: 新时间戳（毫秒）。
            text: 新歌词文本。

        Returns:
            成功返回 ``True``，索引越界返回 ``False``。
        """
        if not (0 <= index < len(self.lines)):
            return False
        self.lines[index].time = max(0, time)
        self.lines[index].text = text
        self.lines.sort(key=lambda x: x.time)
        return True

    def adjust_line_time(self, index: int, new_time_ms: int) -> bool:
        """调整单行歌词时间戳（兼容设计文档命名）。

        等价于保留原文本、仅修改时间的 :meth:`update_line` 简化版。
        操作后按时间重排。

        Args:
            index: 行索引。
            new_time_ms: 新时间戳（毫秒）。

        Returns:
            成功返回 ``True``，索引越界返回 ``False``。
        """
        if not (0 <= index < len(self.lines)):
            return False
        self.lines[index].time = max(0, new_time_ms)
        self.lines.sort(key=lambda x: x.time)
        return True

    # ============================================================
    # 批量操作
    # ============================================================

    def clear(self):
        """清空所有歌词行。"""
        self.lines.clear()

    def offset(self, ms: int):
        """整体偏移所有歌词行的时间。

        偏移后时间小于 0 的行会被截断到 0（不丢弃歌词内容）。

        Args:
            ms: 偏移量（毫秒），正数延后、负数提前。
        """
        for line in self.lines:
            new_time = line.time + ms
            line.time = max(0, new_time)
        self.lines.sort(key=lambda x: x.time)

    def offset_all(self, offset_ms: int):
        """整体偏移（兼容设计文档命名，等价于 :meth:`offset`）。"""
        self.offset(offset_ms)

    # ============================================================
    # 批量设置与文本互转
    # ============================================================

    def set_lyrics(self, text_or_lines: Union[str, List[LyricLine]]):
        """设置歌词，自动判断输入是 LRC 文本还是行列表。

        - 传入 ``str``：经 :func:`parse_lrc` 解析为行列表。
        - 传入 ``list[LyricLine]``：深拷贝后直接使用。

        设置时会保存原始文本快照（若输入为文本），供 :meth:`reset` 使用。

        Args:
            text_or_lines: LRC 文本字符串或 :class:`LyricLine` 列表。
        """
        if isinstance(text_or_lines, str):
            self._original_text = text_or_lines
            self.lines = parse_lrc(text_or_lines)
        elif isinstance(text_or_lines, list):
            self._original_text = format_lrc(text_or_lines)
            # 深拷贝，避免外部修改影响内部数据
            self.lines = [copy.copy(line) for line in text_or_lines]
            self.lines.sort(key=lambda x: x.time)
        else:
            logger.warning(f"set_lyrics 不支持的输入类型: {type(text_or_lines)}")
            self.lines = []
            self._original_text = ""

    def get_text(self) -> str:
        """获取格式化后的 LRC 文本。

        Returns:
            LRC 格式字符串（``[MM:SS.xx]文本`` 逐行）。
        """
        return format_lrc(self.lines)

    def get_current_index(self, progress_ms: int) -> int:
        """根据播放进度返回当前行索引（兼容设计文档命名）。

        等价于 :meth:`update_position`。
        """
        return self.update_position(progress_ms)

    # ============================================================
    # 重置
    # ============================================================

    def snapshot(self):
        """保存当前歌词为原始快照（供 :meth:`reset` 恢复）。"""
        self._original_text = format_lrc(self.lines)

    def reset(self) -> bool:
        """重置到原始歌词快照。

        Returns:
            有快照可恢复返回 ``True``，无快照返回 ``False``。
        """
        if not self._original_text:
            return False
        self.lines = parse_lrc(self._original_text)
        return True

    # ============================================================
    # 便捷属性
    # ============================================================

    def __len__(self) -> int:
        return len(self.lines)

    def __bool__(self) -> bool:
        return bool(self.lines)

    @property
    def is_empty(self) -> bool:
        """是否无歌词行。"""
        return len(self.lines) == 0
