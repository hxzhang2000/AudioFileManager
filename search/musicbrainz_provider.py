"""MusicBrainz Provider（§4.3）— 降级备选。

验证结果：搜索"稻香 周杰伦" → 54 条录音记录。限流 1 req/s（官方硬性要求）。
封面通过 Cover Art Archive 获取：
``https://coverartarchive.org/release/{release_mbid}/front``。

速率控制：MusicBrainz 的 interval=1.1、concurrency=1 已在 RateLimiter 中
保证（§10.7.1），满足官方"每秒不超过 1 次请求"的硬性要求。
"""

from typing import Optional

from search.provider import SearchProvider, TrackMetadata
from version import APP_NAME, APP_VERSION


class MusicBrainzProvider(SearchProvider):
    """MusicBrainz + Cover Art Archive 提供者。

    能力：metadata / cover（见 SearchConfig.PROVIDER_CAPABILITIES）。
    严格的 1 请求/秒限流由 RateLimiter 的 interval=1.1 保证。
    请求头必须包含 User-Agent（官方要求）。
    """

    def _headers(self) -> dict:
        """MusicBrainz 官方要求请求头包含 User-Agent（含应用名与联系方式）"""
        return {"User-Agent": f"{APP_NAME}/{APP_VERSION} (MusicBrainz Provider; metadata+cover)"}

    @property
    def provider_id(self) -> str:
        return "musicbrainz"

    @property
    def display_name(self) -> str:
        return "MusicBrainz + Cover Art Archive"

    def search_metadata(self, title: str, artist: str) -> Optional[TrackMetadata]:
        url = "https://musicbrainz.org/ws/2/recording"
        params = {
            "query": f"artist:{artist} AND recording:{title}",
            "fmt": "json",
            "limit": 3,
        }
        resp = self._request_with_rate_limit(
            "musicbrainz", url, params=params, headers=self._headers(), timeout=10
        )
        if resp is None:
            return None
        try:
            data = resp.json()
        except Exception:
            return None

        recordings = data.get("recordings", [])
        if not recordings:
            return None

        recording = recordings[0]
        artists = recording.get("artist-credit", [])
        artist_name = artists[0].get("name", "") if artists else artist
        releases = recording.get("releases", [])
        album_name = releases[0].get("title", "") if releases else ""
        release_mbid = releases[0].get("id", "") if releases else ""
        release_date = releases[0].get("date", "") if releases else ""

        cover_url = None
        if release_mbid:
            cover_url = f"https://coverartarchive.org/release/{release_mbid}/front"

        return TrackMetadata(
            title=recording.get("title", title),
            artist=artist_name,
            album=album_name,
            cover_url=cover_url,
            release_date=release_date,
            release_year=self._parse_year(release_date),
            source="musicbrainz",
        )

    def search_cover(self, title: str, artist: str, album: str = "") -> Optional[str]:
        query = f"artist:{artist}"
        if album:
            query += f" AND release:{album}"
        url = "https://musicbrainz.org/ws/2/release"
        params = {"query": query, "fmt": "json", "limit": 1}
        resp = self._request_with_rate_limit(
            "musicbrainz", url, params=params, headers=self._headers(), timeout=10
        )
        if resp is None:
            return None
        try:
            data = resp.json()
        except Exception:
            return None

        releases = data.get("releases", [])
        if not releases:
            return None
        release_mbid = releases[0].get("id", "")
        return (
            f"https://coverartarchive.org/release/{release_mbid}/front"
            if release_mbid
            else None
        )

    def search_lyrics(self, title: str, artist: str, album: str = "") -> Optional[str]:
        # MusicBrainz 不提供歌词
        return None
