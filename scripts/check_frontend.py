"""开发期自检：确认前端 app.js 引用的 DOM id 都存在于 index.html，并检查括号配平。

没有浏览器时可以先用它兜住「界面直接报错」这类低级问题。
用法：python scripts/check_frontend.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parent.parent
HTML = WORKSPACE / "app" / "web" / "index.html"
JS = WORKSPACE / "app" / "web" / "app.js"
CSS = WORKSPACE / "app" / "web" / "style.css"

ID_REF_RE = re.compile(r"""\$\(\s*["']([^"']+)["']\s*\)""")

# 运行时由 JS 模板生成后插入 DOM 的 id（HTML 里不需要预先定义）。
DYNAMIC_IDS: set[str] = {
    "btn-open-folder",
    "btn-confirm-search",
    "btn-expand-reset",
    "btn-save-pubmed",  # 旧版配置弹窗里的按钮，已不再生成
    "toast-box",  # 保存结果提示容器，运行时创建
}


def check() -> int:
    js = JS.read_text(encoding="utf-8")
    html = HTML.read_text(encoding="utf-8")
    css = CSS.read_text(encoding="utf-8")

    html_ids = set(re.findall(r'id="([^"]+)"', html))
    js_ids = set(ID_REF_RE.findall(js))
    missing = sorted(js_ids - html_ids - DYNAMIC_IDS)

    print(f"index.html 定义 id：{len(html_ids)} 个")
    print(f"app.js 引用 id：{len(js_ids)} 个（其中运行时动态生成：{sorted(DYNAMIC_IDS)}）")
    print(f"HTML 定义但 JS 未直接引用：{sorted(html_ids - js_ids)}")
    if missing:
        print(f"[FAIL] app.js 引用了不存在的 id：{missing}")
    else:
        print("[OK] app.js 引用的 id 全部存在")

    balance_ok = True
    for name, text in (("app.js", js), ("style.css", css), ("index.html", html)):
        braces = (text.count("{"), text.count("}"))
        parens = (text.count("("), text.count(")"))
        brackets = (text.count("["), text.count("]"))
        ok = braces[0] == braces[1] and parens[0] == parens[1] and brackets[0] == brackets[1]
        balance_ok = balance_ok and ok
        print(
            f"{name}: {{}}={braces} ()={parens} []={brackets} -> {'[OK]' if ok else '[FAIL] 括号不配平'}"
        )

    # 关键接口必须在前端被调用，避免「按钮点了没反应」
    for endpoint in ["/api/config", "/api/search", "/api/runs/", "/api/open-folder"]:
        ok = endpoint in js
        print(f"{'[OK]' if ok else '[FAIL]'} 前端使用了接口 {endpoint}")
        balance_ok = balance_ok and ok

    return 0 if (balance_ok and not missing) else 1


if __name__ == "__main__":
    sys.exit(check())
