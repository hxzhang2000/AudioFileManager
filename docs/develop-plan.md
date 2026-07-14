# 音频文件管理工具 — 开发方案

> 一个 Windows 桌面软件，自动整理、规范命名、补充元数据的音频文件管理工具

**版本**: v5.9 · 最后更新: 2026-07-13

---

## 一、项目概述

### 1.1 目标

制作一款 Windows 桌面软件，解决下载音乐文件后的三大痛点：

| 痛点 | 现状 | 目标 |
|:-----|:-----|:-----|
| **目录混乱** | 几百首歌曲堆在一个文件夹 | 自动按 `歌手→专辑→歌曲` 整理 |
| **文件名不统一** | 各种格式：`稻香.mp3`、`周杰伦 - 稻香.mp3`、`[123]稻香.mp3` | 统一为 `歌曲名-歌手.mp3` |
| **元数据缺失** | ID3 标签空白或错误 | 自动搜索补充专辑、年代、封面、歌词 |

### 1.2 核心功能

| 功能 | 说明 |
|:-----|:------|
| **目录化整理** | 按 `歌手→专辑→歌曲文件` 自动归类 |
| **文件名标准化** | 统一为 `歌曲名 - 歌手.ext` 格式 |
| **元数据补全** | 自动搜索并写入 ID3 标签：专辑、年份、流派、封面 |
| **歌词获取** | 写入 LRC 歌词文件或嵌入 ID3 歌词标签 |
| **试听预览** | 双击歌曲弹出试听窗口，播放音频+滚动歌词 |
| **歌词编辑** | 试听中可通过拖拽歌词调整时间轴，保存到文件 |
| **文件详情面板** | 右侧可收起面板，显示当前选中文件的完整元数据 |
| **批处理** | 支持选择文件夹批量处理 |
| **搜索配置** | 可配置搜索接口优先级和回退 |

---

## 二、技术选型

| 组件 | 选型 | 说明 |
|:-----|:-----|:------|
| UI 框架 | **PyQt6** | 成熟的 Windows 桌面 UI 框架 |
| 音频播放 | **PyQt6.QtMultimedia** | QMediaPlayer 播放本地音频文件，无需额外依赖 |
| 音频处理 | **mutagen** | 读写 MP3/FLAC/OGG 等格式的 ID3 标签 |
| 网络请求 | **httpx** | 同步 HTTP 客户端（httpx.Client），支持超时/重试，运行于 QThread 工作线程 |
| 图片处理 | **Pillow** | 封面图片缩放、格式转换 |
| 打包部署 | **PyInstaller** | 打包为单 exe 或安装程序 |
| 配置存储 | **JSON 文件** | 用户配置保存在 `%APPDATA%/AudioFileManager/` |

> **许可证注意**：PyQt6 使用 GPLv3。若需闭源商业分发，需购买 Riverbank 商业许可证或切换到 PySide6（LGPL）。本方案默认面向开源（GPLv3）场景。

---

## 三、搜索接口架构（核心需求）

> **执行模型决策（v5.7 统一）**：全文档采用**同步模型**。
> - 网络 I/O 一律使用**同步 `httpx`**（`httpx.get` / `httpx.Client`）。
> - 批量处理运行在 **`QThread` 工作线程**（`BatchProcessor`）中，网络/文件阻塞发生在工作线程，不阻塞主 GUI 线程。
> - 速率控制 `RateLimiter` 使用 **`threading`**（`threading.Semaphore` + `time.sleep`），非 `asyncio`。
> - 手动搜索的并行（元数据 / 歌词 / 封面）使用 **`concurrent.futures.ThreadPoolExecutor`**，非 `asyncio.gather`。
> - 文档早期出现的 `*_async` 方法（如 `search_metadata_async`、`search_lyrics_async`）一律删除，统一为同步方法；原 §7.4 / §7.7 的 `await` / `asyncio.gather` 调用改为同步 + `ThreadPoolExecutor`。
> 这样 §3~§4 的同步 Provider、§10.4 的 QThread 批处理、§10.7 的 `RateLimiter` 三处保持一致，避免「同步引擎 + 异步 Provider」的分裂。

### 3.1 接口数据模型

```python
from dataclasses import dataclass, field
from typing import Optional

@dataclass
class TrackMetadata:
    """统一搜索结果模型"""
    title: str                    # 歌曲名
    artist: str                   # 歌手名
    album: str = ""               # 专辑名
    cover_url: Optional[str] = None   # 封面 URL
    release_year: Optional[int] = None  # 发行年份
    release_date: Optional[str] = None  # 发行日期
    genre: Optional[str] = None   # 流派
    lyrics: Optional[str] = None  # 歌词（LRC格式或纯文本）
    has_synced_lyrics: bool = False  # 是否为同步歌词
    cover_data: Optional[bytes] = None  # 已下载的封面二进制（批处理落盘用）
    lyrics_text: Optional[str] = None   # 已获取的歌词文本（批处理落盘用）
    track_number: int = 0         # 曲目编号
    track_count: int = 0          # 专辑总曲目
    source: str = ""              # 数据来源
    preview_url: Optional[str] = None  # 试听URL（iTunes Preview）
```

### 3.2 插件式搜索架构

```python
from abc import ABC, abstractmethod
import time
import httpx

class SearchProvider(ABC):
    """搜索接口抽象类"""

    @property
    @abstractmethod
    def provider_id(self) -> str: ...

    @property
    @abstractmethod
    def display_name(self) -> str: ...

    @abstractmethod
    def search_metadata(self, title: str, artist: str) -> Optional[TrackMetadata]: ...

    @abstractmethod
    def search_cover(self, title: str, artist: str, album: str = "") -> Optional[str]:
        """返回封面图片 **URL（str）**；二进制下载由 SearchEngine.fetch_cover 负责。"""
        ...

    @abstractmethod
    def search_lyrics(self, title: str, artist: str, album: str = "") -> Optional[str]: ...

    # —— 同步速率控制请求封装（v5.7 统一，基于 §10.7 RateLimiter）——
    _rate_limiter: "RateLimiter" = None  # 模块级单例，所有 Provider 共享
    _clients: dict[str, "httpx.Client"] = {}  # 按 provider_id 缓存持久化 Client，复用连接池

    @staticmethod
    def _get_rate_limiter() -> "RateLimiter":
        """获取全局 RateLimiter 单例。按类属性缓存，避免 classmethod 误导。"""
        if SearchProvider._rate_limiter is None:
            from search.rate_limiter import RateLimiter
            SearchProvider._rate_limiter = RateLimiter()
        return SearchProvider._rate_limiter

    def _request_with_rate_limit(
        self, provider_id: str, url: str, params: dict = None,
        headers: dict = None, timeout: int = 5,
    ) -> Optional["httpx.Response"]:
        """带速率限制与自动重试的**同步** HTTP GET。

        所有 Provider 子类应经此方法发起网络请求；§10.7 的 ``RateLimiter``
        负责按 Provider 独立计数、控制并发与间隔、处理 429 封禁。
        内部使用持久化 ``httpx.Client``（按 provider_id 缓存，复用 TLS 连接池），
        运行在 QThread 工作线程中，同步阻塞不卡 GUI。
        失败时返回 ``None``，由调用方决定降级策略。
        """
        limiter = self._get_rate_limiter()
        cfg = limiter.get_provider_config(provider_id)
        # 持久化 Client，避免每次请求重建 TCP/TLS 连接
        client = SearchProvider._clients.get(provider_id)
        if client is None:
            client = httpx.Client()
            SearchProvider._clients[provider_id] = client
        for attempt in range(cfg["max_retries"] + 1):
            limiter.acquire(provider_id)
            try:
                resp = client.get(url, params=params, headers=headers, timeout=timeout)
                if resp.status_code == 429:
                    retry_after = int(resp.headers.get("Retry-After", "60"))
                    limiter.report_429(provider_id, retry_after)
                    logger.warning(f"[{provider_id}] 429 限速，等待 {retry_after}s")
                    continue
                resp.raise_for_status()
                return resp
            except httpx.TimeoutException:
                logger.warning(f"[{provider_id}] 超时 (第 {attempt+1} 次)")
                if attempt == cfg["max_retries"]:
                    return None
                time.sleep(2 ** attempt)
            except httpx.HTTPStatusError as e:
                logger.warning(f"[{provider_id}] HTTP {e.response.status_code}")
                if attempt == cfg["max_retries"]:
                    return None
                time.sleep(2 ** attempt)
            except Exception as e:
                logger.warning(f"[{provider_id}] 请求异常: {e}")
                if attempt == cfg["max_retries"]:
                    return None
        return None


class SearchEngine:
    """
    搜索引擎 — 按配置的优先级顺序调度各 Provider。

    核心能力：
    1. 按配置顺序依次尝试 Provider
    2. 当前 Provider 失败时自动降级到下一个
    3. 可配置每个 Provider 的启用/禁用
    4. 可动态调整优先级顺序
    """

    def __init__(self, config: "SearchConfig"):
        self.config = config
        self.providers: dict[str, SearchProvider] = {}

    def register(self, provider: SearchProvider):
        self.providers[provider.provider_id] = provider

    def search_metadata(self, title: str, artist: str) -> Optional[TrackMetadata]:
        """按优先级搜索元数据"""
        for pid in self.config.active_order:
            provider = self.providers.get(pid)
            if not provider or not self.config.is_enabled(pid):
                continue
            try:
                result = provider.search_metadata(title, artist)
                if result:
                    result.source = pid
                    return result
            except Exception as e:
                logger.warning(f"[{pid}] 搜索失败: {e}")
        return None

    def search_lyrics(self, title: str, artist: str, album: str = "") -> Optional[str]:
        """按优先级搜索歌词"""
        for pid in self.config.active_order:
            provider = self.providers.get(pid)
            if not provider or not self.config.is_enabled(pid):
                continue
            if not self.config.supports_lyrics(pid):
                continue
            try:
                lrc = provider.search_lyrics(title, artist, album)
                if lrc:
                    return lrc
            except Exception as e:
                logger.warning(f"[{pid}] 歌词搜索失败: {e}")
        return None

    def fetch_cover(self, title: str, artist: str, album: str = "") -> Optional[bytes]:
        """搜索并下载封面图片（返回二进制数据）。
        
        与 Provider 层的 ``search_cover``（返回 URL）命名区分：
        Engine 层负责下载，Provider 层只返回 URL。
        """
        for pid in self.config.active_order:
            provider = self.providers.get(pid)
            if not provider or not self.config.is_enabled(pid):
                continue
            if not self.config.supports_cover(pid):
                continue
            try:
                cover_url = provider.search_cover(title, artist, album)
                if cover_url:
                    return self._download_cover(cover_url, pid)
            except Exception as e:
                logger.warning(f"[{pid}] 封面搜索失败: {e}")
        return None

    def search_preview(self, title: str, artist: str) -> Optional[str]:
        """搜索试听音频URL"""
        for pid in self.config.active_order:
            provider = self.providers.get(pid)
            if not provider or not self.config.is_enabled(pid):
                continue
            if not self.config.supports_preview(pid):
                continue
            try:
                meta = provider.search_metadata(title, artist)
                if meta and meta.preview_url:
                    return meta.preview_url
            except Exception as e:
                logger.warning(f"[{pid}] 试听搜索失败: {e}")
        return None

    def _download_cover(self, url: str, provider_id: str = "unknown") -> Optional[bytes]:
        """下载封面图片为字节数据。

        如果是已知 Provider（如 musicbrainz / meting），
        封面下载也计入该 Provider 的速率配额。
        使用持久化 httpx.Client（按 provider_id 缓存，复用 TLS 连接池），
        与 _request_with_rate_limit 的连接池策略保持一致。
        """
        try:
            limiter = SearchProvider._get_rate_limiter()
            cfg = limiter.get_provider_config(provider_id)
            # 复用持久化 Client（与 _request_with_rate_limit 共享连接池策略）
            client = SearchProvider._clients.get(provider_id)
            if client is None:
                client = httpx.Client()
                SearchProvider._clients[provider_id] = client
            resp = None
            for attempt in range(cfg.get("max_retries", 2) + 1):
                limiter.acquire(provider_id)
                try:
                    resp = client.get(url, timeout=10)
                    resp.raise_for_status()
                    break
                except httpx.HTTPStatusError as e:
                    logger.warning(f"[{provider_id}] 封面下载 HTTP {e.response.status_code}")
                    if attempt == cfg.get("max_retries", 2):
                        return None
                    time.sleep(2 ** attempt)
                except httpx.TimeoutException:
                    logger.warning(f"[{provider_id}] 封面下载超时 (第 {attempt+1} 次)")
                    if attempt == cfg.get("max_retries", 2):
                        return None
                    time.sleep(2 ** attempt)
            return resp.content if resp is not None else None
        except Exception as e:
            logger.warning(f"封面下载异常: {e}")
            return None

    def enrich_cover_and_lyrics(
        self,
        meta: TrackMetadata,
        title: str,
        artist: str,
        album: str = "",
    ) -> TrackMetadata:
        """批处理用：为已识别的 meta 补全封面二进制与歌词文本。

        约定（v5.7 统一）：
        - ``SearchProvider.search_cover`` 返回**封面 URL（str）**；本方法负责下载为二进制。
        - ``SearchProvider.search_lyrics`` 返回**歌词文本（str）**；同步歌词以 '[' 开头。
        - 若 ``meta.cover_url`` 已有值（如 iTunes search_metadata 已返回封面 URL），
          优先直接下载该 URL，避免重复调用 search_cover 造成冗余 API 请求。
        - 仅当已有 cover_url 下载失败时，才回退到各 Provider 的 search_cover。
        歌词始终按 providers 顺序尝试；任一 Provider 成功即采用。
        失败不抛异常（封面/歌词为可选项），仅记录警告。
        """
        # —— 封面补全 ——
        if not meta.cover_data:
            # 优先复用已有 cover_url（如 iTunes search_metadata 已返回）
            if meta.cover_url:
                cover_bytes = self._download_cover(meta.cover_url, meta.source or "unknown")
                if cover_bytes:
                    meta.cover_data = cover_bytes
            # 已有 URL 下载失败或无 URL → 回退到各 Provider 的 search_cover
            if not meta.cover_data:
                for pid in self.config.active_order:
                    provider = self.providers.get(pid)
                    if not provider or not self.config.is_enabled(pid):
                        continue
                    if not self.config.supports_cover(pid):
                        continue
                    try:
                        cover_url = provider.search_cover(title, artist, album)
                        if cover_url:
                            cover_bytes = self._download_cover(cover_url, pid)
                            if cover_bytes:
                                meta.cover_url = cover_url
                                meta.cover_data = cover_bytes
                                break
                    except Exception as e:
                        logger.warning(f"[{pid}] 封面补全失败: {e}")

        # —— 歌词补全 ——
        if not meta.lyrics_text:
            for pid in self.config.active_order:
                provider = self.providers.get(pid)
                if not provider or not self.config.is_enabled(pid):
                    continue
                if not self.config.supports_lyrics(pid):
                    continue
                try:
                    lyrics = provider.search_lyrics(title, artist, album)
                except Exception as e:
                    logger.warning(f"[{pid}] 歌词补全失败: {e}")
                    lyrics = None
                if lyrics:
                    meta.lyrics_text = lyrics
                    meta.lyrics = lyrics
                    meta.has_synced_lyrics = lyrics.strip().startswith("[")
                    break
        return meta
```

### 3.3 搜索配置模型

```python
@dataclass
class SearchConfig:
    """
    搜索配置，存储在 %APPDATA%/AudioFileManager/config.json 中。
    支持用户在设置界面实时修改。
    """
    provider_order: list[str] = field(
        default_factory=lambda: ["itunes", "lrclib", "musicbrainz", "meting"]
    )

    provider_enabled: dict[str, bool] = field(
        default_factory=lambda: {
            "itunes": True,
            "lrclib": True,
            "musicbrainz": True,
            "meting": False,   # 默认不启用（部分公开实例不稳定）
        }
    )

    # Provider 特性标记（哪些接口支持什么能力）
    PROVIDER_CAPABILITIES: dict[str, set[str]] = field(
        default_factory=lambda: {
            "itunes": {"metadata", "cover", "preview"},
            "lrclib": {"lyrics"},
            "musicbrainz": {"metadata", "cover"},
            "meting": {"metadata", "cover", "lyrics", "stream"},
        }
    )

    # Meting-API 配置：用户可指定任意公开或自建实例
    meting_api_url: str = "https://api.injahow.cn/meting/"   # 默认公开实例
    meting_server: str = "netease"                            # 默认音源

    request_timeout: int = 10
    total_timeout: int = 30

    def is_enabled(self, provider_id: str) -> bool:
        return self.provider_enabled.get(provider_id, False)

    def supports_lyrics(self, provider_id: str) -> bool:
        return "lyrics" in self.PROVIDER_CAPABILITIES.get(provider_id, set())

    def supports_cover(self, provider_id: str) -> bool:
        return "cover" in self.PROVIDER_CAPABILITIES.get(provider_id, set())

    def supports_preview(self, provider_id: str) -> bool:
        return "preview" in self.PROVIDER_CAPABILITIES.get(provider_id, set())

    @property
    def active_order(self) -> list[str]:
        return [p for p in self.provider_order if self.is_enabled(p)]
```

### 3.4 默认搜索流程

```
用户选择歌曲文件 → 从文件名解析出 (歌曲名, 歌手)

    ↓ 搜索元数据
    iTunes Search ──成功──→ 返回 TrackMetadata（含封面URL、专辑、年份、PreviewURL）
       │失败
       ↓
    MusicBrainz ──成功──→ 返回 TrackMetadata + Cover Art Archive 封面
       │失败
       ↓
    Meting-API（如启用）──成功──→ 返回元数据（中文补充）
       │失败
       ↓
    [返回空，记录日志]

    ↓ 搜索歌词（与元数据搜索独立，批处理中按顺序执行；手动搜索中并行）
    LRCLIB ──成功──→ 返回 syncedLRC 或 plainLyrics
       │失败
       ↓
    Meting-API（如启用）──成功──→ 返回歌词
       │失败
       ↓
    [无歌词，留空]

    ↓ 写入文件（批处理模式）
    写入 ID3 标签（标题、歌手、专辑、年份、流派、封面）
    写入 LRC 歌词文件（与歌曲同目录）

    ↓ 试听（手动触发）
    用户双击/右键→试听 → 弹出 AuditionDialog
    用 PreviewURL 或本地文件播放
    滚动歌词同步显示
    允许拖拽调整歌词时间
    保存调整后的歌词到文件
```

---

## 四、各 Provider 实现

### 4.1 iTunesSearchProvider（首选：元数据+封面+试听）

**验证结果 (2026-07-12)**:
- 搜索"稻香 周杰伦" → 返回 trackName="稻香", artistName="周杰伦"
- 返回专辑《魔杰座》(2008-10-14)、高清封面、30秒 Preview 音频
- 无需 API Key，无明确限流

```python
class iTunesSearchProvider(SearchProvider):
    @property
    def provider_id(self) -> str: return "itunes"
    @property
    def display_name(self) -> str: return "Apple iTunes Search"

    def search_metadata(self, title: str, artist: str) -> Optional[TrackMetadata]:
        url = "https://itunes.apple.com/search"
        params = {"term": f"{title} {artist}", "media": "music",
                  "country": "cn", "limit": 5}
        resp = self._request_with_rate_limit("itunes", url, params=params, timeout=10)
        if resp is None:
            return None
        data = resp.json()
        results = data.get("results", [])
        if not results:
            return None

        track = results[0]
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
            preview_url=track.get("previewUrl"),   # iTunes 30s Preview
            source="itunes",
        )

    def search_cover(self, title, artist, album=""):
        # 注意：调用方 SearchEngine.fetch_cover 应优先复用已有 search_metadata
        # 结果中的 cover_url，避免重复请求 iTunes API。此处作为独立封面搜索的兜底实现。
        meta = self.search_metadata(title, artist)
        return meta.cover_url if meta else None

    def search_lyrics(self, title, artist, album=""):
        return None
```

### 4.2 LRCLIBProvider（首选：同步歌词）

**验证结果 (2026-07-12)**:
- 搜索"稻香 周杰伦" → 20条结果，含2个版本同步歌词
- `GET /api/get?track_name=稻香&artist_name=周杰伦&album_name=魔杰座&duration=243`
  → 54行同步歌词 [00:31.05] 到 [03:11.11]
- 完全免费，无API Key，无限流

```python
class LRCLIBProvider(SearchProvider):
    @property
    def provider_id(self) -> str: return "lrclib"
    @property
    def display_name(self) -> str: return "LRCLIB"

    def search_lyrics(self, title: str, artist: str, album: str = "") -> Optional[str]:
        # 1. 先搜索匹配
        url = "https://lrclib.net/api/search"
        params = {"track_name": title, "artist_name": artist}
        resp = self._request_with_rate_limit("lrclib", url, params=params, timeout=10)
        if resp is None:
            return None
        results = resp.json()
        if not results:
            return None
        # 2. 优先选择有同步歌词的结果
        best = next((r for r in results if r.get("syncedLyrics")), results[0])
        return best.get("syncedLyrics") or best.get("plainLyrics")

    def search_metadata(self, title, artist):
        return None
    def search_cover(self, title, artist, album=""):
        return None
```

### 4.3 MusicBrainzProvider（降级备选）

**验证结果**: 搜索"稻香 周杰伦" → 54条录音记录。限流 1 req/s。

```python
class MusicBrainzProvider(SearchProvider):
    @property
    def provider_id(self) -> str: return "musicbrainz"
    @property
    def display_name(self) -> str: return "MusicBrainz + Cover Art Archive"

    def _headers(self):
        return {"User-Agent": "AudioFileManager/1.0 (contact@example.com)"}

    def search_metadata(self, title, artist):
        url = "https://musicbrainz.org/ws/2/recording"
        params = {"query": f"artist:{artist} AND recording:{title}",
                  "fmt": "json", "limit": 3}
        resp = self._request_with_rate_limit("musicbrainz", url, params=params, headers=self._headers(), timeout=10)
        if resp is None:
            return None
        data = resp.json()

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

    def search_cover(self, title, artist, album=""):
        query = f"artist:{artist}"
        if album:
            query += f" AND release:{album}"
        url = "https://musicbrainz.org/ws/2/release"
        params = {"query": query, "fmt": "json", "limit": 1}
        resp = self._request_with_rate_limit("musicbrainz", url, params=params, headers=self._headers(), timeout=10)
        if resp is None:
            return None
        data = resp.json()

        releases = data.get("releases", [])
        if not releases:
            return None
        release_mbid = releases[0].get("id", "")
        return f"https://coverartarchive.org/release/{release_mbid}/front" if release_mbid else None

    def search_lyrics(self, title, artist, album=""):
        return None
```

### 4.4 MetingProvider（中文深度补充 — 使用公开实例）

**⚠️ 重要说明**: Meting-API 没有统一的官方公共实例。以下是已验证情况的公开实例列表：

| 实例地址 | 状态 | 说明 |
|:---------|:----:|:------|
| `https://api.injahow.cn/meting/` | ✅ 基础可用 | 支持 song/url/pic/lrc，搜索功能受限（无 type=search） |
| `https://api.amarea.cn/meting/` | ❌ 搜索受限 | 同基座，不支持搜索 |
| `https://api.crowya.com/meting/` | ❌ 搜索受限 | 同基座，不支持搜索 |
| `https://metingapi.mo-app.cn/` | ⚠️ Google 418 | 触发反爬，国内IP可能被拦截 |
| `https://metingapi.nanorocky.top/` | ⚠️ Google 418 | 同上，触发了 Cloudflare 反爬 |
| `https://meting.mikus.ink/` | ❓ 不稳定 | 偶尔可访问 |

**推荐配置**: 在设置中允许用户自定义 Meting-API 地址，默认使用 `https://api.injahow.cn/meting/`。用户可切换到任何可用实例或自建实例。

```python
class MetingProvider(SearchProvider):
    """
    Meting-API 提供者。
    默认使用公开实例 https://api.injahow.cn/meting/，
    用户可在设置中切换为其他公开实例或自建实例。

    配置方式:
    - meting_api_url: 设置中的自定义 API 地址
    - meting_server: 音源，netease（网易云）/ tencent（QQ音乐）
    """

    def __init__(self, api_url: str = "https://api.injahow.cn/meting/",
                 server: str = "netease"):
        self.api_url = api_url.rstrip("/") + "/"
        self.server = server

    @property
    def provider_id(self) -> str: return "meting"
    @property
    def display_name(self) -> str: return "Meting-API (网易云/QQ音乐)"

    def search_metadata(self, title: str, artist: str) -> Optional[TrackMetadata]:
        # 先通过搜索找歌曲ID（部分实例不支持，则返回None自动降级）
        song_id, name, artists, album = self._search_song_id(title, artist)
        if not song_id:
            # 直接通过 song 类型尝试
            return self._search_by_song(title, artist)
        cover_url = f"{self.api_url}?server={self.server}&type=pic&id={song_id}"
        return TrackMetadata(
            title=name or title,
            artist=artists or artist,
            album=album or "",
            cover_url=cover_url,
            source="meting",
        )

    def _search_song_id(self, title, artist):
        """搜索歌曲获取 ID，部分实例可能不支持"""
        try:
            params = {"server": self.server, "type": "search",
                      "id": "0", "keyword": f"{title} {artist}"}
            # 部分实例用 id=关键词
            if self.server == "netease":
                params2 = {"server": self.server, "type": "search",
                           "id": f"{title} {artist}"}
                try:
                    resp = self._request_with_rate_limit("meting", self.api_url, params=params2, timeout=5)
                    data = resp.json() if resp is not None else None
                    if isinstance(data, list) and data:
                        return self._extract_first(data)
                except Exception:
                    pass
            resp = self._request_with_rate_limit("meting", self.api_url, params=params, timeout=5)
            if resp is not None:
                data = resp.json()
                if isinstance(data, list) and data:
                    return self._extract_first(data)
        except Exception:
            pass
        return None, None, None, None

    def _extract_first(self, data):
        item = data[0]
        artist_list = item.get("artist", [item.get("artist", "")])
        if isinstance(artist_list, list):
            artists = " / ".join(artist_list)
        else:
            artists = str(artist_list)
        return (item.get("id"), item.get("name"),
                artists, item.get("album", ""))

    def _search_by_song(self, title, artist):
        """备用方案：直接用 song 类型查（需已知ID）"""
        return None

    def search_cover(self, title, artist, album=""):
        meta = self.search_metadata(title, artist)
        return meta.cover_url if meta else None

    def search_lyrics(self, title, artist, album=""):
        # 搜索获取 ID
        song_id, _, _, _ = self._search_song_id(title, artist)
        if not song_id:
            return None
        try:
            params = {"server": self.server, "type": "lrc", "id": song_id}
            resp = self._request_with_rate_limit("meting", self.api_url, params=params, timeout=5)
            if resp is None:
                return None
            return resp.text
        except Exception:
            return None
```

---

## 五、文件名解析与处理

### 5.1 文件名模式识别

```python
import re
import os
import json
from typing import Optional
from dataclasses import dataclass, field

# ============================================================
# §5.1.1 本地歌手库（ArtistDB）
# ============================================================

@dataclass
class ArtistInfo:
    name: str              # 标准化名称
    aliases: list[str] = field(default_factory=list)   # 别名（如 "Jay Chou"→"周杰伦"）
    surnames: str = ""     # 姓氏（中文单字，用于姓氏判断）

class ArtistDB:
    """
    本地歌手数据库，用于文件名解析的艺人名匹配。
    
    数据来源（按优先级）:
    1. 内置预置：高频中文/英文艺人名硬编码（约 100 条，见 _init_builtin）
    2. MusicBrainz 同步：首次安装时从 MusicBrainz 拉取常见中文艺人
    3. 用户纠错：每次用户在详情面板手动修改歌手名后，自动存入本地缓存
    4. 文件名缓存：成功匹配的文件名→艺人映射缓存（避免重复解析）

    存储位置: <app_data>/data/artist_db.json
    """

    # ---- 内置预置艺人（高频中文 + 英文热门） ----
    _BUILTIN_ARTISTS: dict[str, ArtistInfo] = {}

    # ---- 外部库加载 ----
    _user_artists: dict[str, ArtistInfo] = {}      # 用户手动添加
    _filename_cache: dict[str, str] = {}           # 文件名→艺人名缓存

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._load()
        if not self._BUILTIN_ARTISTS:
            self._init_builtin()

    def _init_builtin(self):
        """初始化内置预置艺人列表"""
        # 中文高频艺人
        chinese = [
            "周杰伦", "林俊杰", "陈奕迅", "王力宏", "邓紫棋",
            "张惠妹", "蔡依林", "孙燕姿", "张学友", "刘德华",
            "李荣浩", "薛之谦", "毛不易", "许嵩", "张杰",
            "王菲", "那英", "林志炫", "杨宗纬", "周深",
            "伍佰", "五月天", "苏打绿", "S.H.E", "凤凰传奇",
            "李健", "朴树", "陈粒", "赵雷", "陈雪凝",
            "周笔畅", "张靓颖", "谭维维", "韩红", "萧敬腾",
            "林宥嘉", "方大同", "陶喆", "李宗盛", "罗大佑",
            "刘若英", "莫文蔚", "梁静茹", "张韶涵", "王心凌",
            "杨千嬅", "容祖儿", "谢霆锋", "李克勤", "古巨基",
            "陈慧娴", "林子祥", "叶倩文", "梅艳芳", "张国荣",
            "黄家驹", "Beyond", "草蜢", "温拿乐队",
            "久石让", "坂本龙一", "中岛美嘉", "玉置浩二",
        ]
        for name in chinese:
            surnames = name[0] if name and '\u4e00' <= name[0] <= '\u9fff' else ""
            self._BUILTIN_ARTISTS[name.lower()] = ArtistInfo(
                name=name, surnames=surnames
            )

        # 英文高频艺人
        english = [
            "Taylor Swift", "Adele", "Ed Sheeran", "Lady Gaga",
            "Eminem", "Rihanna", "Bruno Mars", "Beyoncé",
            "Michael Jackson", "The Beatles", "Queen",
            "Elvis Presley", "Madonna", "Prince",
            "Led Zeppelin", "Pink Floyd", "Nirvana",
            "Bob Dylan", "David Bowie", "Radiohead",
            "Coldplay", "U2", "Linkin Park", "Green Day",
            "Metallica", "AC/DC", "Guns N' Roses",
            "M83", "Daft Punk", "The Weeknd",
            "Billie Eilish", "Ariana Grande", "Olivia Rodrigo",
            "Kendrick Lamar", "Drake", "Kanye West",
        ]
        for name in english:
            self._BUILTIN_ARTISTS[name.lower()] = ArtistInfo(name=name)

    def _load(self):
        """从 JSON 加载用户数据和缓存"""
        if os.path.exists(self.db_path):
            with open(self.db_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for name, info in data.get("user_artists", {}).items():
                self._user_artists[name.lower()] = ArtistInfo(**info)
            self._filename_cache = data.get("filename_cache", {})

    def _save(self):
        """持久化用户数据和文件名缓存"""
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        data = {
            "user_artists": {k: v.__dict__ for k, v in self._user_artists.items()},
            "filename_cache": self._filename_cache,
        }
        with open(self.db_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def lookup(self, name: str) -> ArtistInfo | None:
        """按名称（不区分大小写）查找艺人信息"""
        key = name.lower().strip()
        if key in self._filename_cache:
            return self._filename_cache[key]
        if key in self._user_artists:
            return self._user_artists[key]
        return self._BUILTIN_ARTISTS.get(key)

    def add_correction(self, raw_filename: str, artist: str):
        """
        用户手动纠错时调用。
        1. 记录文件名 stem（去扩展名）→艺人映射（避免下次再猜错）
        2. 如果艺人在库中不存在，加入 user_artists
        """
        stem = Path(raw_filename).stem  # 剥离扩展名，与 FileNameParser 解析键一致
        self._filename_cache[stem.lower().strip()] = ArtistInfo(name=artist)
        key = artist.lower().strip()
        if key not in self._BUILTIN_ARTISTS and key not in self._user_artists:
            surnames = artist[0] if artist and '\u4e00' <= artist[0] <= '\u9fff' else ""
            self._user_artists[key] = ArtistInfo(name=artist, surnames=surnames)
        self._save()

    def sync_from_musicbrainz(self):
        """
        从 MusicBrainz 拉取常见中文艺人，扩充本地库。
        
        调用时机：
        - 首次安装 + 网络可用时自动同步
        - 设置界面中的「🔄 同步歌手库」手动触发
        
        实现：使用 MusicBrainz XML API 搜索 tag:chinese，
        取前 500 条结果，存入 user_artists。
        
        由于受 1 req/s 限制，建议在后台线程（QThread）中执行，
        用进度条显示同步进度。
        """
        # 详见 §5.1.1 MusicBrainz 同步实现
        ...

    def get_all_artist_names(self) -> set[str]:
        """返回所有已知艺人名（去重）"""
        names = set()
        for info in self._BUILTIN_ARTISTS.values():
            names.add(info.name)
        for info in self._user_artists.values():
            names.add(info.name)
        return names

    def get_surname_set(self) -> set[str]:
        """返回所有已知中文单姓"""
        surnames = set()
        for info in self._BUILTIN_ARTISTS.values():
            if info.surnames:
                surnames.add(info.surnames)
        for info in self._user_artists.values():
            if info.surnames:
                surnames.add(info.surnames)
        return surnames


# ============================================================
# §5.1.2 文件名多策略解析器
# ============================================================

class FileNameParser:
    """
    从文件名中提取歌曲名和歌手名，使用 4 级策略链。

    支持的命名模式：
    - 歌曲名-歌手.mp3          → (歌曲名, 歌手)
    - 歌曲名 - 歌手.mp3
    - 歌手 - 歌曲名.mp3
    - 01_歌曲名_歌手.mp3
    - 01_歌曲名-歌手.mp3
    - [标签]歌曲名-歌手.mp3
    - 歌手-歌曲名-专辑名.mp3   → 三段式，自动识别专辑
    - 歌手_歌曲名.mp3            → (歌曲名, 歌手)
    - 单纯歌曲名.mp3            → (歌曲名, None)
    """

    _ARTIST_HINTS = re.compile(r'[&＆、,，/／]')  # 多歌手分隔符
    # 姓氏集合统一由 ArtistDB.get_surname_set() 提供，不再硬编码副本

    def __init__(self, artist_db: ArtistDB):
        self.artist_db = artist_db

    def parse(self, filename: str) -> tuple[str, Optional[str], Optional[str], bool]:
        """
        解析文件名，返回 (歌曲名, 歌手, 专辑名, 是否不确定)。

        返回四元素：
        - 专辑名仅在找到三段式模式时非 None
        - 第四元素 is_uncertain 为 True 时，表示解析结果置信度低
          （如三段式无法确认顺序），UI 应标记 "⚠️ 顺序不确定"
        """
        name, _ = os.path.splitext(filename)
        import unicodedata
        name = unicodedata.normalize("NFKC", name).strip()

        # 去除序号前缀/方括号标签
        cleaned = re.sub(r'^(?:\d+[._\s\-]+|\[.*?\]\s*)', '', name)

        # 尝试三段式：歌手-歌曲-专辑 或 专辑-歌曲-歌手
        m3 = re.match(r'^(.+?)\s*[-–—\-]\s*(.+?)\s*[-–—\-]\s*(.+?)$', cleaned)
        if m3:
            p1, p2, p3 = m3.group(1).strip(), m3.group(2).strip(), m3.group(3).strip()
            result = self._resolve_three_part(p1, p2, p3)
            if result:
                title, artist, album, uncertain = result
                return (title, artist, album, uncertain)

        # 两段式：歌曲名-歌手 或 歌手-歌曲名
        m = re.match(r'^(.+?)\s*[-–—\-]\s*(.+?)$', cleaned)
        if m:
            part1, part2 = m.group(1).strip(), m.group(2).strip()
            result = self._resolve_two_part(part1, part2)
            if result:
                return (*result, False)

        return (cleaned, None, None, False)

    def _resolve_two_part(self, p1: str, p2: str) -> tuple[str, str, None] | None:
        # 第1级：多歌手分隔符检测
        if self._ARTIST_HINTS.search(p1):
            return (p2, p1, None)   # p1 是歌手（含多歌手分隔符）
        if self._ARTIST_HINTS.search(p2):
            return (p1, p2, None)   # p2 是歌手

        # 第2级：已知艺人名匹配
        if self.artist_db.lookup(p1) is not None:
            return (p2, p1, None)   # p1 是已知歌手
        if self.artist_db.lookup(p2) is not None:
            return (p1, p2, None)   # p2 是已知歌手

        # 第3级：长度 + 中英文 + 空格判断
        def is_chinese(s: str) -> bool:
            return any('\u4e00' <= c <= '\u9fff' for c in s)

        p1_cn, p2_cn = is_chinese(p1), is_chinese(p2)

        # 中文歌名 + 英文歌手 或 英文歌名 + 中文歌手
        if p1_cn and not p2_cn:
            return (p1, p2, None)   # p1=中文歌名, p2=英文歌手
        if p2_cn and not p1_cn:
            return (p2, p1, None)   # p2=中文歌名, p1=英文歌手

        # 两边都中文或都英文：用长度 + 空格判断
        len_diff = len(p1) - len(p2)
        if abs(len_diff) >= 3:
            if len_diff < 0:
                return (p2, p1, None)  # p1 短 → 歌手
            return (p1, p2, None)

        # 空格判断（英文名多带空格）
        p1_has_space = ' ' in p1.strip()
        p2_has_space = ' ' in p2.strip()
        if p1_has_space and not p2_has_space:
            return (p2, p1, None)  # p1 带空格 → 歌手名
        if p2_has_space and not p1_has_space:
            return (p1, p2, None)

        """
        第4级：姓氏判断（中文 2~3 字，首字为常见姓）
        """
        surnames = self.artist_db.get_surname_set()
        if len(p1) <= 3 and p1[0] in surnames:
            return (p2, p1, None)
        if len(p2) <= 3 and p2[0] in surnames:
            return (p1, p2, None)

        # 默认：歌名-歌手（常见习惯）
        return (p1, p2, None)

    def _resolve_three_part(self, p1: str, p2: str, p3: str) -> tuple | None:
        """三段式：尝试确定 歌曲-歌手-专辑 的顺序。
        
        返回 (歌曲名, 歌手, 专辑名, is_uncertain) 或 None。

        注意（v5.7 明确限制）：三段式文件名顺序本身具有歧义，本解析为
        **尽力而为（best-effort）**，存在以下局限：
        - 仅当某一段能命中 ArtistDB 时才较为可靠（据此推断歌手所在位置）；
        - 若三段均无法命中 ArtistDB，则**默认按 歌手-歌曲-专辑** 处理并
          标记 is_uncertain=True，UI 层应显示 "⚠️ 顺序不确定"；
        - 某段本身含短横（如日期 ``2008-10``）或使用了非 ``- – —`` 分隔符时，
          可能被误判为两段式或错误切分；
        - 仅当三段都能明确命中时才返回专辑名，否则专辑名按 None 处理。
        对于无法可靠解析的三段式，建议用户在详情面板手动纠正，
        纠正结果会写入文件名缓存（见 §5.1.1），下次直接命中。
        """
        # 优先尝试 歌手-歌曲-专辑
        if self.artist_db.lookup(p1) is not None:
            return (p2, p1, p3, False)
        if self.artist_db.lookup(p2) is not None:
            return (p1, p2, p3, False)
        # 尝试 专辑-歌曲-歌手
        if self.artist_db.lookup(p3) is not None:
            return (p2, p3, p1, False)
        # 默认当 歌手-歌曲-专辑 处理（无法可靠推断时），标记为不确定
        return (p2, p1, p3, True)
```

### 5.2 文件名标准化示例

| 原始文件名 | 标准化后 | 说明 |
|:-----------|:---------|:------|
| `稻香.mp3` | `稻香-周杰伦.mp3`（经搜索补全） | 无歌手→搜索补全 |
| `周杰伦-稻香.mp3` | `稻香-周杰伦.mp3` | 第2级：周杰伦命中 ArtistDB |
| `01_稻香_周杰伦.mp3` | `稻香-周杰伦.mp3` | 第2级：周杰伦命中 ArtistDB |
| `[搬运]夜曲-周杰伦.mp3` | `夜曲-周杰伦.mp3` | 第2级：周杰伦命中 ArtistDB |
| `周杰伦-稻香-魔杰座.mp3` | `稻香-周杰伦.mp3` | 三段式，第2级：周杰伦命中 |
| `M83-Wait` | `Wait-M83.mp3` | 第2级：M83 命中 ArtistDB（内置） |
| `Jason Mraz-Butterfly` | `Butterfly-Jason Mraz.mp3` | 第3级：空格+长度判断 |
| `蔡依林-日不落` | `日不落-蔡依林.mp3` | 第2级：蔡依林命中 ArtistDB |
| `久石让-天空之城` | `天空之城-久石让.mp3` | 第2级：久石让命中 ArtistDB（内置） |

> 如果文件名中歌手未被 ArtistDB 收录，用户手动纠错后自动存入本地缓存，下次解析同样文件名直接命中。详见 §5.1.1。
>
> **首次使用建议**：点击设置→「🔄 同步歌手库」，从 MusicBrainz 拉取常见中文艺人扩充本地库。

---

## 六、目录化整理

### 6.1 目标目录结构

```
E:\音乐库\
├── ⚪ 未知艺人\
│   ├── ⚪ 未知专辑\
│   └── 魔杰座\
├── 周杰伦\
│   ├── 魔杰座 (2008)\
│   │   ├── 稻香-周杰伦.mp3
│   │   ├── cover.jpg
│   │   └── 稻香-周杰伦.lrc
│   └── 七里香 (2004)\
│       ├── 七里香-周杰伦.mp3
│       ├── cover.jpg
│       └── 七里香-周杰伦.lrc
```

### 6.2 整理规则

| 元数据状态 | 整理结果 |
|:-----------|:---------|
| 歌手+专辑+年份 | `歌手\专辑(年份)\` |
| 歌手+专辑（无年份） | `歌手\专辑\` |
| 歌手（无专辑） | `歌手\🎵 精选\` |
| 无歌手无专辑 | `⚪ 未知艺人\⚪ 未知专辑\` |

> **整理方式**: 通过配置 `delete_source` 控制。`delete_source=false`（默认）时复制到目标目录保留源文件；`delete_source=true` 时移动文件到目标目录，源文件被删除。可在设置界面选择。

---

## 七、ID3 标签写入

### 7.1 支持的音频格式

| 格式 | 标签标准 | mutagen 支持 | 封面写入 | 歌词写入 |
|:-----|:---------|:------------:|:--------:|:--------:|
| MP3 | ID3v2.4 | ✅ | ✅ (APIC) | ✅ (USLT) |
| FLAC | Vorbis Comment | ✅ | ✅ | ✅ |
| M4A (AAC) | iTunes MP4 | ✅ | ✅ | ✅ |
| OGG | Vorbis Comment | ✅ | ✅ | ✅ |
| WMA | ASF | ✅ | ❌ | ✅ |
| APE | APEtag | ✅ | ✅ | ✅ |

### 7.2 写入字段映射

```python
# 模块：services/tag_writer.py（原 id3_writer.py 已重命名为 tag_writer.py，支持多格式）
from pathlib import Path
from typing import Optional


def write_metadata(file_path: str, meta: "TrackMetadata",
                   cover_data: Optional[bytes] = None,
                   lyrics_text: Optional[str] = None):
    """按文件格式写入标签（MP3/FLAC/M4A/OGG/WMA/APE）。

    字段映射：标题/歌手/专辑/年份/流派/曲目号/封面/歌词。
    各格式标签容器不同（ID3 / Vorbis Comment / MP4 / ASF / APEv2），
    由 _WRITE_DISPATCH 依据扩展名路由到对应写入函数。
    """
    suffix = Path(file_path).suffix.lower().lstrip(".")
    _WRITE_DISPATCH.get(suffix, _write_mp3)(file_path, meta, cover_data, lyrics_text)


def write_cover(file_path: str, cover_data: bytes):
    """仅写入封面（按格式路由）。WMA 在 §7.1 中封面写入标记为 ❌，此处跳过。"""
    suffix = Path(file_path).suffix.lower().lstrip(".")
    _COVER_DISPATCH.get(suffix, _cover_mp3)(file_path, cover_data)


def write_lyrics(file_path: str, lyrics_text: str):
    """仅写入歌词（按格式路由）。"""
    suffix = Path(file_path).suffix.lower().lstrip(".")
    _LYRICS_DISPATCH.get(suffix, _lyrics_mp3)(file_path, lyrics_text)


# ── MP3（ID3v2.4）─────────────────────────────────────────────
def _write_mp3(file_path, meta, cover_data, lyrics_text):
    from mutagen.id3 import ID3, APIC, USLT, TIT2, TPE1, TALB, TDRC, TCON, TRCK, Encoding
    from mutagen.mp3 import MP3
    audio = MP3(file_path, ID3=ID3)
    audio["TIT2"] = TIT2(encoding=Encoding.UTF8, text=[meta.title])
    audio["TPE1"] = TPE1(encoding=Encoding.UTF8, text=[meta.artist])
    audio["TALB"] = TALB(encoding=Encoding.UTF8, text=[meta.album])
    if meta.release_year:
        audio["TDRC"] = TDRC(encoding=Encoding.UTF8, text=[str(meta.release_year)])
    if meta.genre:
        audio["TCON"] = TCON(encoding=Encoding.UTF8, text=[meta.genre])
    if meta.track_number:
        audio["TRCK"] = TRCK(encoding=Encoding.UTF8, text=[str(meta.track_number)])
    if cover_data:
        audio["APIC"] = APIC(encoding=Encoding.UTF8, mime="image/jpeg", type=3, desc="Cover", data=cover_data)
    if lyrics_text:
        audio["USLT"] = USLT(encoding=Encoding.UTF8, lang="chi", desc="Lyrics", text=lyrics_text)
    audio.save()


def _cover_mp3(file_path, cover_data):
    from mutagen.id3 import ID3, APIC, Encoding
    from mutagen.mp3 import MP3
    audio = MP3(file_path, ID3=ID3)
    audio["APIC"] = APIC(encoding=Encoding.UTF8, mime="image/jpeg", type=3, desc="Cover", data=cover_data)
    audio.save()


def _lyrics_mp3(file_path, lyrics_text):
    from mutagen.id3 import ID3, USLT, Encoding
    from mutagen.mp3 import MP3
    audio = MP3(file_path, ID3=ID3)
    audio["USLT"] = USLT(encoding=Encoding.UTF8, lang="chi", desc="Lyrics", text=lyrics_text)
    audio.save()


# ── FLAC（Vorbis Comment）─────────────────────────────────────
def _write_flac(file_path, meta, cover_data, lyrics_text):
    from mutagen.flac import FLAC, Picture
    audio = FLAC(file_path)
    audio["TITLE"], audio["ARTIST"], audio["ALBUM"] = meta.title, meta.artist, meta.album
    if meta.release_year: audio["DATE"] = str(meta.release_year)
    if meta.genre: audio["GENRE"] = meta.genre
    if meta.track_number: audio["TRACKNUMBER"] = str(meta.track_number)
    if lyrics_text: audio["LYRICS"] = lyrics_text
    if cover_data:
        pic = Picture(); pic.type = 3; pic.desc = "Cover"; pic.mime = "image/jpeg"; pic.data = cover_data
        # 仅移除已有封面（type=3），保留 booklet/artist 等其他图片类型
        existing = [p for p in audio.pictures if p.type != 3]
        audio.clear_pictures()
        for p in existing:
            audio.add_picture(p)
        audio.add_picture(pic)
    audio.save()


def _cover_flac(file_path, cover_data):
    from mutagen.flac import FLAC, Picture
    audio = FLAC(file_path)
    pic = Picture(); pic.type = 3; pic.desc = "Cover"; pic.mime = "image/jpeg"; pic.data = cover_data
    audio.clear_pictures(); audio.add_picture(pic); audio.save()


def _lyrics_flac(file_path, lyrics_text):
    from mutagen.flac import FLAC
    audio = FLAC(file_path); audio["LYRICS"] = lyrics_text; audio.save()


# ── M4A（iTunes MP4）──────────────────────────────────────────
def _write_m4a(file_path, meta, cover_data, lyrics_text):
    from mutagen.mp4 import MP4, MP4Cover
    audio = MP4(file_path)
    audio["\xa9nam"], audio["\xa9ART"], audio["\xa9alb"] = meta.title, meta.artist, meta.album
    if meta.release_year: audio["\xa9day"] = str(meta.release_year)
    if meta.genre: audio["\xa9gen"] = meta.genre
    if meta.track_number:
        try: audio["trkn"] = [(int(meta.track_number), 0)]
        except ValueError: pass
    if lyrics_text: audio["\xa9lyr"] = lyrics_text
    if cover_data: audio["covr"] = [MP4Cover(cover_data, imageformat=MP4Cover.FORMAT_JPEG)]
    audio.save()


def _cover_m4a(file_path, cover_data):
    from mutagen.mp4 import MP4, MP4Cover
    audio = MP4(file_path)
    audio["covr"] = [MP4Cover(cover_data, imageformat=MP4Cover.FORMAT_JPEG)]; audio.save()


def _lyrics_m4a(file_path, lyrics_text):
    from mutagen.mp4 import MP4
    audio = MP4(file_path); audio["\xa9lyr"] = lyrics_text; audio.save()


# ── OGG（Vorbis Comment）─────────────────────────────────────
def _write_ogg(file_path, meta, cover_data, lyrics_text):
    from mutagen.oggvorbis import OggVorbis
    from mutagen.flac import Picture
    import base64
    audio = OggVorbis(file_path)
    audio["TITLE"], audio["ARTIST"], audio["ALBUM"] = meta.title, meta.artist, meta.album
    if meta.release_year: audio["DATE"] = str(meta.release_year)
    if meta.genre: audio["GENRE"] = meta.genre
    if meta.track_number: audio["TRACKNUMBER"] = str(meta.track_number)
    if lyrics_text: audio["LYRICS"] = lyrics_text
    if cover_data:
        pic = Picture(); pic.type = 3; pic.desc = "Cover"; pic.mime = "image/jpeg"; pic.data = cover_data
        # OGG Vorbis 的 METADATA_BLOCK_PICTURE 字段要求 base64 编码字符串
        audio["METADATA_BLOCK_PICTURE"] = base64.b64encode(pic.write()).decode("ascii")
    audio.save()


def _cover_ogg(file_path, cover_data):
    from mutagen.oggvorbis import OggVorbis
    from mutagen.flac import Picture
    import base64
    audio = OggVorbis(file_path)
    pic = Picture(); pic.type = 3; pic.desc = "Cover"; pic.mime = "image/jpeg"; pic.data = cover_data
    audio["METADATA_BLOCK_PICTURE"] = base64.b64encode(pic.write()).decode("ascii")
    audio.save()


def _lyrics_ogg(file_path, lyrics_text):
    from mutagen.oggvorbis import OggVorbis
    audio = OggVorbis(file_path); audio["LYRICS"] = lyrics_text; audio.save()


# ── WMA（ASF，封面 §7.1 标记 ❌）─────────────────────────────
def _write_wma(file_path, meta, cover_data, lyrics_text):
    from mutagen.asf import ASF
    audio = ASF(file_path)
    audio["Title"], audio["Author"], audio["WM/AlbumTitle"] = meta.title, meta.artist, meta.album
    if meta.release_year: audio["WM/Year"] = str(meta.release_year)
    if meta.genre: audio["WM/Genre"] = meta.genre
    if meta.track_number: audio["WM/TrackNumber"] = str(meta.track_number)
    if lyrics_text: audio["WM/Lyrics"] = lyrics_text
    # 封面按 §7.1 约定不写入
    audio.save()


def _lyrics_wma(file_path, lyrics_text):
    from mutagen.asf import ASF
    audio = ASF(file_path); audio["WM/Lyrics"] = lyrics_text; audio.save()


# ── APE（APEv2）──────────────────────────────────────────────
def _write_ape(file_path, meta, cover_data, lyrics_text):
    from mutagen.apev2 import APEv2, APEBinaryValue
    audio = APEv2(file_path)
    audio["Title"], audio["Artist"], audio["Album"] = meta.title, meta.artist, meta.album
    if meta.release_year: audio["Year"] = str(meta.release_year)
    if meta.genre: audio["Genre"] = meta.genre
    if meta.track_number: audio["Track"] = str(meta.track_number)
    if lyrics_text: audio["Lyrics"] = lyrics_text
    if cover_data: audio["Cover Art (Front)"] = APEBinaryValue(cover_data)
    audio.save()


def _cover_ape(file_path, cover_data):
    from mutagen.apev2 import APEv2, APEBinaryValue
    audio = APEv2(file_path); audio["Cover Art (Front)"] = APEBinaryValue(cover_data); audio.save()


def _lyrics_ape(file_path, lyrics_text):
    from mutagen.apev2 import APEv2
    audio = APEv2(file_path); audio["Lyrics"] = lyrics_text; audio.save()


_WRITE_DISPATCH = {
    "mp3": _write_mp3, "flac": _write_flac, "m4a": _write_m4a,
    "ogg": _write_ogg, "wma": _write_wma, "ape": _write_ape,
}
_COVER_DISPATCH = {
    "mp3": _cover_mp3, "flac": _cover_flac, "m4a": _cover_m4a,
    "ogg": _cover_ogg, "ape": _cover_ape,   # 不含 wma（§7.1 封面 ❌）
}
_LYRICS_DISPATCH = {
    "mp3": _lyrics_mp3, "flac": _lyrics_flac, "m4a": _lyrics_m4a,
    "ogg": _lyrics_ogg, "wma": _lyrics_wma, "ape": _lyrics_ape,
}
```

### 7.3 歌词写入方式

| 写入方式 | 说明 | 适用场景 |
|:---------|:------|:---------|
| **嵌入 ID3 (USLT)** | 直接写入文件标签 | MP3/FLAC 等支持嵌入式歌词的格式 |
| **同目录 LRC 文件** | 在歌曲旁生成 `歌曲名.lrc` | 通用方案，所有播放器支持 |
| **两者都写** | 文件内 + 外部 LRC | 兼容性最佳 |

---

### 7.4 网络搜索手动补全实现

用户在详情面板点击「🌐 网络搜索信息」按钮时，触发手动补全流程：

```python
# services/manual_search.py
from typing import Optional
from dataclasses import dataclass, field

@dataclass
class SearchResult:
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
```

**搜索流程**:
```python
class ManualSearchService:
    """
    手动搜索服务 — 供详情面板「网络搜索信息」按钮调用。
    与批处理搜索复用相同的 SearchProvider 链，但逻辑不同：
    - 手动搜索只搜 1 首，不需要回退
    - 同时并行搜索元数据 + 歌词（提升响应速度）
    - 所有结果展示在前端，由用户决定是否填入
    """

    def __init__(self, search_engine: SearchEngine):
        self.engine = search_engine

    def search(self, title: str, artist: str) -> list[SearchResult]:
        """并行搜索元数据和歌词封面，返回候选列表（最多 3 条）。

        同步方法：在 QThread worker 线程内调用，故并发使用
        ThreadPoolExecutor（见 §3 执行模型）提交 3 个同步子任务，
        而非 asyncio。
        """
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=3) as ex:
            f_meta = ex.submit(self.engine.search_metadata, title, artist)
            f_lyrics = ex.submit(self.engine.search_lyrics, title, artist)
            f_cover = ex.submit(self.engine.fetch_cover, title, artist, album=None)
            metadata = f_meta.result()
            lyrics = f_lyrics.result()
            cover = f_cover.result()

        # 合并为 SearchResult 列表返回，前端展示在搜索结果预览框中
        return self._merge_results(metadata, lyrics, cover)
```

**前端交互流程**:

| 步骤 | 用户操作 | 后端响应 |
|:-----|:---------|:---------|
| 1 | 点击「🌐 网络搜索信息」按钮 | 从当前输入框读取歌手+歌曲名，调用 `ManualSearchService.search()` |
| 2 | 按钮变为「⏳ 搜索中…」禁用态 | 并行请求 iTunes + LRCLIB + MusicBrainz + CAA |
| 3 | 展示搜索结果预览框 | 返回 `SearchResult` 列表，显示匹配项摘要（歌曲名/歌手/专辑/封面有无/歌词有无） |
| 4 | 点击「✓ 填入元数据」 | 将搜索结果的 title/artist/album/year/genre 填入对应的文本输入框 |
| 5 | 用户编辑确认后点击「💾 保存修改到文件」 | 调用 `save_metadata()` 写入 ID3 标签 |

**代码位置**: `services/manual_search.py`, `app/metadata_edit_controller.py`

---

### 7.5 元数据保存方式实现

根据设置中的 `save_mode` 配置，决定元数据的保存目标：

```python
# services/metadata_saver.py
import shutil
from pathlib import Path
from enum import Enum

class SaveMode(str, Enum):
    TAGS = "tags"       # 仅写入文件标签（ID3/Vorbis/MP4/ASF/APEv2）
    FILES = "files"     # 仅保存为同目录独立文件
    BOTH = "both"       # 两者都保存

class MetadataSaver:
    """
    元数据保存器 — 根据配置决定歌词和封面的保存位置。
    所有写入都需经过此模块，确保行为一致。
    """

    def __init__(self, save_mode: SaveMode, encoding_config: dict):
        self.save_mode = SaveMode(save_mode)
        self.encoding = encoding_config  # 编码设置见 7.6

    def save_lyrics(self, file_path: str, lyrics_text: str):
        """保存歌词，根据 save_mode 选择写入方式"""
        if self.save_mode in (SaveMode.TAGS, SaveMode.BOTH):
            # 写入文件标签（ID3 USLT / Vorbis Comment）
            self._write_lyrics_to_tags(file_path, lyrics_text)

        if self.save_mode in (SaveMode.FILES, SaveMode.BOTH):
            # 保存同目录独立 LRC 文件
            lrc_path = Path(file_path).with_suffix(".lrc")
            # 应用编码设置后再写入
            encoded_text = self._apply_encoding(lyrics_text)
            lrc_path.write_text(encoded_text, encoding=self.encoding.get("charset", "utf-8"))

    def save_cover(self, file_path: str, cover_data: bytes):
        """保存封面，根据 save_mode 选择写入方式"""
        if self.save_mode in (SaveMode.TAGS, SaveMode.BOTH):
            self._write_cover_to_tags(file_path, cover_data)

        if self.save_mode in (SaveMode.FILES, SaveMode.BOTH):
            cover_path = Path(file_path).parent / "cover.jpg"
            cover_path.write_bytes(cover_data)

    def _write_lyrics_to_tags(self, file_path: str, lyrics_text: str):
        """把歌词写入文件标签（按格式路由到 tag_writer）"""
        from tag_writer import write_lyrics
        write_lyrics(file_path, lyrics_text)

    def _write_cover_to_tags(self, file_path: str, cover_data: bytes):
        """把封面写入文件标签（按格式路由到 tag_writer）"""
        from tag_writer import write_cover
        write_cover(file_path, cover_data)

    def save_metadata_to_tags(self, file_path: str, meta: "TrackMetadata",
                              cover_data: Optional[bytes] = None,
                              lyrics_text: Optional[str] = None):
        """用户手动保存时：将当前输入框内容写入文件标签"""
        # 复用 7.2 节的 write_metadata() 函数（已支持多格式），但先应用编码转换
        from tag_writer import write_metadata

        # 对每个文本字段做编码转换
        if self.encoding.get("enabled", False):
            meta = self._convert_encoding(meta)

        # ① 写入标签（含歌词+封面，由 write_metadata 一次性完成）
        write_metadata(file_path, meta, cover_data, lyrics_text)

        # ② 若 save_mode 要求同时保存独立文件，仅落盘文件（不重复写标签）
        if self.save_mode in (SaveMode.FILES, SaveMode.BOTH):
            if lyrics_text:
                lrc_path = Path(file_path).with_suffix(".lrc")
                encoded_text = self._apply_encoding(lyrics_text)
                lrc_path.write_text(encoded_text, encoding=self.encoding.get("charset", "utf-8"))
            if cover_data:
                cover_path = Path(file_path).parent / "cover.jpg"
                cover_path.write_bytes(cover_data)

    def _apply_encoding(self, text: str) -> str:
        """先编码再解码，实现 encoding→charset→UTF-8 的转换"""
        if not self.encoding.get("enabled", False):
            return text
        charset = self.encoding.get("charset", "UTF-8")
        return text.encode(charset, errors="replace").decode(charset, errors="replace")

    def _convert_encoding(self, meta: "TrackMetadata") -> "TrackMetadata":
        """对 TrackMetadata 的所有文本字段做编码转换"""
        import copy
        m = copy.copy(meta)
        for field in ["title", "artist", "album", "genre"]:
            val = getattr(m, field, None)
            if val:
                setattr(m, field, self._apply_encoding(val))
        return m
```

**代码位置**: `services/metadata_saver.py`

---

### 7.6 文字编码统一实现

```python
# services/encoding_service.py
"""
编码统一服务 — 将音频文件的元数据文字统一到指定的字符集。

适用场景：
- 从网络下载的中文歌曲，ID3 标签可能被保存为 GBK/GB2312/Latin1 等编码
- 老旧车载播放器只支持 GBK，需要将 UTF-8 标签转码
- 混用了多种编码的来源文件，统一后保持一致
"""

import chardet  # 可选：用于自动检测原始编码
from pathlib import Path

class EncodingService:
    """
    编码统一服务。

    检查每个文件的标签文本编码（格式无关），转换为目标字符集。
    需用户明确启用此功能（设置中开启"启用编码统一"）。

    支持的编码方案（7.6.1）：
    - CP1252/ISO-8859-1: 老旧 ID3v1 默认编码
    - GBK/GB2312: 中文 Windows 系统常用
    - GB18030: 中文全字符集
    - UTF-8: 现代通用标准（推荐）
    """

    def __init__(self, enabled: bool, target_charset: str = "UTF-8"):
        self.enabled = enabled
        self.target = target_charset.upper()

    def normalize_file(self, file_path: str) -> dict[str, str]:
        """
        读取文件现有标签文本，检测编码，转换为目标编码并**回写**。
        格式无关：用 mutagen.File 自动识别容器（ID3/Vorbis/MP4/ASF/APEv2）。
        返回 {字段名: 转换后的文本} 的字典，供 UI 展示差异。
        """
        if not self.enabled:
            return {}

        try:
            import mutagen
            audio = mutagen.File(file_path, easy=False)
        except Exception:
            return {"_error": "无法读取标签"}
        if audio is None or not getattr(audio, "tags", None):
            return {}

        changes = {}
        for key, value in list(audio.tags.items()):
            texts = value if isinstance(value, (list, tuple)) else [value]
            new_texts = []
            changed = False
            for original in texts:
                if isinstance(original, str):
                    converted = self._detect_and_convert(original)
                    if converted != original:
                        changed = True
                        changes[key] = converted
                    new_texts.append(converted)
                else:
                    new_texts.append(original)
            if changed:
                self._set_tag_text(audio, key, new_texts)
        if changes:
            audio.save()
        return changes

    @staticmethod
    def _set_tag_text(audio, key, new_texts):
        """把转换后的文本写回标签（兼容 ID3 帧与通用容器）"""
        try:
            if hasattr(audio.tags[key], "text"):
                audio.tags[key].text = new_texts  # ID3 帧
            else:
                audio.tags[key] = new_texts
        except Exception:
            audio.tags[key] = new_texts

    def _detect_and_convert(self, text: str) -> str:
        """
        检测字符串的乱码特征，尝试修复并转换为目标字符集。

        兼容两种常见乱码成因，双向尝试：
        A) UTF-8 字节被误按 latin-1/cp1252 解码 → 先用 wrong_src 编码回字节，
           再用 UTF-8 解码；
        B) 多字节编码（GBK/GB2312）字节被误按 UTF-8 解码 → 用 UTF-8 编码回
           字节，再用该编码解码。
        仅当结果无替换字符且可打印时才接受。

        .. warning::
            ``isprintable()`` 对中文恒为 True，存在误判风险。
            若遇到文本未被正确修复，建议引入 ``ftfy`` 库
            （``pip install ftfy``）替代手工启发式：
            ``ftfy.fix_text(text)`` 内部已覆盖 Mojibake 修复逻辑，
            准确率远高于本手工实现。当前实现作为零依赖兜底保留。
        """
        candidates = ["latin-1", "cp1252", "gbk", "gb2312"]
        # 方向 A：以 wrong_src 重新编码为字节，再按 UTF-8 解码
        for wrong_src in candidates:
            try:
                recovered = text.encode(wrong_src).decode("utf-8")
            except (UnicodeEncodeError, UnicodeDecodeError):
                continue
            if "�" not in recovered and recovered.isprintable():
                return recovered.encode(self.target, "replace").decode(self.target)
        # 方向 B：以 UTF-8 编码回字节，再按 src_enc 解码
        for src_enc in candidates:
            try:
                raw = text.encode("utf-8")
                decoded = raw.decode(src_enc, errors="replace")
            except (UnicodeEncodeError, UnicodeDecodeError):
                continue
            if "�" not in decoded and decoded.isprintable():
                return decoded.encode(self.target, "replace").decode(self.target)
        # 默认返回原文本
        return text

    def apply_to_files(self, files: list[str], callback=None):
        """
        批量处理文件列表，逐文件做编码统一。
        返回 {文件路径: {字段: 转换详情}} 的汇总结果。
        pyqtSignal 进度回调（用于主界面进度条）。
        """
        results = {}
        for i, f in enumerate(files):
            if callback:
                callback(i + 1, len(files), f)  # 进度通知
            results[f] = self.normalize_file(f)
        return results
```

**前端交互流程**:

| 步骤 | 操作 |
|:-----|:------|
| 1 | 用户勾选「启用编码统一」并选择目标字符集 |
| 2 | 点「开始处理」时，`EncodingService` 在扫描文件阶段介入 |
| 3 | 对每个文件的 ID3 标签文本做编码检测→转换→回写 |
| 4 | 在文件列表中标记「编码已统一」状态 |
| 5 | 批处理完成后在状态栏显示：`📝 编码统一: 3/5 个文件已转换` |

**代码位置**: `services/encoding_service.py`

---

### 7.7 三者的协作关系

```python
    # 批处理入口中的调用顺序（同步，运行于 QThread worker 内）
    def batch_process(files: list[str], config: dict):
        saver = MetadataSaver(
            save_mode=config.get("save_mode", "tags"),
            encoding_config=config.get("encoding", {"enabled": False, "charset": "UTF-8"})
        )
        encoder = EncodingService(
            enabled=config.get("encoding", {}).get("enabled", False),
            target_charset=config.get("encoding", {}).get("charset", "UTF-8")
        )
        fetch_cover = config.get("fetch_cover", True)
        fetch_lyrics = config.get("fetch_lyrics", True)

        for file_path in files:
            # 1️⃣ 解析文件名（先尝试 ID3，再回退到文件名启发式）
            title, artist, album, uncertain = parser.parse(file_path)
            if uncertain:
                logger.info(f"三段式解析不确定: {file_path}，将在 UI 标记警告")
            # 搜索元数据（同步，自动走 RateLimiter）
            metadata = search_engine.search_metadata(title, artist)
            if metadata and artist and not metadata.title:
                metadata.title = title

            # 搜索失败时创建空 TrackMetadata，避免后续 None 访问崩溃
            if metadata is None:
                metadata = TrackMetadata(title=title, artist=artist or "未知艺人")

            # 1.5️⃣ 批量补齐封面与歌词（统一使用 enrich_cover_and_lyrics）
            #    若元数据搜索已返回 cover_url（如 iTunes），优先下载该 URL，避免重复 API 调用
            if (fetch_cover or fetch_lyrics) and (not metadata.cover_data or not metadata.lyrics_text):
                metadata = search_engine.enrich_cover_and_lyrics(
                    metadata, title, artist, album or metadata.album
                )

            # 2️⃣ 编码统一（如启用）
            if config.get("encoding", {}).get("enabled"):
                encoder.normalize_file(file_path)

            # 3️⃣ 元数据写入（根据 save_mode 决定方式）
            saver.save_metadata_to_tags(file_path, metadata,
                                        cover_data=metadata.cover_data,
                                        lyrics_text=metadata.lyrics_text)

            # 4️⃣ 目录整理（复制/移动）
            organizer.organize(file_path, config.get("organize", {}))
```

---

## 八、UI 设计

### 8.1 主界面布局

```
┌─────────────────────────────────────────────────────────────────────────┐
│ AudioFileManager  v1.0                          —  ☐  ✕  │
├─────────────────────────────────────────────────────────────────────────┤
│  📂 选择文件夹    ⚙️ 设置    ▶ 开始处理    🔍 搜索: [______]           │
│  当前: E:\下载音乐\    8 个子文件夹 · 156 个音频文件                       │
├─────────────────────────────────┬───────────────────────────────────────┤
│                                 │  详情面板 （可收起 ◀）               │
│  ┌─ 文件列表（按目录分组）───┐  │  ┌───────────────────────────────┐  │
│  │ 📁 魔杰座\                │  │  │  📄 文件信息                    │  │
│  │  ☑  🎵 稻香.mp3       │  │  │                                 │  │
│  │      周杰伦    ✅已处理   │  │  │  文件名: 稻香.mp3              │  │
│  │ 📁 七里香\                │  │  │  路径: 魔杰座\                 │  │
│  │  ☑  🎵 七里香-周杰伦   │  │  │  大小: 8.2 MB                 │  │
│  │ ☑  🎵 01_青花瓷-周杰伦  │  │  │  时长: 3:43                    │  │
│  │    周杰伦     ⏳待处理   │  │  │                                 │  │
│  │ ☑  🎵 [未知来源]夜曲    │  │  │  📋 元数据（可编辑）              │  │
│  │    —           ❌失败     │  │  │  [稻香                         ]│  │
│  │ ☑  🎵 Some Song.mp3   │  │  │  [周杰伦                        ]│  │
│  │    Artist     ✅已处理   │  │  │  [魔杰座                         ]│  │
│  │                          │  │  │  [2008                           ]│  │
│  │    📊 已识别: 5/6        │  │  │  [Pop                            ]│  │
│  │    ✅ 已处理: 4/6        │  │  │                                 │  │
│  │    ⏳ 用时: 0:45         │  │  │  🎨 封面（点击可更换）            │  │
│  │                          │  │  │  ┌───┐                         │  │
│  │  右键 → 试听 / 查看详情   │  │  │  │   │ 600×600               │  │
│  └──────────────────────────┘  │  └───┘                         │  │
│                                 │                                 │  │
│                                 │  📝 歌词 (LRC)                   │  │
│                                 │  [00:31] 在这个世界如果你有...  │  │
│                                 │  [00:34] 跌倒了就不敢继续...  │  │
│                                 │  [00:37] 为什么人要这么的...  │  │
│                                 │                                 │  │
│                                 │ [ 🌐 网络搜索信息 ]              │  │
│                                 │  根据"周杰伦 × 稻香"搜索       │  │
│                                 │  ┌─ 搜索结果预览 ─────────┐        │
│                                 │  │ ✅ 找到 3 条  ◀ 1/3 ▶  │    │  │
│                                 │  │        来源: iTunes      │    │  │
│                                 │  │ 歌曲名: 稻香 ✓          │    │  │
│                                 │  │ 歌手: 周杰伦 ✓          │    │  │
│                                 │  │ 专辑: 魔杰座 ✓          │    │  │
│                                 │  │ [✓ 填入元数据] [✕]      │    │  │
│                                 │  └─────────────────────────┘    │  │
│                                 │                                 │  │
│                                 │ [ 💾 保存修改到文件 ]             │  │
│                                 │  修改后将写入 ID3 标签           │  │
│                                 └───────────────────────────────┘  │
├──────────────────────────────┴───────────────────────────────────────┤
│ 🔍 搜索中: 稻香 → iTunes (匹配✓)   封面→iTunes   歌词→LRCLIB          │
└─────────────────────────────────────────────────────────────────────────┘
```

**详情面板交互说明**:

| 操作 | 效果 |
|:-----|:------|
| 点击左侧文件列表某一行 | 右侧详情面板自动显示该文件的完整信息 |
| 点击详情面板右上角 `◀` 按钮 | 收起详情面板，左侧文件列表展开铺满 |
| 右侧宽度 | 约主窗口宽度的 35%，可通过分隔条拖拽调整 |
| 元数据字段（可编辑） | 每个字段均为输入框，用户可直接修改内容 |
| 网络搜索信息按钮 | 点击后根据当前"歌手 + 歌曲名"从网络搜索元数据，展示匹配结果预览框 |
| 搜索结果预览框 | 展示多条候选结果（默认显示第 1 条），用户可通过 **◀ ▶ 切换** 浏览各条结果的内容（歌曲名/歌手/专辑/封面/歌词来源），选中满意的结果后点击「✓ 填入元数据」自动填入各输入框，用户确认后点击保存按钮 |
| 保存按钮 | 将当前输入框中的歌曲名/歌手/专辑/年份/流派/曲目号写入该音频文件的 ID3 标签 |
| 封面 | 显示封面缩略图，hover 提示「点击更换封面」，可点击打开文件选择器更换 |
| 歌词 | 显示前几行预览，点击可展开查看全部 |

### 8.2 设置界面（搜索配置）

```
┌──────────────────────────────────────────────────┐
│ ⚙️ 设置                                  ✕  │
├──────────────────────────────────────────────────┤
│                                                  │
│ 📡 搜索接口配置                                    │
│                                                  │
│  搜索优先级顺序（拖拽调整）：                        │
│  ┌──────────────────────────────────────┐        │
│  │ ≡ ① Apple iTunes Search    ☑启用     │        │
│  │ ≡ ② LRCLIB（歌词）         ☑启用     │        │
│  │ ≡ ③ MusicBrainz            ☑启用     │        │
│  │ ≡ ④ Meting-API（中文补充） ☐启用     │        │
│  └──────────────────────────────────────┘        │
│                                                  │
│  Meting-API 设置                                  │
│  API 地址: [https://api.injahow.cn/meting/]   │
│  音源服务: [netease ▼]   备用服务: [tencent ▼]  │
│  可用实例参考: api.injahow.cn / api.amarea.cn    │
│               metingapi.mo-app.cn (可能被拦截)    │
│               api.crowya.com / 或自建实例          │
│                                                  │
│  请求超时: [10] 秒   总超时: [30] 秒              │
│                                                  │
│  🎤 本地歌手库                                    │
│  ┌──────────────────────────────────────┐        │
│  │ 当前收录: 约 100 位内置艺人             │        │
│  │          + 3 位用户添加              │        │
│  │          + 42 位 MusicBrainz 同步    │        │
│  │                                          │        │
│  │  [🔄 同步歌手库] 从 MusicBrainz 拉取    │        │
│  │                 常见中文艺人扩充本地库   │        │
│  │  上次同步: 2026-07-12 14:30            │        │
│  └──────────────────────────────────────┘        │
│                                                  │
│  📂 目录整理规则                                    │
│  ┌──────────────────────────────────────┐        │
│  │  输出根目录: [E:\整理后的音乐库   ] [📁浏览]│        │
│  │  整理后的文件将自动按歌手→专辑分目录       │        │
│  │                                          │        │
│  │  ☑ 按歌手分目录                       │        │
│  │  ☑ 按专辑分目录                       │        │
│  │  ☑ 追加年份到专辑名（如 "魔杰座(2008)"）│        │
│  │  ☑ 无元数据归入"未知"目录              │        │
│  │                                          │        │
│  │  整理方式：                             │        │
│  │  ○ 复制到目标目录（保留源文件）           │        │
│  │  ● 移动到目标目录（整理后删除源文件）     │        │
│  └──────────────────────────────────────┘        │
│                                                  │
│  🔤 文件名规则                                     │
│  ┌──────────────────────────────────────┐        │
│  │ 格式化: [歌曲名] - [歌手]            │        │
│  │ 分隔符: [-]                          │        │
│  │ ☑ 去除序号前缀                       │        │
│  └──────────────────────────────────────┘        │
│                                                  │
│  💾 元数据保存方式                                  │
│  ┌──────────────────────────────────────┐        │
│  │  ● 写入文件标签（ID3/Vorbis）        │        │
│  │  ○ 保存为同目录独立文件              │        │
│  │     （歌曲名.lrc + cover.jpg）        │        │
│  │  ○ 两者都保存                        │        │
│  └──────────────────────────────────────┘        │
│                                                  │
│  📝 文字编码设置                                   │
│  ┌──────────────────────────────────────┐        │
│  │  ☑ 启用编码统一                     │        │
│  │  目标字符集: [UTF-8 ▼]             │        │
│  │  ⚠ 老旧播放器建议GBK               │        │
│  └──────────────────────────────────────┘        │
│                                                  │
│  [💾 保存]  [↺ 恢复默认]                         │
└──────────────────────────────────────────────────┘
```

---

## 八（续）、试听与歌词编辑功能

### 8B.1 AuditionDialog — 试听弹窗

双击文件列表中的歌曲或右键选择"试听"，弹出独立试听窗口。

```
┌──────────────────────────────────────────────────┐
│  🎵 试听：稻香                          ✕  │
├──────────────────────────────────────────────────┤
│                                                  │
│       ┌────────────────────────┐                 │
│       │   专辑封面 (300x300)    │                 │
│       │                        │                 │
│       └────────────────────────┘                 │
│                                                  │
│     稻香                                    │
│     周杰伦 · 魔杰座 (2008)                       │
│                                                  │
│  ═══●══════════════════════╪══════  00:23 / 03:43 │
│       ◀◀   ▶/⏸   ▶▶   🔊══●══                    │
│                                                  │
│  ┌─ 歌词（可拖拽调整时间）──────────────────┐   │
│  │ [00:31] 在这个世界如果你有太多的抱怨    │   │  ← 已过去（白字）
│  │ [00:34] 跌倒了就不敢继续往前走          │   │
│  │ ▸[00:37] 为什么人要这么的脆弱堕落 ◂    │   │  ← 当前（高亮+加粗）
│  │ [00:41] 请你打开电视看看                │   │  ← 未到（灰字）
│  │ [00:42] 多少人为生命在努力勇敢的走下去  │   │
│  │                                          │   │
│  │  --- 编辑模式 ---                        │   │
│  │  [拖拽] 00:37 → 可拖动调整时间          │   │  ← 当前行时间可拖拽
│  │  [滑动] 整体偏移: +1.5s                 │   │  ← 批量微调
│  │                                          │   │
│  └──────────────────────────────────────────┘   │
│                                                  │
│  歌词来源: LRCLIB  |  版本 1/2    [💾 保存编辑]  │
│  [↻ 重置]  [<< 偏移 -0.5s]  [+0.5s >>]        │
└──────────────────────────────────────────────────┘
```

### 8B.2 播放控制

| 控件 | 功能 | 快捷键 |
|:-----|:------|:-------|
| ▶/⏸ | 播放/暂停 | 空格 |
| ◀◀ | 后退 5 秒 | ← |
| ▶▶ | 前进 5 秒 | → |
| 🔊 进度条 | 拖拽跳转 | — |
| 音量滑块 | 调整音量 | — |
| 歌词行点击 | 跳转到该行时间 | — |

### 8B.3 歌词滚动与同步

```python
class LyricsSyncController:
    """
    歌词同步控制器。
    负责：歌词解析、按播放进度高亮、滚动到当前行。
    """

    def __init__(self):
        self.lines: list[LyricLine] = []
        self._edited_lines: list[LyricLine] = []
        self._is_editing = False

    def parse_lrc(self, lrc_text: str) -> list[LyricLine]:
        """
        解析 LRC 格式歌词。
        支持 [MM:SS.xx] 和 [MM:SS.xxx] 两种格式。
        """
        lines = []
        for line in lrc_text.split("\n"):
            m = re.match(r'\[(\d+):(\d+\.\d+)\]\s*(.*)', line.strip())
            if m:
                minutes = int(m.group(1))
                seconds = float(m.group(2))
                time_ms = int(minutes * 60000 + seconds * 1000)
                text = m.group(3).strip()
                if text:
                    lines.append(LyricLine(time_ms, text))
        lines.sort(key=lambda x: x.time)
        return lines

    def get_current_index(self, progress_ms: int) -> int:
        """根据当前播放进度返回应高亮的歌词行索引"""
        for i in range(len(self.lines) - 1, -1, -1):
            if self.lines[i].time <= progress_ms:
                return i
        return -1

    def adjust_line_time(self, index: int, new_time_ms: int) -> bool:
        """调整单行歌词时间戳"""
        if 0 <= index < len(self.lines):
            self.lines[index].time = new_time_ms
            self.lines.sort(key=lambda x: x.time)
            return True
        return False

    def offset_all(self, offset_ms: int):
        """批量偏移所有歌词时间"""
        for line in self.lines:
            new_time = line.time + offset_ms
            if new_time >= 0:
                line.time = new_time


class LyricLine:
    """单行歌词"""
    def __init__(self, time_ms: int, text: str):
        self.time = time_ms
        self.text = text


class AuditionDialog(QDialog):
    """
    试听弹窗 — 独立窗口，不阻塞主界面。
    支持播放、停止、滚歌词、拖拽调时间、保存编辑到文件。
    """

    def __init__(self, file_path: str, metadata: TrackMetadata,
                 lrc_text: Optional[str], parent=None):
        super().__init__(parent)
        self.file_path = file_path
        self.metadata = metadata
        self.lyrics_ctrl = LyricsSyncController()
        if lrc_text:
            self.lyrics_ctrl.lines = self.lyrics_ctrl.parse_lrc(lrc_text)

        # 播放器
        self.player = QMediaPlayer()
        self.audio_output = QAudioOutput()
        self.player.setAudioOutput(self.audio_output)

        # UI 组件
        self._build_ui()
        self._connect_signals()

    def _build_ui(self):
        """构建试听窗口 UI"""
        self.setWindowTitle(f"🎵 试听：{self.metadata.title}")
        self.setMinimumSize(600, 700)

        layout = QVBoxLayout()

        # --- 封面 ---
        cover_label = QLabel()
        cover_label.setFixedSize(300, 300)
        cover_label.setAlignment(Qt.AlignCenter)
        cover_label.setStyleSheet("background: #333; border-radius: 8px;")
        # 加载封面（不阻塞主线程）
        self._load_cover(cover_label)
        layout.addWidget(cover_label, alignment=Qt.AlignCenter)

        # --- 歌曲信息 ---
        info = QLabel(f"""
            <h2>{self.metadata.title}</h2>
            <p style='color: gray;'>
                {self.metadata.artist}
                · {self.metadata.album or "未知专辑"}
                {f'({self.metadata.release_year})' if self.metadata.release_year else ''}
            </p>
        """)
        info.setAlignment(Qt.AlignCenter)
        layout.addWidget(info)

        # --- 进度条 + 时间 ---
        progress_layout = QHBoxLayout()
        self.progress_slider = QSlider(Qt.Horizontal)
        self.time_label = QLabel("00:00 / 00:00")
        progress_layout.addWidget(self.progress_slider)
        progress_layout.addWidget(self.time_label)
        layout.addLayout(progress_layout)

        # --- 播放控制 ---
        controls = QHBoxLayout()
        self.btn_prev = QPushButton("◀◀")
        self.btn_play = QPushButton("▶")
        self.btn_next = QPushButton("▶▶")
        self.volume_slider = QSlider(Qt.Horizontal)
        self.volume_slider.setMaximumWidth(100)
        self.volume_slider.setValue(80)
        controls.addStretch()
        controls.addWidget(self.btn_prev)
        controls.addWidget(self.btn_play)
        controls.addWidget(self.btn_next)
        controls.addWidget(self.volume_slider)
        controls.addStretch()
        layout.addLayout(controls)

        # --- 歌词列表（可编辑） ---
        lyrics_group = QGroupBox("歌词（点击行跳转，拖拽调整时间）")
        lyrics_layout = QVBoxLayout()

        self.lyrics_list_widget = QListWidget()
        self.lyrics_list_widget.setAlternatingRowColors(True)
        self._populate_lyrics()
        lyrics_layout.addWidget(self.lyrics_list_widget)

        # 歌词编辑工具栏
        edit_toolbar = QHBoxLayout()
        self.btn_toggle_edit = QPushButton("✏️ 进入编辑模式")
        self.btn_offset_minus = QPushButton("<< -0.5s")
        self.btn_offset_plus = QPushButton("+0.5s >>")
        self.btn_reset = QPushButton("↻ 重置")
        self.btn_save = QPushButton("💾 保存到文件")
        self.btn_save.setStyleSheet("background: #4CAF50; color: white; padding: 6px;")
        self.lbl_edit_hint = QLabel("拖拽歌词行调整时间")
        self.lbl_edit_hint.setStyleSheet("color: #FF9800;")
        self.lbl_edit_hint.hide()
        edit_toolbar.addWidget(self.btn_toggle_edit)
        edit_toolbar.addWidget(self.btn_offset_minus)
        edit_toolbar.addWidget(self.btn_offset_plus)
        edit_toolbar.addStretch()
        edit_toolbar.addWidget(self.lbl_edit_hint)
        edit_toolbar.addWidget(self.btn_reset)
        edit_toolbar.addWidget(self.btn_save)
        lyrics_layout.addLayout(edit_toolbar)

        lyrics_group.setLayout(lyrics_layout)
        layout.addWidget(lyrics_group, stretch=1)

        self.setLayout(layout)

    def _load_cover(self, cover_label: QLabel):
        """异步加载封面图片到 QLabel（不阻塞主线程）。
        
        优先级：metadata.cover_data（已下载二进制）→ metadata.cover_url（远程 URL）→ 本地文件标签内嵌封面 → 默认占位图。
        使用 QPixmap 加载，若封面数据来自网络则在 QThread 中下载后通过信号回传。
        """
        from PyQt6.QtGui import QPixmap, QImage
        from PyQt6.QtCore import QBuffer
        import io

        # 1. 优先使用已下载的封面二进制
        if self.metadata.cover_data:
            img = QImage()
            if img.loadFromData(self.metadata.cover_data):
                pixmap = QPixmap.fromImage(img).scaled(
                    300, 300, Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation)
                cover_label.setPixmap(pixmap)
                return

        # 2. 尝试从本地文件标签读取内嵌封面
        try:
            from services.tag_writer import read_cover
            cover_bytes = read_cover(self.file_path)
            if cover_bytes:
                img = QImage()
                if img.loadFromData(cover_bytes):
                    pixmap = QPixmap.fromImage(img).scaled(
                        300, 300, Qt.AspectRatioMode.KeepAspectRatio,
                        Qt.TransformationMode.SmoothTransformation)
                    cover_label.setPixmap(pixmap)
                    return
        except Exception:
            pass

        # 3. 尝试同目录 cover.jpg
        import os
        cover_path = os.path.join(os.path.dirname(self.file_path), "cover.jpg")
        if os.path.exists(cover_path):
            pixmap = QPixmap(cover_path).scaled(
                300, 300, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation)
            if not pixmap.isNull():
                cover_label.setPixmap(pixmap)
                return

        # 4. 无封面 → 显示占位文字
        cover_label.setText("🎵\n无封面")
        cover_label.setStyleSheet("background: #333; border-radius: 8px; color: #666; font-size: 24px;")

    def _populate_lyrics(self):
        """填充歌词列表"""
        self.lyrics_list_widget.clear()
        for line in self.lyrics_ctrl.lines:
            text = f"[{self._ms_to_str(line.time)}] {line.text}"
            item = QListWidgetItem(text)
            self.lyrics_list_widget.addItem(item)

    @staticmethod
    def _ms_to_str(ms: int) -> str:
        minutes = ms // 60000
        seconds = (ms % 60000) / 1000
        return f"{minutes:02d}:{seconds:05.2f}"

    def _connect_signals(self):
        """连接信号"""
        self.btn_play.clicked.connect(self._toggle_play)
        self.player.positionChanged.connect(self._on_position_changed)
        self.player.durationChanged.connect(self._on_duration_changed)
        self.progress_slider.sliderMoved.connect(self.player.setPosition)
        self.volume_slider.valueChanged.connect(
            lambda v: self.audio_output.setVolume(v / 100))
        self.btn_prev.clicked.connect(lambda: self.player.setPosition(
            max(0, self.player.position() - 5000)))
        self.btn_next.clicked.connect(lambda: self.player.setPosition(
            min(self.player.duration(), self.player.position() + 5000)))
        self.lyrics_list_widget.itemClicked.connect(self._on_lyric_clicked)

        # 编辑模式
        self.btn_toggle_edit.clicked.connect(self._toggle_edit_mode)
        self.btn_offset_minus.clicked.connect(
            lambda: self._offset_lyrics(-500))
        self.btn_offset_plus.clicked.connect(
            lambda: self._offset_lyrics(500))
        self.btn_reset.clicked.connect(self._reset_lyrics)
        self.btn_save.clicked.connect(self._save_to_file)

    def _toggle_play(self):
        if self.player.playbackState() == QMediaPlayer.PlayingState:
            self.player.pause()
            self.btn_play.setText("▶")
        else:
            self.player.play()
            self.btn_play.setText("⏸")

    def _on_position_changed(self, pos):
        """播放进度变化 → 更新进度条 + 歌词高亮"""
        self.progress_slider.setValue(pos)
        current = pos // 1000
        total = self.player.duration() // 1000
        self.time_label.setText(f"{current//60:02d}:{current%60:02d}"
                                f" / {total//60:02d}:{total%60:02d}")

        # 高亮当前歌词行
        idx = self.lyrics_ctrl.get_current_index(pos)
        if idx >= 0 and idx < self.lyrics_list_widget.count():
            self.lyrics_list_widget.setCurrentRow(idx)
            self.lyrics_list_widget.scrollToItem(
                self.lyrics_list_widget.item(idx),
                QAbstractItemView.PositionAtCenter)

    def _on_duration_changed(self, duration):
        self.progress_slider.setRange(0, duration)

    def _on_lyric_clicked(self, item):
        """点击歌词行 → 跳转到该行时间"""
        idx = self.lyrics_list_widget.row(item)
        if 0 <= idx < len(self.lyrics_ctrl.lines):
            self.player.setPosition(self.lyrics_ctrl.lines[idx].time)

    def _toggle_edit_mode(self):
        """切换编辑模式。

        编辑模式下，双击歌词行弹出时间编辑对话框，
        而非通过拖拽行排序（行顺序由时间戳决定，拖拽排序无意义）。
        """
        self._is_editing = not getattr(self, '_is_editing', False)
        if self._is_editing:
            self.btn_toggle_edit.setText("✏️ 退出编辑")
            self.btn_toggle_edit.setStyleSheet("background: #FF9800; color: white;")
            self.lbl_edit_hint.show()
            # 编辑模式：切换双击行为为时间编辑
            try:
                self.lyrics_list_widget.itemDoubleClicked.disconnect()
            except TypeError:
                pass  # 首次进入时无连接，安全跳过
            self.lyrics_list_widget.itemDoubleClicked.connect(
                self._on_edit_lyric_time)
        else:
            self.btn_toggle_edit.setText("✏️ 进入编辑模式")
            self.btn_toggle_edit.setStyleSheet("")
            self.lbl_edit_hint.hide()
            # 退出编辑：恢复双击跳转行为
            try:
                self.lyrics_list_widget.itemDoubleClicked.disconnect()
            except TypeError:
                pass
            self.lyrics_list_widget.itemDoubleClicked.connect(
                self._on_lyric_double_clicked)

    def _on_lyric_double_clicked(self, item):
        """非编辑模式下双击歌词行 → 跳转到该行时间"""
        idx = self.lyrics_list_widget.row(item)
        if 0 <= idx < len(self.lyrics_ctrl.lines):
            self.player.setPosition(self.lyrics_ctrl.lines[idx].time)

    def _on_edit_lyric_time(self, item):
        """编辑模式双击 → 弹出时间编辑框"""
        idx = self.lyrics_list_widget.row(item)
        if idx < 0 or idx >= len(self.lyrics_ctrl.lines):
            return
        line = self.lyrics_ctrl.lines[idx]
        current_time = self._ms_to_str(line.time)
        new_time, ok = QInputDialog.getText(
            self, "编辑歌词时间",
            f"当前行: {line.text[:20]}...\n时间 (MM:SS.ss):",
            text=current_time)
        if ok and new_time:
            try:
                parts = new_time.split(":")
                minutes = int(parts[0])
                seconds = float(parts[1])
                new_ms = int(minutes * 60000 + seconds * 1000)
                self.lyrics_ctrl.adjust_line_time(idx, new_ms)
                self._refresh_lyrics_display()
            except (ValueError, IndexError):
                QMessageBox.warning(self, "格式错误", "时间格式应为 MM:SS.ss")

    # ── 歌词版本切换 ──
    def _switch_lyrics_version(self, direction: int):
        """切换歌词版本（← 上一个 / → 下一个）。

        LRCLIB 可能返回多条匹配结果，此方法在搜索结果间轮换。
        不同版本的歌词文本保存在 self._lyrics_versions 列表中。
        """
        if not hasattr(self, '_lyrics_versions') or not self._lyrics_versions:
            return
        self._lyrics_version_idx = (getattr(self, '_lyrics_version_idx', 0) + direction) % len(self._lyrics_versions)
        lrc_text = self._lyrics_versions[self._lyrics_version_idx]
        self.lyrics_ctrl.lines = self.lyrics_ctrl.parse_lrc(lrc_text)
        self._original_lrc = lrc_text
        self._refresh_lyrics_display()
        self.lbl_version.setText(f"版本 {self._lyrics_version_idx + 1}/{len(self._lyrics_versions)}")

    def set_lyrics_versions(self, versions: list[str]):
        """设置多版本歌词列表（由搜索阶段传入）"""
        self._lyrics_versions = versions
        self._lyrics_version_idx = 0

    def _offset_lyrics(self, offset_ms):
        """批量偏移"""
        self.lyrics_ctrl.offset_all(offset_ms)
        self._refresh_lyrics_display()
        offset_sec = offset_ms / 1000
        # 弹出浮动提示
        QToolTip.showText(
            self.btn_offset_plus.mapToGlobal(QPoint(0, 0)),
            f"偏移: {offset_sec:+.1f}s")

    def _reset_lyrics(self):
        """重置到原始歌词"""
        # 从原始 LRC 重新解析
        if self._original_lrc:
            self.lyrics_ctrl.lines = self.lyrics_ctrl.parse_lrc(
                self._original_lrc)
            self._refresh_lyrics_display()
            QToolTip.showText(
                self.btn_reset.mapToGlobal(QPoint(0, 0)),
                "已重置为原始歌词")

    def _save_to_file(self):
        """
        将编辑后的歌词保存到音频文件。
        保存方式：调用 tag_writer.write_lyrics（支持 MP3/FLAC/M4A/OGG/WMA/APE）
        + 生成同目录 LRC 文件。
        """
        if not self.lyrics_ctrl.lines:
            QMessageBox.warning(self, "提示", "没有歌词可保存")
            return

        # 生成 LRC 文本
        lrc_lines = []
        for line in self.lyrics_ctrl.lines:
            minutes = line.time // 60000
            sec = (line.time % 60000) / 1000
            lrc_lines.append(f"[{minutes:02d}:{sec:05.2f}]{line.text}")
        lrc_text = "\n".join(lrc_lines)

        # 写入文件标签（多格式路由，非 MP3-only）
        try:
            from services.tag_writer import write_lyrics
            write_lyrics(self.file_path, lrc_text)
        except Exception as e:
            QMessageBox.warning(self, "写入失败", f"标签写入失败: {e}")

        # 生成同目录 LRC 文件
        lrc_path = os.path.splitext(self.file_path)[0] + ".lrc"
        try:
            with open(lrc_path, "w", encoding="utf-8") as f:
                f.write(lrc_text)
        except Exception as e:
            QMessageBox.warning(self, "写入失败", f"LRC文件写入失败: {e}")

        QMessageBox.information(
            self, "保存成功",
            f"歌词已保存到:\n1. 文件标签\n2. {lrc_path}"
        )

    def _refresh_lyrics_display(self):
        """刷新歌词显示"""
        self._populate_lyrics()


# ── 歌词读写辅助函数（已委托给 services/tag_writer，此处仅保留读取封装）──
def read_lyrics_from_file(file_path: str) -> Optional[str]:
    """从文件标签读取歌词（通用格式路由）。

    实际委托 tag_writer 按格式读取；此处为 AuditionDialog 提供统一入口。
    """
    from services.tag_writer import read_lyrics
    return read_lyrics(file_path)
```

### 8B.4 窗口生命周期

```
用户双击文件 / 右键 → 试听
    │
    ├── AuditionDialog 创建
    │   ├── 读取文件元数据（封面、标题等）
    │   ├── 加载歌词（优先级：缓存→文件标签→LRCLIB→Meting）
    │   ├── 设置播放媒体（优先本地文件，无文件则用预览URL）
    │   └── 显示窗口
    │
    ├── 用户交互
    │   ├── 播放/暂停 → 控制 QMediaPlayer
    │   ├── 进度条拖动 → setPosition
    │   ├── 歌词同步 → positionChanged → 高亮 + 自动滚动
    │   └── 编辑歌词
    │       ├── 点击"编辑模式" → 歌词行可拖拽
    │       ├── 拖拽行 → adjustLineTime → 刷新显示
    │       ├── +/- 0.5s → offsetAll → 刷新显示
    │       └── 保存 → 写入文件标签 + 生成LRC文件
    │
    └── 关闭窗口
        ├── 停止播放
        ├── 清理 QMediaPlayer
        └── 释放资源
```

---

## 九、配置存储

### 9.1 配置文件路径

```python
from pathlib import Path
import os

CONFIG_DIR = Path(os.environ.get("APPDATA", ".")) / "AudioFileManager"
CONFIG_FILE = CONFIG_DIR / "config.json"
CACHE_DIR = CONFIG_DIR / "cache"
COVER_CACHE = CACHE_DIR / "covers"
LYRICS_CACHE = CACHE_DIR / "lyrics"
```

### 9.2 配置结构 (`config.json`)

```json
{
    "version": "3.0",
    "search": {
        "provider_order": ["itunes", "lrclib", "musicbrainz", "meting"],
        "provider_enabled": {
            "itunes": true,
            "lrclib": true,
            "musicbrainz": true,
            "meting": false
        },
        "meting": {
            "api_url": "https://api.injahow.cn/meting/",
            "server": "netease",
            "available_instances": [
                "https://api.injahow.cn/meting/",
                "https://api.amarea.cn/meting/",
                "https://api.crowya.com/meting/"
            ]
        },
        "request_timeout": 10,
        "total_timeout": 30
    },
    "network": {
        "rate_limiter": {
            "itunes":    {"concurrency": 2, "interval": 0.0,  "max_retries": 2},
            "lrclib":    {"concurrency": 3, "interval": 0.0,  "max_retries": 3},
            "musicbrainz": {"concurrency": 1, "interval": 1.1, "max_retries": 2},
            "meting":    {"concurrency": 1, "interval": 0.8,  "max_retries": 2}
        },
        "global_timeout": 30,
        "retry_backoff_base": 2.0
    },
    "organize": {
        "output_dir": "E:\\整理后的音乐库",
        "by_artist": true,
        "by_album": true,
        "album_with_year": true,
        "unknown_dir": true,
        "delete_source": false
    },
    "filename": {
        "template": "{title} - {artist}",
        "separator": "-",
        "strip_number_prefix": true
    },
    "artist_db": {
        "sync_on_first_run": true,
        "sync_count": 500,
        "builtin_artists_path": "data/artist_db.json",
        "user_data_path": "data/user_artists.json"
    },
    "save_mode": "tags",
    "encoding": {
        "enabled": true,
        "charset": "UTF-8"
    },
    "duplicate": {
        "mode": "skip",
        "use_hash": false,
        "hash_algorithm": "sha256"
    },
    "filename_conflict": {
        "mode": "keep_best_quality",
        "skip_existing": false,
        "quality_priority": ["bitrate", "sample_rate", "format"]
    },
    "last_input_dir": "",
    "last_output_dir": ""
}
```

---

### 9.3 配置加载与映射（settings.py）

`config/settings.py` 是配置的唯一加载/落盘入口，负责把 `config.json` 映射为内存配置对象：

- `search` 块 → `SearchConfig`（`search/config.py`）：`provider_order`、`provider_enabled`、`request_timeout`、`total_timeout` 直接对应；`meting.api_url` / `meting.server` 映射为 `meting_api_url` / `meting_server`；`meting.available_instances` 由 `MetingProvider` 用于实例故障转移，不进入 `SearchConfig`。`PROVIDER_CAPABILITIES` 是代码内常量，不持久化。
- `organize` / `filename` / `artist_db` / `encoding` / `duplicate` / `filename_conflict` 等块 → 各自的配置对象或字典（见 §9.2）。
- `version` 字段用于配置迁移：当 `config.json` 结构变更时递增，加载时若版本过低则合并默认值后写回；该字段与文档版本号互相独立。

设置界面修改后调用 `settings.save()`，仅覆盖被修改的块并保留其余字段。

#### 9.3.1 配置版本迁移实现

```python
# config/settings.py — 配置迁移映射

# 版本号 → 迁移函数（对原始 dict 就地修改）
MIGRATIONS: dict[str, callable] = {}

def _migrate_v1_to_v2(cfg: dict):
    """v1→v2: 新增 meting 配置子块"""
    cfg.setdefault("meting", {
        "api_url": "https://api.injahow.cn/meting/",
        "server": "netease",
        "available_instances": [
            "https://api.injahow.cn/meting/",
            "https://api.amarea.cn/meting/",
        ]
    })

MIGRATIONS["2.0"] = _migrate_v1_to_v2

def _migrate_v2_to_v3(cfg: dict):
    """v2→v3: 新增 encoding / duplicate / filename_conflict 配置块"""
    cfg.setdefault("encoding", {"enabled": False, "charset": "UTF-8"})
    cfg.setdefault("duplicate", {"mode": "skip", "use_hash": False, "hash_algorithm": "sha256"})
    cfg.setdefault("filename_conflict", {
        "mode": "keep_best_quality",
        "skip_existing": False,
        "quality_priority": ["bitrate", "sample_rate", "format"]
    })

MIGRATIONS["3.0"] = _migrate_v2_to_v3

def _parse_version(ver: str) -> tuple[int, ...]:
    """将版本号字符串解析为可比较的元组，如 '3.0' → (3, 0)"""
    try:
        return tuple(int(x) for x in ver.split("."))
    except (ValueError, AttributeError):
        return (0,)

def load_config(path: str, defaults: dict) -> dict:
    """加载配置并按需迁移"""
    import json
    if not os.path.exists(path):
        return defaults
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    current_version = cfg.get("version", "1.0")
    # 按版本号顺序执行所有需要的迁移（使用元组比较，避免字符串排序错误）
    for ver in sorted(MIGRATIONS.keys(), key=_parse_version):
        if _parse_version(ver) > _parse_version(current_version):
            MIGRATIONS[ver](cfg)
            cfg["version"] = ver
    # 合并默认值（补齐缺失字段）
    merged = {**defaults, **cfg}
    return merged
```

> 迁移函数的 key 为**目标版本号**，通过 `_parse_version` 元组比较判断 `current_version < ver` 时触发（避免字符串比较导致 `"10.0" < "2.0"` 的错误）。新增配置块时只需写一个新迁移函数并注册到 `MIGRATIONS` 字典即可。

---

## 十、批处理流程

### 10.1 完整处理流程

```
用户选择源文件夹
    │
    ├── 1️⃣ 扫描文件（递归）
    │    ├── 使用 `os.walk()` 递归遍历所有子文件夹
    │    ├── 支持多层嵌套：E:\音乐库\周杰伦\魔杰座\ 等深度不限
    │    ├── 过滤支持的音频扩展名 (.mp3/.flac/.m4a/.ogg/.wma/.ape)
    │    ├── 跳过系统隐藏文件夹（如 $RECYCLE.BIN）
    │    ├── 统计子文件夹数 + 音频文件总数，显示在工具栏
    │    ├── 文件列表按子文件夹分组展示（分组标题）
    │    ├── 读取现有 ID3 标签
    │    └── 填充文件列表（含状态: 待处理/已识别/已处理/失败）
    │
    ├── 2️⃣ 文件名解析（离线）
    │    ├── UTF-8 读取 → Unicode NFKC 规范化（全角→半角）
    │    ├── 从文件名提取 (歌曲名, 歌手)
    │    ├── 从已有 ID3 标签读取已知信息（优先于文件名）
    │    └── 状态 → "已识别"
    │
    ├── 3️⃣ 搜索补全（在线，同步 + ThreadPoolExecutor 并行，最大并发 3）
    │    ├── 按配置优先级依次尝试各 Provider
    │    ├── 每个请求经过 RateLimiter 控制频率（§10.7）
    │    ├── iTunes → 元数据+封面+Preview
    │    ├── MusicBrainz → 降级备选（严格 1 请求/秒）
    │    ├── Meting(如启用) → 中文补充（间隔 800ms）
    │    └── 进度实时更新
    │
    ├── 4️⃣ 元数据写入
    │    ├── 写入 ID3 标签（歌曲名/歌手/专辑/年份/流派/曲目号）
    │    ├── 保存封面为 cover.jpg（专辑目录下）
    │    ├── 保存 LRC 歌词（歌曲名.lrc，与歌曲同目录）
    │    └── 重命名文件 → 歌曲名-歌手.ext
    │
    ├── 5️⃣ 目录整理
    │    ├── 创建 歌手\专辑(年份)\ 目录结构
    │    ├── 根据配置选择整理方式：
    │    │   ├── delete_source=false → 复制文件到目标目录（保留源文件）
    │    │   └── delete_source=true  → 移动文件到目标目录（删除源文件）
    │    └── 将歌词文件、封面一并处理（复制或随文件移动）
    │
    ├── ⏹ 用户点击「停止」
    │    ├── 当前正在处理的一个文件允许完成
    │    ├── 剩余文件全部标记为 pending
    │    └── batch_state.json 保存当前进度
    │
    └── 6️⃣ 生成报告
         ├── 成功数 / 失败数
         └── 失败文件的错误原因
```

#### 10.1.1 文件扫描实现

```python
# processor/file_scanner.py
import os
from pathlib import Path
from dataclasses import dataclass
from typing import Optional

# 支持的音频格式
AUDIO_EXTENSIONS = {".mp3", ".flac", ".m4a", ".ogg", ".wma", ".ape"}

# 需要跳过的系统隐藏文件夹
SKIP_DIRS = {"$RECYCLE.BIN", "System Volume Information", "lost+found"}

@dataclass
class AudioFileEntry:
    file_path: str               # 文件完整路径
    relative_path: str           # 相对于源目录的路径（用于分组显示）
    dir_name: str                # 所在子文件夹名（如 "魔杰座"）
    size: int                    # 文件大小（字节）
    ext: str                     # 扩展名
    title: str = ""              # 解析出的歌曲名
    artist: str = ""             # 解析出的歌手
    status: str = "pending"      # pending | parsed | searching | done | failed
    name_normalized: str = ""    # 归一化后的「歌曲名-歌手」（去重用）
    file_hash: Optional[str] = None  # SHA-256（搜索阶段前计算，L3 去重用）
    is_duplicate: bool = False   # 是否被判定为重复文件
    duplicate_of: Optional[str] = None  # 指向重复源文件的路径

class FileScanner:
    """递归文件扫描器"""

    def __init__(self, base_dir: str):
        self.base_dir = Path(base_dir)

    def scan(self) -> list[AudioFileEntry]:
        """
        递归扫描 base_dir 下的所有子文件夹，收集音频文件。
        返回按目录排序的文件列表（同一子文件夹的文件连续排列）。
        """
        entries = []
        for root, dirs, files in os.walk(self.base_dir, topdown=True):
            # 跳过隐藏文件夹
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith('.')]

            for f in sorted(files):
                ext = Path(f).suffix.lower()
                if ext not in AUDIO_EXTENSIONS:
                    continue

                full_path = Path(root) / f
                rel_path = full_path.relative_to(self.base_dir)

                # 文件大小（被锁定/无权限时降级为 0）
                try:
                    size = full_path.stat().st_size
                except OSError:
                    size = 0

                entry = AudioFileEntry(
                    file_path=str(full_path),
                    relative_path=str(rel_path),
                    dir_name=Path(root).name,
                    size=size,
                    ext=ext
                )
                entries.append(entry)

        return entries

    def pre_read_id3(self, entries: list[AudioFileEntry],
                     metadata_reader=None) -> list[AudioFileEntry]:
        """
        扫描后对每个文件尝试读取已有 ID3 标签，优先填充 title/artist。
        有 ID3 → 直接填充；无 ID3 → 留空，交给 FileNameParser 解析。

        调用时机：扫描完成后、 FileNameParser 解析前。
        """
        if not metadata_reader:
            return entries

        for entry in entries:
            try:
                id3_meta = metadata_reader.read(entry.file_path)
                if id3_meta and id3_meta.get("title"):
                    entry.title = id3_meta["title"]
                    entry.artist = id3_meta.get("artist", "")
                    entry.status = "parsed"
                else:
                    entry.status = "pending"  # 交给 FileNameParser 解析
            except OSError:
                entry.status = "failed"
                logger.warning(f"无法读取 ID3: {entry.file_path}")
        return entries

    @staticmethod
    def get_scan_summary(base_dir: str, entries: list[AudioFileEntry]) -> dict:
        """返回扫描统计信息（用于工具栏显示）"""
        subdirs = set()
        for entry in entries:
            # 获取相对于 base_dir 的父目录
            rel = Path(entry.relative_path).parent
            if str(rel) != ".":
                subdirs.add(str(rel))

        return {
            "total_files": len(entries),
            "subfolder_count": len(subdirs),
            "total_size_mb": sum(e.size for e in entries) / (1024 * 1024),
            "base_dir": base_dir
        }
```

**在 UI 中的调用方式**:

```python
# 扫描阶段：点击「选择文件夹」后
def on_select_folder(self):
    folder = QFileDialog.getExistingDirectory(self, "选择音频文件夹")
    if not folder:
        return

    scanner = FileScanner(folder)
    entries = scanner.scan()
    summary = FileScanner.get_scan_summary(folder, entries)

    # 更新工具栏统计
    self.toolbar_info.setText(
        f"当前: {folder}    {summary['subfolder_count']} 个子文件夹 · "
        f"{summary['total_files']} 个音频文件 ({summary['total_size_mb']:.0f} MB)"
    )

    # 填充文件列表（按子文件夹分组）
    self.file_list_widget.populate(entries)
```

**文件列表分组展示逻辑**:

```python
# 按子文件夹分组的 TreeWidget
def populate(self, entries: list[AudioFileEntry]):
    self.clear()
    current_dir = None
    group_item = None

    for entry in entries:
        if entry.dir_name != current_dir:
            # 创建分组标题
            current_dir = entry.dir_name
            group_item = QTreeWidgetItem([f"📁  {entry.dir_name}\\", ""])
            group_item.setFlags(group_item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            font = group_item.font(0)
            font.setBold(True)
            group_item.setFont(0, font)
            self.addTopLevelItem(group_item)

        # 在该分组下添加文件
        child = QTreeWidgetItem([
            f"🎵  {Path(entry.file_path).name}",
            entry.status
        ])
        child.setData(0, Qt.ItemDataRole.UserRole, entry)
        group_item.addChild(child)
```

**代码位置**: `processor/file_scanner.py`, `ui/file_list_widget.py`

---

### 10.2 输入目录例子

**处理前**:
```
E:\下载音乐\
├── 周杰伦\
│   ├── 魔杰座\
│   │   ├── 稻香.mp3
│   │   └── 01_青花瓷-周杰伦.mp3
│   └── 七里香\
│       ├── 七里香-周杰伦.mp3
│       └── 夜曲.mp3
├── 欧美\
│   └── English Song.mp3
└── 散乱\
    └── [搬运]未知来源.mp3
```
**处理后**:
```
E:\整理后的音乐库\
├── 周杰伦\
│   ├── 魔杰座 (2008)\
│   │   ├── 稻香-周杰伦.mp3
│   │   ├── 稻香-周杰伦.lrc
│   │   └── cover.jpg
│   ├── 七里香 (2004)\
│   │   ├── 七里香-周杰伦.mp3
│   │   ├── 七里香-周杰伦.lrc
│   │   └── cover.jpg
│   └── 依然范特西 (2006)\
│       ├── 夜曲-周杰伦.mp3
│       ├── 青花瓷-周杰伦.mp3
│       └── cover.jpg
└── Some Artist\
    └── Some Album (2020)\
        ├── Some English Song-Some Artist.mp3
        └── cover.jpg
```

> 整理说明：文件名标准化后的 `歌曲名-歌手.ext` 格式，在整理过程中已消除大部分重复可能性。但仍需通过文件内容哈希最后兜底。

---

### 10.3 元数据搜索与写入顺序

批处理中单个文件的处理顺序（详见 §7.7 协作关系）：

1. **文件名解析**（离线）→ 提取 `(title, artist, album)`
2. **元数据搜索**（在线）→ iTunes → MusicBrainz → Meting 降级链
3. **封面+歌词补全**（在线）→ `enrich_cover_and_lyrics` 统一补全，优先复用已有 cover_url
4. **编码统一**（离线，如启用）
5. **标签写入**（离线）→ `save_metadata_to_tags` 按 save_mode 路由
6. **目录整理**（离线）→ 按歌手/专辑分目录 + 文件名冲突处理

> 搜索失败时创建空 `TrackMetadata`，不中断批处理流程。

---

### 10.4 中断/崩溃恢复机制

批量处理几百个文件时，用户可能中途点击「停止」，或程序意外崩溃。需要一个可靠的恢复方案：

#### 10.4.1 状态持久化（断点续传）

```
# processor/batch_state.py

# 状态文件: %APPDATA%/AudioFileManager/batch_state.json
# 格式: 单个 JSON 对象，按 file_path 为 key 索引，便于原子覆盖写入
# 写入策略: 先写临时文件 → os.replace() 原子替换，避免崩溃留下半截文件

{
  "version": 1,
  "source_dir": "E:\\下载音乐",
  "updated_at": "2026-07-12T14:30:45",
  "files": {
    "E:\\下载音乐\\稻香.mp3": {
      "file_hash": "sha256=ab12cd34...",
      "size": 8520000,
      "modified_time": "2026-07-12T14:30:00",
      "status": "done",
      "current_step": "",
      "error": null,
      "processed_at": "2026-07-12T14:30:45"
    }
  }
}
```

**原子写入实现**:

```python
import json, os, tempfile, logging

logger = logging.getLogger(__name__)

def save_state(state_file: str, data: dict):
    """原子写入状态文件：写临时文件→原子替换，防止崩溃损坏"""
    tmp = state_file + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, state_file)  # Windows 上原子操作
    except Exception as e:
        logger.warning(f"状态文件写入失败: {e}")  # 不阻塞批处理主流程，但记录警告
```

**应用关闭优雅退出**:

```python
# ui/main_window.py
def closeEvent(self, event):
    """主窗口关闭时检查批处理状态"""
    if hasattr(self, 'batch_processor') and self.batch_processor.isRunning():
        reply = QMessageBox.question(
            self, "批处理进行中",
            "批处理尚未完成。是否等待当前文件处理完毕后保存进度并退出？",
            QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel
        )
        if reply == QMessageBox.Yes:
            self.batch_processor.stop()         # 发出停止信号
            self.batch_processor.wait(5000)     # 等待最多 5 秒
            event.accept()
        elif reply == QMessageBox.No:
            self.batch_processor.terminate()     # 强制终止
            event.accept()
        else:
            event.ignore()                       # 取消关闭
    else:
        event.accept()
```

**运行流程中的断点逻辑**:

```
程序启动 / 用户选择源文件夹
    │
    ├── 检查 batch_state.json 是否存在
    │   ├── 不存在 → 全新处理
    │   └── 存在 → 询问用户：
    │       ├── "检测到上次有未完成的批处理，是否继续？"
    │       │   ├── [继续] → 读取状态文件，status=done 的跳过
    │       │   ├── [重新处理] → 清空状态文件，从头开始
    │       │   └── [取消] → 不做处理
    │       └── 用户选择后开始对应流程
    │
    └── 每次处理完一个文件，更新状态文件
        ├── 文件处理成功后 status → "done"，写入 processed_at
        └── 文件处理失败后 status → "failed"，记录 error 信息
```

#### 10.4.2 「停止」按钮实现

```python
# 停止信号（线程安全）
import threading
from PyQt6.QtCore import QThread, pyqtSignal

class BatchProcessor(QThread):
    # 进度信号（细粒度）：(current_file_index, total_files, sub_step_name, sub_step_index, sub_step_total)
    progress_updated = pyqtSignal(int, int, str, int, int)
    file_finished = pyqtSignal(str, str)     # file_path, status

    def __init__(self, files: list[str], config: dict, state_file: str):
        super().__init__()
        self._stop_event = threading.Event()  # 线程安全的停止信号
        self.files = files
        self.config = config
        self.state_file = state_file

    def stop(self):
        """外部调用：点击「停止」按钮触发"""
        self._stop_event.set()

    @property
    def _is_stopped(self) -> bool:
        return self._stop_event.is_set()

    def run(self):
        for i, file_path in enumerate(self.files):
            if self._is_stopped:
                logger.info(f"用户手动停止批处理，已处理 {i}/{len(self.files)} 个文件")
                break

            self.progress_updated.emit(i, len(self.files), "searching", 0, 0)

            try:
                result = self._process_one(file_path, i, len(self.files))
                self._save_state(file_path, "done", result)
                self.file_finished.emit(file_path, "done")
            except Exception as e:
                self._save_state(file_path, "failed", str(e))
                self.file_finished.emit(file_path, "failed")

            if self._is_stopped:
                break
```

**用户界面交互**:

```
点击「▶ 开始处理」 → 按钮变为「⏹ 停止」
    │
    ├── 文件逐个处理，进度条更新
    ├── 点击「⏹ 停止」
    │   ├── 当前正在处理的一个文件允许完成
    │   ├── 剩余文件全部标记为 pending
    │   └── batch_state.json 保存当前进度
    │
    └── 再次点击「▶ 开始处理」
        ├── 检测到未完成状态
        ├── 询问用户：继续上次进度 / 重新处理 / 取消
        └── 继续 → 跳过 status=done 的文件，从第一个 pending 开始
```

#### 10.4.3 程序崩溃恢复

```python
# 启动时执行
def recover_from_crash(state_file: str) -> dict | None:
    """检查异常退出留下的状态文件（单 JSON 对象格式）"""
    if not Path(state_file).exists():
        return None

    try:
        with open(state_file, 'r', encoding='utf-8') as f:
            data = json.load(f)
    except json.JSONDecodeError:
        # 文件损坏（异常崩溃导致）→ 无法恢复
        logger.warning("状态文件损坏，无法恢复")
        return None

    files = data.get("files", {})
    unfinished = [k for k, v in files.items() if v["status"] not in ("done", "failed")]
    done_count = len([k for k, v in files.items() if v["status"] == "done"])

    if unfinished or done_count > 0:
        return {
            "source_dir": data.get("source_dir", ""),
            "records": files,
            "done_count": done_count,
            "unfinished_count": len(unfinished),
            "total": len(files)
        }
    return None
```

**使用场景**:

| 场景 | 表现 |
|:-----|:------|
| 用户点击「停止」 | 当前文件完成后停止，状态文件完整（原子写入），下次可选继续 |
| 程序窗口被强行关闭 | 当前正在处理的一个文件可能处于中间态，状态文件完整（最后成功写入的版本） |
| 程序闪退/崩溃 | 若崩溃发生在原子写入之前，状态文件保持上一次完整版本；若发生在 os.replace 期间，Windows 保证原子性，不产生半截文件 |
| 下次启动 | 检测到状态文件 → 提示用户恢复或重新处理 |

#### 10.4.4 状态文件生命周期

```
第一次点击「开始处理」 → 创建 batch_state.json
    │
    ├── 每处理完一个文件 → 追加一行
    ├── 用户点击停止 → 状态保留
    ├── 程序崩溃 → 状态保留
    │
    └── 用户选择「重新处理」→ 删除旧的状态文件
    └── 用户选择「继续」→ 读取状态文件，跳过已完成
    └── 所有文件都 done → 状态文件保留，下次启动时用户选择「全新处理」即可
```

**代码位置**: `processor/batch_state.py`, `processor/batch_processor.py`

---

### 10.5 文件冲突与重复处理

批处理过程中，可能遇到两类问题：

| 场景 | 本质 | 检测方法 |
|:-----|:------|:---------|
| **内容重复** | 两个文件完全相同（同一首歌下载了两次，或复制了一份） | SHA-256 完全一致 |
| **文件名冲突** | 两个文件不同（不同版本/音质/来源），整理后产生相同文件名 `稻香-周杰伦.mp3` | 目标目录已存在同名文件 |

前者需要去重，后者需要避免覆盖。

---

#### 10.5.1 内容重复检测（哈希精确匹配）

| 检测层级 | 方法 | 识别时机 |
|:---------|:-----|:---------|
| **L1 — 文件名+大小** | 同目录下归一化名称 + 文件大小一致 | 扫描阶段 |
| **L2 — 文件内容哈希** | SHA-256 哈希比对（最精确，也是最终防线） | 搜索阶段前 |

```python
from processor.file_scanner import AudioFileEntry

class DuplicateDetector:
    """重复文件检测器 — 检测内容完全相同的文件（复用 §10.1.1 的 AudioFileEntry）"""

    def __init__(self, config: dict):
        self.config = config
        self.seen_by_name: dict[str, list[AudioFileEntry]] = {}   # 名称+大小 去重表
        self.seen_by_hash: dict[str, AudioFileEntry] = {}          # 哈希去重表

    def check(self, entry: AudioFileEntry) -> AudioFileEntry:
        """
        双重检测，返回标记后的 AudioFileEntry。
        - L1: 同目录下同名文件（归一化名称）+ 大小一致
        - L2: 文件内容哈希完全一致（精确，最终防线）
        """
        # L1: 归一化名称 + 大小
        key = f"{entry.name_normalized}|{entry.size}"
        if key in self.seen_by_name:
            entry.is_duplicate = True
            entry.duplicate_of = self.seen_by_name[key][0].file_path
            return entry
        self.seen_by_name.setdefault(key, []).append(entry)

        # L2: 文件哈希（精确检测）
        if entry.file_hash:
            if entry.file_hash in self.seen_by_hash:
                entry.is_duplicate = True
                entry.duplicate_of = self.seen_by_hash[entry.file_hash].file_path
            else:
                self.seen_by_hash[entry.file_hash] = entry

        return entry
```

#### 10.5.2 内容重复处理策略

| 模式 | 行为 | 适用场景 |
|:-----|:------|:---------|
| **skip（跳过）** | 检测到重复文件时，跳过且不做任何操作 | 默认安全行为，不丢失任何文件 |
| **overwrite（覆盖）** | 用新文件替换旧文件 | 下载了更高音质版本替换旧版本 |
| **keep_all（保留全部）** | 不视为重复，两个文件都保留 | 同一首歌不同版本（不同长度/音质） |
| **ask（询问）** | 弹窗让用户选择当前文件如何处理 | 首次运行时或少量文件场景 |

> ⚠️ **注意**: 对于内容重复的文件，不管选什么模式，最终文件名都是一样的（`稻香-周杰伦.mp3`），所以 keep_all 的含义是让第二个文件保留在原目录不动，不被整理到输出目录。

---

#### 10.5.3 文件名冲突处理策略

**这是更常见的场景**：两首不同的歌曲（不同版本、不同音质、不同来源）整理后产生相同的目标文件名 `稻香-周杰伦.mp3`，Windows 文件系统不允许同一目录下存在同名文件。

**五种冲突处理模式**:

| 模式 | 行为 | 适用场景 |
|:-----|:------|:---------|
| **keep_first** | 保留第一个已存在的文件，后续同名文件跳过不处理 | 已有高质量收藏，不想要更低版本 |
| **keep_last** | 始终用新文件覆盖旧文件 | 想用最新的版本替换旧的（原名 overwrite） |
| **keep_best_quality** | 对比两个文件的音质参数，保留质量更好的那个 | 混合了 128kbps 和 320kbps 的下载，只留高音质 |
| **rename_new** | 新文件自动添加序号后缀，全部保留 | 想要所有版本（无损 + 有损都留着） |
| **ask** | 弹窗让用户现场决定：覆盖/重命名/跳过/保留音质好的 | 少量文件或不确定的场景 |

**设置 UI**:

```
📑 文件名冲突处理
┌──────────────────────────────────────┐
│  当目标目录已存在同名文件时：          │
│  ○ 保留第一个，跳过后续              │
│  ● 保留最后一个（后处理覆盖先处理）   │
│  ○ 保留音质最好的                    │
│  ○ 自动重命名（全部保留，添加序号）   │
│  ○ 每次询问我                        │
│                                       │
│  ☑ 跳过目标目录已存在的文件           │
│     （断点续传模式）                  │
└──────────────────────────────────────┘
```

**配置JSON字段**:

```json
{
    "duplicate": {
        "mode": "skip",
        "use_hash": false,
        "hash_algorithm": "sha256"
    },
    "filename_conflict": {
        "mode": "keep_best_quality",
        "skip_existing": false,
        "quality_priority": ["bitrate", "sample_rate", "format"]
    }
}
```

---

**音质对比核心逻辑**:

```python
from dataclasses import dataclass
from pathlib import Path
import re

@dataclass
class AudioQuality:
    """音频质量参数——从 mutagen 读取"""
    bitrate: int = 0           # 比特率 (kbps)
    sample_rate: int = 0       # 采样率 (Hz)
    format: str = ""           # 格式 (MP3/FLAC/M4A/OGG)
    channels: int = 0          # 声道数

    def score(self) -> float:
        """质量评分——数值越高越好"""
        score = 0.0
        # 格式权重（无损 > 有损）
        format_weights = {"FLAC": 100, "ALAC": 95, "WAV": 90, "AIFF": 85,
                          "APE": 80, "OGG": 60, "M4A": 55, "MP3": 50, "WMA": 45}
        score += format_weights.get(self.format.upper(), 30)

        # 比特率权重
        score += self.bitrate * 0.1

        # 采样率权重
        if self.sample_rate >= 96000:
            score += 40
        elif self.sample_rate >= 48000:
            score += 30
        elif self.sample_rate >= 44100:
            score += 20
        else:
            score += 5

        # 声道权重
        score += self.channels * 5

        return score

    def summary(self) -> str:
        """人类可读的质量概要，用于 ask 弹窗"""
        return f"{self.format} · {self.bitrate}kbps · {self.sample_rate}Hz · {self.channels}ch"

    @staticmethod
    def from_file(file_path: str) -> "AudioQuality":
        """从音频文件中读取质量参数"""
        from mutagen import File as MutagenFile
        try:
            audio = MutagenFile(file_path)
            if audio is None:
                return AudioQuality()

            info = audio.info
            # 比特率
            bitrate = getattr(info, 'bitrate', 0)
            if bitrate:
                bitrate = bitrate // 1000  # bps → kbps

            return AudioQuality(
                bitrate=bitrate,
                sample_rate=getattr(info, 'sample_rate', 0),
                format=Path(file_path).suffix[1:].upper(),
                channels=getattr(info, 'channels', 0)
            )
        except Exception:
            return AudioQuality()


def resolve_conflict(target_path: str, new_file_path: str,
                     mode: str = "rename_new",
                     quality_priority: list[str] | None = None) -> tuple[str, str]:
    """
    解决目标文件名冲突 + 智能文件选择。

    Args:
        target_path: 期望的目标路径
        new_file_path: 新文件的原始路径
        mode: "keep_first" | "keep_last" | "keep_best_quality" | "rename_new" | "ask"

    Returns:
        (实际使用的目标路径, 操作描述)
        - 如果是 keep_last，返回 target_path（覆盖）
        - 如果是 rename_new，返回一个新的不冲突路径
        - 如果是 keep_first，返回 "" 表示跳过
        - 如果是 keep_best_quality，可能跳过也可能覆盖
    """
    path = Path(target_path)
    if not path.exists():
        return target_path, "create"  # 目标不存在，直接写入

    if mode == "keep_first":
        return "", "skipped"  # 跳过，保留第一个

    if mode == "keep_last":
        return target_path, "overwrite"  # 覆盖

    if mode == "keep_best_quality":
        existing_quality = AudioQuality.from_file(str(path))
        new_quality = AudioQuality.from_file(new_file_path)

        if new_quality.score() > existing_quality.score():
            # 新文件质量更好，覆盖
            return target_path, f"overwrite (better quality: {new_quality.summary()} > {existing_quality.summary()})"
        else:
            # 已有文件质量更好（或相同），跳过
            return "", f"skipped (existing better: {existing_quality.summary()} >= {new_quality.summary()})"

    if mode == "rename_new":
        parent = path.parent
        stem = path.stem
        suffix = path.suffix

        # 解析已有序号，从下一个递增
        pattern = re.compile(rf"^{re.escape(stem)}\((\d+)\){re.escape(suffix)}$")
        num_suffixes = []
        if parent.exists():
            for f in parent.iterdir():
                if f.is_file():
                    m = pattern.match(f.name)
                    if m:
                        num_suffixes.append(int(m.group(1)))

        counter = max(num_suffixes) + 1 if num_suffixes else 2
        new_path = parent / f"{stem}({counter}){suffix}"
        return str(new_path), f"renamed (→ {new_path.name})"

    if mode == "ask":
        return target_path, "ask"  # 调用者弹窗

    return target_path, "overwrite"
```

---

**在批处理流程中的位置**:

```python
def _organize_step(self, file_path: str, meta: TrackMetadata):
    """目录整理步骤：文件名冲突处理"""

    target_dir = self._get_target_dir(meta)
    target_path = os.path.join(target_dir, f"{meta.title}-{meta.artist}{Path(file_path).suffix}")

    # 文件名冲突检测
    conflict_cfg = self.config["filename_conflict"]
    actual_path, action = resolve_conflict(
        target_path=target_path,
        new_file_path=file_path,
        mode=conflict_cfg["mode"],
        quality_priority=conflict_cfg.get("quality_priority")
    )

    if action.startswith("skipped"):
        # 跳过操作
        self._save_state(file_path, "skipped_conflict", action)
        return

    if action == "ask":
        # 弹窗询问用户
        self.ask_filename_conflict.emit(file_path, target_path)
        return

    if action.startswith("overwrite") or action == "rename":
        if self.config.get("delete_source", False):
            shutil.move(file_path, actual_path)
        else:
            shutil.copy2(file_path, actual_path)

        if action.startswith("overwrite"):
            self._save_state(file_path, "done_overwritten", action)
        else:
            self._save_state(file_path, "done_renamed", action)
```

---

**ask 模式弹窗 UI**（原「覆盖」选项改为保留第一个/最后一个/音质最好选项）：

```
┌───────────────────────────────────────────┐
│ ⚠️ 目标目录已存在同名文件                   │
│                                            │
│ 新文件: E:\下载\稻香(HiFi).mp3           │
│         大小: 42.8 MB · MP3 · 320kbps      │
│         来源: iTunes 搜索                  │
│                                            │
│ 已存在: E:\整理后\周杰伦\               │
│               稻香-周杰伦.mp3               │
│         大小: 8.2 MB · MP3 · 128kbps       │
│         来源: 自动补全                     │
│                                            │
│  [ 保留新文件 ]  [ 保留现有 ]  [ 保留音质最好的 ] │
│  [ 自动重命名 ]  [ 跳过 ]                  │
│                                            │
│  ☐ 对此歌手的同名文件都应用此选择           │
└───────────────────────────────────────────┘
```

**代码位置**: `processor/conflict_resolver.py`

---

#### 10.5.4 一键跳过同名文件（中断续传场景）

用户重跑批处理时，输出目录中已有部分文件。如果想跳过这些已有的文件，在设置中勾选：

```
☑ 跳过目标目录已存在的文件（断点续传模式）
   不覆盖也不重命名，仅跳过已整理完成的文件
```

这个选项与文件名冲突策略不同：它不是"冲突了怎么办"，而是"已知已存在就不处理"。批处理前扫描目标目录，对每个待处理文件提前判断输出路径是否存在，存在则直接标记为 `skipped_existing`。

**配置字段**:

```json
{
    "filename_conflict": {
        "mode": "rename_new",
        "suffix_style": "number",
        "skip_existing": false     # 跳过目标目录已存在的文件
    }
}
```

---

#### 10.5.5 用户界面交互（ask 模式）

```

┌─────────────────────────────────────────┐
│ ⚠️ 检测到重复文件                        │
│                                          │
│ 新文件: E:\下载音乐\稻香(2).mp3       │
│ 已有:   E:\整理后的音乐库\周杰伦\     │
│              魔杰座\稻香-周杰伦.mp3       │
│                                          │
│  大小: 8.2 MB  vs  8.1 MB               │
│  音质: 320kbps   vs  256kbps             │
│                                          │
│  [ 跳过 ]  [ 覆盖 ]  [ 保留全部 ]  [ 取消 ] │
│                                          │
│  ☐ 对所有重复应用此选择                    │
└─────────────────────────────────────────┘
```

**代码位置**: `processor/duplicate_detector.py`, `processor/conflict_resolver.py`, `processor/batch_processor.py`

---

### 10.6 文件结构的更新

```diff
 processor/
+├── batch_state.py             # 中断/崩溃恢复状态管理
+├── batch_processor.py         # 批处理调度器（含停止控制+重复检测集成）
+├── duplicate_detector.py      # 重复文件检测器
  ├── file_scanner.py            # 文件扫描
  ├── file_organizer.py           # 目录整理
  └── file_renamer.py            # 文件重命名
```

---

### 10.7 网络请求速率控制（API 友好策略）

不同搜索接口的请求速率限制差异巨大。不加控制地连续请求会导致 IP 被限或封禁。需要针对每个 Provider 设计独立的请求调度策略。

#### 10.7.1 各接口速率限制对照表

| Provider | 允许并发 | 建议频率 | 限速表现 | 被限后恢复 |
|:---------|:--------:|:---------|:---------|:-----------|
| **iTunes Search** | ✅ 可少量并发 | 每文件 1 次请求（无额外延迟） | 返回空结果/HTTP 500 | 自动重试 1 次 |
| **LRCLIB** | ✅ 可并发 | 每文件 1~2 次请求 | 429 Too Many Requests | 等待 60 秒后重试 |
| **MusicBrainz** | ❌ 严禁并发 | **每秒最多 1 次请求**（官方硬性要求） | 503/HTTP 429 | 等待 `Retry-After` 头部 |
| **CAA（专辑封面）** | ❌ 严禁并发 | 同 MusicBrainz，共用速率 | 同 MusicBrainz | 同 MusicBrainz |
| **Meting-API（公开实例）** | ❌ 严禁并发 | 每次请求后等待 **500ms~1s** | 空响应/连接重置/超时 | 等待 30~60 秒+降低频率 |
| **Meting-API（自建实例）** | ✅ 可少量并发（建议 2） | 每请求间隔 200ms | 偶发超时 | 自动重试 1 次 |

> MusicBrainz 官方要求见：https://musicbrainz.org/doc/XML_Web_Service/Rate_Limiting — **必须保持每秒不超过 1 次请求**，且禁止并行。违反会导致 IP 被临时封禁。

#### 10.7.2 设计方案：Provider 级别速率限制器

```python
# search/rate_limiter.py
import time
import threading
from collections import defaultdict

class RateLimiter:
    """
    按 Provider 独立计数的速率限制器（同步实现，基于 threading）。

    - 每个 Provider 有独立的请求间隔和并发上限
    - 支持令牌桶风格的等待（time.sleep 阻塞，运行在 QThread 工作线程，不阻塞 GUI）
    - 遇到 429 自动记录封禁时间并回退
    详见 §3 执行模型：速率控制使用 threading.Semaphore + time.sleep，非 asyncio。
    """

    def __init__(self):
        # { provider_id: (last_request_time, ban_until) }
        self._state: dict[str, tuple[float, float]] = defaultdict(
            lambda: (0.0, 0.0)
        )
        # 并发控制信号量（同步）
        self._semaphores: dict[str, threading.Semaphore] = {}
        self._lock = threading.Lock()

    def get_provider_config(self, provider_id: str) -> dict:
        """返回每个 Provider 的速率配置"""
        configs = {
            "itunes":    {"concurrency": 2, "interval": 0.0,  "max_retries": 2},
            "lrclib":    {"concurrency": 3, "interval": 0.0,  "max_retries": 3},
            "musicbrainz": {"concurrency": 1, "interval": 1.1, "max_retries": 2},
            "meting":    {"concurrency": 1, "interval": 0.8,  "max_retries": 2},
        }
        return configs.get(provider_id, {"concurrency": 1, "interval": 0.5, "max_retries": 1})

    def acquire(self, provider_id: str):
        """
        获取发送请求的许可（同步阻塞）。
        - 等待并发信号量
        - 等待间隔时间（确保频率不超标）
        - 如果处于封禁期，等待封禁结束
        """
        cfg = self.get_provider_config(provider_id)

        # 并发控制（防止并行请求突破限速）
        if provider_id not in self._semaphores:
            self._semaphores[provider_id] = threading.Semaphore(cfg["concurrency"])

        with self._semaphores[provider_id]:
            # 检查是否被封禁
            with self._lock:
                last_time, ban_until = self._state[provider_id]
            now = time.time()

            if ban_until > now:
                wait = ban_until - now
                time.sleep(wait)

            # 确保间隔时间
            elapsed = now - last_time
            if elapsed < cfg["interval"]:
                time.sleep(cfg["interval"] - elapsed)

            # 更新最后请求时间
            with self._lock:
                self._state[provider_id] = (time.time(), self._state[provider_id][1])

    def report_429(self, provider_id: str, retry_after: int = 60):
        """记录 429/限速响应，设置封禁期"""
        with self._lock:
            _, ban_until = self._state[provider_id]
            new_ban = time.time() + retry_after
            if new_ban > ban_until:
                self._state[provider_id] = (self._state[provider_id][0], new_ban)
```

#### 10.7.3 集成到 Provider 基类

```python
# search/provider.py — 基类增加速率控制
# 注意：此处在 §10.7 中展示基类与速率控制的集成关系，
# 完整实现见 §3.2 SearchProvider._request_with_rate_limit（持久化 httpx.Client + 懒加载 RateLimiter 单例）。
# 此处仅展示集成概要，避免与 §3.2 重复定义造成两套实现分叉。

import logging
import time

import httpx
from abc import ABC, abstractmethod
from search.rate_limiter import RateLimiter

logger = logging.getLogger(__name__)

class SearchProvider(ABC):

    # RateLimiter 单例由 §3.2 的 _get_rate_limiter() 懒加载，所有 Provider 共享。
    # 持久化 httpx.Client 由 §3.2 的 _clients dict 按 provider_id 缓存。
    # ⚠️ 不要在此处重新实例化 RateLimiter 或 httpx.Client，否则会产生多个独立实例导致速率控制失效。

    def _request_with_rate_limit(
        self, provider_id: str, url: str,
        params: dict = None, headers: dict = None, timeout: int = 5
    ) -> "httpx.Response | None":
        """
        带速率限制和自动重试的同步请求封装。
        完整实现见 §3.2 — 使用持久化 httpx.Client（复用 TLS 连接池），
        失败时返回 None（由调用方决定降级策略），不抛异常。
        """
        # 委托给 §3.2 的完整实现
        return SearchProvider._request_with_rate_limit_impl(
            self, provider_id, url, params, headers, timeout
        )
```

#### 10.7.4 各 Provider 的覆盖实现

```python
# search/itunes_provider.py — 不需要特殊处理，使用基类方法即可
class iTunesProvider(SearchProvider):
    provider_id = "itunes"
    display_name = "Apple iTunes Search"

    def search_metadata(self, title: str, artist: str) -> TrackMetadata | None:
        url = "https://itunes.apple.com/search"
        params = {"term": f"{title} {artist}", "media": "music", "limit": 5}
        resp = self._request_with_rate_limit("itunes", url, params)
        # 解析 iTunes 响应...


# search/musicbrainz_provider.py — 严格的 1 请求/秒
class MusicBrainzProvider(SearchProvider):
    provider_id = "musicbrainz"
    display_name = "MusicBrainz"

    def search_metadata(self, title: str, artist: str) -> TrackMetadata | None:
        url = "https://musicbrainz.org/ws/2/recording/"
        params = {
            "query": f"recording:{title} AND artist:{artist}",
            "fmt": "json", "limit": 5
        }
        # MusicBrainz 的 interval=1.1 已在 RateLimiter 中保证
        # 请求头必须包含 User-Agent（官方要求）
        headers = {"User-Agent": "AudioFileManager/1.0 (contact@example.com)"}
        resp = self._request_with_rate_limit("musicbrainz", url, params)
        # 解析...


# search/meting_provider.py — 最严格，间隔最长
class MetingProvider(SearchProvider):
    provider_id = "meting"
    display_name = "Meting-API (中文补充)"

    def search_metadata(self, title: str, artist: str) -> TrackMetadata | None:
        # Meting 的 concurrency=1 和 interval=0.8 已在 RateLimiter 中保证
        # 每处理完一个文件才请求一次 Meting，实际间隔远大于 0.8s
        url = f"{self.api_url}?server=netease&type=search&id={title}%20{artist}"
        resp = self._request_with_rate_limit("meting", url)
        # 解析...
```

#### 10.7.5 批处理中的请求节奏总控制

```python
# processor/batch_processor.py — 批处理中的网络请求节奏管理

def _process_one(self, file_path: str, file_index: int = 0, total_files: int = 1):
    """
    处理单个文件（从解析到写入），支持子步骤进度上报。
    注意：将文件的多个请求分散在时间线上，
    避免短时间内对同一个 Provider 连续请求。
    """
    total_steps = 4  # 解析 → 搜索 → 写入 → 整理

    # 步骤 1：解析（离线）
    self.progress_updated.emit(file_index, total_files, "parsing", 1, total_steps)
    # ... 解析逻辑 ...

    # 步骤 2：搜索（在线，子步骤: metadata / lyrics / cover）
    self.progress_updated.emit(file_index, total_files, "searching", 2, total_steps)
    metadata = self.engine.search_metadata(title, artist)  # 内部走 RateLimiter
    lyrics = self.engine.search_lyrics(title, artist, album)  # 内部走 RateLimiter
    cover = self.engine.fetch_cover(title, artist, album)  # 内部走 RateLimiter

    # 步骤 3：写入标签（离线）
    self.progress_updated.emit(file_index, total_files, "writing", 3, total_steps)

    # 步骤 4：整理（离线）
    self.progress_updated.emit(file_index, total_files, "organizing", 4, total_steps)
```

**为什么不需要额外的 global throttle**:

| 场景 | 实际效果 |
|:-----|:---------|
| 100 个文件，每个文件请求 MusicBrainz 1 次 | `interval=1.1` 确保每秒最多 1 次请求。100 个文件 ≈ 110 秒完成，Compliance 100% ✅ |
| 100 个文件，每个文件请求 Meting 1 次 | `interval=0.8`，但 `concurrency=1`。100 个文件 ≈ 80 秒完成 ✅ |
| 100 个文件，每个文件请求 iTunes 3 次（搜索+封面+歌词） | `concurrency=2` 允许少量并行，但自然分布在不同的文件时间点上 ✅ |
| 用户手动连续点击「网络搜索」 | 每次独立请求，RateLimiter 会强行等待间隔时间 ✅ |

#### 10.7.6 配置字段

```json
{
    "network": {
        "rate_limiter": {
            "itunes":    {"concurrency": 2, "interval": 0.0, "max_retries": 2},
            "lrclib":    {"concurrency": 3, "interval": 0.0, "max_retries": 3},
            "musicbrainz": {"concurrency": 1, "interval": 1.1, "max_retries": 2},
            "meting":    {"concurrency": 1, "interval": 0.8, "max_retries": 2}
        },
        "global_timeout": 30,
        "retry_backoff_base": 2.0
    }
}
```

#### 10.7.7 测试建议

```python
# 测试：确认 MusicBrainz 每秒不超过 1 次请求（同步，无需 pytest-asyncio）
def test_musicbrainz_rate_limit():
    limiter = RateLimiter()
    last_time = 0
    for _ in range(5):
        limiter.acquire("musicbrainz")
        now = time.time()
        assert now - last_time >= 1.0, f"请求间隔过短: {now - last_time:.2f}s"
        last_time = now
    print("✅ MusicBrainz 速率控制通过")

# 测试：429 后自动等待
def test_429_backoff():
    limiter = RateLimiter()
    limiter.report_429("meting", retry_after=5)
    start = time.time()
    limiter.acquire("meting")
    elapsed = time.time() - start
    assert elapsed >= 4.5, f"封禁等待不足: {elapsed:.2f}s"
    print("✅ 429 回退控制通过")
```

**代码位置**: `search/rate_limiter.py`（新增）, `search/provider.py`（修改基类）

---

### 10.8 文件锁定处理

Windows 上音频文件可能被其他程序（WMP、VLC、资源管理器预览）占用，导致 `PermissionError`。需要在所有文件操作位置增加重试机制。

#### 10.8.1 同步重试装饰器（用于扫描/同步文件操作）

```python
# utils/retry_on_locked.py
import time
import functools

def retry_on_locked(max_attempts: int = 3, delay: float = 1.0):
    """
    文件被锁定时自动重试的装饰器（同步版本）。
    捕获 PermissionError / OSError，等待 delay 秒后重试。
    仅用于同步文件操作（如扫描阶段、ID3 读取）。

    用法:
        @retry_on_locked(max_attempts=3, delay=1.0)
        def write_metadata(file_path, meta):
            audio = MP3(file_path, ID3=ID3)
            audio.save()
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_exc = None
            for attempt in range(max_attempts):
                try:
                    return func(*args, **kwargs)
                except (PermissionError, OSError) as e:
                    last_exc = e
                    if attempt < max_attempts - 1:
                        time.sleep(delay)
            raise last_exc
        return wrapper
    return decorator
```

#### 10.8.2 统一说明（v5.7 同步模型）

v5.7 起执行模型统一为**同步**（见 §3）：批处理在 `QThread` 工作线程中运行，文件操作和 `httpx` 请求均为同步阻塞调用，不使用 `asyncio` 事件循环。

因此**只需保留同步的 `retry_on_locked`**（§10.8.1）。原异步版本 `async_retry_on_locked`（基于 `asyncio.sleep` / `asyncio.to_thread`）已从方案中移除——在同步模型下不存在「阻塞事件循环」的问题，文件锁定重试直接由同步装饰器覆盖全部场景（扫描、写入、重命名、复制/移动）。

> 若未来需要在同一线程内并发处理多个文件，应使用 `concurrent.futures.ThreadPoolExecutor`（见 §3 与 §10.4），而非 `asyncio.gather`。

#### 10.8.3 受影响的操作与使用指引

| 操作 | 定位 | 风险 | 使用哪个装饰器 |
|:-----|:------|:------|:--------------|
| 读取 ID3 标签 | `metadata_reader.py` | 文件正在预览中 | `retry_on_locked`（同步） |
| 写入 ID3 标签 | `services/metadata_saver.py` / §7.5 | 文件正在被播放 | `retry_on_locked`（同步） |
| 文件重命名 | `file_renamer.py` | 文件被资源管理器锁定 | `retry_on_locked` |
| 复制/移动文件 | `file_organizer.py` | 文件被其他进程占用 | `retry_on_locked`（同步操作） |

---

### 10.9 离线模式

当网络不可用时，应用应降级为纯本地模式，避免所有搜索操作全部失败。

```python
# processor/batch_processor.py — 离线模式检测

class BatchProcessor(QThread):
    def __init__(self, ...):
        ...
        self._offline_mode = False

    def _check_network(self) -> bool:
        """检测网络连通性"""
        import socket
        try:
            socket.create_connection(("8.8.8.8", 53), timeout=3)
            return True
        except OSError:
            return False

    def run(self):
        self._offline_mode = not self._check_network()
        if self._offline_mode:
            logger.warning("网络不可用，进入离线模式：仅基于已有 ID3 和文件名处理")
        ...
```

**离线模式下的行为**:

| 正常模式 | 离线模式 |
|:---------|:---------|
| 网络搜索元数据 → 写入 ID3 | 仅从已有 ID3 读取；无 ID3 则从文件名解析 |
| 下载封面 → 嵌入 + cover.jpg | 跳过封面 |
| 下载歌词 → 嵌入 + .lrc | 仅保留已有歌词 |
| 搜索补全歌手名 | 仅使用 ArtistDB 本地库 + 文件名启发式 |
| 文件列表状态标记 "✅已处理" | 标记为 "📴 离线处理" |

**UI 提示**: 离线模式下工具栏显示 `📴 离线模式 — 仅处理本地元数据`。

---

## 十一、项目文件结构
AudioFileManager/
├── main.py                        # 程序入口
├── requirements.txt               # 依赖: PyQt6>=6.6 mutagen>=1.47 httpx>=0.27 Pillow>=10.0 chardet>=5.0
├── README.md                      # 使用说明
│
├── config/
│   ├── settings.py                # 配置管理
│   └── default_config.json        # 默认配置
│
├── search/
│   ├── engine.py                  # SearchEngine 搜索引擎
│   ├── provider.py                # SearchProvider 基类
│   ├── config.py                  # SearchConfig 配置模型
│   ├── itunes_provider.py         # iTunes Search
│   ├── lrclib_provider.py         # LRCLIB 歌词
│   ├── musicbrainz_provider.py    # MusicBrainz + CAA
│   └── meting_provider.py         # Meting-API（使用公开实例）
│
├── services/
│   ├── tag_writer.py              # 多格式标签写入（MP3/FLAC/M4A/OGG/WMA/APE）
│   ├── manual_search.py           # 手动搜索补全（元数据/歌词/封面并行，ThreadPoolExecutor）
│   ├── metadata_saver.py          # 元数据保存（标签/文件/两者，SaveMode 路由到 tag_writer）
│   └── encoding_service.py        # 文件名/标签编码检测→转换→回写
│
├── parser/
│   ├── __init__.py
│   ├── filename_parser.py         # FileNameParser（多策略解析）
│   ├── artist_db.py               # ArtistDB（本地歌手库 + MusicBrainz 同步）
│   └── metadata_reader.py         # 现有 ID3 标签读取
│
├── processor/
│   ├── batch_state.py              # 中断/崩溃恢复状态管理
│   ├── batch_processor.py          # 批处理调度器（停止控制+重复检测集成）
│   ├── duplicate_detector.py       # 内容重复检测器（哈希）
│   ├── conflict_resolver.py        # 文件名冲突解决器
│   ├── file_scanner.py             # 文件扫描
│   ├── file_organizer.py           # 目录整理
│   └── file_renamer.py             # 文件重命名
│
├── audition/
│   ├── audition_dialog.py         # 试听弹窗（AuditionDialog）
│   ├── lyrics_controller.py       # 歌词同步/编辑控制器（LyricsSyncController）
│   └── lyrics_io.py               # 歌词读写（标签/文件）
│
├── cache/
│   ├── cover_cache.py             # 封面缓存
│   └── lrc_cache.py               # 歌词缓存
│
├── ui/
│   ├── __init__.py
│   ├── main_window.py             # 主窗口
│   ├── file_list_widget.py        # 文件列表面板
│   ├── file_detail_panel.py       # 右侧文件详情面板（可收起）
│   ├── settings_dialog.py         # 设置对话框
│   └── progress_widget.py         # 进度显示
│
├── utils/
│   ├── logger.py                  # 日志
│   ├── helpers.py                 # 工具函数
│   └── retry_on_locked.py         # 文件锁定重试装饰器
│
├── docs/
│   └── 接口探索.md                # API 调研文档
│
└── tests/
    ├── test_filename_parser.py
    ├── test_artist_db.py
    ├── test_search_engine.py
    ├── test_rate_limiter.py
    ├── test_retry_on_locked.py
    ├── test_batch_state.py
    ├── test_conflict_resolver.py
```

### 测试策略

#### 测试框架

| 工具 | 用途 |
|:-----|:------|
| **pytest** | 单元测试 + 集成测试（同步，无需 pytest-asyncio） |
| **respx** | Mock HTTP 请求（防止测试时真调用 API） |
| **pytest-cov** | 覆盖率报告 |

#### 核心测试用例

| 模块 | 测试内容 | Mock 需求 |
|:-----|:---------|:-----------|
| `test_filename_parser.py` | 各种命名格式解析、Unicode 规范化、三段式（含 uncertain 标记） | 无（纯函数） |
| `test_search_engine.py` | Provider 优先级、启用/禁用、降级链、`fetch_cover` 速率控制 | 需要 respx mock |
| `test_rate_limiter.py` | 间隔控制、并发控制、429 回退、指数退避 | 无（纯逻辑，放宽时间断言 ±0.3s） |
| `test_retry_on_locked.py` | PermissionError 重试、超时后抛出 | 无（纯函数） |
| `test_batch_state.py` | 状态文件原子读写、崩溃恢复检测、JSON 损坏容错 | Mock 文件系统 |
| `test_conflict_resolver.py` | keep_first/keep_last/keep_best_quality/rename_new/ask | 无（纯函数） |
| `test_encoding_service.py` | 编码检测修复（Mojibake 场景）、目标字符集转换 | 无（纯函数） |
| `test_offline_mode.py` | 网络不可用时降级行为 | Mock socket |

#### 集成测试与性能测试

| 测试类型 | 内容 | 工具 |
|:---------|:-----|:-----|
| **端到端集成测试** | 放置混乱文件 → 批处理 → 验证输出目录结构/标签/歌词/封面 | pytest + 临时目录 |
| **Qt UI 测试** | 主窗口交互、详情面板、试听弹窗、设置对话框 | pytest-qt |
| **性能基准测试** | 100/500/1000 文件的批处理时间基准（含网络 mock） | pytest-benchmark |
| **Windows 特定测试** | 文件锁定场景、路径 > 260 字符、编码异常文件 | 需 Windows 环境 |

#### 测试命令

```bash
# 运行全部测试
pytest tests/ -v

# 含覆盖率
pytest tests/ --cov=. --cov-report=term-missing

# 仅运行不含网络的测试（离线模式）
pytest tests/ -m "not network"
```

---

## 十二、开发计划

| 阶段 | 内容 | 产出 | 工期 |
|:----:|:-----|:-----|:----:|
| **Phase 1** | 基础框架 | 项目结构、配置管理、日志 | 1 天 |
| **Phase 2** | 文件名解析器 | `FileNameParser` 支持多种命名模式 | 1 天 |
| **Phase 3** | 搜索架构 | `SearchEngine` + `SearchProvider` + `SearchConfig` | 1.5 天 |
| **Phase 4** | Provider 实现 + RateLimiter | iTunes、LRCLIB、MusicBrainz（接入 `_request_with_rate_limit`）、Meting 四个实现 + `rate_limiter.py` | 2 天 |
| **Phase 5** | 文件扫描+元数据读写 | 扫描目录（递归+分组）、读取 ID3、写入 ID3（含 `@retry_on_locked`） | 1.5 天 |
| **Phase 6** | 目录整理+重命名 | 按歌手/专辑分类、文件名标准化（含 Unicode NFKC 规范化）、`conflict_resolver.py` | 1.5 天 |
| **Phase 7** | **批处理调度器** | `batch_processor.py` + `batch_state.py`（断点续传）+ `duplicate_detector.py` + 停止按钮逻辑 | 2 天 |
| **Phase 8** | **主界面布局** | 左侧文件列表 + 右侧可收起详情面板 | 2 天 |
| **Phase 9** | **试听弹窗** | AuditionDialog：播放控制、歌词滚动、高亮同步 | 2 天 |
| **Phase 10** | **歌词编辑** | 拖拽调时间、批量偏移、保存到文件 | 1.5 天 |
| **Phase 11** | 设置界面 | 搜索配置 UI（拖拽排序、Meting 地址配置） | 1 天 |
| **Phase 12** | 缓存 + 打包 | 封面/歌词缓存、PyInstaller 打包 | 1.5 天 |

**总计**: 约 21 个工作日

### 关键依赖

```
PyQt6>=6.6.0
mutagen>=1.47.0
httpx>=0.27.0
Pillow>=10.0.0
```

> PyQt6 内置了 `QtMultimedia`（QMediaPlayer / QAudioOutput），无需额外安装音频播放库即可播放本地/网络音频文件。

---

## 十三、变更日志

| 版本 | 日期 | 变更内容 |
|:----:|:----:|:---------|
| v1.0 | 2026-07-12 | 初版：基础框架、搜索架构、Provider、批处理 |
| v2.0 | 2026-07-12 | 搜索接口改为插件式拖拽排序；新增 Meting-API 公开实例配置；新增输出根目录配置；新增右侧详情面板可编辑元数据+保存按钮；新增试听弹窗（播放控制+滚动歌词+拖拽调时间）；新增复制/移动整理方式选项；新增 UI 设计稿 HTML 原型 |
| v2.1 | 2026-07-12 | 新增「🌐 网络搜索信息」按钮（手动一键搜索并填入元数据）；新增「元数据保存方式」设置（标签/独立文件/两者）；新增「文字编码统一」设置（启用开关+目标字符集选择）；更新 UI 设计稿三处对应修改 |
| v3.0 | 2026-07-12 | 新增 7.4~7.7 实现方案章节：ManualSearchService 手动搜索实现（asyncio.gather 并行搜索）；MetadataSaver 保存方式控制（标签/文件/两者）；EncodingService 编码检测→转换→回写；三者协作关系与批处理入口调用顺序 |
| v4.0 | 2026-07-12 | 新增 10.4~10.6 工程稳健机制：中断/崩溃恢复（batch_state.json 断点续传、停止按钮实现、程序崩溃检测恢复）；重复文件处理（L1→L2→L3 三重检测、4 种处理模式 skip/overwrite/keep_all/ask、ask 模式弹窗 UI）；更新项目文件结构（新增 3 个 processor/ 文件）|
| v5.0 | 2026-07-12 | 重构 10.5 区分「内容重复（哈希相同）」与「文件名冲突（不同文件同名）」两个独立概念；新增文件名冲突处理策略（overwrite/rename_new/ask 三种模式 + rename_new 自动序号递增实现 + skip_existing 断点续传跳过选项）；修改 UI 设置面板（旧「重复文件处理」拆分为「内容重复检测」+「文件名冲突处理」两个独立区块）；新增 `processor/conflict_resolver.py` |
| v5.1 | 2026-07-12 | 文件名冲突处理从 3 种模式扩展为 5 种：增加 keep_first（保留第一个）、keep_best_quality（保留音质最好的，基于 bitrate/sample_rate/format/channels 综合评分）；新增 AudioQuality 类用于从 mutagen 读取音质参数并评分（格式权重+比特率+采样率+声道）；ASK 弹窗增加「保留音质最好的」选项；默认模式改为 keep_best_quality |
| v5.2 | 2026-07-12 | 递归扫描支持多层子文件夹：新增 FileScanner 使用 os.walk 递归扫描、跳过隐藏文件夹、返回 AudioFileEntry 含相对路径；文件列表按子文件夹分组展示（group-header）；工具栏增加统计数据（子文件夹数+音频文件总数+总大小）；详情面板增加路径信息 |
| v5.3 | 2026-07-12 | 新增 10.7 网络请求速率控制：各接口限速对照表（iTunes 可少量并发/LRCLIB 可并发/MusicBrainz 1 请求/秒官方硬限制/Meting 严禁并发）；Provider 级别 RateLimiter（独立信号量+间隔+429 封禁回退+指数退避）；集成到 SearchProvider 基类 `_request_with_rate_limit`；全局 JSON 配置项 |
| v5.4 | 2026-07-12 | 审阅修复：§4.3 MusicBrainz 标注临时 `_rate_limit()` 将被替换；§9.2 config.json 合并 `network.rate_limiter` / `duplicate` / `filename_conflict` 块；§10.7.3 签名修复为 `-> httpx.Response \| None`；新增 §10.8 文件锁定处理（`retry_on_locked` 装饰器）；§5.1 增加 Unicode NFKC 规范化；§12 增加测试策略章节、细化 12 阶段；§10.1 流程图增加停止分支；§5.2 增加三段式文件名解析示例；§10.2 输入目录改为子文件夹例子；IDE.md 同步窗口协作关系 |
| v5.5 | 2026-07-12 | 新增 §5.1.1 本地歌手库（ArtistDB）：4 级数据源（内置预置~200条、MusicBrainz 同步、用户纠错自动学习、文件名缓存）；§5.1 文件名解析重写为多策略链（多歌手分隔符→ArtistDB 匹配→长度+中英文+空格→姓氏判断）+ 三段式解析；§5.2 示例表更新（标注每行命中策略）；§9.2 config.json 新增 `artist_db` 配置块；§8.2 设置界面新增「🎤 本地歌手库」区块（[🔄 同步歌手库]按钮+状态显示）；§11 项目结构新增 `parser/artist_db.py` |
| v5.6 | 2026-07-12 | 全面审阅修复：①ArtistDB.lookup() 类型兼容（str→ArtistInfo）+ add_correction 缓存键剥离扩展名；②retry_on_locked 拆分同步/异步两个版本（async_retry_on_locked + asyncio.to_thread 指引）；③§7.7 移除不存在的 search(file_path)，改为 parser.parse→search_metadata_async；④FileScanner 新增 pre_read_id3() ID3 预读取 + stat() OSError 异常保护；⑤§11 tests/ 补全 4 个缺失测试文件；⑥§5.1.2 triple-quoted string 注释改为 # 注释 |
| v5.7 | 2026-07-12 | 同步模型收尾：①§10.7 RateLimiter 由 asyncio.Semaphore/await 改为 threading.Semaphore + time.sleep 同步实现；②§10.7.3 基类 `_request_with_rate_limit` 改为同步（httpx.Client，移除 AsyncClient/await）；③§10.7.4 / §10.7.7 Provider 实现与测试由 async/await 改为同步；④§10.8 移除 `async_retry_on_locked` 与 asyncio.to_thread 指引，仅保留同步 `retry_on_locked`；⑤§12 测试策略移除 pytest-asyncio；⑥§2 技术选型表 httpx 标注改为同步客户端；⑦行内误导性「异步」措辞（§5.1.1 歌手库同步、§7 详情面板封面加载、§10.1 搜索补全）改为同步/后台线程表述 |
| v5.8 | 2026-07-13 | 全面审阅修复（第2轮）：①§2 新增 PyQt6 GPLv3 许可证提示；②§3.2 `_get_rate_limiter` 由 @classmethod 改为 @staticmethod + 模块级单例，`_request_with_rate_limit` 改用持久化 httpx.Client 复用连接池；③§3.2 `SearchEngine.search_cover` 重命名为 `fetch_cover`（与 Provider 层 URL 返回值语义区分），`_download_cover` 增加 provider_id 参数走速率控制；④§4.1 iTunes `search_cover` 增加重复调用注释；⑤§5.1.1 `ArtistDB.lookup` 移除死代码；⑥§5.1.2 移除 `_COMMON_SURNAMES` 硬编码副本，统一由 ArtistDB 提供；`parse()` 返回四元组含 `is_uncertain` 标记；`_resolve_three_part` 返回 `is_uncertain=True` 供 UI 显示 "⚠️ 顺序不确定"；⑦§7.2 `_write_flac` 仅替换封面（type=3），保留 booklet/artist 等图片；⑧§7.6 `_detect_and_convert` 增加 ftfy 替代方案警告；⑨§8B.3 `_save_to_file` 改用 `tag_writer.write_lyrics`（支持 MP3/FLAC/M4A 等多格式），移除独立 `write_lyrics_to_file`；拖拽编辑改为双击弹窗输入时间；新增 `_switch_lyrics_version` 歌词版本切换 + `set_lyrics_versions` 多版本加载；⑩§9.3 新增配置版本迁移映射 `MIGRATIONS` + `load_config` 实现；⑪§10.1 批处理进度信号扩展为细粒度五元组（子步骤名+索引）；⑫§10.4 `batch_state.json` 由行式 JSON 改为单对象 + `os.replace` 原子写入；新增主窗口 `closeEvent` 优雅退出逻辑；⑬§10.5.1 修复 L1/L2/L3 编号缺口；⑭新增 §10.9 离线模式（网络检测+降级行为）；⑮§11 requirements.txt 补充 `chardet>=5.0`；⑯§12 测试策略扩展：新增 encoding/offline 单元测试 + 集成/Qt UI/性能/Windows 特定测试 |
| **v5.9** | **2026-07-13** | **全面审阅修复（第3轮，P0~P3 共 17 项）**：**P0 崩溃修复**：①§9.3.1 `MIGRATIONS.register` 替换为显式字典赋值（dict 无 register 方法）；②§9.3.1 版本号比较由字符串改为 `_parse_version` 元组比较（避免 `"10.0" < "2.0"` 错误）；③§7.7 批处理 `search_metadata` 返回 None 时创建空 TrackMetadata，避免后续 `.cover_data` 崩溃；④§10.7.3 移除与 §3.2 矛盾的第二套 `_request_with_rate_limit` 实现，统一委托 §3.2（消除两个独立 RateLimiter 实例导致速率控制失效的风险）；⑤§8B.3 `_toggle_edit_mode` 的 `disconnect()` 包裹 try/except TypeError（首次无连接时不崩溃）；⑥§8B.3 补全缺失的 `_on_lyric_double_clicked` 方法。**P1 逻辑修复**：⑦§7.5 `save_metadata_to_tags` BOTH 模式下不再调 `save_lyrics`/`save_cover`（避免歌词/封面重复写入标签两次），改为直接落盘 LRC/cover.jpg；⑧§7.2 OGG 封面写入 `METADATA_BLOCK_PICTURE` 改为 base64 编码（原 `pic.write()` 返回原始字节，不符合 OGG Vorbis 规范）；⑨§3.2 `enrich_cover_and_lyrics` 优先复用已有 `meta.cover_url` 下载封面（避免 iTunes 重复 API 调用），移除未使用的 `timeout` 参数；§7.7 批处理统一使用 `enrich_cover_and_lyrics` 替代手动分离调用。**P2 效率/完整性修复**：⑩§3.2 `_download_cover` 改用持久化 httpx.Client（与 `_request_with_rate_limit` 连接池策略一致）；⑪§10.4.1 `save_state` 的 `except Exception: pass` 改为 `logger.warning`（不再静默吞掉写入失败）；⑫§8B.3 新增 `_load_cover` 方法实现（优先级：cover_data→标签内嵌→cover.jpg→占位图）；⑬§3.4 流程图"并行"措辞修正为"批处理中按顺序执行；手动搜索中并行"。**P3 一致性修复**：⑭§10.7.4 MusicBrainz User-Agent 邮箱统一为 `contact@example.com`（与 §4.3 一致）；⑮§10.5.1 `DuplicateDetector.check` docstring L3→L2（与行内注释和表格一致）；⑯§10.4.2 `_is_stopped` 布尔值改为 `threading.Event`（线程安全）；⑰新增 §10.3 元数据搜索与写入顺序（填补章节编号缺口） |
