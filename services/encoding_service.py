"""编码统一服务（§7.6）。

将音频文件的元数据文字统一到指定的字符集。

适用场景：
- 从网络下载的中文歌曲，ID3 标签可能被保存为 GBK/GB2312/Latin1 等编码
- 老旧车载播放器只支持 GBK，需要将 UTF-8 标签转码
- 混用了多种编码的来源文件，统一后保持一致
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# chardet 为可选依赖（requirements.txt 已声明），缺失时降级为不自动检测
try:
    import chardet  # type: ignore  # noqa: F401
    _HAS_CHARDET = True
except ImportError:  # pragma: no cover - 仅在缺少依赖时触发
    _HAS_CHARDET = False


class EncodingService:
    """编码统一服务。

    检查每个文件的标签文本编码（格式无关），转换为目标字符集。
    需用户明确启用此功能（设置中开启 "启用编码统一"）。

    支持的编码方案（§7.6.1）：
    - CP1252/ISO-8859-1: 老旧 ID3v1 默认编码
    - GBK/GB2312: 中文 Windows 系统常用
    - GB18030: 中文全字符集
    - UTF-8: 现代通用标准（推荐）
    """

    def __init__(self, enabled: bool, target_charset: str = "UTF-8"):
        self.enabled = enabled
        # 目标字符集统一大写，便于比较与回写
        self.target = target_charset.upper()

    # ------------------------------------------------------------
    # 单文件处理
    # ------------------------------------------------------------
    def normalize_file(self, file_path: str) -> dict[str, str]:
        """读取文件现有标签文本，检测编码，转换为目标编码并**回写**。

        格式无关：用 ``mutagen.File`` 自动识别容器
        （ID3/Vorbis/MP4/ASF/APEv2）。
        返回 ``{字段名: 转换后的文本}`` 的字典，供 UI 展示差异。

        .. note::
            ID3（MP3）/ASF（WMA）/APEv2（APE）的 ``tags.items()`` 返回的是
            帧对象/属性对象而非纯字符串，``isinstance(value, str)`` 恒为 False，
            故需按标签容器类型分别处理：分别读取 ``frame.text`` /
            ``attribute.value`` / ``value.value`` 后才能做编码检测与转换。
            FLAC/OGG（Vorbis Comment）/M4A（MP4Tags）的值本身就是字符串列表，
            沿用通用逻辑。
        """
        if not self.enabled:
            return {}

        try:
            import mutagen
            audio = mutagen.File(file_path, easy=False)
        except Exception as e:
            logger.warning(f"编码统一：无法读取标签 {file_path}: {e}")
            return {"_error": "无法读取标签"}
        if audio is None or not getattr(audio, "tags", None):
            return {}

        # 按标签容器类型分别处理（ID3/ASF/APEv2 的 items() 返回帧/属性对象，
        # 旧的通用循环对它们恒不触发转换）
        tags_cls = type(audio.tags).__name__
        if tags_cls == "ID3":
            changes = self._normalize_id3(audio)
        elif tags_cls == "ASFTags":
            changes = self._normalize_asf(audio)
        elif tags_cls == "APEv2":
            changes = self._normalize_ape(audio)
        else:
            # FLAC(Vorbis)/OGG(Vorbis)/M4A(MP4Tags)：值本身为字符串列表
            changes = self._normalize_vorbis_like(audio)

        if changes:
            try:
                # ID3 格式保存时指定 v2_version=4 以确保 UTF-8 兼容性（ID3v2.4）
                if tags_cls == "ID3":
                    audio.save(v2_version=4)
                else:
                    audio.save()
            except Exception as e:
                logger.warning(f"编码统一：回写失败 {file_path}: {e}")
                changes["_error"] = "回写失败"
        return changes

    # ------------------------------------------------------------
    # 各格式编码转换
    # ------------------------------------------------------------
    def _normalize_id3(self, audio) -> dict[str, str]:
        """MP3 (ID3)：遍历文本帧，对 ``frame.text`` 做编码转换。

        ID3 的 ``tags.items()`` 返回 ``(key, Frame)``，Frame 是帧对象而非 str，
        旧的 ``isinstance(original, str)`` 判断恒为 False，转换分支永不执行。
        此处改为读取 ``frame.text`` 列表中的字符串做转换。仅处理 TextFrame
        （TIT2/TPE1/TALB/TCON/TDRC 等），跳过 APIC/USLT/COMM 等非文本帧；
        TDRC 等时间戳帧的 text 是 ID3TimeStamp 对象，单独处理。
        """
        from mutagen.id3 import TextFrame, ID3TimeStamp, Encoding
        changes: dict[str, str] = {}
        for key, frame in list(audio.tags.items()):
            if not isinstance(frame, TextFrame):
                continue  # 跳过非文本帧（APIC/USLT 等）
            new_texts = []
            changed = False
            for original in frame.text:
                if isinstance(original, str):
                    converted = self._detect_and_convert(original)
                    new_texts.append(converted)
                    if converted != original:
                        changed = True
                        changes[key] = converted
                elif isinstance(original, ID3TimeStamp):
                    # TDRC 等时间戳帧的 text 是 ID3TimeStamp 对象
                    s = str(original)
                    converted = self._detect_and_convert(s)
                    new_texts.append(ID3TimeStamp(converted))
                    if converted != s:
                        changed = True
                        changes[key] = converted
                else:
                    new_texts.append(original)
            if changed:
                # 帧对象在 tags 中以引用持有，就地修改 text 即可
                frame.text = new_texts
                # 转换后可能含 CJK 等字符，若帧原编码为 Latin-1 会无法存储，
                # 统一升级为 UTF8（UTF8 可表示全部 Unicode，且与 _write_mp3
                # 的写入约定一致）。ASF/APE/Vorbis/MP4 均以 UTF-8/UTF-16 存储，
                # 无此问题，仅 ID3 需处理。
                frame.encoding = Encoding.UTF8
        return changes

    def _normalize_asf(self, audio) -> dict[str, str]:
        """WMA (ASF)：``ASFBaseAttribute.value`` 属性是文本。

        ASF 的 ``tags.items()`` 返回 ``(key, [ASFBaseAttribute, ...])``，
        每个 attribute 是属性对象而非 str，需读取 ``attribute.value`` 做转换。
        仅处理 ``ASFUnicodeAttribute``（字符串），跳过 DWORD/Word/QWord/Bool/
        binary 等非文本属性；修改在原属性对象上就地完成。
        """
        from mutagen.asf import ASFUnicodeAttribute
        changes: dict[str, str] = {}
        for key, attrs in list(audio.tags.items()):
            if not attrs:
                continue
            for attr in attrs:
                if not isinstance(attr, ASFUnicodeAttribute):
                    continue  # 跳过非文本属性（DWORD/Word/QWord/Bool/binary）
                original = attr.value
                if not isinstance(original, str):
                    continue
                converted = self._detect_and_convert(original)
                if converted != original:
                    changes[key] = converted
                    attr.value = converted  # 就地修改属性对象
        return changes

    def _normalize_ape(self, audio) -> dict[str, str]:
        """APE (APEv2)：``APEValue.value`` 属性是文本。

        APE 的 ``tags.items()`` 返回 ``(key, APEValue)``，APEValue 是值对象
        而非 str，需读取 ``value.value``（APETextValue 为 str）做转换。
        仅处理 ``APETextValue``，跳过 ``APEBinaryValue``；修改在原值对象上就地完成。
        """
        from mutagen.apev2 import APETextValue
        changes: dict[str, str] = {}
        for key, value in list(audio.tags.items()):
            if not isinstance(value, APETextValue):
                continue  # 跳过二进制值（APEBinaryValue）
            original = value.value
            if not isinstance(original, str):
                continue
            converted = self._detect_and_convert(original)
            if converted != original:
                changes[key] = converted
                value.value = converted  # 就地修改值对象
        return changes

    def _normalize_vorbis_like(self, audio) -> dict[str, str]:
        """FLAC/OGG（Vorbis Comment）/M4A（MP4Tags）：值本身为字符串列表。

        这些容器的 ``tags.items()`` 返回 ``(key, [str, ...])``（M4A 文本字段
        同样返回字符串列表），可直接对字符串做编码转换，沿用原有通用逻辑。
        """
        changes: dict[str, str] = {}
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
                    # 非文本帧（如 APIC / APEBinaryValue / MP4 trkn 元组）原样保留
                    new_texts.append(original)
            if changed:
                self._set_tag_text(audio, key, new_texts)
        return changes

    @staticmethod
    def _set_tag_text(audio, key, new_texts):
        """把转换后的文本写回标签（兼容 ID3 帧与通用容器）。"""
        try:
            frame = audio.tags[key]
            if hasattr(frame, "text"):
                # ID3 帧：直接替换 text 属性
                frame.text = new_texts
            else:
                # 通用容器（Vorbis/MP4/ASF/APE）：整体赋值
                audio.tags[key] = new_texts
        except Exception:
            # 兜底：直接整体赋值
            audio.tags[key] = new_texts

    # ------------------------------------------------------------
    # 乱码检测与修复
    # ------------------------------------------------------------
    def _detect_and_convert(self, text: str) -> str:
        """检测字符串的乱码特征，尝试修复并转换为目标字符集。

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

    # ------------------------------------------------------------
    # 批量处理
    # ------------------------------------------------------------
    def apply_to_files(
        self,
        files: list[str],
        callback: Optional[Callable[[int, int, str], None]] = None,
    ) -> dict[str, dict[str, str]]:
        """批量处理文件列表，逐文件做编码统一。

        返回 ``{文件路径: {字段: 转换详情}}`` 的汇总结果。
        ``callback(current, total, file_path)`` 为进度回调
        （用于主界面进度条更新）。
        """
        results: dict[str, dict[str, str]] = {}
        for i, f in enumerate(files):
            if callback:
                # 进度通知：当前序号（从 1 起）、总数、当前文件路径
                callback(i + 1, len(files), f)
            try:
                results[f] = self.normalize_file(f)
            except Exception as e:
                logger.warning(f"编码统一：处理异常 {f}: {e}")
                results[f] = {"_error": str(e)}
        return results
