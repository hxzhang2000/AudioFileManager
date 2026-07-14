"""搜索配置模型（§3.3）。

存储在 ``%APPDATA%/AudioFileManager/config.json`` 的 ``"search"`` 段中，
支持用户在设置界面实时修改。
"""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class SearchConfig:
    """搜索配置数据模型。

    字段说明见开发方案 §3.3。配置可在运行时被设置界面修改，
    ``SearchEngine`` 每次搜索都会重新读取 ``active_order`` 等属性，
    因此修改即时生效。
    """

    # Provider 调用优先级顺序（从前到后依次尝试）
    provider_order: list[str] = field(
        default_factory=lambda: ["itunes", "lrclib", "musicbrainz", "meting"]
    )

    # 各 Provider 的启用/禁用开关
    provider_enabled: dict[str, bool] = field(
        default_factory=lambda: {
            "itunes": True,
            "lrclib": True,
            "musicbrainz": True,
            "meting": False,  # 默认不启用（部分公开实例不稳定）
        }
    )

    # Provider 特性标记：哪些接口支持什么能力。
    # 能力取值：metadata / cover / lyrics / preview / stream
    # 注意：该表为代码维护的内部能力声明，不从用户配置文件持久化。
    PROVIDER_CAPABILITIES: dict[str, set[str]] = field(
        default_factory=lambda: {
            "itunes": {"metadata", "cover", "preview"},
            "lrclib": {"lyrics"},
            "musicbrainz": {"metadata", "cover"},
            "meting": {"metadata", "cover", "lyrics", "stream"},
        }
    )

    # Meting-API 配置：用户可指定任意公开或自建实例
    meting_api_url: str = "https://api.injahow.cn/meting/"  # 默认公开实例
    meting_server: str = "netease"                          # 默认音源（网易云）

    # 请求超时（秒）
    request_timeout: int = 10
    total_timeout: int = 30

    # ------------------------------------------------------------
    # 能力查询方法
    # ------------------------------------------------------------
    def is_enabled(self, provider_id: str) -> bool:
        """该 Provider 是否启用"""
        return self.provider_enabled.get(provider_id, False)

    def supports_lyrics(self, provider_id: str) -> bool:
        """该 Provider 是否支持歌词搜索"""
        return "lyrics" in self.PROVIDER_CAPABILITIES.get(provider_id, set())

    def supports_cover(self, provider_id: str) -> bool:
        """该 Provider 是否支持封面搜索"""
        return "cover" in self.PROVIDER_CAPABILITIES.get(provider_id, set())

    def supports_preview(self, provider_id: str) -> bool:
        """该 Provider 是否支持试听"""
        return "preview" in self.PROVIDER_CAPABILITIES.get(provider_id, set())

    @property
    def active_order(self) -> list[str]:
        """已启用的 Provider 调用顺序（过滤掉禁用的，保持配置顺序）"""
        return [p for p in self.provider_order if self.is_enabled(p)]

    # ------------------------------------------------------------
    # 序列化辅助（与 config.json 的 "search" 段互转）
    # ------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        """转换为可写入 config.json 的 dict。

        注意：``PROVIDER_CAPABILITIES`` 为内部能力表，由代码维护，
        不在用户配置中持久化，因此不写入。
        """
        return {
            "provider_order": list(self.provider_order),
            "provider_enabled": dict(self.provider_enabled),
            "meting": {
                "api_url": self.meting_api_url,
                "server": self.meting_server,
            },
            "request_timeout": self.request_timeout,
            "total_timeout": self.total_timeout,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SearchConfig":
        """从 config.json 的 ``"search"`` 段构建 SearchConfig。

        缺失字段使用默认值，保证向前兼容（旧版配置文件可正常加载）。
        """
        if not data:
            return cls()
        defaults = cls()
        meting = data.get("meting", {}) or {}
        return cls(
            provider_order=list(data.get("provider_order") or defaults.provider_order),
            provider_enabled=dict(
                data.get("provider_enabled") or defaults.provider_enabled
            ),
            meting_api_url=meting.get("api_url", defaults.meting_api_url),
            meting_server=meting.get("server", defaults.meting_server),
            request_timeout=int(data.get("request_timeout", defaults.request_timeout)),
            total_timeout=int(data.get("total_timeout", defaults.total_timeout)),
        )
