"""文件名+标签元数据解析（供文件列表与后台加载线程共用，§8A.2/§5.1.2）。

将 :class:`ui.file_list_widget.FileListWidget` 中 ``_resolve_meta`` 的解析
逻辑抽取为纯函数，使后台 :class:`FileLoadWorker` 能在非 UI 线程复用同一套
「文件名推断 + 标签真实值覆盖」的合并规则，保证列表显示与批处理流程一致。
"""

from __future__ import annotations

from pathlib import Path


def resolve_file_meta(
    file_path: str,
    parser=None,
    reader=None,
) -> tuple[str, str, str]:
    """推断 (title, artist, album)。

    先按文件名解析，再用文件已有标签覆盖已知字段（标签真实元数据优先于
    文件名推断）。``parser`` / ``reader`` 为可复用的解析器/读取器实例，传
    ``None`` 时跳过对应步骤（仅返回文件名推断值）。线程安全：调用方负责在
    单一线程内复用实例，本函数不持有任何共享状态。
    """
    title, artist, album = _guess_meta(file_path, parser)
    if reader is not None:
        try:
            existing = reader.read(file_path)
            if existing.get("title"):
                title = existing["title"]
            if existing.get("artist"):
                artist = existing["artist"]
            if existing.get("album"):
                album = existing["album"]
        except Exception:
            # 读取标签失败（损坏/无标签）→ 维持文件名推断值
            pass
    return title, artist, album


def _guess_meta(file_path: str, parser) -> tuple[str, str, str]:
    """从文件名解析 (title, artist, album)。

    优先使用 parser（FileNameParser）；不可用时回退到 ``-`` 分割启发式。
    专辑始终返回空串（需读取标签或搜索后才可知）。
    """
    name = Path(file_path).stem
    if parser is not None:
        try:
            title, artist, _album, _uncertain = parser.parse(Path(file_path).name)
            return title or name, artist or "", ""
        except Exception:
            pass
    # 回退启发式：「歌手 - 标题」或「标题 - 歌手」拆分
    for sep in (" - ", "—", "-", "_"):
        if sep in name:
            left, right = name.split(sep, 1)
            left, right = left.strip(), right.strip()
            if left and right:
                return right, left, ""
    return name, "", ""
