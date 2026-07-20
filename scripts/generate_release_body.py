"""从 version.py 和 CHANGELOG.md 提取当前版本的 Release 说明。

输出：release_body.md（供 GitHub Actions 的 softprops/action-gh-release 使用）。
任何异常都不会中断流程（catch-all 兜底），确保 CI 不因本脚本卡住。

匹配策略（从精确到宽松）：
  1. [主匹配] `## [版本号] - YYYY-MM-DD` → 下一个 `## [`
  2. [宽松匹配] 仅 `## [版本号]`（不要求日期）→ 下一个 `## [`
  3. [兜底] 占位文本
"""

import re
import sys
import traceback
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _extract_section(text: str, ver: str) -> str | None:
    """从 CHANGELOG 文本中提取版本号对应的章节。

    按顺序尝试三种策略：
      1. 标准格式 `## [版本号] - 日期`
      2. 宽松格式 `## [版本号]`（无日期）
      3. 文本搜索 + 括号层级截取
    """

    def _match_next(pattern: str, label: str) -> str | None:
        m = re.search(pattern, text, re.DOTALL | re.MULTILINE)
        if m:
            body = m.group(0).strip()
            print(f"[release-body]  [{label}] 匹配到 {len(body)} 字符 (m=1)")
            return body
        print(f"[release-body]  [{label}] 未匹配")
        return None

    escaped = re.escape(ver)

    # 策略 1: 标准格式 `## [1.2.1] - 2026-07-20`
    body = _match_next(
        rf"^## \[{escaped}\]\s*-\s*\d{{4}}-\d{{2}}-\d{{2}}.*?(?=^## \[|\Z)",
        "标准格式",
    )
    if body:
        return body

    # 策略 2: 宽松格式 `## [1.2.1]`（不要求日期）
    body = _match_next(
        rf"^## \[{escaped}\].*?(?=^## \[|\Z)",
        "宽松格式",
    )
    if body:
        return body

    # 策略 3: 文本搜索（完全不依赖正则格式）
    print(f"[release-body]  [文本搜索] ver={ver!r}")
    marker = f"[{ver}]"
    idx = text.find(f"## {marker}")
    if idx == -1:
        print(f"[release-body]  [文本搜索] 未找到 '## [{ver}]'，搜索 '{marker}'")
        idx = text.find(marker)

    if idx >= 0:
        # 从这一行开始，截取到下一个 `## [` 或文件末尾
        # 找到这一行的行首
        line_start = text.rfind("\n", 0, idx)
        if line_start == -1:
            line_start = 0
        # 找下一个 `## [` 或 `\Z`
        next_section = re.search(r"^## \[", text[line_start + 1:], re.MULTILINE)
        if next_section and next_section.start() > 0:
            # next_section.start() 是相对于 text[line_start+1:] 的位置
            end = line_start + 1 + next_section.start()
            body = text[line_start + 1:end].strip()
        elif next_section and next_section.start() == 0:
            body = text[line_start + 1:].strip()
        else:
            body = text[line_start + 1:].strip()
        print(f"[release-body]  [文本搜索] 匹配到 {len(body)} 字符")
        return body

    return None


def main() -> int:
    """生成 release_body.md，返回 0 表示成功，1 表示降级成功。"""
    version_py = PROJECT_ROOT / "version.py"
    changelog = PROJECT_ROOT / "CHANGELOG.md"

    print(f"[release-body] CWD: {Path.cwd()}")
    print(f"[release-body] __file__: {__file__}")
    print(f"[release-body] PROJECT_ROOT: {PROJECT_ROOT}")
    print(f"[release-body] version.py exists: {version_py.exists()}")
    print(f"[release-body] CHANGELOG.md exists: {changelog.exists()}")

    # ---------- 1. 读取版本号 ----------
    try:
        src = version_py.read_text(encoding="utf-8")
    except Exception as e:
        print(f"[release-body] ERROR: 读取 version.py 失败: {e}")
        return 1
    m = re.search(r'APP_VERSION\s*=\s*"(.*?)"', src)
    if not m:
        print("[release-body] ERROR: 无法从 version.py 解析 APP_VERSION")
        print(f"[release-body] version.py 前 500 字符:\n{src[:500]}")
        return 1
    ver = m.group(1)
    print(f"[release-body] 版本号: {ver!r}")

    # ---------- 2. 从 CHANGELOG 提取对应版本的条目 ----------
    try:
        text = changelog.read_text(encoding="utf-8")
    except Exception as e:
        print(f"[release-body] ERROR: 读取 CHANGELOG.md 失败: {e}")
        return 1

    print(f"[release-body] CHANGELOG.md 共 {len(text)} 字符")
    print(f"[release-body] 前 100 字符: {text[:100]!r}")

    body = _extract_section(text, ver)

    if body:
        print(f"[release-body] 策略成功，body 共 {len(body)} 字符")
    else:
        print(f"[release-body] WARNING: 所有策略均未找到 [{ver}] 条目，使用占位文本")
        print(f"[release-body] CHANGELOG 中 `## [` 出现次数: {text.count('## [')}")
        print(f"[release-body] CHANGELOG 中 `[{ver}]` 出现次数: {text.count(f'[{ver}]')}")
        body = f"## [{ver}]\n\n请编辑 CHANGELOG.md 补充变更说明。\n"

    # ---------- 3. 写入 release_body.md ----------
    out_path = PROJECT_ROOT / "release_body.md"
    out_path.write_text(body, encoding="utf-8")
    print(f"[release-body] 已写入 release_body.md（{len(body)} 字符）")
    return 0


if __name__ == "__main__":
    try:
        rc = main()
        sys.exit(rc)
    except Exception:
        print("[release-body] UNEXPECTED ERROR:")
        traceback.print_exc()
        # 兜底：写一个占位 body，不阻塞 CI
        try:
            fallback = PROJECT_ROOT / "release_body.md"
            fallback.write_text(
                "## 版本更新\n\n请查看 CHANGELOG.md 获取完整变更说明。",
                encoding="utf-8",
            )
            print(f"[release-body] 已写入 fallback release_body.md")
        except Exception:
            pass
        sys.exit(0)
