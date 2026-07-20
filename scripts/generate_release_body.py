"""从 version.py 和 CHANGELOG.md 提取当前版本的 Release 说明。

输出：release_body.md（供 GitHub Actions 的 softprops/action-gh-release 使用）。
任何异常都不会中断流程（catch-all 兜底），确保 CI 不因本脚本卡住。
"""

import re
import sys
import traceback
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


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
    src = version_py.read_text(encoding="utf-8")
    m = re.search(r'APP_VERSION\s*=\s*"(.*?)"', src)
    if not m:
        print("[release-body] ERROR: 无法从 version.py 解析 APP_VERSION")
        print(f"[release-body] version.py raw:\n{src}")
        # 尝试从 tag 名获取版本号（CI 环境）
        return 1
    ver = m.group(1)
    print(f"[release-body] 版本号: {ver}")

    # ---------- 2. 从 CHANGELOG 提取对应版本的条目 ----------
    text = changelog.read_text(encoding="utf-8")
    # 匹配 "## [版本号]" 到下一个 "## [" 或文件末尾
    pattern = rf"^## \[{re.escape(ver)}\]\s*-.*?(?=^## \[|\Z)"
    print(f"[release-body] 正则: {pattern}")
    found = re.search(pattern, text, re.DOTALL | re.MULTILINE)

    if found:
        body = found.group(0).strip()
        print(f"[release-body] 匹配到 {len(body)} 字符")
    else:
        print(f"[release-body] WARNING: 未找到 [{ver}] 条目，使用占位文本")
        body = f"## [{ver}]\n\n请编辑 CHANGELOG.md 补充变更说明。"

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
            fallback = (PROJECT_ROOT / "release_body.md")
            fallback.write_text("## 版本更新\n\n请查看 CHANGELOG.md 获取完整变更说明。", encoding="utf-8")
            print(f"[release-body] 已写入 fallback release_body.md")
        except Exception:
            pass
        sys.exit(0)
