"""日志模块 — 全局日志配置"""

import logging
import os
from pathlib import Path
from datetime import datetime

# 文件日志运行时开关（由配置文件 / 设置对话框控制）
_file_logging_enabled = True

# 已知会输出大量噪声的第三方库（http 客户端、Qt 绑定等），
# 其日志不写入控制台与文件，避免淹没应用自身日志。
_NOISY_LOGGERS = {
    "httpx", "urllib3", "anyio", "httpcore",
    "PyQt6", "PyQt5", "PySide6", "PySide2", "PyQt6.QtCore",
}


class _AppLogFilter(logging.Filter):
    """仅放行应用自身日志，屏蔽嘈杂的第三方库。"""

    def filter(self, record: logging.LogRecord) -> bool:
        return record.name.split(".")[0] not in _NOISY_LOGGERS


class _FileLoggingToggleFilter(logging.Filter):
    """根据全局开关决定是否将记录写入文件。"""

    def filter(self, record: logging.LogRecord) -> bool:
        return _file_logging_enabled


def set_file_logging_enabled(enabled: bool) -> None:
    """运行时开关文件日志（设置对话框调用）。"""
    global _file_logging_enabled
    _file_logging_enabled = bool(enabled)


def is_file_logging_enabled() -> bool:
    """返回当前文件日志开关状态。"""
    return _file_logging_enabled


def setup_logger(name: str = "AudioFileManager", level: int = logging.INFO) -> logging.Logger:
    """配置并返回全局 logger。

    日志输出到控制台 + %APPDATA%/AudioFileManager/logs/ 目录。
    文件日志可通过 :func:`set_file_logging_enabled` 运行时开关控制。

    为确保所有模块（无论使用 ``logging.getLogger(__name__)`` 还是共享
    ``utils.logger.logger``）都能写入同一份日志，处理器挂在 **root**
    logger 上，子 logger 自动向上传播；并用 :class:`_AppLogFilter` 屏蔽
    第三方库噪声。
    """
    root = logging.getLogger()
    if getattr(root, "_afm_configured", False):
        return logging.getLogger(name)

    root.setLevel(logging.DEBUG)

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 控制台输出（始终开启，受 _AppLogFilter 控制噪声）
    console_handler = logging.StreamHandler()
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)
    console_handler.addFilter(_AppLogFilter())
    root.addHandler(console_handler)

    # 文件输出（受开关控制）
    try:
        log_dir = Path(os.environ.get("APPDATA", ".")) / "AudioFileManager" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / f"{datetime.now().strftime('%Y-%m-%d')}.log"
        file_handler = logging.FileHandler(str(log_file), encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(formatter)
        file_handler.addFilter(_AppLogFilter())
        file_handler.addFilter(_FileLoggingToggleFilter())
        root.addHandler(file_handler)
    except Exception:
        pass  # 文件日志不可用时不影响程序运行

    root._afm_configured = True  # type: ignore[attr-defined]
    return logging.getLogger(name)


# 全局 logger 实例
logger = setup_logger()
