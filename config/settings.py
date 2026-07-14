"""配置管理 — 配置的唯一加载/落盘入口。

负责把 config.json 映射为内存配置对象，支持版本迁移。
"""

import json
import os
from pathlib import Path
from typing import Any

from utils.logger import logger

# 配置文件路径
CONFIG_DIR = Path(os.environ.get("APPDATA", ".")) / "AudioFileManager"
CONFIG_FILE = CONFIG_DIR / "config.json"
CACHE_DIR = CONFIG_DIR / "cache"
COVER_CACHE = CACHE_DIR / "covers"
LYRICS_CACHE = CACHE_DIR / "lyrics"

# 默认配置文件路径（项目内置）
_DEFAULT_CONFIG_PATH = Path(__file__).parent / "default_config.json"


# ============================================================
# 版本迁移
# ============================================================

# 版本号 → 迁移函数（对原始 dict 就地修改）
MIGRATIONS: dict[str, callable] = {}


def _migrate_v1_to_v2(cfg: dict):
    """v1→v2: 新增 search.meting 配置子块"""
    cfg.setdefault("search", {}).setdefault("meting", {
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


def _load_default_config() -> dict:
    """加载内置默认配置"""
    with open(_DEFAULT_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def load_config(path: str | Path | None = None) -> dict:
    """加载配置并按需迁移。

    Args:
        path: 配置文件路径，默认为 CONFIG_FILE。

    Returns:
        合并后的完整配置 dict。
    """
    path = Path(path) if path else CONFIG_FILE
    defaults = _load_default_config()

    if not path.exists():
        logger.info("配置文件不存在，使用默认配置")
        return defaults

    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"配置文件读取失败: {e}，使用默认配置")
        return defaults

    current_version = cfg.get("version", "1.0")
    # 按版本号顺序执行所有需要的迁移（使用元组比较，避免字符串排序错误）
    for ver in sorted(MIGRATIONS.keys(), key=_parse_version):
        if _parse_version(ver) > _parse_version(current_version):
            MIGRATIONS[ver](cfg)
            cfg["version"] = ver

    # 深度合并默认值（补齐缺失字段，递归一层）
    merged = _deep_merge(defaults, cfg)
    return merged


def _deep_merge(base: dict, override: dict) -> dict:
    """深度合并两个字典，override 中的值优先。"""
    result = base.copy()
    for key, val in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = val
    return result


def save_config(config: dict, path: str | Path | None = None):
    """保存配置到文件。

    Args:
        config: 完整配置 dict。
        path: 配置文件路径，默认为 CONFIG_FILE。
    """
    path = Path(path) if path else CONFIG_FILE
    path.parent.mkdir(parents=True, exist_ok=True)

    # 原子写入：先写临时文件，再原子替换
    tmp = path.with_suffix(".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=2)
        os.replace(str(tmp), str(path))
    except Exception as e:
        logger.warning(f"配置文件保存失败: {e}")


def get_config_dir() -> Path:
    """获取配置目录"""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    return CONFIG_DIR


def get_cache_dir() -> Path:
    """获取缓存目录"""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR


# 全局配置实例
_config: dict | None = None


def get_config() -> dict:
    """获取全局配置实例（首次调用时自动加载）"""
    global _config
    if _config is None:
        _config = load_config()
    return _config


def reload_config() -> dict:
    """重新加载配置"""
    global _config
    _config = load_config()
    return _config


def update_config(updates: dict):
    """更新配置中的部分字段并保存。

    Args:
        updates: 要更新的字段（支持嵌套 dict）。
    """
    global _config
    if _config is None:
        _config = load_config()
    _config = _deep_merge(_config, updates)
    save_config(_config)
