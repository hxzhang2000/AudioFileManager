"""iTunes Search Provider（§4.1）— 首选：元数据 + 封面 + 试听。

验证结果 (2026-07-12)：
- 搜索"稻香 周杰伦" → trackName="稻香", artistName="周杰伦"
- 返回专辑《魔杰座》(2008-10-14)、高清封面、30 秒 Preview 音频
- 无需 API Key，无明确限流
"""

from typing import Optional

from search.provider import SearchProvider, TrackMetadata


class iTunesSearchProvider(SearchProvider):
    """Apple iTunes Search 提供者。

    能力：metadata / cover / preview（见 SearchConfig.PROVIDER_CAPABILITIES）。
    无需 API Key。封面 URL 可由 ``search_metadata`` 直接返回，调用方
    （SearchEngine.fetch_cover / enrich_cover_and_lyrics）应优先复用
    已有 cover_url，避免重复请求 iTunes API。
    """

    @property
    def provider_id(self) -> str:
        return "itunes"

    @property
    def display_name(self) -> str:
        return "Apple iTunes Search"

    def search_metadata(self, title: str, artist: str) -> Optional[TrackMetadata]:
        url = "https://itunes.apple.com/search"
        params = {
            "term": f"{title} {artist}",
            "media": "music",
            "country": "cn",
            "limit": 5,
        }
        resp = self._request_with_rate_limit("itunes", url, params=params, timeout=10)
        if resp is None:
            return None
        try:
            data = resp.json()
        except Exception:
            return None
        results = data.get("results", [])
        if not results:
            return None

        track = results[0]
        # 封面升级到 600x600（iTunes 默认返回 100x100）
        cover_url = track.get("artworkUrl100", "")
        if cover_url:
            cover_url = cover_url.replace("100x100bb", "600x600bb")

        return TrackMetadata(
            title=track.get("trackName", title),
            artist=track.get("artistName", artist),
            album=track.get("collectionName", ""),
            cover_url=cover_url,
            release_date=track.get("releaseDate", ""),
            release_year=self._parse_year(track.get("releaseDate", "")),
            genre=track.get("primaryGenreName", ""),
            track_number=track.get("trackNumber", 0),
            track_count=track.get("trackCount", 0),
            preview_url=track.get("previewUrl"),  # iTunes 30s Preview
            source="itunes",
        )

    def search_cover(self, title: str, artist: str, album: str = "") -> Optional[str]:
        # 注意：调用方 SearchEngine.fetch_cover / enrich_cover_and_lyrics 应优先
        # 复用已有 search_metadata 结果中的 cover_url，避免重复请求 iTunes API。
        # 此处作为独立封面搜索的兜底实现。
        meta = self.search_metadata(title, artist)
        return meta.cover_url if meta else None

    def search_lyrics(self, title: str, artist: str, album: str = "") -> Optional[str]:
        # iTunes 不提供歌词
        return None
