"""从 version.py 和 CHANGELOG.md 提取当前版本的 Release 说明。

输出：release_body.md（供 GitHub Actions 的 softprops/action-gh-release 使用）。
"""

import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    # 1. 读取版本号
    version_py = PROJECT_ROOT / "version.py"
    src = version_py.read_text(encoding="utf-8")
    m = re.search(r'APP_VERSION\s*=\s*"(.*?)"', src)
    if not m:
        print("[错误] 无法从 version.py 解析 APP_VERSION")
        sys.exit(1)
    ver = m.group(1)

    # 2. 从 CHANGELOG 提取对应版本的条目
    changelog = PROJECT_ROOT / "CHANGELOG.md"
    text = changelog.read_text(encoding="utf-8")
    pattern = rf"## \[({re.escape(ver)})].*?(?=\n## \[|\Z)"
    found = re.search(pattern, text, re.DOTALL)

    if found:
        body = found.group(0).strip()
    else:
        body = f"## [{ver}]\n\n未找到 CHANGELOG 条目，请编辑 CHANGELOG.md 补充变更说明。"

    # 3. 写入 release_body.md
    out_path = PROJECT_ROOT / "release_body.md"
    out_path.write_text(body, encoding="utf-8")
    print(f"已生成 release_body.md（版本 {ver}，{len(body)} 字符）")


if __name__ == "__main__":
    main()
