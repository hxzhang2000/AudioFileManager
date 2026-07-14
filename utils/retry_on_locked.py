"""文件锁定重试装饰器（同步版本）"""

import time
import functools
import logging

logger = logging.getLogger(__name__)


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
                except FileNotFoundError:
                    raise
                except (PermissionError, OSError) as e:
                    last_exc = e
                    if attempt < max_attempts - 1:
                        logger.warning(
                            f"文件锁定/权限错误 (第 {attempt+1}/{max_attempts} 次): {e}")
                        time.sleep(delay)
            raise last_exc
        return wrapper
    return decorator
