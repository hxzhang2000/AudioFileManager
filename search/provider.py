"""搜索接口架构（§3.1 数据模型 + §3.2 插件式搜索架构）。

执行模型（v5.7 统一）：同步模型。
- 网络 I/O 使用同步 ``httpx``（``httpx.Client`` 持久化复用 TLS 连接池）。
- 速率控制使用 ``threading``（见 :mod:`search.rate_limiter.RateLimiter`）。
- 运行在 QThread 工作线程中，同步阻塞不卡 GUI 主线程。

§10.7.3 约定：``_request_with_rate_limit`` 的唯一实现位于本模块（§3.2），
各 Provider 子类直接继承使用，不重复定义，避免两套实现分叉。
"""

import time
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable, Optional, TYPE_CHECKING

import httpx

from utils.logger import logger
from cache.cover_cache import CoverCache
from cache.lrc_cache import LrcCache

if TYPE_CHECKING:
    # 仅用于类型检查，避免运行期循环导入
    from search.config import SearchConfig
    from search.rate_limiter import RateLimiter


# ============================================================
# §3.1 接口数据模型
# ============================================================

@dataclass
class TrackMetadata:
    """统一搜索结果模型。

    各 Provider 的 ``search_metadata`` 返回此类型；封面二进制与歌词文本
    可由 :meth:`SearchEngine.enrich_cover_and_lyrics` 后续补全。
    """

    title: str                          # 歌曲名
    artist: str                         # 歌手名
    album: str = ""                     # 专辑名
    cover_url: Optional[str] = None     # 封面 URL
    release_year: Optional[int] = None  # 发行年份
    release_date: Optional[str] = None  # 发行日期
    genre: Optional[str] = None         # 流派
    lyrics: Optional[str] = None        # 歌词（LRC 格式或纯文本）
    has_synced_lyrics: bool = False     # 是否为同步歌词
    cover_data: Optional[bytes] = None  # 已下载的封面二进制（批处理落盘用）
    lyrics_text: Optional[str] = None   # 已获取的歌词文本（批处理落盘用）
    track_number: int = 0               # 曲目编号
    track_count: int = 0                # 专辑总曲目
    source: str = ""                    # 数据来源（provider_id）
    preview_url: Optional[str] = None   # 试听 URL（iTunes Preview）


# ============================================================
# §3.2 插件式搜索架构
# ============================================================

class SearchProvider(ABC):
    """搜索接口抽象基类。

    所有 Provider 子类应通过 :meth:`_request_with_rate_limit` 发起网络请求，
    由全局 ``RateLimiter``（§10.7）按 ``provider_id`` 独立计数、控制并发与间隔、
    处理 429 封禁。内部使用持久化 ``httpx.Client``（按 ``provider_id`` 缓存，
    复用 TLS 连接池）。
    """

    # RateLimiter 模块级单例，所有 Provider 共享（懒加载，见 _get_rate_limiter）
    # 字符串注解避免运行期求值（RateLimiter 仅在 TYPE_CHECKING 下导入）
    _rate_limiter: "Optional[RateLimiter]" = None  # type: ignore[valid-type]
    # 按 provider_id 缓存的持久化 httpx.Client，复用连接池
    _clients: dict[str, httpx.Client] = {}
    # 保护 _clients 缓存的线程锁
    _clients_lock = threading.Lock()

    @property
    @abstractmethod
    def provider_id(self) -> str:
        """Provider 唯一标识（如 ``'itunes'``）"""
        ...

    @property
    @abstractmethod
    def display_name(self) -> str:
        """Provider 显示名称（用于 UI）"""
        ...

    @abstractmethod
    def search_metadata(self, title: str, artist: str) -> Optional[TrackMetadata]:
        """搜索元数据（标题/歌手/专辑/年份/封面URL/试听URL 等）"""
        ...

    @abstractmethod
    def search_cover(self, title: str, artist: str, album: str = "") -> Optional[str]:
        """返回封面图片 URL（str）；二进制下载由 :class:`SearchEngine` 负责。"""
        ...

    @abstractmethod
    def search_lyrics(self, title: str, artist: str, album: str = "") -> Optional[str]:
        """搜索歌词文本（LRC 同步歌词或纯文本）"""
        ...

    # —— 同步速率控制请求封装（v5.7 统一，基于 §10.7 RateLimiter）——

    @staticmethod
    def _get_rate_limiter() -> "RateLimiter":
        """获取全局 RateLimiter 单例（懒加载）。

        按类属性缓存，避免重复实例化导致速率控制失效。
        使用 ``staticmethod`` 而非 ``classmethod``，语义上更明确为
        "获取一个共享单例"。
        """
        if SearchProvider._rate_limiter is None:
            # 延迟导入，避免与 rate_limiter 模块产生循环依赖
            from search.rate_limiter import RateLimiter
            SearchProvider._rate_limiter = RateLimiter()
        return SearchProvider._rate_limiter

    @staticmethod
    def _new_client() -> httpx.Client:
        """创建一个持久化 httpx.Client。

        - ``follow_redirects=True``：覆盖封面 CDN 等跳转场景。
        - 默认超时 10s：防止 DNS 解析/连接/读取被单次请求阻塞。
        """
        return httpx.Client(follow_redirects=True, timeout=httpx.Timeout(10.0))

    def _request_with_rate_limit(
        self,
        provider_id: str,
        url: str,
        params: dict = None,
        headers: dict = None,
        timeout: int = 5,
    ) -> Optional[httpx.Response]:
        """带速率限制与自动重试的同步 HTTP GET。

        所有 Provider 子类应经此方法发起网络请求；§10.7 的 ``RateLimiter``
        负责按 Provider 独立计数、控制并发与间隔、处理 429 封禁。
        内部使用持久化 ``httpx.Client``（按 ``provider_id`` 缓存，复用 TLS
        连接池），运行在 QThread 工作线程中，同步阻塞不卡 GUI。
        失败时返回 ``None``，由调用方决定降级策略，不抛异常。

        注意（§10.7.3）：此处为 ``_request_with_rate_limit`` 的唯一实现，
        各 Provider 不应重复定义，直接继承使用即可。
        """
        limiter = self._get_rate_limiter()
        cfg = limiter.get_provider_config(provider_id)

        # 持久化 Client，避免每次请求重建 TCP/TLS 连接
        with SearchProvider._clients_lock:
            client = SearchProvider._clients.get(provider_id)
            if client is None:
                client = SearchProvider._new_client()
                SearchProvider._clients[provider_id] = client

        for attempt in range(cfg["max_retries"] + 1):
            with limiter.acquire(provider_id):
                try:
                    resp = client.get(
                        url, params=params, headers=headers, timeout=timeout
                    )
                    if resp.status_code == 429:
                        try:
                            retry_after = int(
                                resp.headers.get("Retry-After", "60")
                            )
                        except (ValueError, TypeError):
                            retry_after = 60
                        limiter.report_429(provider_id, retry_after)
                        logger.warning(
                            f"[{provider_id}] 429 限速，等待 {retry_after}s"
                        )
                        continue  # __exit__ 释放信号量，下一循环 acquire 会等待 ban
                    resp.raise_for_status()
                    return resp
                except httpx.TimeoutException:
                    logger.warning(
                        f"[{provider_id}] 超时 (第 {attempt + 1} 次)"
                    )
                except httpx.HTTPStatusError as e:
                    logger.warning(
                        f"[{provider_id}] HTTP {e.response.status_code}"
                    )
                except httpx.RequestError as e:
                    logger.warning(
                        f"[{provider_id}] 网络请求异常: {e}"
                    )
                except Exception as e:
                    logger.warning(
                        f"[{provider_id}] 未知请求异常: {e}"
                    )
            # with 块外：信号量已释放，退避等待不占用并发槽位
            if attempt == cfg["max_retries"]:
                return None
            time.sleep(2 ** attempt)  # 指数退避
        return None

    # —— 通用工具方法 ——

    @staticmethod
    def _parse_year(date_str: str) -> Optional[int]:
        """从日期字符串解析发行年份。

        支持格式：``'2008-10-14'``、``'2008-10'``、``'2008'``。
        解析失败返回 ``None``。
        """
        if not date_str:
            return None
        try:
            return int(str(date_str).strip()[:4])
        except (ValueError, TypeError):
            return None

    @classmethod
    def close_clients(cls):
        """关闭所有缓存的 httpx.Client，释放连接池资源。

        应在搜索不再使用时调用（如批处理结束、应用退出）。
        """
        with cls._clients_lock:
            for provider_id, client in cls._clients.items():
                try:
                    client.close()
                except Exception:
                    pass
            cls._clients.clear()


class SearchEngine:
    """搜索引擎 — 按配置的优先级顺序调度各 Provider。

    核心能力：
    1. 按配置顺序依次尝试 Provider。
    2. 当前 Provider 失败时自动降级到下一个。
    3. 可配置每个 Provider 的启用/禁用。
    4. 可动态调整优先级顺序（修改 ``config.provider_order`` 即时生效）。
    """

    def __init__(self, config: "SearchConfig"):
        self.config = config
        self.providers: dict[str, SearchProvider] = {}
        self.cover_cache = CoverCache()
        self.lrc_cache = LrcCache()

    def register(self, provider: SearchProvider):
        """注册一个 Provider 到引擎（按 ``provider_id`` 索引）"""
        self.providers[provider.provider_id] = provider

    def search_metadata(
        self,
        title: str,
        artist: str,
        similarity_fn: Optional[Callable[[str, str], float]] = None,
        similarity_threshold: float = 0.35,
    ) -> Optional[TrackMetadata]:
        """按优先级搜索元数据。

        依次尝试 ``config.active_order`` 中已启用的 Provider。
        当提供了 ``similarity_fn`` 时：首个返回且标题相似度 ≥
        ``similarity_threshold`` 的 Provider 结果直接采用（短路）；
        若所有 Provider 均返回低分结果，则从中选出相似度最高者返回，
        并记录一条 info 日志说明降级情况。

        未提供 ``similarity_fn`` 时保持原有的"首个非空即收"行为。
        """
        if similarity_fn is None:
            # 原始行为：首个非空即收
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

        # 降级模式：逐 Provider 尝试，高分短路，低分收集
        best_score = -1.0
        best_result: Optional[TrackMetadata] = None
        for pid in self.config.active_order:
            provider = self.providers.get(pid)
            if not provider or not self.config.is_enabled(pid):
                continue
            try:
                result = provider.search_metadata(title, artist)
                if result:
                    result.source = pid
                    score = similarity_fn(title, result.title or "")
                    if score >= similarity_threshold:
                        return result  # 足够可靠，短路
                    if score > best_score:
                        best_score = score
                        best_result = result
            except Exception as e:
                logger.warning(f"[{pid}] 搜索失败: {e}")

        if best_result is not None:
            logger.info(
                f"所有搜索结果与查询「{title}」相似度均低于 "
                f"{similarity_threshold:.0%}，采用来自 "
                f"[{best_result.source}] 的最高得分 {best_score:.0%}"
            )
        return best_result

    def search_lyrics(self, title: str, artist: str, album: str = "") -> Optional[str]:
        """按优先级搜索歌词（仅尝试支持歌词能力的 Provider）。

        先查 LrcCache 缓存，命中直接返回；未命中则搜索，成功后写入缓存。
        """
        # 先查缓存
        cached = self.lrc_cache.get(title, artist)
        if cached:
            logger.debug(f"歌词缓存命中: {title} - {artist}")
            return cached

        for pid in self.config.active_order:
            provider = self.providers.get(pid)
            if not provider or not self.config.is_enabled(pid):
                continue
            if not self.config.supports_lyrics(pid):
                continue
            try:
                lrc = provider.search_lyrics(title, artist, album)
                if lrc:
                    self.lrc_cache.put(title, artist, lrc)
                    return lrc
            except Exception as e:
                logger.warning(f"[{pid}] 歌词搜索失败: {e}")
        return None

    def fetch_cover(self, title: str, artist: str, album: str = "") -> Optional[bytes]:
        """搜索并下载封面图片（返回二进制数据）。

        与 Provider 层的 ``search_cover``（返回 URL）命名区分：
        Engine 层负责下载，Provider 层只返回 URL。

        先查 CoverCache 缓存，命中直接返回；未命中则搜索下载，成功后写入缓存。
        """
        # 先查缓存
        cached = self.cover_cache.get(artist, album)
        if cached:
            logger.debug(f"封面缓存命中: {artist} - {album}")
            return cached

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
                        self.cover_cache.put(artist, album, cover_bytes)
                    return cover_bytes
            except Exception as e:
                logger.warning(f"[{pid}] 封面搜索失败: {e}")
        return None

    def search_preview(self, title: str, artist: str) -> Optional[str]:
        """搜索试听音频 URL（如 iTunes 30s Preview）。

        通过重新调用支持 preview 的 Provider 的 ``search_metadata`` 获取
        ``preview_url``。首个命中即返回。
        """
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

        如果是已知 Provider（如 musicbrainz / meting），封面下载也计入
        该 Provider 的速率配额。使用持久化 ``httpx.Client``（按
        ``provider_id`` 缓存，复用 TLS 连接池），与
        :meth:`SearchProvider._request_with_rate_limit` 的连接池策略一致。
        """
        try:
            limiter = SearchProvider._get_rate_limiter()
            cfg = limiter.get_provider_config(provider_id)
            # 复用持久化 Client（与 _request_with_rate_limit 共享连接池策略，follow_redirects 覆盖封面 CDN 跳转）
            with SearchProvider._clients_lock:
                client = SearchProvider._clients.get(provider_id)
                if client is None:
                    client = SearchProvider._new_client()
                    SearchProvider._clients[provider_id] = client
            resp = None
            for attempt in range(cfg.get("max_retries", 2) + 1):
                with limiter.acquire(provider_id):
                    try:
                        resp = client.get(url, timeout=10)
                        if resp.status_code == 429:
                            try:
                                retry_after = int(
                                    resp.headers.get("Retry-After", "60")
                                )
                            except (ValueError, TypeError):
                                retry_after = 60
                            limiter.report_429(provider_id, retry_after)
                            logger.warning(
                                f"[{provider_id}] 封面下载 429 限速，等待 {retry_after}s"
                            )
                            continue  # __exit__ 释放信号量，下一循环 acquire 会等待 ban
                        resp.raise_for_status()
                        break  # 下载成功，跳出重试循环
                    except httpx.HTTPStatusError as e:
                        logger.warning(
                            f"[{provider_id}] 封面下载 HTTP {e.response.status_code}"
                        )
                    except httpx.TimeoutException:
                        logger.warning(
                            f"[{provider_id}] 封面下载超时 (第 {attempt + 1} 次)"
                        )
                    except httpx.RequestError as e:
                        logger.warning(
                            f"[{provider_id}] 封面下载网络异常: {e} (第 {attempt + 1} 次)"
                        )
                    except Exception as e:
                        logger.warning(
                            f"[{provider_id}] 封面下载异常: {e} (第 {attempt + 1} 次)"
                        )
                # with 块外：信号量已释放，退避等待不占用并发槽位
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
        - ``search_cover`` 返回封面 URL（str）；本方法负责下载为二进制。
        - ``search_lyrics`` 返回歌词文本（str）；同步歌词以 ``'['`` 开头。
        - 若 ``meta.cover_url`` 已有值（如 iTunes ``search_metadata`` 已返回
          封面 URL），优先直接下载该 URL，避免重复调用 ``search_cover``
          造成冗余 API 请求。
        - 仅当已有 ``cover_url`` 下载失败时，才回退到各 Provider 的
          ``search_cover``。
        歌词始终按 providers 顺序尝试；任一 Provider 成功即采用。
        失败不抛异常（封面/歌词为可选项），仅记录警告。
        """
        # —— 封面补全 ——
        if not meta.cover_data:
            # 先查缓存
            cached_cover = self.cover_cache.get(artist, album)
            if cached_cover:
                meta.cover_data = cached_cover

            # 优先复用已有 cover_url（如 iTunes search_metadata 已返回）
            if not meta.cover_data and meta.cover_url:
                cover_bytes = self._download_cover(
                    meta.cover_url, meta.source or "unknown"
                )
                if cover_bytes:
                    meta.cover_data = cover_bytes
                    self.cover_cache.put(artist, album, cover_bytes)
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
                                self.cover_cache.put(artist, album, cover_bytes)
                                break
                    except Exception as e:
                        logger.warning(f"[{pid}] 封面补全失败: {e}")

        # —— 歌词补全 ——
        if not meta.lyrics_text:
            # 先查缓存
            cached_lrc = self.lrc_cache.get(title, artist)
            if cached_lrc:
                meta.lyrics_text = cached_lrc
                meta.lyrics = cached_lrc
                meta.has_synced_lyrics = cached_lrc.strip().startswith("[")

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
                        self.lrc_cache.put(title, artist, lyrics)
                        meta.lyrics_text = lyrics
                        meta.lyrics = lyrics
                        meta.has_synced_lyrics = lyrics.strip().startswith("[")
                        break
        return meta

    def close(self):
        """关闭搜索引擎，清理 Provider 缓存和连接池资源。

        应在批处理结束或应用退出时调用。
        """
        SearchProvider.close_clients()
        self.cover_cache.clear()
        self.lrc_cache.clear()
