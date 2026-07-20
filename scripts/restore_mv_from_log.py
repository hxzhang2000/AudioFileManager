"""从批处理日志中提取已移动的 MV 文件，移回原始目录。

用法：
    python scripts/restore_mv_from_log.py <日志文件路径>

日志格式要求：
    → 路径变更：原始文件名 → 目标完整路径

文件会按"路径变更"行中的新文件名（不改名）移回 SOURCE_DIR。
日志中 "⊘ 已跳过" 的文件不处理。
"""

import os
import re
import shutil
import sys
from pathlib import Path

# ============================================================
# 配置：原始源目录（日志中批处理读取文件的目录）
# ============================================================
SOURCE_DIR = R"E:\mkv\MKV精选1万首（一）"


def parse_log(log_path: str) -> list[tuple[str, str]]:
    """解析日志，提取所有 `路径变更` 行。

    Returns:
        (原始文件名, 目标完整路径) 列表。
    """
    pattern = re.compile(
        r"→\s*路径变更：\s*(.+?)\s*→\s*(.+)$"
    )
    entries: list[tuple[str, str]] = []

    with open(log_path, encoding="utf-8") as f:
        for line in f:
            m = pattern.search(line)
            if m:
                original_name = m.group(1).strip()
                target_path = m.group(2).strip()
                entries.append((original_name, target_path))

    return entries


def restore_files(entries: list[tuple[str, str]], dry_run: bool = False) -> None:
    """将文件从目标路径移回源目录。

    Args:
        entries: (原始文件名, 目标完整路径) 列表。
        dry_run: True 时只打印不实际移动。
    """
    source_dir = Path(SOURCE_DIR)
    if not source_dir.is_dir():
        print(f"[错误] 源目录不存在: {source_dir}")
        sys.exit(1)

    moved = 0
    errors = 0

    for original_name, target_path in entries:
        target = Path(target_path)
        if not target.is_file():
            print(f"[跳过] 目标文件不存在: {target}")
            errors += 1
            continue

        # 用户要求：文件名不需要改回去，保留重命名后的文件名
        dest = source_dir / target.name

        if dest == target:
            print(f"[跳过] 目标路径与源路径相同: {target}")
            continue

        if dest.exists():
            print(f"[冲突] 目标已存在，跳过: {dest}")
            errors += 1
            continue

        if dry_run:
            print(f"[模拟] {target} → {dest}")
        else:
            try:
                shutil.move(str(target), str(dest))
                print(f"[移动] {target} → {dest}")
                moved += 1
            except Exception as e:
                print(f"[错误] 移动失败 {target}: {e}")
                errors += 1

    print(f"\n完成：成功 {moved} 个，失败/跳过 {errors} 个")


def main() -> None:
    if len(sys.argv) < 2:
        print("用法：python restore_mv_from_log.py <日志文件路径>")
        print("示例：python restore_mv_from_log.py D:\\log.txt")
        sys.exit(1)

    log_path = sys.argv[1]
    if not os.path.isfile(log_path):
        print(f"[错误] 日志文件不存在: {log_path}")
        sys.exit(1)

    dry_run = "--dry-run" in sys.argv

    entries = parse_log(log_path)
    if not entries:
        print("[提示] 未找到任何「路径变更」记录，请确认日志格式。")
        sys.exit(0)

    print(f"找到 {len(entries)} 个已移动文件记录")
    restore_files(entries, dry_run=dry_run)


if __name__ == "__main__":
    main()
