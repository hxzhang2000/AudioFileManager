"""速率限制器 — 按 Provider 独立计数的同步速率控制（§10.7）。

执行模型（v5.7 统一）：
- 基于 ``threading.Semaphore`` + ``time.sleep`` 的同步实现，非 asyncio。
- 运行在 QThread 工作线程中，阻塞不卡 GUI 主线程。

详见开发方案 §10.7.1（各接口速率限制对照表）与 §10.7.2（设计方案）。
"""

import time
import threading
from collections import defaultdict

from utils.logger import logger


class RateLimiter:
    """按 Provider 独立计数的速率限制器（同步实现，基于 threading）。

    - 每个 Provider 有独立的请求间隔（interval）和并发上限（concurrency）。
    - 使用 ``threading.Semaphore`` 控制并发，``time.sleep`` 控制间隔
      （阻塞当前工作线程，不阻塞 GUI）。
    - 遇到 429 自动记录封禁时间（ban_until），后续请求会等待封禁结束。

    用法（v5.8 修正）：
        with limiter.acquire("musicbrainz"):
            resp = client.get(url)
        # 信号量在 HTTP 请求期间持续持有，__exit__ 时才释放并更新 last_time。
    """

    class _AcquireContext:
        """上下文管理器：__enter__ 获取信号量，__exit__ 更新 last_time 并释放信号量。"""

        def __init__(self, limiter, provider_id, sem):
            self.limiter = limiter
            self.provider_id = provider_id
            self.sem = sem

        def __enter__(self):
            self.sem.acquire()
            return self

        def __exit__(self, *args):
            with self.limiter._lock:
                self.limiter._provider_state[self.provider_id][
                    "last_time"
                ] = time.monotonic()
            self.sem.release()
            return False

    def __init__(self):
        # { provider_id: {"last_time": float, "ban_until": float} }
        self._provider_state: dict[str, dict] = defaultdict(
            lambda: {"last_time": 0.0, "ban_until": 0.0}
        )
        # 并发控制信号量（按 provider_id 独立）
        self._semaphores: dict[str, threading.Semaphore] = {}
        # 保护 _provider_state / _semaphores 的互斥锁
        self._lock = threading.Lock()

    def get_provider_config(self, provider_id: str) -> dict:
        """返回每个 Provider 的速率配置。

        Returns:
            包含以下字段的 dict：
            - concurrency: 允许的并发请求数（1 表示串行）
            - interval:    两次请求之间的最小间隔（秒）
            - max_retries: 最大重试次数（不含首次请求）

            未知 Provider 返回保守的默认配置。

        详见 §10.7.1 各接口速率限制对照表。
        """
        configs = {
            # iTunes：可少量并发，无额外延迟
            "itunes":      {"concurrency": 2, "interval": 0.0, "max_retries": 2},
            # LRCLIB：可并发
            "lrclib":      {"concurrency": 3, "interval": 0.0, "max_retries": 3},
            # MusicBrainz：严禁并发，官方硬性要求每秒最多 1 次
            "musicbrainz": {"concurrency": 1, "interval": 1.1, "max_retries": 2},
            # Meting 公开实例：严禁并发，间隔较长
            "meting":      {"concurrency": 1, "interval": 0.8, "max_retries": 2},
        }
        if provider_id not in configs:
            logger.warning(
                f"[RateLimiter] 未知 provider_id {provider_id!r}，使用保守默认配置"
            )
        return configs.get(
            provider_id,
            {"concurrency": 1, "interval": 0.5, "max_retries": 1},
        )

    def acquire(self, provider_id: str):
        """获取请求许可，返回一个用于 ``with`` 语句的上下文管理器。

        用法:
            with limiter.acquire("musicbrainz"):
                resp = client.get(url)

        执行步骤：
        1. 在锁内完成 ban 检查与 interval 等待时间计算（sleep 在锁外）。
        2. 懒初始化并发信号量（锁内保护，避免竞态创建多个实例）。
        3. 返回 ``_AcquireContext``：``__enter__`` 时获取信号量，
           ``__exit__`` 时更新 last_time 并释放信号量。
        这样信号量在整个 HTTP 请求期间持续持有，真正起到并发控制作用。
        """
        cfg = self.get_provider_config(provider_id)

        with self._lock:
            state = self._provider_state[provider_id]

            # 封禁期检查（仅在锁内计算等待时间）
            now = time.monotonic()
            ban_wait = max(0, state["ban_until"] - now)

            # 间隔检查（在锁内计算等待时间）
            interval = cfg.get("interval", 0.5)
            interval_wait = max(0, state["last_time"] + interval - now)

        # 在锁外 sleep（不阻塞其他 provider）
        wait = max(ban_wait, interval_wait)
        if wait > 0:
            logger.debug(f"[{provider_id}] 等待 {wait:.1f}s 后请求")
            time.sleep(wait)

        # 懒初始化信号量（锁内保护）
        with self._lock:
            if provider_id not in self._semaphores:
                self._semaphores[provider_id] = threading.Semaphore(
                    cfg.get("concurrency", 1)
                )
            sem = self._semaphores[provider_id]

        # 返回一个上下文管理器：__enter__ 获取信号量，
        # __exit__ 更新 last_time 并释放信号量。
        return RateLimiter._AcquireContext(self, provider_id, sem)

    def report_429(self, provider_id: str, retry_after: int = 60):
        """记录 429/限速响应，设置封禁期。

        Args:
            provider_id: 触发限速的 Provider 标识。
            retry_after: 封禁时长（秒），通常取自响应头 ``Retry-After``，
                         默认 60 秒。
        """
        with self._lock:
            state = self._provider_state[provider_id]
            new_ban = time.monotonic() + retry_after
            # 只延长封禁期，不缩短（避免并发请求互相覆盖缩短封禁）
            if new_ban > state["ban_until"]:
                state["ban_until"] = new_ban
        logger.warning(f"[{provider_id}] 收到 429，封禁 {retry_after}s")
