"""文件重命名器（§6.3 文件命名规范）。

按用户配置的模板将音频文件重命名为规范文件名，并处理重名冲突。

支持占位符：
    - ``{title}``        歌曲名
    - ``{artist}``       歌手
    - ``{album}``        专辑
    - ``{year}``         发行年份
    - ``{track_number}`` 曲目编号（两位补零，如 ``01``）

默认模板：``{title} - {artist}``

配置字段（``filename`` 配置块）::

    "filename": {
        "template": "{title} - {artist}",
        "separator": "-",
        "strip_number_prefix": true
    }

``strip_number_prefix`` 为 ``True`` 时，会先去除标题中可能残留的序号前缀
（如 ``01.`` / ``01 -`` / ``1-``），再套用模板。

重名冲突处理：若目标文件名在所在目录已存在且非源文件本身，自动添加
``(2)`` / ``(3)`` … 序号后缀，避免覆盖。

依赖：
- ``utils.helpers.sanitize_filename``：清理文件名非法字符
- 元数据对象可为 :class:`search.provider.TrackMetadata` 或 ``dict``
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Optional

from utils.helpers import make_unique_path, sanitize_filename
from utils.retry_on_locked import retry_on_locked

logger = logging.getLogger(__name__)


# 默认命名模板
DEFAULT_TEMPLATE = "{title} - {artist}"

# 序号前缀正则：匹配开头处的 1~3 位数字 + 分隔符（.-)_] 或空白）
#   "01. 稻香"  → "稻香"
#   "01 - 稻香" → "稻香"
#   "01 稻香"   → "稻香"
#   "1-稻香"    → "稻香"
_NUMBER_PREFIX_RE = re.compile(r"^\s*\d{1,3}(?:\s*[.\-)\]_]\s*|\s+)")


def _meta_get(metadata: Any, key: str, default: Any = None) -> Any:
    """从元数据对象中取值，兼容 TrackMetadata（dataclass）与 dict。"""
    if metadata is None:
        return default
    if isinstance(metadata, dict):
        return metadata.get(key, default)
    return getattr(metadata, key, default)


def _get_year(metadata: Any) -> str:
    """从元数据中提取发行年份字符串，兼容 release_year / year 字段名。"""
    year = _meta_get(metadata, "release_year", None)
    if year is None or year == "":
        year = _meta_get(metadata, "year", None)
    if year is None or year == "":
        return ""
    try:
        return str(int(str(year).strip()[:4]))
    except (ValueError, TypeError):
        return ""


def _get_str(metadata: Any, key: str) -> str:
    """从元数据中取字符串字段，统一去首尾空白。"""
    val = _meta_get(metadata, key, "")
    if val is None:
        return ""
    return str(val).strip()


class FileRenamer:
    """文件重命名器 — 按模板重命名音频文件，并处理重名冲突。

    使用方式::

        renamer = FileRenamer(config["filename"])
        new_path = renamer.rename(r"E:\\下载\\01.稻香.mp3", meta)
        # → "E:\\下载\\稻香 - 周杰伦.mp3"
    """

    def __init__(self, config: Optional[dict] = None):
        """初始化重命名器。

        Args:
            config: ``filename`` 配置块（或完整配置 dict）。
                缺省时使用默认模板 ``{title} - {artist}`` 与
                ``strip_number_prefix=True``。
        """
        cfg = self._extract_filename_config(config)
        self.template: str = cfg.get("template", DEFAULT_TEMPLATE)
        self.separator: str = cfg.get("separator", "-")
        self.strip_number_prefix: bool = bool(cfg.get("strip_number_prefix", True))

    # ----------------------------------------------------------
    # 主入口
    # ----------------------------------------------------------
    def rename(
        self,
        file_path: str,
        metadata: Any,
        template: Optional[str] = None,
    ) -> str:
        """按模板重命名文件（§6.3）。

        流程：
        1. 从元数据中提取 title/artist/album/year/track_number；
        2. 若 ``strip_number_prefix`` 开启，去除标题序号前缀；
        3. 套用模板生成新文件名（保留原扩展名）；
        4. 清理非法字符，处理重名冲突；
        5. 在磁盘上执行重命名（文件锁定时自动重试）。

        Args:
            file_path: 源文件路径。
            metadata: 元数据对象（TrackMetadata 或 dict）。
            template: 命名模板，为 ``None`` 时使用实例默认模板。

        Returns:
            重命名后的文件完整路径（若无需改名则返回原路径）。
        """
        src = Path(file_path)
        if not src.exists():
            raise FileNotFoundError(f"源文件不存在: {file_path}")

        use_template = template or self.template

        # 1. 提取标题并按需去除序号前缀
        title = _get_str(metadata, "title")
        if not title:
            # 标题缺失时回退到原文件名 stem，避免生成空文件名
            title = src.stem
        if self.strip_number_prefix:
            title = self._strip_number_prefix(title)

        # 2. 套用模板生成新文件名（stem）
        new_stem = self._apply_template(use_template, metadata, title_override=title)
        # 3. 清理非法字符
        new_stem = sanitize_filename(new_stem)
        if not new_stem or new_stem == "unknown":
            # 模板渲染结果为空（如所有字段都缺失），回退到原 stem
            new_stem = src.stem

        new_path = src.with_name(f"{new_stem}{src.suffix}")

        # 4. 与源文件相同则无需改名
        try:
            same_file = new_path.resolve() == src.resolve()
        except OSError:
            same_file = str(new_path) == str(src)

        if same_file:
            return str(src)

        # 5. 重名冲突处理：目标已存在且非源文件 → 加序号后缀
        new_path = self._resolve_rename_conflict(new_path)

        # 6. 执行重命名（带锁定重试）
        self._do_rename(str(src), str(new_path))
        logger.info(f"重命名: {src.name} → {new_path.name}")
        return str(new_path)

    # ----------------------------------------------------------
    # 模板应用
    # ----------------------------------------------------------
    def _apply_template(
        self,
        template: str,
        metadata: Any,
        title_override: Optional[str] = None,
    ) -> str:
        """应用命名模板，替换占位符。

        支持占位符：``{title}`` / ``{artist}`` / ``{album}`` /
        ``{year}`` / ``{track_number}``。缺失字段替换为空串。
        ``track_number`` 渲染为两位补零（如 ``3`` → ``03``）。

        Args:
            template: 命名模板字符串。
            metadata: 元数据对象。
            title_override: 标题覆盖值（已去除序号前缀），为 ``None`` 时
                从元数据读取。

        Returns:
            渲染后的文件名 stem（未做非法字符清理）。
        """
        title = title_override if title_override is not None else _get_str(metadata, "title")
        artist = _get_str(metadata, "artist")
        album = _get_str(metadata, "album")
        year = _get_year(metadata)

        # 曲目编号：两位补零
        track_number = _meta_get(metadata, "track_number", None)
        track_str = ""
        if track_number:
            try:
                track_str = f"{int(track_number):02d}"
            except (ValueError, TypeError):
                track_str = str(track_number)

        result = template.format(
            title=title,
            artist=artist,
            album=album,
            year=year,
            track_number=track_str,
        )

        # 清理因字段缺失而产生的首尾分隔符残留（如 " - 周杰伦" / "稻香 - "）
        result = self._cleanup_separators(result)
        return result

    # ----------------------------------------------------------
    # 序号前缀处理
    # ----------------------------------------------------------
    @staticmethod
    def _strip_number_prefix(text: str) -> str:
        """去除标题开头的序号前缀。

        匹配 ``01.`` / ``01 -`` / ``01 `` / ``1-`` / ``01_`` / ``01)`` 等
        常见序号前缀形式。注意：这是启发式处理，对于本身以数字开头的歌名
        （如 ``17 岁``）可能误删，由 ``strip_number_prefix`` 配置项控制开关。

        Args:
            text: 原始标题。

        Returns:
            去除序号前缀后的标题（若无匹配则原样返回）。
        """
        if not text:
            return text
        cleaned = _NUMBER_PREFIX_RE.sub("", text, count=1)
        return cleaned.strip()

    # ----------------------------------------------------------
    # 分隔符残留清理
    # ----------------------------------------------------------
    def _cleanup_separators(self, text: str) -> str:
        """清理因字段缺失导致的首尾分隔符残留。

        例如模板 ``{title} - {artist}``，artist 为空时得到 ``稻香 - ``，
        本方法将其清理为 ``稻香``。

        Args:
            text: 模板渲染后的字符串。

        Returns:
            清理首尾分隔符与空白后的字符串。
        """
        sep = re.escape(self.separator)
        # 首部：分隔符 + 空白
        text = re.sub(rf"^\s*{sep}\s*", "", text)
        # 尾部：空白 + 分隔符
        text = re.sub(rf"\s*{sep}\s*$", "", text)
        return text.strip()

    # ----------------------------------------------------------
    # 重名冲突处理
    # ----------------------------------------------------------
    def _resolve_rename_conflict(self, new_path: Path) -> Path:
        """目标文件名已存在时，添加序号后缀避免覆盖。

        ``稻香 - 周杰伦.mp3`` 已存在 → ``稻香 - 周杰伦 (2).mp3``。

        Args:
            new_path: 期望的新路径。

        Returns:
            不冲突的最终路径（若目标不存在则原样返回）。
        """
        return make_unique_path(new_path)

    # ----------------------------------------------------------
    # 实际重命名操作（带锁定重试）
    # ----------------------------------------------------------
    @retry_on_locked(max_attempts=3, delay=0.5)
    def _do_rename(self, src: str, dst: str) -> None:
        """在磁盘上执行重命名，文件被资源管理器锁定时自动重试。

        使用 :func:`os.replace` 实现原子重命名（同盘内）。
        """
        os.replace(src, dst)

    # ----------------------------------------------------------
    # 配置归一化
    # ----------------------------------------------------------
    @staticmethod
    def _extract_filename_config(config: Optional[dict]) -> dict:
        """从入参中提取 ``filename`` 配置块。

        兼容两种入参：
        - 完整配置 dict（含 ``filename`` 子块）→ 取 ``config["filename"]``
        - ``filename`` 子块本身 → 直接使用

        为 ``None`` 时返回空 dict（由各字段回退到默认值）。
        """
        if config is None:
            return {}
        if isinstance(config, dict) and "filename" in config and isinstance(config["filename"], dict):
            return config["filename"]
        return config

    # ----------------------------------------------------------
    # 便捷方法：仅生成新文件名（不实际重命名）
    # ----------------------------------------------------------
    def build_filename(self, metadata: Any, template: Optional[str] = None) -> str:
        """根据元数据与模板生成目标文件名（stem，不含扩展名）。

        不会修改磁盘文件，仅返回规范化后的文件名 stem，便于调用方
        （如 FileOrganizer）在复制/移动前确定目标文件名。

        Args:
            metadata: 元数据对象（TrackMetadata 或 dict）。
            template: 命名模板，为 ``None`` 时使用实例默认模板。

        Returns:
            规范化后的文件名 stem（已清理非法字符）。
        """
        use_template = template or self.template
        title = _get_str(metadata, "title")
        if self.strip_number_prefix:
            title = self._strip_number_prefix(title)
        stem = self._apply_template(use_template, metadata, title_override=title)
        stem = sanitize_filename(stem)
        if not stem or stem == "unknown":
            stem = "unknown"
        return stem
