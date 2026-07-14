"""歌词缓存模块 — 按 title+artist 键缓存 LRC 歌词文本。

缓存目录默认为 config.settings.LYRICS_CACHE（见开发方案 §9.1）。
缓存文件命名规则：{title}_{artist}.lrc（文件名经 sanitize_filename 处理）。
"""

from pathlib import Path
from typing import Optional

from config import settings
from utils.helpers import sanitize_filename
from utils.logger import logger


class LrcCache:
    """歌词缓存。

    以 ``title + artist`` 作为键，将 LRC 歌词文本持久化到磁盘，
    避免对同一首歌反复发起网络请求。

    Args:
        cache_dir: 缓存目录路径，默认使用 ``config.settings.LYRICS_CACHE``。
    """

    def __init__(self, cache_dir: Optional[str | Path] = None):
        # 未指定目录时回退到全局配置中的歌词缓存目录
        self.cache_dir: Path = Path(cache_dir) if cache_dir else settings.LYRICS_CACHE
        # 确保缓存目录存在
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    def _key_path(self, title: str, artist: str) -> Path:
        """根据 title+artist 生成缓存文件路径。

        文件名格式：{title}_{artist}.lrc，各字段经 sanitize_filename 处理，
        空值以 "unknown" 兜底，避免生成空文件名。
        """
        title = sanitize_filename(title or "unknown")
        artist = sanitize_filename(artist or "unknown")
        return self.cache_dir / f"{title}_{artist}.lrc"

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------
    def get(self, title: str, artist: str) -> Optional[str]:
        """按 title+artist 键查找缓存。

        Returns:
            歌词文本字符串；缓存不存在或读取失败时返回 None。
        """
        path = self._key_path(title, artist)
        if not path.exists():
            return None
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            logger.warning(f"读取歌词缓存失败: {path} -> {e}")
            return None

    def put(self, title: str, artist: str, text: str) -> None:
        """保存歌词到缓存。

        Args:
            text: LRC 歌词文本；为 None 时跳过写入。
        """
        if text is None:
            return
        path = self._key_path(title, artist)
        try:
            path.write_text(text, encoding="utf-8")
        except OSError as e:
            logger.warning(f"写入歌词缓存失败: {path} -> {e}")

    def clear(self) -> None:
        """清空缓存目录下的所有文件。"""
        removed = 0
        for item in self.cache_dir.iterdir():
            if item.is_file():
                try:
                    item.unlink()
                    removed += 1
                except OSError as e:
                    logger.warning(f"删除歌词缓存失败: {item} -> {e}")
        logger.info(f"已清空歌词缓存目录 {self.cache_dir}，共删除 {removed} 个文件")
