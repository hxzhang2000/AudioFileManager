"""手动搜索服务（§7.4）。

用户在详情面板点击「网络搜索信息」按钮时触发手动补全流程。

与批处理搜索复用相同的 SearchProvider 链，但逻辑不同：
- 手动搜索只搜 1 首，不需要回退
- 同时并行搜索元数据 + 歌词 + 封面（提升响应速度）
- 所有结果展示在前端，由用户决定是否填入

执行模型：同步方法，在 QThread worker 线程内调用，
并发使用 ``ThreadPoolExecutor``（见 §3 执行模型）提交 3 个同步子任务，
而非 asyncio。
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    # 仅用于类型检查，运行时不导入，避免对尚未实现的 search 引擎产生硬依赖
    from search.provider import SearchEngine, TrackMetadata  # noqa: F401

logger = logging.getLogger(__name__)


@dataclass
class SearchResult:
    """手动搜索结果条目（供前端展示与「填入元数据」操作）。"""

    title: Optional[str] = None
    artist: Optional[str] = None
    album: Optional[str] = None
    release_year: Optional[str] = None
    genre: Optional[str] = None
    track_number: Optional[str] = None
    cover_data: Optional[bytes] = None        # 封面图片二进制
    cover_source: Optional[str] = None        # 封面来源说明
    lyrics_text: Optional[str] = None         # LRC 格式歌词文本
    lyrics_source: Optional[str] = None       # 歌词来源说明
    source: str = ""                          # 数据来源（如 "iTunes"）


class ManualSearchService:
    """手动搜索服务 — 供详情面板「网络搜索信息」按钮调用。"""

    def __init__(self, search_engine: "SearchEngine"):
        self.engine = search_engine

    def search(self, title: str, artist: str) -> list[SearchResult]:
        """并行搜索元数据、歌词和封面，返回候选列表（最多 3 条）。

        同步方法：在 QThread worker 线程内调用，故并发使用
        ``ThreadPoolExecutor`` 提交 3 个同步子任务，而非 asyncio。
        """
        with ThreadPoolExecutor(max_workers=3) as ex:
            f_meta = ex.submit(self._safe_search_metadata, title, artist)
            f_lyrics = ex.submit(self._safe_search_lyrics, title, artist)
            f_cover = ex.submit(self._safe_fetch_cover, title, artist)
            metadata = f_meta.result()
            lyrics = f_lyrics.result()
            cover = f_cover.result()

        # 合并为 SearchResult 列表返回，前端展示在搜索结果预览框中
        return self._merge_results(metadata, lyrics, cover)

    # ------------------------------------------------------------
    # 并行子任务的安全封装（捕获异常，避免线程池内抛错导致 result() 失败）
    # ------------------------------------------------------------
    def _safe_search_metadata(
        self, title: str, artist: str
    ) -> "Optional[TrackMetadata]":
        try:
            return self.engine.search_metadata(title, artist)
        except Exception as e:
            logger.warning(f"手动搜索元数据失败: {e}")
            return None

    def _safe_search_lyrics(self, title: str, artist: str) -> Optional[str]:
        try:
            return self.engine.search_lyrics(title, artist)
        except Exception as e:
            logger.warning(f"手动搜索歌词失败: {e}")
            return None

    def _safe_fetch_cover(
        self, title: str, artist: str
    ) -> Optional[bytes]:
        try:
            return self.engine.fetch_cover(title, artist, album="")
        except Exception as e:
            logger.warning(f"手动搜索封面失败: {e}")
            return None

    # ------------------------------------------------------------
    # 结果合并
    # ------------------------------------------------------------
    def _merge_results(
        self,
        metadata: "Optional[TrackMetadata]",
        lyrics: Optional[str],
        cover: Optional[bytes],
    ) -> list[SearchResult]:
        """把元数据 / 歌词 / 封面合并为 ``SearchResult`` 列表（最多 3 条）。

        合并策略：
        - 若 ``metadata`` 命中，以其为主结果，附加歌词/封面来源说明；
        - 若 ``metadata`` 未命中但歌词或封面有结果，仍构造一条结果供用户参考；
        - 全部为空时返回空列表。

        Note:
            ``engine.search_metadata`` 当前返回单条结果，故列表通常为 1 条。
            上限 3 条为前端展示约束，预留多候选扩展空间。
        """
        results: list[SearchResult] = []

        if metadata is not None:
            result = SearchResult(
                title=metadata.title,
                artist=metadata.artist,
                album=metadata.album or None,
                release_year=str(metadata.release_year) if metadata.release_year else None,
                genre=metadata.genre,
                track_number=str(metadata.track_number) if metadata.track_number else None,
                cover_data=cover,
                cover_source=("网络下载" if cover else None),
                lyrics_text=lyrics,
                lyrics_source=("网络搜索" if lyrics else None),
                source=metadata.source or "",
            )
            results.append(result)
        elif lyrics or cover:
            # 元数据未命中但歌词/封面可用，构造一条仅含歌词/封面的结果
            result = SearchResult(
                cover_data=cover,
                cover_source=("网络下载" if cover else None),
                lyrics_text=lyrics,
                lyrics_source=("网络搜索" if lyrics else None),
                source="",
            )
            results.append(result)

        # 上限 3 条（当前实现至多 1 条，预留多候选扩展空间）
        return results[:3]
