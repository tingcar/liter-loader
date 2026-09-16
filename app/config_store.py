"""安全地修改 config.json（给网页「配置」弹窗用）。

设计要点：

1. **只改点名的字段**：按路径（例如 `llm.models[deepseek-flash].api_key`、
   `search.api_keys.scopus`）逐层定位，其余内容原样保留；
2. **写前校验**：新内容必须能被 `load_config` 解析通过，否则不落盘；
3. **原子写 + 备份**：先写 `.tmp`，备份成 `config.json.bak`，再替换原文件，
   避免断电/异常导致配置损坏；
4. **绝不回显密钥**：本模块只写不读密钥；调用方拿到的结果里只有「有没有配 key」。
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import AppConfig, ConfigError, load_config


class ConfigWriteError(RuntimeError):
    """配置文件不可写或写入后校验失败。"""


@dataclass
class UpdateResult:
    changed: list[str] = field(default_factory=list)
    backup_path: str = ""
    message: str = ""


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigWriteError(f"找不到配置文件：{path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ConfigWriteError(f"config.json 不是合法 JSON：第 {exc.lineno} 行第 {exc.colno} 列 —— {exc.msg}") from exc
    if not isinstance(data, dict):
        raise ConfigWriteError("config.json 顶层必须是对象。")
    return data


def _atomic_write(path: Path, data: dict[str, Any]) -> str:
    """原子写入并留一份备份，返回备份路径。"""
    backup = path.with_suffix(path.suffix + ".bak")
    tmp = path.with_suffix(path.suffix + ".tmp")
    payload = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    try:
        tmp.write_text(payload, encoding="utf-8")
    except OSError as exc:
        raise ConfigWriteError(f"配置文件不可写（{exc.__class__.__name__}）：{path}") from exc
    if path.exists():
        try:
            shutil.copyfile(path, backup)
        except OSError:
            backup = Path("")
    try:
        os.replace(tmp, path)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise ConfigWriteError(f"替换配置文件失败：{exc}") from exc
    return str(backup) if backup else ""


def _ensure_mapping(parent: dict[str, Any], key: str, *, label: str) -> dict[str, Any]:
    node = parent.get(key)
    if not isinstance(node, dict):
        if node not in (None, ""):
            raise ConfigWriteError(f"{label} 不是对象，无法修改。")
        node = {}
        parent[key] = node
    return node


def _ensure_list(parent: dict[str, Any], key: str, *, label: str) -> list[Any]:
    node = parent.get(key)
    if not isinstance(node, list):
        node = []
        parent[key] = node
    return node


def _upsert(entries: list[Any], key: str, spec: dict[str, Any]) -> dict[str, Any]:
    """按 name 找到条目并更新，找不到就追加。返回命中的条目。"""
    for entry in entries:
        if isinstance(entry, dict) and str(entry.get("name") or entry.get("model") or "") == key:
            entry.update(spec)
            return entry
    created = {"name": key, **spec}
    entries.append(created)
    return created


def update_config_file(
    path: Path | str,
    *,
    source_keys: dict[str, str] | None = None,
    sources: dict[str, bool] | None = None,
    model_keys: dict[str, str] | None = None,
    provider_keys: dict[str, str] | None = None,
    email: str | None = None,
    unpaywall_email: str | None = None,
    new_provider: dict[str, Any] | None = None,
) -> UpdateResult:
    """把需要修改的字段写进 config.json，并在写前用 load_config 校验。

    - `source_keys`：`{"scopus": "key", "wos": "key", "pubmed": "key"}`
    - `sources`：`{"scopus": true}` 打开/关闭检索渠道
    - `model_keys`：`{"deepseek-flash": "sk-..."}`
    - `provider_keys`：`{"deepseek": "sk-..."}`
    - `new_provider`：新增/更新一个 LLM 提供商（`{name, type, base_url, api_key?}`）
    """
    config_path = Path(path).expanduser().resolve()
    data = _read_json(config_path)
    result = UpdateResult()

    search = _ensure_mapping(data, "search", label="search")

    if source_keys:
        keys = _ensure_mapping(search, "api_keys", label="search.api_keys")
        for name, value in source_keys.items():
            keys[name] = str(value or "").strip()
            result.changed.append(f"search.api_keys.{name}")

    if sources:
        source_flags = _ensure_mapping(search, "sources", label="search.sources")
        for name, value in sources.items():
            source_flags[name] = bool(value)
            result.changed.append(f"search.sources.{name}")

    if email is not None:
        search["email"] = str(email).strip()
        result.changed.append("search.email")
    if unpaywall_email is not None:
        search["unpaywall_email"] = str(unpaywall_email).strip()
        result.changed.append("search.unpaywall_email")

    llm_needed = bool(model_keys or provider_keys or new_provider)
    llm = _ensure_mapping(data, "llm", label="llm") if llm_needed else data.get("llm")
    if llm_needed and isinstance(llm, dict):
        if new_provider:
            name = str(new_provider.get("name") or "").strip()
            if not name:
                raise ConfigWriteError("新增/更新提供商时必须提供 name。")
            providers = _ensure_list(llm, "providers", label="llm.providers")
            spec: dict[str, Any] = {}
            for key in ("type", "base_url"):
                if new_provider.get(key) is not None:
                    spec[key] = str(new_provider[key]).strip()
            if new_provider.get("api_key") is not None:
                spec["api_key"] = str(new_provider["api_key"]).strip()
            _upsert(providers, name, spec)
            result.changed.append(f"llm.providers[{name}]")

        if provider_keys:
            providers = _ensure_list(llm, "providers", label="llm.providers")
            for name, value in provider_keys.items():
                _upsert(providers, name, {"api_key": str(value or "").strip()})
                result.changed.append(f"llm.providers[{name}].api_key")

        if model_keys:
            models = _ensure_list(llm, "models", label="llm.models")
            for name, value in model_keys.items():
                _upsert(models, name, {"api_key": str(value or "").strip()})
                result.changed.append(f"llm.models[{name}].api_key")

    if not result.changed:
        raise ConfigWriteError("没有需要修改的内容。")

    # 写前校验：必须能被正式配置解析器读通
    result.backup_path = _atomic_write(config_path, data)
    try:
        load_config(config_path)
    except ConfigError as exc:
        if result.backup_path:
            shutil.copyfile(result.backup_path, config_path)
        raise ConfigWriteError(f"写入后的配置校验失败，已回滚：{exc}") from exc

    result.message = "配置已保存到 config.json" + (f"（备份：{Path(result.backup_path).name}）" if result.backup_path else "")
    return result


def reload_config(config: AppConfig) -> AppConfig:
    """按原路径重新读盘（写完配置后调用）。"""
    return load_config(config.path)
