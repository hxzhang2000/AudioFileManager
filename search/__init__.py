"""搜索模块 — 插件式搜索架构（§3）与各 Provider 实现（§4）。

公共导入入口：
- ``SearchConfig``:                搜索配置（§3.3）
- ``TrackMetadata``:               统一搜索结果模型（§3.1）
- ``SearchProvider``:              搜索接口抽象基类（§3.2）
- ``SearchEngine``:                搜索引擎调度器（§3.2）
- ``RateLimiter``:                 速率限制器（§10.7）
- ``iTunesSearchProvider``:        iTunes（§4.1）
- ``LRCLIBProvider``:              LRCLIB 歌词（§4.2）
- ``MusicBrainzProvider``:         MusicBrainz（§4.3）
- ``MetingProvider``:              Meting-API（§4.4）

典型用法::

    from search import (
        SearchConfig, SearchEngine,
        iTunesSearchProvider, LRCLIBProvider,
        MusicBrainzProvider, MetingProvider,
    )

    config = SearchConfig()
    engine = SearchEngine(config)
    engine.register(iTunesSearchProvider())
    engine.register(LRCLIBProvider())
    engine.register(MusicBrainzProvider())
    engine.register(MetingProvider(config.meting_api_url, config.meting_server))

    meta = engine.search_metadata("稻香", "周杰伦")
"""

from search.config import SearchConfig
from search.rate_limiter import RateLimiter
from search.provider import SearchEngine, SearchProvider, TrackMetadata
from search.itunes_provider import iTunesSearchProvider
from search.lrclib_provider import LRCLIBProvider
from search.musicbrainz_provider import MusicBrainzProvider
from search.meting_provider import MetingProvider

__all__ = [
    "SearchConfig",
    "RateLimiter",
    "SearchEngine",
    "SearchProvider",
    "TrackMetadata",
    "iTunesSearchProvider",
    "LRCLIBProvider",
    "MusicBrainzProvider",
    "MetingProvider",
]
