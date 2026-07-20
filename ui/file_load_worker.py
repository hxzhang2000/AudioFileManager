"""后台加载工作线程：递归扫描文件夹 + 读取元数据，避免阻塞 UI（§8A.2）。

将原来在 UI 线程同步执行的 ``os.walk`` + ``mutagen`` 标签读取迁移到
``QThread``，通过 ``pyqtSignal`` 回报进度，主线程据此显示「加载中」进度条，
完成后批量建表。沿用本仓库既有的 ``QThread`` + ``pyqtSignal`` 并发约定
（见 :class:`processor.batch_processor.BatchProcessor`）。
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

from PyQt6.QtCore import QThread, pyqtSignal

from utils.logger import logger
from parser.metadata_resolver import resolve_file_meta
from parser.filename_parser import FileNameParser
from parser.artist_db import ArtistDB
from parser.metadata_reader import MetadataReader

# 与 file_list_widget.AUDIO_EXTENSIONS / processor.file_scanner.AUDIO_EXTENSIONS 保持一致
AUDIO_EXTENSIONS = {".mp3", ".flac", ".m4a", ".ogg", ".wma", ".ape"}
# MV 视频文件扩展名（与 processor.batch_processor.DEFAULT_VIDEO_EXTENSIONS 保持一致）
DEFAULT_VIDEO_EXTENSIONS = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".webm", ".m4v"}
# 所有支持的扩展名（音频 + 视频）
SUPPORTED_EXTENSIONS = AUDIO_EXTENSIONS | DEFAULT_VIDEO_EXTENSIONS
# 递归扫描时跳过的系统/隐藏目录（避免无权限或垃圾目录拖慢扫描）
_SKIP_DIRS = {"$recycle.bin", "system volume information", "lost+found"}


def _walk_error(err: OSError):
    """``os.walk`` 遇到无权限子目录时的回调：记录并跳过。"""
    logger.warning(f"跳过无权限目录: {getattr(err, 'filename', '')} ({err})")


class FileLoadWorker(QThread):
    """后台加载单个/多个文件或文件夹。

    Signals:
        progress (int, int, str): 已完成数量、总数量、当前文件路径
        finished (list): 已解析条目 ``[(path, title, artist, album), ...]``
        error (str): 扫描阶段出错信息
    """

    progress = pyqtSignal(int, int, str)
    finished = pyqtSignal(list)
    error = pyqtSignal(str)

    def __init__(self, items: list[str]):
        super().__init__()
        self._items = items
        self._stop = threading.Event()

    def request_stop(self):
        """请求中止（用户点击进度条「取消」时调用）。"""
        self._stop.set()

    def run(self):
        # 1) 展开文件夹（递归），收集音频文件
        paths: list[str] = []
        try:
            for item in self._items:
                if self._stop.is_set():
                    self.finished.emit([])
                    return
                if os.path.isdir(item):
                    for root, dirs, filenames in os.walk(item, onerror=_walk_error):
                        # 原地修改 dirs 以跳过系统隐藏文件夹（os.walk 约定）
                        dirs[:] = [d for d in dirs if d.lower() not in _SKIP_DIRS]
                        for fn in filenames:
                            if Path(fn).suffix.lower() in SUPPORTED_EXTENSIONS:
                                paths.append(os.path.join(root, fn))
                elif os.path.isfile(item) and Path(item).suffix.lower() in SUPPORTED_EXTENSIONS:
                    paths.append(item)
        except Exception as e:
            self.error.emit(str(e))
            return

        # 去重并保持插入顺序
        seen: set[str] = set()
        unique: list[str] = []
        for p in paths:
            pn = os.path.normpath(p)
            if pn not in seen:
                seen.add(pn)
                unique.append(pn)
        paths = unique

        total = len(paths)
        if total == 0:
            self.finished.emit([])
            return

        # 2) 在后台线程解析元数据（mutagen 文件 I/O），并回报进度
        try:
            parser = FileNameParser(ArtistDB())
        except Exception:
            parser = None
        try:
            reader = MetadataReader()
        except Exception:
            reader = None

        entries: list[tuple[str, str, str, str]] = []
        for i, p in enumerate(paths):
            if self._stop.is_set():
                break
            title, artist, album = resolve_file_meta(p, parser, reader)
            entries.append((p, title, artist, album))
            # 节流：每 20 个文件回报一次，避免海量信号压垮主线程事件队列
            if (i + 1) % 20 == 0 or (i + 1) == total:
                self.progress.emit(i + 1, total, p)

        self.finished.emit(entries)
