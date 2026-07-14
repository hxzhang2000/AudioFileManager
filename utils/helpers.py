"""工具函数模块"""

import os
import re
from pathlib import Path
from typing import Optional


def sanitize_filename(name: str) -> str:
    """清理文件名中的非法字符（Windows 文件系统不允许的字符）。

    替换 < > : " / \\ | ? * 为下划线。
    去除首尾空格和点号（Windows 不允许以点号结尾）。
    """
    name = re.sub(r'[<>:"/\\|?*]', '_', name)
    name = name.strip().rstrip('.')
    if not name:
        name = "unknown"
    # Windows 路径长度限制（含扩展名 255 字符）
    if len(name) > 200:
        name = name[:200]
    return name


def format_time(ms: int) -> str:
    """毫秒转 MM:SS 格式"""
    seconds = ms // 1000
    minutes = seconds // 60
    seconds = seconds % 60
    return f"{minutes:02d}:{seconds:02d}"


def format_time_ms(ms: int) -> str:
    """毫秒转 MM:SS.ss 格式（含毫秒，用于歌词时间戳）"""
    minutes = ms // 60000
    seconds = (ms % 60000) / 1000
    return f"{minutes:02d}:{seconds:05.2f}"


def parse_time_str(time_str: str) -> int:
    """解析 MM:SS.ss 格式时间为毫秒"""
    try:
        parts = time_str.strip().split(":")
        if len(parts) == 2:
            minutes = int(parts[0])
            seconds = float(parts[1])
            return int(minutes * 60000 + seconds * 1000)
        elif len(parts) == 3:
            hours = int(parts[0])
            minutes = int(parts[1])
            seconds = float(parts[2])
            return int(hours * 3600000 + minutes * 60000 + seconds * 1000)
    except (ValueError, IndexError):
        pass
    return 0


def get_app_data_dir() -> Path:
    """获取应用数据目录 %APPDATA%/AudioFileManager/"""
    return Path(os.environ.get("APPDATA", ".")) / "AudioFileManager"


def ensure_dir(path: str | Path) -> Path:
    """确保目录存在，不存在则创建"""
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def make_unique_path(
    target: str | Path,
    src: Optional[str | Path] = None,
    suffix_template: str = " ({})",
    start: int = 2,
) -> Path:
    """为目标路径生成不冲突的最终路径。

    若目标不存在，或与 ``src`` 指向同一文件（原地操作），则直接返回。
    否则按 ``stem (2).suffix``、``stem (3).suffix`` … 递增，直到找到
    不存在的路径。

    Args:
        target: 期望的目标路径。
        src: 源文件路径。若目标与源为同一文件，视为无需冲突处理。
        suffix_template: 序号后缀模板，``{}`` 为序号占位符。
        start: 序号起始值。

    Returns:
        不冲突的最终目标路径。
    """
    target = Path(target)
    if not target.exists():
        return target

    if src is not None:
        src = Path(src)
        try:
            if target.resolve() == src.resolve():
                return target
        except OSError:
            if str(target) == str(src):
                return target

    parent = target.parent
    stem = target.stem
    suffix = target.suffix
    counter = start
    while True:
        candidate = parent / f"{stem}{suffix_template.format(counter)}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def is_chinese_char(ch: str) -> bool:
    """判断字符是否为中文"""
    return '\u4e00' <= ch <= '\u9fff'


def has_chinese(text: str) -> bool:
    """判断字符串是否包含中文"""
    return any(is_chinese_char(c) for c in text)
