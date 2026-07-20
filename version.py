"""AudioFileManager 版本号管理模块。

本模块为单一来源（single source of truth）的版本号定义，
所有需要展示或判断版本号的位置都应从此处导入，避免多处硬编码。

版本号遵循 `语义化版本控制 2.0.0 <https://semver.org/lang/zh-CN/>`_：
``MAJOR.MINOR.PATCH[-prerelease][+build]``

- MAJOR：不兼容的 API 或配置变更
- MINOR：向下兼容的功能新增
- PATCH：向下兼容的问题修复
- prerelease：可选的预发布标识（如 ``beta.1``）
- build：可选的构建元数据（如 ``20260714``）
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Tuple

# ============================================================
# 应用元信息（修改版本号时只需调整此处）
# ============================================================

APP_NAME = "AudioFileManager"
APP_VERSION = "1.2.2"
APP_DESCRIPTION = "音频文件管理工具：自动识别元数据、补全封面与歌词、整理目录结构；支持 MV 视频文件自动归类"
APP_AUTHOR = "AudioFileManager Team"
APP_HOMEPAGE = "https://github.com/hxzhang2000/AudioFileManager"
APP_GITHUB_URL = "https://github.com/hxzhang2000/AudioFileManager"


# ============================================================
# 版本号解析与比较
# ============================================================

@dataclass(frozen=True, order=False)
class VersionInfo:
    """解析后的版本号信息。

    Attributes:
        major: 主版本号。
        minor: 次版本号。
        patch: 修订号。
        prerelease: 预发布标识（如 ``beta.1``），无则为空字符串。
        build: 构建元数据（如 ``20260714``），无则为空字符串。
        raw: 原始版本号字符串。
    """

    major: int
    minor: int
    patch: int
    prerelease: str
    build: str
    raw: str

    def __lt__(self, other: "VersionInfo") -> bool:
        """语义化版本比较（build 元数据不参与优先级比较）。"""
        if not isinstance(other, VersionInfo):
            return NotImplemented
        if (self.major, self.minor, self.patch) != (other.major, other.minor, other.patch):
            return (self.major, self.minor, self.patch) < (other.major, other.minor, other.patch)
        return _prerelease_priority(self.prerelease) < _prerelease_priority(other.prerelease)

    def __le__(self, other: "VersionInfo") -> bool:
        return self == other or self < other

    def __gt__(self, other: "VersionInfo") -> bool:
        return not self <= other

    def __ge__(self, other: "VersionInfo") -> bool:
        return not self < other

    def __str__(self) -> str:
        return self.raw


# 匹配 MAJOR.MINOR.PATCH[-prerelease][+build]
_VERSION_RE = re.compile(
    r"^(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)"
    r"(?:-(?P<prerelease>[A-Za-z0-9.]+))?"
    r"(?:\+(?P<build>[A-Za-z0-9.]+))?$"
)


def _prerelease_priority(prerelease: str) -> Tuple[int, ...]:
    """将预发布标识转换为可比较元组。

    正式版本（无预发布标识）优先级高于任何预发布版本。
    """
    if not prerelease:
        return (1,)
    parts = prerelease.split(".")
    result: list[int] = [0]
    for part in parts:
        if part.isdigit():
            result.append(int(part))
        else:
            # 非数字部分按 ASCII 比较，统一用负数占位避免与数字冲突
            result.append(-(sum(ord(c) for c in part)))
    return tuple(result)


def parse_version(version: str) -> VersionInfo:
    """解析版本号字符串为 :class:`VersionInfo`。

    Args:
        version: 版本号字符串，如 ``"1.0.1"``、``"1.2.0-beta.1+20260714"``。

    Returns:
        解析后的版本号信息。

    Raises:
        ValueError: 版本号格式不合法。
    """
    m = _VERSION_RE.match(version.strip())
    if not m:
        raise ValueError(f"非法版本号格式: {version!r}")
    return VersionInfo(
        major=int(m.group("major")),
        minor=int(m.group("minor")),
        patch=int(m.group("patch")),
        prerelease=m.group("prerelease") or "",
        build=m.group("build") or "",
        raw=version.strip(),
    )


def current_version_info() -> VersionInfo:
    """返回当前应用版本的 :class:`VersionInfo`。"""
    return parse_version(APP_VERSION)


def version_display() -> str:
    """返回适合界面展示的版本字符串，如 ``"AudioFileManager v1.0.1"``。"""
    return f"{APP_NAME} v{APP_VERSION}"
