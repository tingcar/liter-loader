#!/usr/bin/env python
"""敏感信息检查：确认不会被推到远端仓库。

这个脚本**不调用 git 子进程**（某些受限环境不允许捕获子进程输出），而是：

1. 自己解析 `.gitignore`，判断每个文件「会不会被提交」；
2. 扫描工作区文本文件，按正则找 API key、Bearer token、密码字段、邮箱、本机绝对路径；
3. 分别报告「会被提交的文件」与「已被忽略的文件」里的命中；
4. 额外检查 `.gitignore` 是否覆盖了必须忽略的敏感项（真实配置、备份、.env、运行产物等）。

用法：
    python scripts/check_secrets.py            # 有「会被提交的敏感内容」或缺规则则非零退出
    python scripts/check_secrets.py --json     # 输出 JSON，便于接 CI / pre-commit
    python scripts/check_secrets.py --quiet    # 只打印结论
把它接到 pre-commit 钩子即可在提交前拦住密钥。
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import re
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

WORKSPACE = Path(__file__).resolve().parent.parent

SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf", ".zip", ".woff", ".woff2", ".ico", ".pyc"}
SKIP_DIRS = {".git", "__pycache__", ".venv", "node_modules", ".pytest_cache"}

# 仓库里允许出现的「示例密钥」（示例配置/测试夹具）
ALLOWLIST_VALUES = {
    "sk-...",
    "sk-your-key",
    "sk-mock-local",
    "sk-e2e-secret",
    "sk-test-secret",
    "sk-probe-not-real",
    "sk-mock-not-a-real-key",
    "--cleanup--",
    "your-model-name",
}

# 明显是占位/测试用的值：包含这些词就不当作真实泄露
# （判断只作用于「被赋值的那段文本」，避免把真实密钥所在文件的路径名也混进来）
PLACEHOLDER_MARKERS = (
    "test",
    "mock",
    "dummy",
    "fake",
    "sample",
    "example",
    "placeholder",
    "literal",
    "provider-secret",
    "model-secret",
    "secret-key",
    "your-",
    "your_",
    "xxx",
    "not-a-real",
    "<",
)

# 形如 sk-aaaa… / sk-111… 的重复字符密钥，属于占位写法而非真实密钥
REPEATED_VALUE_RE = re.compile(r"^(sk-)?(.)\2{7,}$")

# 引用环境变量的写法不是密钥本身
ENV_REFERENCE_RE = re.compile(r"^env:[A-Za-z0-9_]+$")

ALLOWED_EMAIL_DOMAINS = {"example.com", "example.org", "test.com"}

PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("真实密钥（sk- 开头且长度像真 key）", re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}\b")),
    ("api_key 非空赋值", re.compile(r"""["']?api[_-]?key["']?\s*[:=]\s*["']([^"'\s]{8,})["']""", re.I)),
    ("Bearer / Authorization 令牌", re.compile(r"\bBearer\s+[A-Za-z0-9_\-\.]{16,}\b")),
    (
        "密码类字段",
        re.compile(r"""["']?(password|passwd|secret|access_token|auth_token)["']?\s*[:=]\s*["']([^"'\s]{8,})["']""", re.I),
    ),
    ("邮箱地址", re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")),
    ("本机绝对路径（含 Windows 用户名）", re.compile(r"[A-Za-z]:\\+Users\\+[A-Za-z0-9_.\-]+", re.I)),
]

# .gitignore 必须覆盖的敏感项（path, 说明）
REQUIRED_IGNORES: list[tuple[str, str]] = [
    ("config.json", "真实配置，含 LLM / Scopus / WoS 的 api_key"),
    ("config.json.bak", "配置写回时的备份，同样含 api_key"),
    (".env", "环境变量文件"),
    ("paperdown/runs/demo/文献清单.md", "运行产物（含本机路径与检索记录）"),
    ("config.private.json", "其它私有配置命名"),
    ("token.txt", "随手保存的令牌文件"),
]


class IgnoreRules:
    """极简版 .gitignore 匹配：后者覆盖前者，支持取反、目录、* / ? / **。"""

    def __init__(self, patterns: list[str]) -> None:
        self.rules: list[tuple[re.Pattern[str], bool, bool]] = []
        for raw in patterns:
            line = raw.rstrip("\n")
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            negate = line.startswith("!")
            body = line[1:] if negate else line
            body = body.replace("\\", "/").strip()
            if not body:
                continue
            dir_only = body.endswith("/")
            body = body.strip("/")
            if not body:
                continue
            self.rules.append((re.compile(self._to_regex(body)), negate, dir_only))

    @staticmethod
    def _to_regex(pattern: str) -> str:
        # 先处理 **，再处理 * 与 ?
        parts = pattern.split("**")
        converted = []
        for index, part in enumerate(parts):
            chunk = ""
            for char in part:
                if char == "*":
                    chunk += "[^/]*"
                elif char == "?":
                    chunk += "[^/]"
                else:
                    chunk += re.escape(char)
            converted.append(chunk)
            if index != len(parts) - 1:
                converted.append(".*")
        body = "".join(converted)
        anchor = "" if pattern.startswith("/") or "/" in pattern else "(?:.*/)?"
        return f"^{anchor}{body}(?:/.*)?$"

    def is_ignored(self, rel_path: str) -> bool:
        path = rel_path.replace("\\", "/").strip("/")
        candidates = [path]
        parts = path.split("/")
        for i in range(1, len(parts)):
            candidates.append("/".join(parts[:i]))
        ignored = False
        for pattern, negate, dir_only in self.rules:
            for candidate in candidates:
                if dir_only and candidate != path:
                    continue
                if pattern.match(candidate):
                    ignored = not negate
                    break
        return ignored


def load_ignore_rules() -> IgnoreRules:
    path = WORKSPACE / ".gitignore"
    patterns = path.read_text(encoding="utf-8", errors="ignore").splitlines() if path.exists() else []
    return IgnoreRules(patterns)


def iter_workspace_files() -> list[Path]:
    files: list[Path] = []
    for path in WORKSPACE.rglob("*"):
        if not path.is_file():
            continue
        rel_parts = path.relative_to(WORKSPACE).parts
        if any(part in SKIP_DIRS for part in rel_parts):
            continue
        if path.suffix.lower() in SKIP_SUFFIXES:
            continue
        files.append(path)
    return sorted(files)


def is_allowed(match_text: str, group: str | None, kind: str) -> bool:
    """判断命中是不是「示例/占位/环境变量引用」，而不是真实泄露。

    注意要对**整段命中文本**做判断（例如 `api_key = "sk-provider-secret"`），
    只看捕获组会漏掉 "provider-secret" 这类测试夹具命名。
    """
    value = (group or "").strip().strip("'\"")
    haystack = f"{value} {match_text}".lower()
    if value in ALLOWLIST_VALUES or match_text.strip() in ALLOWLIST_VALUES:
        return True
    if value and ENV_REFERENCE_RE.match(value):
        return True
    if value and REPEATED_VALUE_RE.match(value.strip("'\"")):
        return True
    if any(marker in haystack for marker in PLACEHOLDER_MARKERS):
        return True
    if kind == "邮箱地址":
        domain = match_text.lower().rsplit("@", 1)[-1]
        if domain in ALLOWED_EMAIL_DOMAINS:
            return True
    return False


def scan(files: list[Path]) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    for path in files:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        rel = path.relative_to(WORKSPACE).as_posix()
        for kind, pattern in PATTERNS:
            for match in pattern.finditer(text):
                # 取「被赋值的值」所在的捕获组（各模式下位置不同，统一取第 1 组）
                group = match.group(1) if pattern.groups >= 1 else None
                if is_allowed(match.group(0), group, kind):
                    continue
                findings.append({"file": rel, "kind": kind, "snippet": " ".join(match.group(0).split())[:90]})
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description="检查仓库里是否可能提交敏感信息")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument("--quiet", action="store_true", help="只打印结论")
    args = parser.parse_args()

    rules = load_ignore_rules()
    all_files = iter_workspace_files()
    committed = [p for p in all_files if not rules.is_ignored(p.relative_to(WORKSPACE).as_posix())]
    ignored = [p for p in all_files if rules.is_ignored(p.relative_to(WORKSPACE).as_posix())]

    committed_findings = scan(committed)
    ignored_findings = scan(ignored)
    missing_rules = [{"path": name, "why": why} for name, why in REQUIRED_IGNORES if not rules.is_ignored(name)]

    # 反向确认：真实配置文件必须处于「已忽略」状态
    suspicious_existing = [
        name
        for name in ("config.json", "config.smoke.json", "config.mock-llm.json", "config.json.bak")
        if (WORKSPACE / name).exists() and not rules.is_ignored(name)
    ]

    if args.json:
        print(
            json.dumps(
                {
                    "would_be_committed": [p.relative_to(WORKSPACE).as_posix() for p in committed],
                    "ignored": [p.relative_to(WORKSPACE).as_posix() for p in ignored],
                    "findings_in_committed": committed_findings,
                    "findings_in_ignored": ignored_findings,
                    "missing_ignore_rules": missing_rules,
                    "unignored_sensitive_files": suspicious_existing,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1 if (committed_findings or missing_rules or suspicious_existing) else 0

    ok = not (committed_findings or missing_rules or suspicious_existing)
    if not args.quiet:
        print("=" * 72)
        print("敏感信息检查（不调用 git，规则由 .gitignore 解析得出）")
        print("=" * 72)
        print(f"工作区文件：{len(all_files)} 个 → 会被提交 {len(committed)} 个 / 已被忽略 {len(ignored)} 个")

        print("\n--- ① .gitignore 关键规则是否到位 ---")
        for name, why in REQUIRED_IGNORES:
            hit = rules.is_ignored(name)
            print(f"  [{'OK' if hit else '缺失'}] {name}（{why}）")

        print("\n--- ② 会被提交的文件里的敏感内容 ---")
        if committed_findings:
            for item in committed_findings:
                print(f"  [!!] {item['file']} ← {item['kind']}：{item['snippet']}")
        else:
            print("  [OK] 未发现")

        print("\n--- ③ 被忽略的文件里的敏感内容（不会提交，仅作知情）---")
        if ignored_findings:
            for item in ignored_findings:
                print(f"  [忽略] {item['file']} ← {item['kind']}：{item['snippet']}")
        else:
            print("  （无）")

        if suspicious_existing:
            print("\n--- ④ 反向确认失败 ---")
            for name in suspicious_existing:
                print(f"  [!!] {name} 存在但会被提交，请加入 .gitignore")
        else:
            print("\n--- ④ 反向确认：真实配置/备份均处于已忽略状态 ---")

    print("\n" + ("通过：可以安全提交。" if ok else "未通过：请先处理上面标记的问题。"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
