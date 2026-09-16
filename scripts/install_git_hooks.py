#!/usr/bin/env python
"""安装 git 钩子：提交前自动检查敏感信息。

它会写 `.git/hooks/pre-commit`（以及 Windows 上可用的 pre-commit.cmd），
内容是调用 `python scripts/check_secrets.py --quiet`。钩子属于本地仓库配置，
不会提交到远端。

用法：
    python scripts/install_git_hooks.py          # 安装/更新
    python scripts/install_git_hooks.py --check  # 只看是否已安装
    python scripts/install_git_hooks.py --remove # 卸载
"""

from __future__ import annotations

import argparse
import os
import stat
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]

WORKSPACE = Path(__file__).resolve().parent.parent
HOOK_DIR = WORKSPACE / ".git" / "hooks"
SOURCE = WORKSPACE / "scripts" / "git-hooks" / "pre-commit"

SH_HOOK = """#!/bin/sh
# 由 scripts/install_git_hooks.py 安装：提交前检查敏感信息
python scripts/check_secrets.py --quiet || {{
    echo ""
    echo "提交已中止：可能泄露敏感信息，或 .gitignore 缺少关键规则。"
    echo "查看详情：python scripts/check_secrets.py"
    echo "确认无碍可临时跳过：git commit --no-verify"
    exit 1
}}
exit 0
"""

CMD_HOOK = """@echo off
REM 由 scripts/install_git_hooks.py 安装：提交前检查敏感信息（Windows）
python scripts\\check_secrets.py --quiet
if errorlevel 1 (
    echo.
    echo 提交已中止：可能泄露敏感信息，或 .gitignore 缺少关键规则。
    echo 查看详情：python scripts\\check_secrets.py
    echo 确认无碍可临时跳过：git commit --no-verify
    exit /b 1
)
exit /b 0
"""


def is_git_repo() -> bool:
    return (WORKSPACE / ".git").exists()


def main() -> int:
    parser = argparse.ArgumentParser(description="安装/卸载「提交前检查敏感信息」的 git 钩子")
    parser.add_argument("--check", action="store_true", help="只检查是否已安装")
    parser.add_argument("--remove", action="store_true", help="卸载钩子")
    args = parser.parse_args()

    if not is_git_repo():
        print(f"当前目录不是 git 仓库（找不到 {WORKSPACE / '.git'}），无需安装钩子。")
        return 0

    hook_sh = HOOK_DIR / "pre-commit"
    hook_cmd = HOOK_DIR / "pre-commit.cmd"

    if args.check:
        installed = hook_sh.exists() or hook_cmd.exists()
        print(f"pre-commit 钩子：{'已安装' if installed else '未安装'}")
        return 0

    if args.remove:
        for path in (hook_sh, hook_cmd):
            if path.exists():
                path.unlink()
                print(f"已删除 {path}")
        print("钩子已卸载。")
        return 0

    HOOK_DIR.mkdir(parents=True, exist_ok=True)
    hook_sh.write_text(SH_HOOK, encoding="utf-8", newline="\n")
    try:
        hook_sh.chmod(hook_sh.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except OSError:
        pass
    hook_cmd.write_text(CMD_HOOK, encoding="utf-8")

    if not SOURCE.exists():
        print(f"提示：{SOURCE} 不存在（不影响钩子功能）。")
    print(f"已安装 pre-commit 钩子：\n  {hook_sh}\n  {hook_cmd}")
    print("以后每次 git commit 都会先跑 python scripts/check_secrets.py。")
    print("跳过检查：git commit --no-verify")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
