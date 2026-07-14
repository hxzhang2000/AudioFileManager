"""本地歌手库（ArtistDB） — §5.1.1

提供艺人名匹配能力，供文件名解析器（FileNameParser）使用。

数据来源（按优先级）:
1. 内置预置：高频中文/英文艺人名硬编码（约 100 条）
2. MusicBrainz 同步：首次安装时从 MusicBrainz 拉取常见中文艺人
3. 用户纠错：每次用户在详情面板手动修改歌手名后，自动存入本地缓存
4. 文件名缓存：成功匹配的文件名→艺人映射缓存（避免重复解析）

存储位置: <app_data>/data/artist_db.json
"""

import os
import json
import logging
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

from utils.helpers import get_app_data_dir

logger = logging.getLogger(__name__)


# ============================================================
# §5.1.1 本地歌手库（ArtistDB）
# ============================================================

@dataclass
class ArtistInfo:
    """单个艺人的信息。"""
    name: str                                            # 标准化名称
    aliases: list = field(default_factory=list)          # 别名（如 "Jay Chou"→"周杰伦"）
    surnames: str = ""                                   # 姓氏（中文单字，用于姓氏判断）


class ArtistDB:
    """
    本地歌手数据库，用于文件名解析的艺人名匹配。

    数据来源（按优先级）:
    1. 内置预置：高频中文/英文艺人名硬编码（约 100 条，见 _init_builtin）
    2. MusicBrainz 同步：首次安装时从 MusicBrainz 拉取常见中文艺人
    3. 用户纠错：每次用户在详情面板手动修改歌手名后，自动存入本地缓存
    4. 文件名缓存：成功匹配的文件名→艺人映射缓存（避免重复解析）

    存储位置: <app_data>/data/artist_db.json
    """

    # 默认存储路径：<app_data>/data/artist_db.json
    _DEFAULT_DB_PATH = get_app_data_dir() / "data" / "artist_db.json"

    def __init__(self, db_path: Optional[str | Path] = None):
        # 各字典均为实例属性（避免类级别可变字典在多实例间共享）
        self.db_path = str(db_path) if db_path else str(self._DEFAULT_DB_PATH)
        self._BUILTIN_ARTISTS: dict[str, ArtistInfo] = {}   # 内置预置艺人
        self._user_artists: dict[str, ArtistInfo] = {}      # 用户手动添加 / 纠错入库
        self._filename_cache: dict[str, ArtistInfo] = {}    # 文件名 stem→ArtistInfo 缓存

        self._load()
        if not self._BUILTIN_ARTISTS:
            self._init_builtin()

    # ------------------------------------------------------------
    # 内置预置艺人列表
    # ------------------------------------------------------------
    def _init_builtin(self):
        """初始化内置预置艺人列表（高频中文 + 英文热门，约 100 条）。"""
        # 中文高频艺人
        chinese = [
            "周杰伦", "林俊杰", "陈奕迅", "王力宏", "邓紫棋",
            "张惠妹", "蔡依林", "孙燕姿", "张学友", "刘德华",
            "李荣浩", "薛之谦", "毛不易", "许嵩", "张杰",
            "王菲", "那英", "林志炫", "杨宗纬", "周深",
            "伍佰", "五月天", "苏打绿", "S.H.E", "凤凰传奇",
            "李健", "朴树", "陈粒", "赵雷", "陈雪凝",
            "周笔畅", "张靓颖", "谭维维", "韩红", "萧敬腾",
            "林宥嘉", "方大同", "陶喆", "李宗盛", "罗大佑",
            "刘若英", "莫文蔚", "梁静茹", "张韶涵", "王心凌",
            "杨千嬅", "容祖儿", "谢霆锋", "李克勤", "古巨基",
            "陈慧娴", "林子祥", "叶倩文", "梅艳芳", "张国荣",
            "黄家驹", "Beyond", "草蜢", "温拿乐队",
            "久石让", "坂本龙一", "中岛美嘉", "玉置浩二",
        ]
        for name in chinese:
            # 中文艺人取首字作为姓氏单字，供姓氏判断策略使用
            surnames = name[0] if name and '\u4e00' <= name[0] <= '\u9fff' else ""
            self._BUILTIN_ARTISTS[name.lower()] = ArtistInfo(
                name=name, surnames=surnames
            )

        # 英文高频艺人
        english = [
            "Taylor Swift", "Adele", "Ed Sheeran", "Lady Gaga",
            "Eminem", "Rihanna", "Bruno Mars", "Beyoncé",
            "Michael Jackson", "The Beatles", "Queen",
            "Elvis Presley", "Madonna", "Prince",
            "Led Zeppelin", "Pink Floyd", "Nirvana",
            "Bob Dylan", "David Bowie", "Radiohead",
            "Coldplay", "U2", "Linkin Park", "Green Day",
            "Metallica", "AC/DC", "Guns N' Roses",
            "M83", "Daft Punk", "The Weeknd",
            "Billie Eilish", "Ariana Grande", "Olivia Rodrigo",
            "Kendrick Lamar", "Drake", "Kanye West",
        ]
        for name in english:
            self._BUILTIN_ARTISTS[name.lower()] = ArtistInfo(name=name)

        logger.debug(f"内置预置艺人加载完成：{len(self._BUILTIN_ARTISTS)} 条")

    # ------------------------------------------------------------
    # 持久化：加载 / 保存
    # ------------------------------------------------------------
    def _load(self):
        """从 JSON 加载用户数据和文件名缓存。"""
        if not os.path.exists(self.db_path):
            return
        try:
            with open(self.db_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"艺人库读取失败: {e}，使用内置数据")
            return

        # 用户纠错入库的艺人
        for name, info in data.get("user_artists", {}).items():
            try:
                self._user_artists[name.lower()] = ArtistInfo(**info)
            except TypeError:
                # 兼容历史数据字段缺失
                self._user_artists[name.lower()] = ArtistInfo(name=info.get("name", name))

        # 文件名→艺人缓存（历史数据可能存的是字符串，统一转换为 ArtistInfo）
        for key, val in data.get("filename_cache", {}).items():
            if isinstance(val, str):
                self._filename_cache[key] = ArtistInfo(name=val)
            elif isinstance(val, dict):
                self._filename_cache[key] = ArtistInfo(**val)

    def _save(self):
        """持久化用户数据和文件名缓存。"""
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        data = {
            "user_artists": {k: v.__dict__ for k, v in self._user_artists.items()},
            "filename_cache": {k: v.__dict__ for k, v in self._filename_cache.items()},
        }
        try:
            with open(self.db_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except OSError as e:
            logger.warning(f"艺人库保存失败: {e}")

    # ------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------
    def lookup(self, name: str) -> Optional[ArtistInfo]:
        """
        按名称（不区分大小写）查找艺人信息。

        查找顺序：文件名缓存 → 用户纠错库 → 内置预置库。
        返回 ArtistInfo 或 None。
        """
        if not name:
            return None
        key = name.lower().strip()
        if key in self._filename_cache:
            return self._filename_cache[key]
        if key in self._user_artists:
            return self._user_artists[key]
        return self._BUILTIN_ARTISTS.get(key)

    def add_correction(self, raw_filename: str, artist: str):
        """
        用户手动纠错时调用。
        1. 记录文件名 stem（去扩展名）→艺人映射（避免下次再猜错）
        2. 如果艺人在库中不存在，加入 user_artists
        """
        stem = Path(raw_filename).stem  # 剥离扩展名，与 FileNameParser 解析键一致
        self._filename_cache[stem.lower().strip()] = ArtistInfo(name=artist)

        key = artist.lower().strip()
        if key not in self._BUILTIN_ARTISTS and key not in self._user_artists:
            surnames = artist[0] if artist and '\u4e00' <= artist[0] <= '\u9fff' else ""
            self._user_artists[key] = ArtistInfo(name=artist, surnames=surnames)
        self._save()
        logger.info(f"记录用户纠错：{stem!r} → {artist!r}")

    # ------------------------------------------------------------
    # 批量查询接口
    # ------------------------------------------------------------
    def get_all_artist_names(self) -> set:
        """返回所有已知艺人名（去重）。"""
        names = set()
        for info in self._BUILTIN_ARTISTS.values():
            names.add(info.name)
        for info in self._user_artists.values():
            names.add(info.name)
        return names

    def get_surname_set(self) -> set:
        """返回所有已知中文单姓（用于文件名解析第 4 级姓氏判断）。"""
        surnames = set()
        for info in self._BUILTIN_ARTISTS.values():
            if info.surnames:
                surnames.add(info.surnames)
        for info in self._user_artists.values():
            if info.surnames:
                surnames.add(info.surnames)
        return surnames

    # ------------------------------------------------------------
    # MusicBrainz 同步（占位实现）
    # ------------------------------------------------------------
    def sync_from_musicbrainz(self):
        """
        从 MusicBrainz 拉取常见中文艺人，扩充本地库。

        调用时机：
        - 首次安装 + 网络可用时自动同步
        - 设置界面中的「🔄 同步歌手库」手动触发

        实现：使用 MusicBrainz XML API 搜索 tag:chinese，
        取前 500 条结果，存入 user_artists。

        由于受 1 req/s 限制，建议在后台线程（QThread）中执行，
        用进度条显示同步进度。

        TODO: 接入 MusicBrainz XML API（见 §5.1.1 同步实现）。
        """
        # 占位实现：当前版本不执行网络请求，仅保留接口签名以便 UI 调用
        logger.info("sync_from_musicbrainz: 当前为占位实现，未执行网络同步")
        return 0
