"""目录化整理器（§6.1 目标目录结构、§6.2 整理规则）。

根据音频文件元数据（歌手/专辑/年份）将其整理到规范的目录结构中：

    E:\\音乐库\\
    ├── ⚪ 未知艺人\\
    │   ├── ⚪ 未知专辑\\
    │   └── 魔杰座\\
    ├── 周杰伦\\
    │   ├── 魔杰座 (2008)\\
    │   │   ├── 稻香-周杰伦.mp3
    │   │   ├── cover.jpg
    │   │   └── 稻香-周杰伦.lrc
    │   └── 七里香 (2004)\\

整理规则（§6.2）：

    | 元数据状态          | 整理结果                       |
    | ------------------- | ------------------------------ |
    | 歌手 + 专辑 + 年份  | ``歌手\\专辑 (年份)\\``         |
    | 歌手 + 专辑（无年份）| ``歌手\\专辑\\``               |
    | 歌手（无专辑）       | ``歌手\\🎵 精选\\``            |
    | 无歌手无专辑         | ``⚪ 未知艺人\\⚪ 未知专辑\\``  |

整理方式由 ``delete_source`` 控制：``False``（默认）复制保留源文件，
``True`` 移动文件并删除源文件。

伴随文件：整理音频时会一并迁移其同目录的 ``.lrc`` 歌词与封面图
（``cover.<ext>`` 专辑级封面保留原名，或 ``<原名>.<img>`` 同名封面
随音频改名），避免素材遗留在源目录。

依赖：
- ``utils.helpers.sanitize_filename``：清理文件名/目录名非法字符
- ``utils.helpers.ensure_dir``：递归创建目录
- 元数据对象可为 :class:`search.provider.TrackMetadata` 或 ``dict``
  （同时兼容 ``release_year`` / ``year`` 两种年份字段名）
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any, Optional, Union

from utils.helpers import make_unique_path, sanitize_filename, ensure_dir
from utils.retry_on_locked import retry_on_locked

logger = logging.getLogger(__name__)


# ============================================================
# 目录名常量（与 §6.1 目录结构示例保持一致）
# ============================================================
UNKNOWN_ARTIST_DIR = "⚪ 未知艺人"   # 无歌手时的兜底目录
UNKNOWN_ALBUM_DIR = "⚪ 未知专辑"    # 无专辑时的兜底目录
COMPILATION_DIR = "🎵 精选"          # 有歌手但无专辑时的精选目录

# 伴随文件：整理音频时一并迁移
_LRC_EXT = ".lrc"                                   # 歌词文件扩展名
_COVER_EXTS = (".jpg", ".jpeg", ".png", ".webp")    # 支持的封面图扩展名


# ============================================================
# 元数据访问辅助函数
# ============================================================
def _meta_get(metadata: Any, key: str, default: Any = None) -> Any:
    """从元数据对象中取值，兼容 TrackMetadata（dataclass）与 dict。

    年份字段同时兼容 ``release_year``（TrackMetadata / tag_writer）与
    ``year``（metadata_reader 返回的 dict）。
    """
    if metadata is None:
        return default
    if isinstance(metadata, dict):
        return metadata.get(key, default)
    # dataclass / 普通对象：属性访问
    return getattr(metadata, key, default)


def _get_year(metadata: Any) -> Optional[int]:
    """从元数据中提取发行年份，兼容 release_year / year 两种字段名。"""
    year = _meta_get(metadata, "release_year", None)
    if year is None or year == "":
        year = _meta_get(metadata, "year", None)
    if year is None or year == "":
        return None
    try:
        return int(str(year).strip()[:4])
    except (ValueError, TypeError):
        return None


def _get_str(metadata: Any, key: str) -> str:
    """从元数据中取字符串字段，统一去首尾空白。"""
    val = _meta_get(metadata, key, "")
    if val is None:
        return ""
    return str(val).strip()


class FileOrganizer:
    """目录化整理器 — 依据元数据将音频文件归档到规范的目录结构中。

    使用方式::

        organizer = FileOrganizer()
        target = organizer.organize(
            file_path=r"E:\\下载\\稻香.mp3",
            metadata=meta,                      # TrackMetadata 或 dict
            config=config["organize"],         # 整理配置块
        )
    """

    def __init__(self, default_config: Optional[dict] = None):
        """初始化整理器。

        Args:
            default_config: 默认整理配置（``organize`` 配置块），
                当 :meth:`organize` 未显式传入 config 时使用。
        """
        self.default_config: dict = default_config or {}

    # ----------------------------------------------------------
    # 主入口
    # ----------------------------------------------------------
    @retry_on_locked(max_attempts=3, delay=0.5)
    def organize(
        self,
        file_path: str,
        metadata: Any,
        config: Optional[dict] = None,
        target_filename: Optional[str] = None,
        original_path: Optional[str] = None,
    ) -> str:
        """将文件整理到目标目录结构中（§6.1 / §6.2）。

        根据 ``metadata``（歌手/专辑/年份）与整理 ``config`` 构建目标目录，
        创建目录后将源文件复制或移动到目标位置。

        Args:
            file_path: 源音频文件路径。
            metadata: 元数据对象（TrackMetadata 或 dict），用于确定目录层级。
            config: 整理配置块（``organize``），为 ``None`` 时使用实例默认配置。
                支持字段：``output_dir`` / ``by_artist`` / ``by_album`` /
                ``album_with_year`` / ``unknown_dir`` / ``delete_source``。
            target_filename: 目标文件名（不含目录）。为 ``None`` 时保留源文件名。

        Returns:
            最终的目标音频文件完整路径（伴随的 .lrc / 封面图也已迁移）。
        """
        org_config = self._get_organize_config(config)
        src = Path(file_path)
        if not src.exists():
            raise FileNotFoundError(f"源文件不存在: {file_path}")

        # 1. 构建目标目录
        target_dir = self._build_target_path(metadata, org_config)
        ensure_dir(target_dir)

        # 2. 确定目标文件名
        name = target_filename or src.name
        target_path = Path(target_dir) / name

        # 3. 目标目录内同名冲突保护（避免覆盖已有文件）
        target_path = self._resolve_target_conflict(src, target_path)

        # 4. 复制或移动
        delete_source = bool(org_config.get("delete_source", False))
        final_audio = self.copy_or_move(str(src), str(target_path), delete_source)

        # 5. 迁移伴随文件（.lrc / 封面图），保持与音频的相对关系
        moved = self._organize_companions(
            str(src), final_audio, delete_source, original_audio=original_path
        )
        if moved:
            logger.info(
                f"已迁移伴随文件: {', '.join(Path(p).name for p in moved)}"
            )

        logger.info(
            f"整理完成: {src.name} → {final_audio} "
            f"({'移动' if delete_source else '复制'})"
        )
        return final_audio

    # ----------------------------------------------------------
    # 伴随文件迁移（.lrc / 封面图）
    # ----------------------------------------------------------
    def _organize_companions(
        self,
        src_audio: str,
        final_audio: str,
        delete_source: bool,
        original_audio: Optional[str] = None,
    ) -> list[str]:
        """整理音频时一并迁移同目录的伴随文件（§6.2 补充）。

        整理一首音频到输出目录后，其相邻文件也应随之迁移，否则会遗留
        在源目录。定位伴随文件使用音频**重命名前**的原始名
        （``original_audio``），写入目标时使用音频**整理后**的新名，
        从而兼容「先改名、后整理」的批处理流程：

        - ``<原名>.lrc`` → ``<新名>.lrc``：歌词随音频重命名；
        - ``cover.<ext>`` → 同名保留：专辑级封面（目录共享资源），
          目标已存在时跳过，避免同一专辑重复生成 ``cover (2).jpg``；
        - ``<原名>.<img>`` → ``<新名>.<img>``：与音频同名的封面图。

        Args:
            src_audio: 源音频文件完整路径（整理后当前所在路径）。
            final_audio: 已整理完成的目标音频完整路径。
            delete_source: 与主文件一致地移动（``True``）/ 复制（``False``）。
            original_audio: 音频重命名前的原始路径；为 ``None`` 时退化为
                以 ``src_audio`` 的当前名定位伴随文件。

        Returns:
            成功迁移的伴随文件目标路径列表。
        """
        src = Path(src_audio)
        final = Path(final_audio)
        tgt_dir = final.parent
        tgt_stem = final.stem

        # 伴随文件与音频同名（.lrc / 同名封面）的部分，按「重命名前」的原名定位
        orig = Path(original_audio) if original_audio else src
        orig_stem = orig.stem
        moved: list[str] = []

        def _relocate(source: Path, target: Path) -> bool:
            # 目标已存在则跳过（不覆盖、不重命名），避免重复封面 / 误覆盖
            if target.exists() and target.resolve() != source.resolve():
                return False
            if source.resolve() == src.resolve():
                return False  # 防御：不会指向音频自身
            try:
                self.copy_or_move(str(source), str(target), delete_source)
                moved.append(str(target))
                return True
            except OSError as e:
                logger.warning(f"伴随文件迁移失败 {source.name} → {target.name}: {e}")
                return False

        # 1. 歌词 .lrc（随音频改名）
        lrc_src = src.parent / f"{orig_stem}{_LRC_EXT}"
        if lrc_src.is_file() and lrc_src.resolve() != src.resolve():
            _relocate(lrc_src, tgt_dir / f"{tgt_stem}{_LRC_EXT}")

        # 2. 专辑级封面 cover.<ext>（保留原名）
        for ext in _COVER_EXTS:
            cover_src = src.parent / f"cover{ext}"
            if cover_src.is_file() and cover_src.resolve() != src.resolve():
                _relocate(cover_src, tgt_dir / f"cover{ext}")

        # 3. 与音频同名的封面图 <原名>.<img>（改名以匹配新音频）
        for ext in _COVER_EXTS:
            img_src = src.parent / f"{orig_stem}{ext}"
            if img_src.is_file() and img_src.resolve() != src.resolve():
                _relocate(img_src, tgt_dir / f"{tgt_stem}{ext}")

        return moved

    # ----------------------------------------------------------
    # 复制 / 移动
    # ----------------------------------------------------------
    @retry_on_locked(max_attempts=3, delay=0.5)
    def copy_or_move(
        self,
        file_path: str,
        target_dir: str,
        delete_source: bool = False,
    ) -> str:
        """将文件复制或移动到目标位置。

        ``target_dir`` 既可以是目录（文件按原名放入），也可以是完整文件路径
        （直接使用该路径）。目标父目录不存在时会自动创建。

        Args:
            file_path: 源文件路径。
            target_dir: 目标目录或完整目标文件路径。
            delete_source: ``True`` 移动源文件（源被删除）；
                ``False`` 复制源文件（保留源文件，默认）。

        Returns:
            实际写入的目标文件完整路径。
        """
        src = Path(file_path)
        target = Path(target_dir)

        # 若 target 指向一个已存在的目录，则把文件按原名放入其中
        if target.is_dir():
            target = target / src.name

        # 确保父目录存在
        target.parent.mkdir(parents=True, exist_ok=True)

        if delete_source:
            # 移动：shutil.move 在目标已存在时会覆盖（Windows 下行为一致）
            shutil.move(str(src), str(target))
        else:
            # 复制：copy2 保留元数据（时间戳等）
            shutil.copy2(str(src), str(target))
        return str(target)

    # ----------------------------------------------------------
    # 目标路径构建
    # ----------------------------------------------------------
    def predict_target_path(self, metadata: Any, config: dict) -> str:
        """根据元数据与配置构建目标目录路径（公开接口）。

        行为与内部 :meth:`_build_target_path` 一致，供外部模块在调用
        :meth:`organize` 前推断目标目录。
        """
        return self._build_target_path(metadata, config)

    def _build_target_path(self, metadata: Any, config: dict) -> str:
        """根据元数据与配置构建目标目录路径（§6.2 整理规则）。

        规则：
            - 歌手 + 专辑 + 年份 → ``output_dir\\歌手\\专辑 (年份)\\``
            - 歌手 + 专辑（无年份）→ ``output_dir\\歌手\\专辑\\``
            - 歌手（无专辑）→ ``output_dir\\歌手\\🎵 精选\\``
            - 无歌手无专辑 → ``output_dir\\⚪ 未知艺人\\⚪ 未知专辑\\``

        ``by_artist`` / ``by_album`` 为 ``False`` 时跳过对应层级；
        ``unknown_dir`` 为 ``False`` 时不对未知歌手/专辑创建兜底目录
        （直接落到 output_dir）。

        Args:
            metadata: 元数据对象（TrackMetadata 或 dict）。
            config: 整理配置块。

        Returns:
            目标目录完整路径（字符串）。
        """
        output_dir = Path(config.get("output_dir", "") or "")
        by_artist = bool(config.get("by_artist", True))
        by_album = bool(config.get("by_album", True))
        album_with_year = bool(config.get("album_with_year", True))
        unknown_dir = bool(config.get("unknown_dir", True))

        artist = _get_str(metadata, "artist")
        album = _get_str(metadata, "album")
        year = _get_year(metadata)

        parts: list[str] = []

        # —— 第一层：歌手 ——
        if by_artist:
            if artist:
                parts.append(self._sanitize_dir_name(artist))
            elif unknown_dir:
                parts.append(UNKNOWN_ARTIST_DIR)
            # unknown_dir=False 且无歌手 → 不创建歌手层

        # —— 第二层：专辑 ——
        if by_album:
            if album:
                album_name = self._sanitize_dir_name(album)
                if album_with_year and year:
                    album_name = f"{album_name} ({year})"
                parts.append(album_name)
            elif artist:
                # 有歌手但无专辑 → 归入精选
                parts.append(COMPILATION_DIR)
            elif unknown_dir:
                # 无歌手也无专辑 → 未知专辑
                parts.append(UNKNOWN_ALBUM_DIR)
            # unknown_dir=False 且无专辑 → 不创建专辑层

        target_dir = output_dir.joinpath(*parts) if parts else output_dir
        return str(target_dir)

    # ----------------------------------------------------------
    # 目录名清理
    # ----------------------------------------------------------
    def _sanitize_dir_name(self, name: str) -> str:
        """清理目录名中的非法字符。

        委托 :func:`utils.helpers.sanitize_filename` 处理 Windows 文件系统
        不允许的字符（``< > : " / \\ | ? *``），并去除首尾空格与点号。
        目录名与文件名在 Windows 下的非法字符限制一致，故复用同一清理函数。
        """
        cleaned = sanitize_filename(name)
        # sanitize_filename 对空串返回 "unknown"，此处保持一致
        return cleaned

    # ----------------------------------------------------------
    # 目标同名冲突保护
    # ----------------------------------------------------------
    @staticmethod
    def _resolve_target_conflict(src: Path, target: Path) -> Path:
        """目标路径已存在且非源文件时，添加序号后缀避免覆盖。

        例如 ``稻香-周杰伦.mp3`` 已存在 → ``稻香-周杰伦 (2).mp3``。

        Args:
            src: 源文件路径。
            target: 期望的目标路径。

        Returns:
            不冲突的最终目标路径（若目标不存在或即源文件本身，则原样返回）。
        """
        return make_unique_path(target, src)

    # ----------------------------------------------------------
    # 配置归一化
    # ----------------------------------------------------------
    def _get_organize_config(self, config: Optional[dict]) -> dict:
        """归一化整理配置。

        兼容两种入参：
        - 完整配置 dict（含 ``organize`` 子块）→ 取 ``config["organize"]``
        - ``organize`` 子块本身 → 直接使用

        为 ``None`` 时回退到实例默认配置。
        """
        if config is None:
            return self.default_config or {}
        if isinstance(config, dict) and "organize" in config and isinstance(config["organize"], dict):
            return config["organize"]
        return config
