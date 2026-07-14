"""Meting Provider（§4.4）— 中文深度补充（使用公开实例）。

重要说明：Meting-API 没有统一的官方公共实例。以下是已验证情况的公开实例：

| 实例地址                              | 状态         | 说明                                       |
|:--------------------------------------|:------------:|:-------------------------------------------|
| https://api.injahow.cn/meting/        | 基础可用     | 支持 song/url/pic/lrc，搜索功能受限         |
| https://api.amarea.cn/meting/         | 搜索受限     | 同基座，不支持搜索                          |
| https://api.crowya.com/meting/        | 搜索受限     | 同基座，不支持搜索                          |
| https://metingapi.mo-app.cn/          | Google 418   | 触发反爬，国内 IP 可能被拦截                |
| https://metingapi.nanorocky.top/      | Google 418   | 触发 Cloudflare 反爬                        |
| https://meting.mikus.ink/             | 不稳定       | 偶尔可访问                                 |

推荐配置：在设置中允许用户自定义 Meting-API 地址，默认使用
``https://api.injahow.cn/meting/``。用户可切换到任何可用实例或自建实例。

速率控制：Meting 的 concurrency=1、interval=0.8 已在 RateLimiter 中保证
（§10.7.1），公开实例最严格，间隔最长。
"""

import re
from typing import Optional

from search.provider import SearchProvider, TrackMetadata


class MetingProvider(SearchProvider):
    """Meting-API 提供者。

    默认使用公开实例 ``https://api.injahow.cn/meting/``，
    用户可在设置中切换为其他公开实例或自建实例。

    配置方式：
    - api_url: Meting-API 地址（公开实例或自建实例）
    - server:  音源，netease（网易云）/ tencent（QQ音乐）

    能力：metadata / cover / lyrics / stream（见 SearchConfig.PROVIDER_CAPABILITIES）。
    部分实例不支持搜索功能（type=search），此时自动降级返回 None。
    """

    # 合法的 URL scheme 列表
    _VALID_SCHEMES = {"http", "https"}

    def __init__(
        self,
        api_url: str = "https://api.injahow.cn/meting/",
        server: str = "netease",
    ):
        self.api_url = self._validate_url(api_url)
        self.server = server

    @staticmethod
    def _validate_url(url: str) -> str:
        """校验 Meting-API 地址的合法性。

        - scheme 必须是 http 或 https
        - 必须有网络位置（netloc，如 api.example.com）
        - 拒绝 localhost / 127.0.0.1 / 私有 IP（SSRF 防护）

        Raises:
            ValueError: URL 不合法时抛出，由 _build_search_engine 捕获并禁用该 Provider。
        """
        from urllib.parse import urlparse
        parsed = urlparse(url)
        if parsed.scheme not in MetingProvider._VALID_SCHEMES:
            raise ValueError(f"Meting-API URL scheme 必须是 http/https，当前: {parsed.scheme!r}")
        if not parsed.netloc:
            raise ValueError(f"Meting-API URL 缺少网络地址: {url!r}")
        # SSRF 防护：拒绝本地回环与私有地址
        host = parsed.hostname or ""
        if host in ("localhost", "127.0.0.1", "::1", "0.0.0.0") or host.startswith("10.") or host.startswith("172.16.") or host.startswith("192.168."):
            raise ValueError(f"Meting-API 不允许使用本地/私有地址: {host!r}")
        return url.rstrip("/") + "/"

    @property
    def provider_id(self) -> str:
        return "meting"

    @property
    def display_name(self) -> str:
        return "Meting-API (网易云/QQ音乐)"

    def search_metadata(self, title: str, artist: str) -> Optional[TrackMetadata]:
        # 先通过搜索找歌曲 ID（部分实例不支持，则返回 None 自动降级）
        song_id, name, artists, album = self._search_song_id(title, artist)
        if not song_id:
            # 直接通过 song 类型尝试（备用方案，需已知 ID）
            return self._search_by_song(title, artist)
        cover_url = f"{self.api_url}?server={self.server}&type=pic&id={song_id}"
        return TrackMetadata(
            title=name or title,
            artist=artists or artist,
            album=album or "",
            cover_url=cover_url,
            source="meting",
        )

    def _search_song_id(self, title: str, artist: str):
        """搜索歌曲获取 ID，部分实例可能不支持搜索。

        Returns:
            (song_id, name, artists, album) 四元组；失败时各项均为 None。
        """
        try:
            params = {
                "server": self.server,
                "type": "search",
                "id": "0",
                "keyword": f"{title} {artist}",
            }
            # 部分实例用 id=关键词（netease 常见用法）
            if self.server == "netease":
                params2 = {
                    "server": self.server,
                    "type": "search",
                    "id": f"{title} {artist}",
                }
                try:
                    resp = self._request_with_rate_limit(
                        "meting", self.api_url, params=params2, timeout=5
                    )
                    data = resp.json() if resp is not None else None
                    if isinstance(data, list) and data:
                        return self._extract_first(data)
                except Exception:
                    pass
            resp = self._request_with_rate_limit(
                "meting", self.api_url, params=params, timeout=5
            )
            if resp is not None:
                data = resp.json()
                if isinstance(data, list) and data:
                    return self._extract_first(data)
        except Exception:
            pass
        return None, None, None, None

    def _extract_first(self, data):
        """从搜索结果列表中提取首条歌曲信息。

        Returns:
            (song_id, name, artists, album) 四元组。
        """
        item = data[0]
        artist_list = item.get("artist") or []
        if isinstance(artist_list, list):
            artists = " / ".join(str(a) for a in artist_list if a)
        else:
            artists = str(artist_list) if artist_list else ""
        return (
            item.get("id"),
            item.get("name") or "",
            artists,
            item.get("album") or "",
        )

    def _search_by_song(self, title: str, artist: str) -> Optional[TrackMetadata]:
        """备用方案：直接用 song 类型查（需已知 ID）。

        当前无可靠方式在未知 ID 时通过 song 类型查询，故返回 None 触发降级。
        """
        return None

    def search_cover(self, title: str, artist: str, album: str = "") -> Optional[str]:
        meta = self.search_metadata(title, artist)
        return meta.cover_url if meta else None

    def search_lyrics(self, title: str, artist: str, album: str = "") -> Optional[str]:
        # 搜索获取 ID
        song_id, _, _, _ = self._search_song_id(title, artist)
        if not song_id:
            return None
        try:
            params = {"server": self.server, "type": "lrc", "id": song_id}
            resp = self._request_with_rate_limit(
                "meting", self.api_url, params=params, timeout=5
            )
            if resp is None:
                return None
            return resp.text
        except Exception:
            return None
