"""文件冲突与重复处理（§10.5）。

批处理过程中会遇到两类问题：

| 场景         | 本质                                          | 检测方法              |
| ------------ | --------------------------------------------- | --------------------- |
| **内容重复** | 两个文件完全相同（同一首歌下载了两次）         | SHA-256 完全一致       |
| **文件名冲突** | 两个不同文件整理后产生相同目标文件名           | 目标目录已存在同名文件 |

本模块提供三类组件：

1. :class:`AudioQuality` —— 音频质量参数数据类（从 mutagen 读取）。
2. :func:`get_file_quality` —— 读取音频文件质量信息为 :class:`AudioQuality`。
3. :class:`DuplicateDetector` —— 内容重复检测器（L1 名称+大小，L2 哈希）。
4. :class:`ConflictResolver` —— 文件名冲突解决器（keep_best_quality/skip/
   overwrite/rename_new）。

依赖：
- ``mutagen``：读取音频流信息（bitrate/sample_rate/channels/bits_per_sample）
- ``processor.file_scanner.AudioFileEntry``：重复检测的条目类型
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ============================================================
# §10.5.2 音频质量数据类
# ============================================================

# 格式权重表（无损 > 有损），用于音质评分与格式对比
_FORMAT_WEIGHTS: dict[str, int] = {
    "FLAC": 100, "ALAC": 95, "WAV": 90, "AIFF": 85,
    "APE": 80, "OGG": 60, "M4A": 55, "MP4": 55, "AAC": 55,
    "MP3": 50, "WMA": 45,
}


@dataclass
class AudioQuality:
    """音频质量参数 —— 从 mutagen 读取。

    Attributes:
        bitrate: 比特率（kbps）。
        sample_rate: 采样率（Hz）。
        format: 格式（MP3/FLAC/M4A/OGG/WMA/APE 等，大写）。
        channels: 声道数。
        bit_depth: 位深（bits per sample，无损格式才有意义）。
    """

    bitrate: int = 0           # 比特率 (kbps)
    sample_rate: int = 0       # 采样率 (Hz)
    format: str = ""           # 格式 (MP3/FLAC/M4A/OGG...)
    channels: int = 0          # 声道数
    bit_depth: int = 0         # 位深 (bits per sample)

    # —— 质量评分 ——
    def score(self) -> float:
        """综合质量评分，数值越高越好。

        评分构成：
        - 格式权重（无损 > 有损，FLAC=100 ... WMA=45，未知=30）
        - 比特率权重（``bitrate * 0.1``）
        - 采样率权重（≥96kHz=40 / ≥48kHz=30 / ≥44.1kHz=20 / 其他=5）
        - 声道权重（``channels * 5``）
        - 位深权重（``bit_depth * 0.5``，无损才有意义）
        """
        score = 0.0
        # 格式权重（无损 > 有损）
        score += _FORMAT_WEIGHTS.get(self.format.upper(), 30)
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
        # 位深权重（FLAC/WAV 等无损格式才有意义）
        score += self.bit_depth * 0.5
        return score

    def summary(self) -> str:
        """人类可读的质量概要，用于 ask 弹窗显示。"""
        parts = [self.format] if self.format else []
        if self.bitrate:
            parts.append(f"{self.bitrate}kbps")
        if self.sample_rate:
            parts.append(f"{self.sample_rate}Hz")
        if self.channels:
            parts.append(f"{self.channels}ch")
        if self.bit_depth:
            parts.append(f"{self.bit_depth}-bit")
        return " · ".join(parts) if parts else "未知音质"

    @staticmethod
    def from_file(file_path: str) -> "AudioQuality":
        """从音频文件中读取质量参数（委托 :func:`get_file_quality`）。"""
        return get_file_quality(file_path)


# ============================================================
# 读取音频质量
# ============================================================

def get_file_quality(file_path: str) -> AudioQuality:
    """读取音频文件的质量信息，返回 :class:`AudioQuality`。

    通过 ``mutagen.File`` 统一打开各格式音频，从 ``audio.info`` 读取
    比特率 / 采样率 / 声道数 / 位深。无法识别或读取失败时返回仅含格式名
    的 :class:`AudioQuality`（不抛异常，便于批处理连续运行）。

    Args:
        file_path: 音频文件路径。

    Returns:
        :class:`AudioQuality` 实例。
    """
    suffix = Path(file_path).suffix.lstrip(".").upper()
    try:
        from mutagen import File as MutagenFile
    except ImportError:
        logger.warning("mutagen 未安装，无法读取音质信息")
        return AudioQuality(format=suffix)

    try:
        audio = MutagenFile(file_path)
        if audio is None:
            return AudioQuality(format=suffix)

        info = audio.info

        # 比特率：bps → kbps
        bitrate = getattr(info, "bitrate", 0) or 0
        if bitrate:
            bitrate = bitrate // 1000

        sample_rate = getattr(info, "sample_rate", 0) or 0
        channels = getattr(info, "channels", 0) or 0

        # 位深：不同格式字段名不同（FLAC=bits_per_sample，MP3 无此字段）
        bit_depth = (
            getattr(info, "bits_per_sample", 0)
            or getattr(info, "bit_depth", 0)
            or getattr(info, "bits", 0)
            or 0
        )

        return AudioQuality(
            bitrate=bitrate,
            sample_rate=sample_rate,
            format=suffix,
            channels=channels,
            bit_depth=bit_depth,
        )
    except Exception as e:
        logger.warning(f"读取音质信息失败 {file_path}: {e}")
        return AudioQuality(format=suffix)


# ============================================================
# §10.5.1 内容重复检测器
# ============================================================

class DuplicateDetector:
    """重复文件检测器 —— 检测内容完全相同的文件（§10.5.1）。

    双重检测：
    - **L1（名称+大小）**：同目录下归一化名称 + 文件大小一致 → 疑似重复。
    - **L2（内容哈希）**：SHA-256 哈希完全一致 → 精确重复（最终防线）。

    使用方式::

        detector = DuplicateDetector(config)
        for entry in entries:
            detector.check(entry)
            if entry.is_duplicate:
                ...  # 跳过 / 处理重复

    复用 :class:`processor.file_scanner.AudioFileEntry`，检测后在其上
    标记 ``is_duplicate`` 与 ``duplicate_of`` 字段。
    """

    def __init__(self, config: Optional[dict] = None):
        """初始化重复检测器。

        Args:
            config: 重复检测配置（``duplicate`` 配置块），支持字段：
                ``use_hash``（是否启用 L2 哈希检测）、``hash_algorithm``
                （哈希算法，默认 sha256）。
        """
        self.config: dict = config or {}
        # 名称+大小 去重表：key = "name_normalized|size" → [entry, ...]
        self.seen_by_name: dict[str, list[Any]] = {}
        # 哈希去重表：key = file_hash → entry
        self.seen_by_hash: dict[str, Any] = {}
        # 累计检测到的重复文件数（L1 + L2）
        self.duplicate_count: int = 0

    def check(self, entry: Any) -> Any:
        """对单个文件条目执行双重重复检测，返回标记后的条目。

        Args:
            entry: :class:`AudioFileEntry` 实例（需含 ``name_normalized``、
                ``size``、可选 ``file_hash`` 字段）。

        Returns:
            标记后的同一个 entry 对象（``is_duplicate`` / ``duplicate_of``
            被原地更新）。
        """
        # —— L1：归一化名称 + 大小 ——
        name_key = self._name_key(entry)
        if name_key and name_key in self.seen_by_name:
            entry.is_duplicate = True
            entry.duplicate_of = self.seen_by_name[name_key][0].file_path
            self.duplicate_count += 1
            return entry
        if name_key:
            self.seen_by_name.setdefault(name_key, []).append(entry)

        # —— L2：文件内容哈希（精确检测） ——
        if self._use_hash():
            file_hash = self._ensure_hash(entry)
            if file_hash:
                if file_hash in self.seen_by_hash:
                    entry.is_duplicate = True
                    entry.duplicate_of = self.seen_by_hash[file_hash].file_path
                    self.duplicate_count += 1
                else:
                    self.seen_by_hash[file_hash] = entry

        return entry

    # ----------------------------------------------------------
    # 辅助方法
    # ----------------------------------------------------------
    def _name_key(self, entry: Any) -> str:
        """构建 L1 检测的 key：``name_normalized|size``。"""
        name = getattr(entry, "name_normalized", "") or ""
        size = getattr(entry, "size", 0) or 0
        if not name:
            return ""
        return f"{name}|{size}"

    def _use_hash(self) -> bool:
        """是否启用 L2 哈希检测（默认 True）。"""
        return bool(self.config.get("use_hash", True))

    def _hash_algorithm(self) -> str:
        """哈希算法名（默认 sha256）。"""
        return str(self.config.get("hash_algorithm", "sha256")).lower()

    def _ensure_hash(self, entry: Any) -> str:
        """确保 entry 已计算 file_hash，未计算则就地计算。

        Args:
            entry: :class:`AudioFileEntry` 实例。

        Returns:
            文件哈希字符串；计算失败返回空串。
        """
        existing = getattr(entry, "file_hash", None)
        if existing:
            return existing

        file_path = getattr(entry, "file_path", None)
        if not file_path:
            return ""

        try:
            h = self._compute_hash(file_path)
            # 就地写回，避免重复计算
            try:
                entry.file_hash = h
            except (AttributeError, TypeError):
                pass
            return h
        except Exception as e:
            logger.warning(f"计算文件哈希失败 {file_path}: {e}")
            return ""

    def _compute_hash(self, file_path: str) -> str:
        """计算文件内容的哈希值（分块读取，避免大文件占用内存）。"""
        algo = self._hash_algorithm()
        hasher = hashlib.new(algo)
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                hasher.update(chunk)
        return hasher.hexdigest()

    # ----------------------------------------------------------
    # 统计
    # ----------------------------------------------------------
    def stats(self) -> dict:
        """返回检测统计信息。"""
        return {
            "unique_by_name": len(self.seen_by_name),
            "unique_by_hash": len(self.seen_by_hash),
            "duplicate_count": self.duplicate_count,
        }


# ============================================================
# §10.5.2 / §10.5.3 文件名冲突解决器
# ============================================================

# 支持的冲突解决模式
CONFLICT_MODES = {"keep_best_quality", "skip", "overwrite", "rename_new", "keep_first", "keep_last", "ask", "replace"}

# 默认音质优先级（按字段顺序比较）
_DEFAULT_QUALITY_PRIORITY: list[str] = ["bitrate", "sample_rate", "format"]


class ConflictResolver:
    """文件名冲突解决器（§10.5.2 / §10.5.3）。

    当新文件的目标位置已存在同名文件时，按配置模式决定保留哪个文件。
    支持 ``keep_best_quality`` / ``skip`` / ``overwrite`` / ``rename_new``
    等模式，并通过 :meth:`_compare_quality` 按优先级比较音质。

    使用方式::

        resolver = ConflictResolver(config["filename_conflict"])
        action, target, reason = resolver.resolve(
            new_file=r"E:\\下载\\稻香(HiFi).mp3",
            existing_file=r"E:\\库\\周杰伦\\稻香-周杰伦.mp3",
            mode="keep_best_quality",
        )
        if action == "overwrite":
            shutil.move(new_file, target)
        elif action == "rename_new":
            shutil.move(new_file, target)
        # action == "skip" → 不处理新文件
    """

    def __init__(self, config: Optional[dict] = None):
        """初始化冲突解决器。

        Args:
            config: ``filename_conflict`` 配置块，支持字段：
                ``mode``（默认模式）、``quality_priority``（音质比较优先级，
                如 ``["bitrate", "sample_rate", "format"]``）、
                ``skip_existing``（断点续传：跳过已存在文件）。
        """
        self.config: dict = config or {}
        self.quality_priority: list[str] = list(
            self.config.get("quality_priority", _DEFAULT_QUALITY_PRIORITY)
        )
        self.default_mode: str = self.config.get("mode", "keep_best_quality")
        self.skip_existing: bool = bool(self.config.get("skip_existing", False))

    # ----------------------------------------------------------
    # 主入口
    # ----------------------------------------------------------
    def resolve(
        self,
        new_file: str,
        existing_file: str,
        mode: Optional[str] = None,
    ) -> tuple[str, str, str]:
        """解决文件名冲突，返回处理决策。

        Args:
            new_file: 新文件路径（待写入）。
            existing_file: 已存在的目标文件路径。
            mode: 冲突处理模式，为 ``None`` 时使用实例默认模式。支持：
                - ``keep_best_quality``：对比音质，保留更好的（默认）
                - ``skip``：跳过新文件，保留现有
                - ``overwrite``：用新文件覆盖现有
                - ``rename_new``：为新文件添加序号后缀，全部保留
                - ``keep_first``：保留现有（等价 skip）
                - ``keep_last``：用新文件覆盖（等价 overwrite）
                - ``ask``：返回 ask，由调用方弹窗询问

        Returns:
            ``(action, target_path, reason)`` 三元组：
            - ``action``：``"skip"`` / ``"overwrite"`` / ``"rename_new"``
              / ``"ask"`` 之一。``"skip"`` 表示不写入新文件（保留现有）；
              ``"overwrite"`` 表示用新文件覆盖 target_path；
              ``"rename_new"`` 表示把新文件写入 target_path（已改名）。
            - ``target_path``：实际应写入的目标路径。
            - ``reason``：决策的人类可读说明。
        """
        use_mode = (mode or self.default_mode).lower()
        if use_mode not in CONFLICT_MODES:
            logger.warning(f"未知冲突模式 '{use_mode}'，回退到 keep_best_quality")
            use_mode = "keep_best_quality"

        # 断点续传模式：直接跳过已存在文件（优先级最高）
        if self.skip_existing:
            return "skip", existing_file, "skip_existing（断点续传模式）"

        existing_path = Path(existing_file)

        if use_mode == "skip" or use_mode == "keep_first":
            return "skip", existing_file, "保留现有文件，跳过新文件"

        if use_mode in ("overwrite", "keep_last", "replace"):
            return "overwrite", existing_file, "用新文件覆盖现有文件"

        if use_mode == "rename_new":
            renamed = self._build_renamed_path(existing_path)
            return "rename_new", str(renamed), f"新文件重命名为 {renamed.name} 以避免冲突"

        if use_mode == "ask":
            return "ask", existing_file, "需用户确认"

        # keep_best_quality（默认）
        return self._resolve_by_quality(new_file, existing_file)

    # ----------------------------------------------------------
    # 音质比较
    # ----------------------------------------------------------
    def _compare_quality(self, new: AudioQuality, existing: AudioQuality) -> int:
        """比较两个音质对象，返回更优者。

        按 ``quality_priority`` 配置的字段顺序逐项比较：
        - 数值字段（bitrate/sample_rate/channels/bit_depth）：大者优。
        - format 字段：按格式权重表比较（FLAC > MP3 ...）。

        Args:
            new: 新文件音质。
            existing: 现有文件音质。

        Returns:
            ``1`` 表示新文件更好；``-1`` 表示现有更好；``0`` 表示相等。
        """
        for attr in self.quality_priority:
            cmp = self._compare_attr(new, existing, attr)
            if cmp != 0:
                return cmp
        # 配置字段都比较完仍相等，回退到综合评分
        new_score = new.score()
        existing_score = existing.score()
        if new_score > existing_score:
            return 1
        if new_score < existing_score:
            return -1
        return 0

    @staticmethod
    def _compare_attr(new: AudioQuality, existing: AudioQuality, attr: str) -> int:
        """比较单个音质属性。

        Args:
            new: 新文件音质。
            existing: 现有文件音质。
            attr: 属性名（bitrate/sample_rate/format/channels/bit_depth）。

        Returns:
            ``1`` / ``-1`` / ``0``。
        """
        if attr == "format":
            new_w = _FORMAT_WEIGHTS.get(new.format.upper(), 30)
            existing_w = _FORMAT_WEIGHTS.get(existing.format.upper(), 30)
            if new_w > existing_w:
                return 1
            if new_w < existing_w:
                return -1
            return 0

        new_val = getattr(new, attr, 0) or 0
        existing_val = getattr(existing, attr, 0) or 0
        if new_val > existing_val:
            return 1
        if new_val < existing_val:
            return -1
        return 0

    # ----------------------------------------------------------
    # keep_best_quality 决策
    # ----------------------------------------------------------
    def _resolve_by_quality(
        self, new_file: str, existing_file: str
    ) -> tuple[str, str, str]:
        """按音质比较结果决策（keep_best_quality 模式）。"""
        new_quality = get_file_quality(new_file)
        existing_quality = get_file_quality(existing_file)
        cmp = self._compare_quality(new_quality, existing_quality)

        if cmp > 0:
            # 新文件质量更好 → 覆盖
            return (
                "overwrite",
                existing_file,
                f"新文件音质更好 → 覆盖 "
                f"({new_quality.summary()} > {existing_quality.summary()})",
            )
        elif cmp < 0:
            # 现有文件质量更好 → 跳过
            return (
                "skip",
                existing_file,
                f"现有文件音质更好 → 跳过 "
                f"({existing_quality.summary()} >= {new_quality.summary()})",
            )
        else:
            # 音质相同 → 默认保留现有（避免无意义覆盖）
            return (
                "skip",
                existing_file,
                f"音质相同 → 保留现有 ({existing_quality.summary()})",
            )

    # ----------------------------------------------------------
    # rename_new 路径生成
    # ----------------------------------------------------------
    @staticmethod
    def _build_renamed_path(existing_path: Path) -> Path:
        """为新文件生成不冲突的重命名路径。

        解析目标目录中已存在的 ``name (N).ext`` 序号，从下一个递增。
        若无已存在序号，则从 2 开始。

        Args:
            existing_path: 已存在的目标文件路径。

        Returns:
            不冲突的新路径。
        """
        parent = existing_path.parent
        stem = existing_path.stem
        suffix = existing_path.suffix

        # 解析已存在的序号后缀：stem(N).ext
        pattern = re.compile(rf"^{re.escape(stem)} \((\d+)\){re.escape(suffix)}$")
        used_numbers: set[int] = set()
        if parent.exists():
            for f in parent.iterdir():
                if f.is_file():
                    m = pattern.match(f.name)
                    if m:
                        used_numbers.add(int(m.group(1)))

        # 从 2 开始找第一个未占用的序号
        counter = 2
        while True:
            if counter not in used_numbers:
                candidate = parent / f"{stem} ({counter}){suffix}"
                if not candidate.exists():
                    return candidate
            counter += 1


# ============================================================
# 便捷函数：模块级冲突解决（兼容 §10.5.3 示例 resolve_conflict）
# ============================================================

def resolve_conflict(
    target_path: str,
    new_file_path: str,
    mode: str = "rename_new",
    quality_priority: Optional[list[str]] = None,
    skip_existing: bool = False,
) -> tuple[str, str]:
    """解决目标文件名冲突 + 智能文件选择（§10.5.3 示例接口）。

    与 :class:`ConflictResolver.resolve` 功能等价的便捷函数，
    返回值与开发方案文档中的示例签名一致。

    Args:
        target_path: 期望的目标路径（已存在的冲突文件）。
        new_file_path: 新文件的原始路径。
        mode: 冲突处理模式。
        quality_priority: 音质比较优先级（为 ``None`` 时用默认）。
        skip_existing: 断点续传模式（直接跳过）。

    Returns:
        ``(实际使用的目标路径, 操作描述)``：
        - 目标不存在 → ``(target_path, "create")``
        - skip / keep_first → ``("", "skipped")``
        - overwrite / keep_last → ``(target_path, "overwrite")``
        - rename_new → ``(renamed_path, "renamed (→ name)")``
        - keep_best_quality → 视音质比较结果
        - ask → ``(target_path, "ask")``
    """
    path = Path(target_path)
    # 目标不存在 → 直接创建
    if not path.exists():
        return target_path, "create"

    config = {
        "mode": mode,
        "skip_existing": skip_existing,
    }
    if quality_priority is not None:
        config["quality_priority"] = quality_priority

    resolver = ConflictResolver(config)
    action, resolved_path, reason = resolver.resolve(
        new_file=new_file_path,
        existing_file=target_path,
        mode=mode,
    )

    if action == "skip":
        return "", f"skipped ({reason})"
    if action == "overwrite":
        return resolved_path, f"overwrite ({reason})"
    if action == "rename_new":
        return resolved_path, f"renamed (→ {Path(resolved_path).name})"
    if action == "ask":
        return target_path, "ask"
    return resolved_path, action
