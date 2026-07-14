"""断点续传状态管理（§10.4）。

状态文件采用单个 JSON 对象格式，按 ``file_path`` 为 key 索引，
便于原子覆盖写入。写入策略：先写临时文件 → ``os.replace()`` 原子替换，
避免崩溃留下半截文件。

状态文件路径：通常为 ``%APPDATA%/AudioFileManager/batch_state.json``

状态文件格式示例::

    {
      "version": 1,
      "source_dir": "E:\\\\下载音乐",
      "updated_at": "2026-07-12T14:30:45",
      "files": {
        "E:\\\\下载音乐\\\\稻香.mp3": {
          "status": "done",
          "current_step": "",
          "error": null,
          "result": "已处理: 稻香 - 周杰伦",
          "processed_at": "2026-07-12T14:30:45"
        }
      }
    }
"""

import json
import os
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# ============================================================
# §10.4.1 模块级函数：原子写入 / 加载
# ============================================================

def save_state(state_file: str, data: dict):
    """原子写入状态文件：写临时文件→原子替换，防止崩溃损坏。

    使用 ``state_file + ".tmp"`` 作为临时文件，写入成功后通过
    ``os.replace()`` 原子替换为目标文件（Windows 上保证原子性）。
    写入失败时仅记录 ``logger.warning``，不抛异常，避免阻塞批处理主流程。

    Args:
        state_file: 状态文件目标路径。
        data: 要写入的完整状态字典。
    """
    tmp = state_file + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, state_file)  # Windows 上原子操作
    except Exception as e:
        logger.warning(f"状态文件写入失败: {e}")


def load_state(state_file: str) -> dict:
    """加载状态文件。

    Args:
        state_file: 状态文件路径。

    Returns:
        状态字典。文件不存在或损坏时返回空字典 ``{}``。
    """
    if not Path(state_file).exists():
        return {}
    try:
        with open(state_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            logger.warning("状态文件格式异常（非 JSON 对象），返回空字典")
            return {}
        return data
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"状态文件读取失败: {e}")
        return {}


# ============================================================
# §10.4.1 BatchStateManager — 断点续传状态管理器
# ============================================================

class BatchStateManager:
    """断点续传状态管理器（§10.4.1）。

    管理批处理中每个文件的处理状态，支持：

    - 标记文件完成 / 失败（``mark_done`` / ``mark_failed``）
    - 查询文件是否已完成（``is_done``，断点续传跳过已处理文件）
    - 获取失败文件列表（``get_failed_files``，供重试使用）
    - 获取整体进度（``get_progress``，返回 done/total/failed 三元组）
    - 清空状态（``clear``，重新处理时调用）

    状态持久化采用原子写入（临时文件→``os.replace``），保证崩溃安全。
    内部状态为单个 JSON 对象，按 ``file_path`` 为 key 索引。
    """

    def __init__(self, state_file: str):
        """初始化状态管理器，加载已有状态文件。

        Args:
            state_file: 状态文件路径。文件不存在时初始化为空状态。
        """
        self.state_file = state_file
        self._state: dict = load_state(state_file)
        # 确保基本结构存在
        self._state.setdefault("version", 1)
        self._state.setdefault("files", {})

    # --------------------------------------------------------
    # 标记文件状态
    # --------------------------------------------------------

    def mark_done(self, file_path: str, result: Optional[str] = None):
        """标记文件已完成，并持久化到状态文件。

        Args:
            file_path: 文件完整路径（作为状态索引 key）。
            result: 处理结果描述（可选，如 ``"已处理: 稻香 - 周杰伦"``）。
        """
        now = datetime.now().isoformat(timespec="seconds")
        self._state.setdefault("files", {})[file_path] = {
            "status": "done",
            "current_step": "",
            "error": None,
            "result": result,
            "processed_at": now,
        }
        self._state["updated_at"] = now
        self._persist()

    def mark_failed(self, file_path: str, error: str):
        """标记文件处理失败，并持久化到状态文件。

        Args:
            file_path: 文件完整路径（作为状态索引 key）。
            error: 错误信息（异常字符串或描述）。
        """
        now = datetime.now().isoformat(timespec="seconds")
        self._state.setdefault("files", {})[file_path] = {
            "status": "failed",
            "current_step": "",
            "error": error,
            "result": None,
            "processed_at": now,
        }
        self._state["updated_at"] = now
        self._persist()

    # --------------------------------------------------------
    # 查询
    # --------------------------------------------------------

    def is_done(self, file_path: str) -> bool:
        """判断文件是否已完成（断点续传跳过判断）。

        Args:
            file_path: 文件完整路径。

        Returns:
            已完成返回 ``True``，否则 ``False``。
        """
        entry = self._state.get("files", {}).get(file_path)
        return entry is not None and entry.get("status") in ("done", "skipped")

    def get_failed_files(self) -> list[str]:
        """获取所有失败文件的路径列表。

        Returns:
            状态为 ``"failed"`` 的文件路径列表。
        """
        return [
            path
            for path, info in self._state.get("files", {}).items()
            if info.get("status") == "failed"
        ]

    def get_progress(self) -> tuple[int, int, int]:
        """获取处理进度。

        Returns:
            ``(done, total, failed)`` 三元组：

            - ``done``: 已完成的文件数
            - ``total``: 状态文件中记录的文件总数
            - ``failed``: 失败的文件数
        """
        files = self._state.get("files", {})
        done = sum(1 for info in files.values() if info.get("status") == "done")
        failed = sum(1 for info in files.values() if info.get("status") == "failed")
        total = len(files)
        return (done, total, failed)

    # --------------------------------------------------------
    # 状态管理
    # --------------------------------------------------------

    def clear(self):
        """清空状态文件和内存状态（重新处理时调用）。

        删除磁盘上的状态文件，并将内存状态重置为空。
        """
        self._state = {"version": 1, "files": {}, "updated_at": None}
        try:
            if Path(self.state_file).exists():
                os.remove(self.state_file)
        except OSError as e:
            logger.warning(f"状态文件删除失败: {e}")

    def get_state(self) -> dict:
        """获取当前完整状态字典（内存中的引用）。

        Returns:
            完整状态字典，包含 ``version``、``files``、``updated_at`` 等字段。
        """
        return self._state

    # --------------------------------------------------------
    # 内部方法
    # --------------------------------------------------------

    def _persist(self):
        """将当前状态持久化到磁盘（原子写入）。"""
        save_state(self.state_file, self._state)
