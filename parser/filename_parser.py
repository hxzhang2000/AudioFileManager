"""文件名多策略解析器（FileNameParser） — §5.1.2

从文件名中提取歌曲名和歌手名，使用 4 级策略链：

支持的命名模式：
- 歌曲名-歌手.mp3          → (歌曲名, 歌手)
- 歌曲名 - 歌手.mp3
- 歌手 - 歌曲名.mp3
- 01_歌曲名_歌手.mp3
- 01_歌曲名-歌手.mp3
- [标签]歌曲名-歌手.mp3
- 歌手-歌曲名-专辑名.mp3   → 三段式，自动识别专辑
- 歌手_歌曲名.mp3            → (歌曲名, 歌手)
- 单纯歌曲名.mp3            → (歌曲名, None)
"""

import os
import re
import unicodedata
import logging
from typing import Optional

from parser.artist_db import ArtistDB

logger = logging.getLogger(__name__)


# ============================================================
# §5.1.2 文件名多策略解析器
# ============================================================

class FileNameParser:
    """
    从文件名中提取歌曲名和歌手名，使用 4 级策略链。

    支持的命名模式：
    - 歌曲名-歌手.mp3          → (歌曲名, 歌手)
    - 歌曲名 - 歌手.mp3
    - 歌手 - 歌曲名.mp3
    - 01_歌曲名_歌手.mp3
    - 01_歌曲名-歌手.mp3
    - [标签]歌曲名-歌手.mp3
    - 歌手-歌曲名-专辑名.mp3   → 三段式，自动识别专辑
    - 歌手_歌曲名.mp3            → (歌曲名, 歌手)
    - 单纯歌曲名.mp3            → (歌曲名, None)
    """

    # 多歌手分隔符：& ＆ 、 , ， / ／
    _ARTIST_HINTS = re.compile(r'[&＆、,，/／]')
    # 姓氏集合统一由 ArtistDB.get_surname_set() 提供，不再硬编码副本

    def __init__(self, artist_db: ArtistDB):
        self.artist_db = artist_db

    def parse(self, filename: str) -> tuple:
        """
        解析文件名，返回 (歌曲名, 歌手, 专辑名, 是否不确定)。

        返回四元素：
        - 专辑名仅在找到三段式模式时非 None
        - 第四元素 is_uncertain 为 True 时，表示解析结果置信度低
          （如三段式无法确认顺序），UI 应标记 "⚠️ 顺序不确定"
        """
        name, _ = os.path.splitext(filename)
        # Unicode NFKC 规范化：全角字符→半角，便于后续分隔符匹配
        name = unicodedata.normalize("NFKC", name).strip()

        # 去除序号前缀（如 01_ / 01. / 01- / 01 ）与方括号标签（如 [搬运]）
        cleaned = re.sub(r'^(?:\d+[._\s\-]+|\[.*?\]\s*)', '', name).strip()

        # 文件名缓存快速命中：若该 stem 已被用户纠错过，直接复用其歌手
        cached = self.artist_db.lookup(cleaned)
        if cached is not None and cached.name:
            # 缓存命中时，歌手已知，仍需从文件名切出歌曲名（去除歌手片段）
            # 简单处理：若文件名包含歌手名，则取剩余部分作为歌曲名
            if cached.name in cleaned:
                title = cleaned.replace(cached.name, "").strip(" -–—_")
                title = title or cleaned
            else:
                title = cleaned
            return (title, cached.name, None, False)

        # 尝试三段式：歌手-歌曲-专辑 或 专辑-歌曲-歌手
        m3 = re.match(r'^(.+?)\s*[-_–—]\s*(.+?)\s*[-_–—]\s*(.+?)$', cleaned)
        if m3:
            p1, p2, p3 = m3.group(1).strip(), m3.group(2).strip(), m3.group(3).strip()
            result = self._resolve_three_part(p1, p2, p3)
            if result:
                title, artist, album, uncertain = result
                return (title, artist, album, uncertain)

        # 两段式：歌曲名-歌手 或 歌手-歌曲名
        m = re.match(r'^(.+?)\s*[-–—]\s*(.+?)$', cleaned)
        if m:
            part1, part2 = m.group(1).strip(), m.group(2).strip()
            result = self._resolve_two_part(part1, part2)
            if result:
                return (*result, False)

        # 兜底：当作单纯歌曲名处理
        return (cleaned, None, None, False)

    def _resolve_two_part(self, p1: str, p2: str) -> Optional[tuple]:
        """
        两段式解析：歌曲名-歌手 或 歌手-歌曲名。

        返回 (歌曲名, 歌手, 专辑名=None) 或 None。
        """
        # 第1级：多歌手分隔符检测
        if self._ARTIST_HINTS.search(p1):
            return (p2, p1, None)   # p1 是歌手（含多歌手分隔符）
        if self._ARTIST_HINTS.search(p2):
            return (p1, p2, None)   # p2 是歌手

        # 第2级：已知艺人名匹配
        if self.artist_db.lookup(p1) is not None:
            return (p2, p1, None)   # p1 是已知歌手
        if self.artist_db.lookup(p2) is not None:
            return (p1, p2, None)   # p2 是已知歌手

        # 第3级：长度 + 中英文 + 空格判断
        def is_chinese(s: str) -> bool:
            return any('\u4e00' <= c <= '\u9fff' for c in s)

        p1_cn, p2_cn = is_chinese(p1), is_chinese(p2)

        # 中文歌名 + 英文歌手 或 英文歌名 + 中文歌手
        if p1_cn and not p2_cn:
            return (p1, p2, None)   # p1=中文歌名, p2=英文歌手
        if p2_cn and not p1_cn:
            return (p2, p1, None)   # p2=中文歌名, p1=英文歌手

        # 两边都中文或都英文：用长度 + 空格判断
        len_diff = len(p1) - len(p2)
        if abs(len_diff) >= 3:
            if len_diff < 0:
                return (p2, p1, None)  # p1 短 → 歌手
            return (p1, p2, None)

        # 空格判断（英文名多带空格）
        p1_has_space = ' ' in p1.strip()
        p2_has_space = ' ' in p2.strip()
        if p1_has_space and not p2_has_space:
            return (p2, p1, None)  # p1 带空格 → 歌手名
        if p2_has_space and not p1_has_space:
            return (p1, p2, None)

        # 第4级：姓氏判断（中文 2~3 字，首字为常见姓）
        surnames = self.artist_db.get_surname_set()
        if p1 and len(p1) <= 3 and p1[0] in surnames:
            return (p2, p1, None)
        if p2 and len(p2) <= 3 and p2[0] in surnames:
            return (p1, p2, None)

        # 默认：歌名-歌手（常见习惯）
        return (p1, p2, None)

    def _resolve_three_part(self, p1: str, p2: str, p3: str) -> Optional[tuple]:
        """
        三段式：尝试确定 歌曲-歌手-专辑 的顺序。

        返回 (歌曲名, 歌手, 专辑名, is_uncertain) 或 None。

        注意（v5.7 明确限制）：三段式文件名顺序本身具有歧义，本解析为
        **尽力而为（best-effort）**，存在以下局限：
        - 仅当某一段能命中 ArtistDB 时才较为可靠（据此推断歌手所在位置）；
        - 若三段均无法命中 ArtistDB，则**默认按 歌手-歌曲-专辑** 处理并
          标记 is_uncertain=True，UI 层应显示 "⚠️ 顺序不确定"；
        - 某段本身含短横（如日期 ``2008-10``）或使用了非 ``- – —`` 分隔符时，
          可能被误判为两段式或错误切分；
        - 仅当三段都能明确命中时才返回专辑名，否则专辑名按 None 处理。

        对于无法可靠解析的三段式，建议用户在详情面板手动纠正，
        纠正结果会写入文件名缓存（见 §5.1.1），下次直接命中。
        """
        # 优先尝试 歌手-歌曲-专辑
        if self.artist_db.lookup(p1) is not None:
            return (p2, p1, p3, False)
        if self.artist_db.lookup(p2) is not None:
            return (p1, p2, p3, False)
        # 尝试 专辑-歌曲-歌手
        if self.artist_db.lookup(p3) is not None:
            return (p2, p3, p1, False)
        # 默认当 歌手-歌曲-专辑 处理（无法可靠推断时），标记为不确定
        return (p2, p1, p3, True)
