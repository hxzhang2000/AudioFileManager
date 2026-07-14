"""LRCLIB Provider（§4.2）— 首选：同步歌词。

验证结果 (2026-07-12)：
- 搜索"稻香 周杰伦" → 20 条结果，含 2 个版本同步歌词
- ``GET /api/get?track_name=稻香&artist_name=周杰伦&album_name=魔杰座&duration=243``
  → 54 行同步歌词 [00:31.05] 到 [03:11.11]
- 完全免费，无 API Key，无限流
"""

from typing import Optional

from search.provider import SearchProvider, TrackMetadata


class LRCLIBProvider(SearchProvider):
    """LRCLIB 歌词提供者。

    能力：lyrics（见 SearchConfig.PROVIDER_CAPABILITIES）。
    优先返回同步歌词（syncedLyrics），其次返回纯文本歌词（plainLyrics）。
    不提供元数据与封面。
    """

    @property
    def provider_id(self) -> str:
        return "lrclib"

    @property
    def display_name(self) -> str:
        return "LRCLIB"

    def search_lyrics(self, title: str, artist: str, album: str = "") -> Optional[str]:
        # 1. 先搜索匹配
        url = "https://lrclib.net/api/search"
        params = {"track_name": title, "artist_name": artist}
        resp = self._request_with_rate_limit("lrclib", url, params=params, timeout=10)
        if resp is None:
            return None
        try:
            results = resp.json()
        except Exception:
            return None
        if not results:
            return None
        # 2. 优先选择有同步歌词的结果
        best = next((r for r in results if r.get("syncedLyrics")), results[0])
        return best.get("syncedLyrics") or best.get("plainLyrics")

    def search_metadata(self, title: str, artist: str) -> Optional[TrackMetadata]:
        # LRCLIB 仅提供歌词，不提供元数据
        return None

    def search_cover(self, title: str, artist: str, album: str = "") -> Optional[str]:
        # LRCLIB 不提供封面
        return None
