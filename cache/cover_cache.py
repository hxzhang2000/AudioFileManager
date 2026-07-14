"""封面缓存模块 — 按 artist+album 键缓存专辑封面图片。

缓存目录默认为 config.settings.COVER_CACHE（见开发方案 §9.1）。
缓存文件命名规则：{artist}_{album}.jpg（文件名经 sanitize_filename 处理）。
"""

from pathlib import Path
from typing import Optional

from config import settings
from utils.helpers import sanitize_filename
from utils.logger import logger


class CoverCache:
    """封面缓存。

    以 ``artist + album`` 作为键，将封面图片二进制数据持久化到磁盘，
    避免对同一专辑反复发起网络请求。

    Args:
        cache_dir: 缓存目录路径，默认使用 ``config.settings.COVER_CACHE``。
    """

    def __init__(self, cache_dir: Optional[str | Path] = None):
        # 未指定目录时回退到全局配置中的封面缓存目录
        self.cache_dir: Path = Path(cache_dir) if cache_dir else settings.COVER_CACHE
        # 确保缓存目录存在
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    def _key_path(self, artist: str, album: str) -> Path:
        """根据 artist+album 生成缓存文件路径。

        文件名格式：{artist}_{album}.jpg，各字段经 sanitize_filename 处理，
        空值以 "unknown" 兜底，避免生成空文件名。
        """
        artist = sanitize_filename(artist or "unknown")
        album = sanitize_filename(album or "unknown")
        return self.cache_dir / f"{artist}_{album}.jpg"

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------
    def get(self, artist: str, album: str) -> Optional[bytes]:
        """按 artist+album 键查找缓存。

        Returns:
            封面图片的二进制数据；缓存不存在或读取失败时返回 None。
        """
        path = self._key_path(artist, album)
        if not path.exists():
            return None
        try:
            return path.read_bytes()
        except OSError as e:
            logger.warning(f"读取封面缓存失败: {path} -> {e}")
            return None

    def put(self, artist: str, album: str, data: bytes) -> None:
        """保存封面到缓存。

        Args:
            data: 封面图片的二进制数据；为空时跳过写入。
        """
        if not data:
            return
        path = self._key_path(artist, album)
        try:
            path.write_bytes(data)
        except OSError as e:
            logger.warning(f"写入封面缓存失败: {path} -> {e}")

    def get_path(self, artist: str, album: str) -> Optional[Path]:
        """获取缓存文件路径。

        Returns:
            缓存文件存在时返回其 Path，否则返回 None。
        """
        path = self._key_path(artist, album)
        return path if path.exists() else None

    def clear(self) -> None:
        """清空缓存目录下的所有文件。"""
        removed = 0
        for item in self.cache_dir.iterdir():
            if item.is_file():
                try:
                    item.unlink()
                    removed += 1
                except OSError as e:
                    logger.warning(f"删除封面缓存失败: {item} -> {e}")
        logger.info(f"已清空封面缓存目录 {self.cache_dir}，共删除 {removed} 个文件")
