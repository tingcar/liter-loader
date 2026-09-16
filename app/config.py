"""读取与校验 config.json。

每次检索都会重新读盘，因此用户改完期刊白名单后**不需要重启服务**。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .models import JournalEntry
from .query_scope import DEFAULT_SCOPE, normalize_scope, scope_catalog, source_catalog

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = WORKSPACE_ROOT / "config.json"


class ConfigError(ValueError):
    """config.json 内容不合法。"""


@dataclass
class SearchConfig:
    year_from: int = 2021
    year_to: int = 2026
    max_results_per_source: int = 100
    scope: str = DEFAULT_SCOPE
    sources: dict[str, bool] = field(
        default_factory=lambda: {
            "openalex": True,
            "crossref": True,
            "europepmc": True,
            "pubmed": True,
            "scopus": False,  # 需要 API key，默认关闭
            "wos": False,
        }
    )
    exclude_types: list[str] = field(
        default_factory=lambda: ["editorial", "commentary", "news", "conference abstract", "patent"]
    )
    prefer_oa_only: bool = False
    email: str = ""
    unpaywall_email: str = ""
    api_keys: dict[str, str] = field(default_factory=dict)


@dataclass
class DownloadConfig:
    output_root: str = "paperdown"
    timeout_seconds: int = 30
    max_attempts_per_record: int = 4
    delay_seconds: float = 0.6
    concurrency: int = 2


@dataclass
class JournalMatchConfig:
    allow_abbrev_prefix: bool = True
    allow_fuzzy_tokens: bool = True


@dataclass
class LLMProviderConfig:
    """LLM 提供商（一个 OpenAI 兼容端点）。"""

    name: str
    type: str = "deepseek"
    base_url: str = ""
    api_key: str = ""
    label: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.type,
            "label": self.label or PROVIDER_LABEL.get(self.type, self.type),
            "base_url": self.base_url,
            "has_api_key": bool(self.api_key),
        }


@dataclass
class LLMModelConfig:
    """一个可选的 LLM 模型。"""

    name: str
    provider: str
    model: str = ""
    label: str = ""
    api_key: str = ""
    key_source: str = ""
    base_url: str = ""
    temperature: float | None = None
    max_tokens: int = 2048
    json_mode: bool = True
    thinking: str = "disabled"
    reasoning_effort: str = ""

    @property
    def api_model_name(self) -> str:
        return self.model or self.name

    @property
    def has_api_key(self) -> bool:
        return bool(self.api_key)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label or self.name,
            "provider": self.provider,
            "model": self.api_model_name,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "json_mode": self.json_mode,
            "thinking": self.thinking,
            "reasoning_effort": self.reasoning_effort,
            "has_api_key": bool(self.api_key),
            "key_source": self.key_source,
            "base_url": self.base_url,
        }


@dataclass
class LLMConfig:
    """关键词扩展用的 LLM 配置。"""

    enabled: bool = True
    auto_expand: bool = False
    default_model: str = ""
    timeout_seconds: int = 90
    max_new_terms_per_group: int = 10
    topic: str = ""
    language_hint: str = ""
    providers: list[LLMProviderConfig] = field(default_factory=list)
    models: list[LLMModelConfig] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        """是否具备可调用的条件（未配置 key 时界面会提示）。"""
        return bool(self.enabled and self.default_model and self.models)

    def public_view(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "auto_expand": self.auto_expand,
            "default_model": self.default_model,
            "timeout_seconds": self.timeout_seconds,
            "max_new_terms_per_group": self.max_new_terms_per_group,
            "topic": self.topic,
            "language_hint": self.language_hint,
            "providers": [provider.to_dict() for provider in self.providers],
            "models": [model.to_dict() for model in self.models],
            "provider_presets": dict(PROVIDER_PRESETS),
        }

    def sanitized(self) -> dict[str, Any]:
        """写进运行快照的版本：不含任何密钥。"""
        return {
            "enabled": self.enabled,
            "auto_expand": self.auto_expand,
            "default_model": self.default_model,
            "models": [model.name for model in self.models],
        }


PROVIDER_PRESETS: dict[str, dict[str, str]] = {
    "deepseek": {"label": "DeepSeek 官方", "base_url": "https://api.deepseek.com"},
    "openai-compatible": {"label": "OpenAI 兼容端点（自建/其它厂商）", "base_url": ""},
}
PROVIDER_LABEL: dict[str, str] = {name: preset["label"] for name, preset in PROVIDER_PRESETS.items()}


@dataclass
class AppConfig:
    version: int = 1
    search: SearchConfig = field(default_factory=SearchConfig)
    download: DownloadConfig = field(default_factory=DownloadConfig)
    journal_match: JournalMatchConfig = field(default_factory=JournalMatchConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    journals: list[JournalEntry] = field(default_factory=list)
    path: Path = DEFAULT_CONFIG_PATH
    warnings: list[str] = field(default_factory=list)

    @property
    def output_root(self) -> Path:
        root = Path(self.download.output_root).expanduser()
        return root if root.is_absolute() else (WORKSPACE_ROOT / root)

    @property
    def journal_count(self) -> int:
        return len(self.journals)

    def public_view(self) -> dict[str, Any]:
        """给前端的脱敏视图：不返回 api key 明文。"""
        return {
            "version": self.version,
            "path": str(self.path),
            "output_root": str(self.output_root),
            "search": {
                "year_from": self.search.year_from,
                "year_to": self.search.year_to,
                "max_results_per_source": self.search.max_results_per_source,
                "scope": self.search.scope,
                "scope_options": scope_catalog(),
                "sources": dict(self.search.sources),
                "source_catalog": source_catalog(self.search.api_keys),
                "exclude_types": list(self.search.exclude_types),
                "prefer_oa_only": self.search.prefer_oa_only,
                "email": self.search.email,
                "unpaywall_email": self.search.unpaywall_email,
                "pubmed_api_key_configured": bool(self.search.api_keys.get("pubmed")),
                "api_key_configured": {
                    name: bool(str(self.search.api_keys.get(name) or "").strip())
                    for name in ("pubmed", "scopus", "wos")
                },
            },
            "download": {
                "output_root": self.download.output_root,
                "timeout_seconds": self.download.timeout_seconds,
                "max_attempts_per_record": self.download.max_attempts_per_record,
                "delay_seconds": self.download.delay_seconds,
                "concurrency": self.download.concurrency,
            },
            "journal_match": {
                "allow_abbrev_prefix": self.journal_match.allow_abbrev_prefix,
                "allow_fuzzy_tokens": self.journal_match.allow_fuzzy_tokens,
            },
            "llm": self.llm.public_view(),
            "journals": [entry.to_dict() for entry in self.journals],
            "warnings": list(self.warnings),
        }


def _resolve_secret(value: Any, env_names: list[str]) -> tuple[str, str]:
    """把配置里的 api_key 解析成真实密钥。

    优先级（**字面 key 永远优先**，这一点很关键）：
    1. `"api_key": "sk-..."` 直接写明（注意别把 config.json 提交到仓库）
    2. `"api_key": "env:VAR"` 或 `api_key_env: "VAR"` —— 从环境变量取；
       **若该变量未设置，则回退到字面 key / 其它候选环境变量，而不是清空**
       （早期版本在这里清空了密钥，导致「弹窗保存 key 成功，紧接着测试连接报没有 api_key」）
    3. `api_key` 留空时自动探测常见环境变量（如 DEEPSEEK_API_KEY）

    返回（密钥，来源说明）。
    """
    text = str(value or "").strip()

    if text.startswith("env:"):
        name = text[4:].strip()
        resolved = os.environ.get(name, "")
        if resolved:
            return resolved, f"环境变量 {name}"
        for fallback in env_names:
            resolved = os.environ.get(fallback, "")
            if resolved:
                return resolved, f"环境变量 {fallback}"
        return "", f"环境变量 {name}（未设置）"

    if text:
        return text, "config.json（api_key）"

    for name in env_names:
        resolved = os.environ.get(name, "")
        if resolved:
            return resolved, f"环境变量 {name}"
    return "", ""


def _model_secret(item: dict[str, Any], model_name: str, warnings: list[str]) -> tuple[str, str]:
    """解析一个模型的密钥：字面 key 优先，`api_key_env` 只作为回退。

    注意：`api_key_env` 指向的变量未设置时，**绝不清空已填写的字面 key**。
    早期版本正是在这里清空了密钥，导致「弹窗里保存 key 成功，紧接着测试连接却报
    没有配置 api_key」。
    """
    candidates = [
        f"LITLOADER_{model_name.upper().replace('-', '_')}_API_KEY",
        "DEEPSEEK_API_KEY",
    ]
    literal = str(item.get("api_key") or "").strip()
    api_key, source = _resolve_secret(literal, candidates)

    env_name = str(item.get("api_key_env") or "").strip()
    if env_name and not api_key:
        from_env = os.environ.get(env_name, "")
        if from_env:
            api_key, source = from_env, f"环境变量 {env_name}"
        else:
            # 既没有字面 key，指定的环境变量也没设置 —— 这种情况才提示
            warnings.append(
                f"llm.models「{model_name}」指定了 api_key_env={env_name}，但该环境变量未设置、"
                "也没有填写 api_key；请在网页右上角「LLM配置」里填一个 key，或设置该环境变量。"
            )
    return api_key, source


def _parse_llm_config(raw: Any, warnings: list[str]) -> LLMConfig:
    if raw is None:
        return LLMConfig(enabled=False)
    if not isinstance(raw, dict):
        raise ConfigError("config.json 的 llm 字段必须是对象。")

    providers_raw = raw.get("providers")
    if providers_raw is None:
        providers_raw = []
    if not isinstance(providers_raw, list):
        raise ConfigError("config.json 的 llm.providers 必须是数组。")

    providers: list[LLMProviderConfig] = []
    seen_providers: set[str] = set()
    for index, item in enumerate(providers_raw, start=1):
        if not isinstance(item, dict):
            raise ConfigError(f"llm.providers 第 {index} 条必须是对象。")
        name = str(item.get("name") or "").strip()
        if not name:
            raise ConfigError(f"llm.providers 第 {index} 条缺少 name。")
        if name in seen_providers:
            warnings.append(f"LLM 提供商「{name}」重复，已忽略后面的重复项。")
            continue
        seen_providers.add(name)
        provider_type = str(item.get("type") or "deepseek").strip() or "deepseek"
        base_url = str(item.get("base_url") or PROVIDER_PRESETS.get(provider_type, {}).get("base_url", "")).strip()
        provider_api_key, _provider_source = _resolve_secret(
            item.get("api_key"),
            [f"LITLOADER_{name.upper().replace('-', '_')}_API_KEY", "DEEPSEEK_API_KEY"],
        )
        providers.append(
            LLMProviderConfig(
                name=name,
                type=provider_type,
                base_url=base_url,
                api_key=provider_api_key,
                label=str(item.get("label") or "").strip(),
            )
        )

    models_raw = raw.get("models")
    if models_raw is None:
        models_raw = []
    if not isinstance(models_raw, list):
        raise ConfigError("config.json 的 llm.models 必须是数组。")

    models: list[LLMModelConfig] = []
    seen_models: set[str] = set()
    for index, item in enumerate(models_raw, start=1):
        if isinstance(item, str):
            item = {"name": item, "model": item}
        if not isinstance(item, dict):
            raise ConfigError(f"llm.models 第 {index} 条必须是对象（或模型名字符串）。")
        name = str(item.get("name") or item.get("model") or "").strip()
        if not name:
            raise ConfigError(f"llm.models 第 {index} 条缺少 name。")
        if name in seen_models:
            warnings.append(f"LLM 模型「{name}」重复，已忽略后面的重复项。")
            continue
        seen_models.add(name)
        provider_name = str(item.get("provider") or "").strip()
        if not provider_name and len(providers) == 1:
            provider_name = providers[0].name
        if not provider_name:
            provider_name = "deepseek"
        # 字面 api_key 优先；api_key_env 只在没有字面 key 时才作为来源
        api_key, key_source = _model_secret(item, name, warnings)
        temperature_raw = item.get("temperature")
        temperature: float | None = None
        if temperature_raw is not None and str(temperature_raw).strip() != "":
            try:
                temperature = max(0.0, min(float(temperature_raw), 2.0))
            except (TypeError, ValueError):
                warnings.append(f"llm.models「{name}」的 temperature 不是数字，已忽略。")
        models.append(
            LLMModelConfig(
                name=name,
                provider=provider_name,
                model=str(item.get("model") or name).strip(),
                label=str(item.get("label") or "").strip(),
                api_key=api_key,
                key_source=key_source,
                base_url=str(item.get("base_url") or "").strip(),
                temperature=temperature,
                max_tokens=max(64, min(_as_int(item.get("max_tokens"), 2048), 32768)),
                json_mode=_as_bool(item.get("json_mode"), True),
                thinking=str(item.get("thinking") or "disabled").strip().lower() or "disabled",
                reasoning_effort=str(item.get("reasoning_effort") or "").strip(),
            )
        )

    default_model = str(raw.get("default_model") or (models[0].name if models else "")).strip()
    if default_model and default_model not in seen_models:
        warnings.append(f"llm.default_model「{default_model}」不在 llm.models 里，已回退到第一个模型。")
        default_model = models[0].name if models else ""

    enabled = _as_bool(raw.get("enabled"), True)
    if enabled and not models:
        enabled = False
        warnings.append("llm.enabled 为 true，但 llm.models 为空，关键词扩展已自动关闭。")
    if enabled and models and not any(model.api_key for model in models):
        warnings.append(
            "LLM 已启用，但所有模型都还没有可用的 api_key；点网页右上角「LLM配置」填一个 key 即可使用关键词扩展。"
        )

    return LLMConfig(
        enabled=enabled,
        auto_expand=_as_bool(raw.get("auto_expand"), False),
        default_model=default_model,
        timeout_seconds=max(5, min(_as_int(raw.get("timeout_seconds"), 90), 300)),
        max_new_terms_per_group=max(1, min(_as_int(raw.get("max_new_terms_per_group"), 10), 20)),
        topic=str(raw.get("topic") or "").strip(),
        language_hint=str(raw.get("language_hint") or "").strip(),
        providers=providers,
        models=models,
    )


def _as_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def load_config(path: Path | str | None = None) -> AppConfig:
    """读取 config.json；格式错误时抛 ConfigError（带可读中文原因）。"""
    config_path = Path(path).expanduser() if path else DEFAULT_CONFIG_PATH
    if not config_path.is_absolute():
        config_path = (WORKSPACE_ROOT / config_path).resolve()

    if not config_path.exists():
        raise ConfigError(f"找不到配置文件：{config_path}")

    try:
        raw = json.loads(config_path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"config.json 不是合法 JSON：第 {exc.lineno} 行第 {exc.colno} 列 —— {exc.msg}") from exc

    if not isinstance(raw, dict):
        raise ConfigError("config.json 顶层必须是一个 JSON 对象。")

    warnings: list[str] = []
    search_raw = raw.get("search") or {}
    if not isinstance(search_raw, dict):
        raise ConfigError("config.json 的 search 字段必须是对象。")

    sources_raw = search_raw.get("sources") or {}
    if not isinstance(sources_raw, dict):
        raise ConfigError("config.json 的 search.sources 字段必须是对象。")
    # 免费源默认开启；Scopus / Web of Science 需要订阅 API key，默认关闭（缺 key 也不会跑）
    default_on = {
        "openalex": True,
        "crossref": True,
        "europepmc": True,
        "pubmed": True,
        "scopus": False,
        "wos": False,
    }
    sources = {
        name: _as_bool(sources_raw.get(name), default)
        for name, default in default_on.items()
    }

    api_keys_raw = search_raw.get("api_keys") or {}
    if not isinstance(api_keys_raw, dict):
        raise ConfigError("config.json 的 search.api_keys 字段必须是对象。")

    year_from = _as_int(search_raw.get("year_from"), 2021)
    year_to = _as_int(search_raw.get("year_to"), 2026)
    if year_from > year_to:
        warnings.append(f"年份范围异常（{year_from} > {year_to}），已自动交换。")
        year_from, year_to = year_to, year_from

    exclude_raw = search_raw.get("exclude_types")
    if exclude_raw is None:
        exclude_types = ["editorial", "commentary", "news", "conference abstract", "patent"]
    elif isinstance(exclude_raw, str):
        exclude_types = [item.strip() for item in exclude_raw.split(";") if item.strip()]
    elif isinstance(exclude_raw, list):
        exclude_types = [str(item).strip() for item in exclude_raw if str(item).strip()]
    else:
        raise ConfigError("config.json 的 search.exclude_types 必须是数组或分号分隔的字符串。")

    search = SearchConfig(
        year_from=year_from,
        year_to=year_to,
        max_results_per_source=max(1, min(_as_int(search_raw.get("max_results_per_source"), 100), 500)),
        scope=normalize_scope(search_raw.get("scope")),
        sources=sources,
        exclude_types=exclude_types,
        prefer_oa_only=_as_bool(search_raw.get("prefer_oa_only"), False),
        email=str(search_raw.get("email") or "").strip(),
        unpaywall_email=str(search_raw.get("unpaywall_email") or "").strip(),
        api_keys={str(key): str(value or "").strip() for key, value in api_keys_raw.items()},
    )
    if not any(sources.values()):
        warnings.append("所有数据源都被关闭了，检索不会返回结果。")
    if sources.get("scopus") and not str(search.api_keys.get("scopus") or "").strip():
        warnings.append("已启用 Scopus，但没有配置 search.api_keys.scopus，检索时会直接跳过并给出提示。")
    if sources.get("wos") and not str(search.api_keys.get("wos") or "").strip():
        warnings.append("已启用 Web of Science，但没有配置 search.api_keys.wos，检索时会直接跳过并给出提示。")

    download_raw = raw.get("download") or {}
    if not isinstance(download_raw, dict):
        raise ConfigError("config.json 的 download 字段必须是对象。")
    download = DownloadConfig(
        output_root=str(download_raw.get("output_root") or "paperdown").strip() or "paperdown",
        timeout_seconds=max(5, min(_as_int(download_raw.get("timeout_seconds"), 30), 300)),
        max_attempts_per_record=max(1, min(_as_int(download_raw.get("max_attempts_per_record"), 4), 10)),
        delay_seconds=max(0.0, min(_as_float(download_raw.get("delay_seconds"), 0.6), 30.0)),
        concurrency=max(1, min(_as_int(download_raw.get("concurrency"), 2), 8)),
    )

    match_raw = raw.get("journal_match") or {}
    if not isinstance(match_raw, dict):
        raise ConfigError("config.json 的 journal_match 字段必须是对象。")
    journal_match = JournalMatchConfig(
        allow_abbrev_prefix=_as_bool(match_raw.get("allow_abbrev_prefix"), True),
        allow_fuzzy_tokens=_as_bool(match_raw.get("allow_fuzzy_tokens"), True),
    )

    llm = _parse_llm_config(raw.get("llm"), warnings)

    journals_raw = raw.get("journals")
    if journals_raw is None:
        journals_raw = []
    if not isinstance(journals_raw, list):
        raise ConfigError("config.json 的 journals 字段必须是数组。")

    journals: list[JournalEntry] = []
    seen_names: set[str] = set()
    for index, item in enumerate(journals_raw, start=1):
        if not isinstance(item, dict):
            raise ConfigError(f"journals 第 {index} 条必须是对象。")
        entry = JournalEntry.from_dict(item)
        if not entry.name and not entry.issn:
            raise ConfigError(f"journals 第 {index} 条既没有 name 也没有 issn，无法用于匹配。")
        key = entry.name.casefold()
        if key and key in seen_names:
            warnings.append(f"期刊「{entry.name}」在白名单中重复，已忽略后面的重复项。")
            continue
        if key:
            seen_names.add(key)
        journals.append(entry)

    if not journals:
        warnings.append("期刊白名单为空：检索结果会被全部过滤。请先编辑 config.json 的 journals 字段。")

    return AppConfig(
        version=_as_int(raw.get("version"), 1),
        search=search,
        download=download,
        journal_match=journal_match,
        llm=llm,
        journals=journals,
        path=config_path,
        warnings=warnings,
    )
