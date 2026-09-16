"""用大语言模型扩展检索关键词。

流程：

    用户关键词（按 `;` 分块） → LLM 逐块给出同义词/变体/相关术语
    → 校验、去重、限量 → 与用户关键词合并成最终检索式

设计要点：

1. **不破坏原有行为**：LLM 关闭、未配置 key、网络失败、返回格式不对时，
   一律退回「只用用户关键词」，并把原因写进 warnings，检索照常进行。
2. **按概念块返回**：提示词要求模型按块下标（0..n-1）返回，避免把不同概念的
   词混在一起破坏 `AND`/`OR` 结构。
3. **密钥不出后端**：`api_key` 只用于请求头；日志、错误信息、运行快照里都不写明文。
4. **兼容 OpenAI 格式**：DeepSeek 官方 `https://api.deepseek.com/chat/completions`
   与任何 OpenAI 兼容端点都能用，因此「自建/其它厂商」通过 `base_url` + `model` + `api_key` 配置。

DeepSeek 相关契约（2026-09 核对官方文档）：base_url `https://api.deepseek.com`、
可用模型 `deepseek-flash`（对应 DeepSeek-V4.1-Flash）与 `deepseek-v4-pro`、
JSON 输出用 `response_format={"type": "json_object"}`、
思考开关用 `{"thinking": {"type": "enabled|disabled"}}`。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

import requests

from .config import LLMConfig, LLMModelConfig, LLMProviderConfig

# OpenAI 兼容的对话补全路径
CHAT_PATH = "/chat/completions"

DEEPSEEK_DEFAULT_BASE = "https://api.deepseek.com"
DEEPSEEK_BASE_HOSTS = {"api.deepseek.com"}

PROVIDER_PRESETS: dict[str, dict[str, str]] = {
    "deepseek": {
        "label": "DeepSeek 官方",
        "base_url": DEEPSEEK_DEFAULT_BASE,
        "default_model": "deepseek-flash",
    },
    "openai-compatible": {
        "label": "OpenAI 兼容端点（自建/其它厂商）",
        "base_url": "",
        "default_model": "",
    },
}

# 单块最多补充多少个词、整体最多提交多少个词
MAX_TERMS_PER_GROUP = 10
MAX_TERMS_TOTAL = 30
MIN_TERMS_PER_GROUP = 8

TERM_MAX_LENGTH = 60
PROMPT_JOURNAL_LIMIT = 20
MODEL_TIMEOUT_SECONDS = 90

SYSTEM_PROMPT = """你是一名学术文献检索专家。用户会给出一个研究主题的若干「概念块」，
每个概念块里已经有若干同义词。请为每个概念块补充新的检索关键词。

要求：
1. 只做「同义/近义/变体/缩写/上下位/常用别名」扩展，不要改变概念本身；
2. 补充的词必须与原概念块语义相关，不能引入新主题，也不能与其它概念块串味；
3. 每个概念块补充 8-10 个词；优先给英文词（数据源 OpenAlex/Crossref/PubMed 以英文为主）；
   用户已给出的词不要重复；
4. 可用 1-4 个词组成的短语，不要写布尔运算符、通配符或括号；
5. 参考用户给出的期刊白名单（这些是本主题下的常见发表期刊），优先使用这些领域公认的术语；
6. 严格输出 JSON，不要输出任何解释性文字。

输出 JSON 结构（必须严格遵守）：
{
  "groups": [
    {"index": 0, "terms": ["term a", "term b"], "reason": "一句话说明"}
  ]
}
其中 index 必须与输入的概念块下标一致。"""


@dataclass
class ExpandedGroup:
    """一个概念块扩展后的结果。"""

    index: int
    user_terms: list[str]
    llm_terms: list[str]
    reason: str = ""

    @property
    def all_terms(self) -> list[str]:
        return [*self.user_terms, *self.llm_terms]

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "user_terms": list(self.user_terms),
            "llm_terms": list(self.llm_terms),
            "all_terms": self.all_terms,
            "reason": self.reason,
        }


@dataclass
class ExpansionResult:
    """一次关键词扩展的完整结果。"""

    groups: list[ExpandedGroup] = field(default_factory=list)
    model: str = ""
    model_label: str = ""
    prompt_chars: int = 0
    elapsed_seconds: float = 0.0
    usage: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    raw_preview: str = ""

    @property
    def llm_term_count(self) -> int:
        return sum(len(group.llm_terms) for group in self.groups)

    def to_dict(self) -> dict[str, Any]:
        return {
            "groups": [group.to_dict() for group in self.groups],
            "model": self.model,
            "model_label": self.model_label,
            "llm_term_count": self.llm_term_count,
            "elapsed_seconds": round(self.elapsed_seconds, 2),
            "usage": dict(self.usage),
            "warnings": list(self.warnings),
            "prompt_chars": self.prompt_chars,
        }


class LLMError(RuntimeError):
    """LLM 调用或返回解析失败（信息里不含密钥）。"""


# ---------------------------------------------------------------- 配置解析


def resolve_model(config: LLMConfig, model_name: str | None = None) -> tuple[LLMModelConfig, LLMProviderConfig]:
    """按名称取出模型配置与它所属的提供商配置。"""
    target = (model_name or config.default_model or "").strip()
    if not target:
        raise LLMError("没有指定 LLM 模型：请在 config.json 的 llm.default_model 里设置，或在界面上选择。")
    model = next((item for item in config.models if item.name == target), None)
    if model is None:
        known = "、".join(item.name for item in config.models) or "（无）"
        raise LLMError(f"未知的 LLM 模型「{target}」。config.json 里可用的模型：{known}")
    provider = next((item for item in config.providers if item.name == model.provider), None)
    if provider is None:
        known = "、".join(item.name for item in config.providers) or "（无）"
        raise LLMError(f"模型「{model.name}」引用的提供商「{model.provider}」不存在。可用的提供商：{known}")
    return model, provider


def effective_base_url(model: LLMModelConfig, provider: LLMProviderConfig) -> str:
    """模型级 base_url 优先，其次提供商级，最后 DeepSeek 默认值。"""
    for value in (model.base_url, provider.base_url):
        if value and value.strip():
            return value.strip().rstrip("/")
    return DEEPSEEK_DEFAULT_BASE


def effective_api_key(model: LLMModelConfig, provider: LLMProviderConfig) -> str:
    return (model.api_key or provider.api_key or "").strip()


def chat_endpoint(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    if base.endswith("/v1"):
        return f"{base}/chat/completions"
    return f"{base}{CHAT_PATH}"


def _is_deepseek(base_url: str) -> bool:
    host = base_url.split("//", 1)[-1].split("/", 1)[0].lower()
    return host in DEEPSEEK_BASE_HOSTS


def build_request_payload(
    model: LLMModelConfig,
    provider: LLMProviderConfig,
    messages: list[dict[str, str]],
    *,
    json_mode: bool = True,
) -> dict[str, Any]:
    """构造 OpenAI 兼容的请求体。

    - `response_format={"type": "json_object"}` 由模型级 `json_mode` 控制
      （不是所有兼容端点都支持，可以关掉）；
    - DeepSeek 的思考开关走 `thinking`；用 deepseek-reasoner 这类推理模型时
      会忽略 temperature。
    """
    payload: dict[str, Any] = {
        "model": model.model or model.name,
        "messages": messages,
        "stream": False,
        "max_tokens": max(256, min(int(model.max_tokens or 2048), 8192)),
    }
    if json_mode and model.json_mode:
        payload["response_format"] = {"type": "json_object"}

    deepseek = _is_deepseek(effective_base_url(model, provider))
    thinking = (model.thinking or "disabled").strip().lower()
    if deepseek:
        payload["thinking"] = {"type": "enabled" if thinking in {"enabled", "on", "true", "yes"} else "disabled"}
        if thinking in {"enabled", "on", "true", "yes"} and model.reasoning_effort:
            payload["reasoning_effort"] = model.reasoning_effort
    if thinking not in {"enabled", "on", "true", "yes"} and model.temperature is not None:
        payload["temperature"] = float(model.temperature)
    return payload


# ---------------------------------------------------------------- 提示词


def build_user_prompt(
    groups: list[list[str]],
    *,
    topic: str = "",
    journals: list[str] | None = None,
    scope_label: str = "",
    scope_fields: str = "",
    language_hint: str = "",
) -> str:
    """把概念块、白名单期刊与检索范围拼成提示词。"""
    lines: list[str] = []
    if topic:
        lines.append(f"研究主题：{topic}")
    if scope_label:
        lines.append(
            f"检索范围：{scope_label}"
            + (f"（关键词会匹配：{scope_fields}）" if scope_fields else "")
            + "。请优先给出会出现在这些字段里的术语。"
        )
    if language_hint:
        lines.append(f"语言偏好：{language_hint}")

    lines.append("")
    lines.append("概念块（index 从 0 开始）：")
    for index, group in enumerate(groups):
        lines.append(f"- index {index}：{' | '.join(group)}")

    if journals:
        shown = journals[:PROMPT_JOURNAL_LIMIT]
        lines.append("")
        lines.append(
            "期刊白名单（本主题常见发表期刊，仅供参考其研究范围/术语习惯）："
            + "、".join(shown)
            + ("…" if len(journals) > len(shown) else "")
        )

    lines.append("")
    lines.append(
        f"请为每个概念块补充 {MIN_TERMS_PER_GROUP}-{MAX_TERMS_PER_GROUP} 个新的检索关键词，"
        f"不要重复用户已给出的词，按规定的 JSON 结构输出。"
    )
    return "\n".join(lines)


# ---------------------------------------------------------------- 返回解析


JSON_BLOCK_RE = re.compile(r"\{.*\}", re.S)
BAD_TERM_CHARS = set('()[]{}"\'*?~^:;,|')
# 拆词时的分隔符：逗号/顿号/分号/换行/竖线/斜杠，以及独立的 AND / OR
TERM_SPLIT_RE = re.compile(r"[,;、\n|/]+|\s+(?:AND|OR|and|or)\s+")
# 单词级布尔运算符（大小写都清）
BOOLEAN_WORD_RE = re.compile(r"\b(?:AND|OR|NOT|and|or|not)\b")
# 纯文本兜底时的编号行："1) a, b" / "1. a, b"
LIST_LINE_RE = re.compile(r"^(\d{1,2})\s*[).、]\s*(.+)$")


def _clean_terms(raw_terms: Any) -> list[str]:
    """清洗模型给出的词：去掉布尔符号、引号、超长内容，并去重。"""
    if isinstance(raw_terms, str):
        candidates: list[Any] = TERM_SPLIT_RE.split(raw_terms)
    elif isinstance(raw_terms, (list, tuple, set)):
        candidates = list(raw_terms)
    else:
        return []

    cleaned: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        if isinstance(item, dict):
            item = item.get("term") or item.get("keyword") or item.get("text") or ""
        text = str(item or "").strip().strip("\"'").strip()
        text = "".join(" " if char in BAD_TERM_CHARS else char for char in text)
        # 兜底清掉布尔运算符（例如 "(LLM OR GPT)" 会被拆成 "LLM GPT"）
        text = BOOLEAN_WORD_RE.sub(" ", text)
        text = " ".join(text.split())
        if not text or len(text) > TERM_MAX_LENGTH:
            continue
        if text.casefold() in seen:
            continue
        seen.add(text.casefold())
        cleaned.append(text)
    return cleaned


def parse_expansion_payload(content: str, group_count: int, user_groups: list[list[str]]) -> list[ExpandedGroup]:
    """把模型返回解析成按块组织的结果。

    容忍三种常见形态：
    1. `{"groups": [{"index": 0, "terms": [...]}]}`（首选）
    2. `{"keywords": [...]}`：扁平列表，按顺序尽量平均分回各块
    3. 纯文本行：每行 `index: term, term`
    """
    text = (content or "").strip()
    if not text:
        raise LLMError("模型返回了空内容（DeepSeek 的 JSON 模式偶发空返回，可重试或改用非 JSON 模式）。")

    parsed: Any = None
    match = JSON_BLOCK_RE.search(text)
    if match:
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            parsed = None
    if parsed is None:
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None

    per_group: dict[int, list[str]] = {}
    flat: list[str] = []
    reasons: dict[int, str] = {}

    if isinstance(parsed, dict):
        entries = parsed.get("groups") or parsed.get("concepts") or parsed.get("concept_groups")
        if isinstance(entries, list):
            for position, entry in enumerate(entries):
                if not isinstance(entry, dict):
                    terms = _clean_terms(entry)
                    if terms:
                        per_group.setdefault(position, []).extend(terms)
                    continue
                index = entry.get("index", entry.get("group", entry.get("concept_index", position)))
                try:
                    index = int(index)
                except (TypeError, ValueError):
                    index = position
                terms = _clean_terms(entry.get("terms") or entry.get("keywords") or entry.get("synonyms"))
                if terms:
                    per_group.setdefault(index, []).extend(terms)
                reason = str(entry.get("reason") or entry.get("note") or "").strip()
                if reason:
                    reasons[index] = reason
        else:
            for key in ("keywords", "terms", "synonyms", "expanded_keywords"):
                if isinstance(parsed.get(key), list):
                    flat = _clean_terms(parsed[key])
                    break
    elif isinstance(parsed, list):
        if parsed and all(isinstance(item, (str,)) for item in parsed):
            flat = _clean_terms(parsed)
        else:
            for position, entry in enumerate(parsed):
                terms = _clean_terms(entry)
                if terms:
                    per_group.setdefault(position, []).extend(terms)

    if not per_group and not flat:
        # 纯文本兜底：只在「看起来像词表」时启用，避免把一句道歉话当成关键词
        line_groups: dict[int, list[str]] = {}
        for line in text.splitlines():
            stripped = line.strip().lstrip("-*•# ").strip()
            if not stripped:
                continue
            head, sep, tail = stripped.partition(":")
            if sep and head.strip().isdigit():
                terms = _clean_terms(tail)
                if terms:
                    line_groups.setdefault(int(head.strip()), []).extend(terms)
                continue
            if seq_no_match := LIST_LINE_RE.match(stripped):
                terms = _clean_terms(seq_no_match.group(2))
                if terms:
                    line_groups.setdefault(int(seq_no_match.group(1)), []).extend(terms)
                continue
            # 没有编号的行：只在明显是「词 + 逗号/斜杠」的词表时才接受，且整行要够短
            if stripped.endswith(("。", "！", "？", ".", "!", "?")) or len(stripped) > 120:
                continue
            if not re.search(r"[,;、/|]", stripped):
                continue
            terms = _clean_terms(stripped)
            if terms:
                line_groups.setdefault(len(line_groups), []).extend(terms)
        per_group = line_groups

    if not per_group and not flat:
        raise LLMError(f"模型返回的内容无法解析成关键词（前 200 字）：{_shorten(text, 200)}")

    if flat and not per_group:
        # 扁平结果按顺序轮流分配回各块
        for position, term in enumerate(flat):
            per_group.setdefault(position % max(group_count, 1), []).append(term)

    # 模型返回了不存在的块下标 → 提示词或模型输出有问题，直接报错比静默丢词更好
    if per_group:
        out_of_range = sorted(index for index in per_group if index < 0 or index >= group_count)
        if out_of_range:
            raise LLMError(
                f"模型返回的概念块下标超出范围（得到 {out_of_range}，本次只有 {group_count} 个概念块）；"
                "请重试，或检查提示词里的概念块数量。"
            )

    results: list[ExpandedGroup] = []
    for index in range(group_count):
        user_terms = list(user_groups[index]) if index < len(user_groups) else []
        existing = {term.casefold() for term in user_terms}
        llm_terms: list[str] = []
        for term in per_group.get(index, []):
            if term.casefold() in existing:
                continue
            existing.add(term.casefold())
            llm_terms.append(term)
        llm_terms = llm_terms[:MAX_TERMS_PER_GROUP]
        results.append(ExpandedGroup(index=index, user_terms=user_terms, llm_terms=llm_terms, reason=reasons.get(index, "")))
    return results


def cap_total_terms(groups: list[ExpandedGroup], limit: int = MAX_TERMS_TOTAL) -> list[ExpandedGroup]:
    """限制整体新增词数量，保证请求体不会过长。"""
    total = 0
    capped: list[ExpandedGroup] = []
    for group in groups:
        remaining = max(limit - total, 0)
        terms = group.llm_terms[:remaining]
        total += len(terms)
        capped.append(
            ExpandedGroup(index=group.index, user_terms=group.user_terms, llm_terms=terms, reason=group.reason)
        )
    return capped


# ---------------------------------------------------------------- 调用


@dataclass
class LLMClient:
    """极简 OpenAI 兼容客户端。"""

    base_url: str
    api_key: str
    timeout: int = MODEL_TIMEOUT_SECONDS
    session: requests.Session = field(default_factory=requests.Session)

    def chat(self, payload: dict[str, Any], *, attempts: int = 2) -> dict[str, Any]:
        url = chat_endpoint(self.base_url)
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                response = self.session.post(url, headers=headers, json=payload, timeout=self.timeout)
            except requests.Timeout as exc:
                last_error = LLMError(f"调用 LLM 超时（{self.timeout}s）：{url}")
                if attempt < attempts:
                    time.sleep(1.0 * attempt)
                    continue
                raise last_error from exc
            except requests.RequestException as exc:
                raise LLMError(f"无法连接 LLM 服务：{type(exc).__name__}") from exc

            if response.status_code in {429, 500, 502, 503, 504} and attempt < attempts:
                last_error = LLMError(f"LLM 服务返回 HTTP {response.status_code}（将重试）")
                time.sleep(1.5 * attempt)
                continue
            if response.status_code >= 400:
                # 只回显状态码与错误摘要，绝不回显请求头（含 key）
                raise LLMError(f"LLM 服务返回 HTTP {response.status_code}：{_shorten(response.text)}")
            try:
                return response.json()
            except ValueError as exc:
                raise LLMError(f"LLM 返回的不是 JSON：{_shorten(response.text)}") from exc
        if last_error is not None:
            raise last_error
        raise LLMError("调用 LLM 失败")

    def close(self) -> None:
        self.session.close()


def _shorten(text: str, limit: int = 300) -> str:
    return " ".join(str(text or "").split())[:limit]


def extract_content(payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """从 OpenAI 兼容响应里取出内容与用量。"""
    if not isinstance(payload, dict):
        raise LLMError("LLM 返回结构异常。")
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        error = payload.get("error")
        if isinstance(error, dict):
            raise LLMError(f"LLM 报错：{_shorten(str(error.get('message') or error))}")
        raise LLMError("LLM 返回里没有 choices 字段。")
    first = choices[0] if isinstance(choices[0], dict) else {}
    message = first.get("message") if isinstance(first.get("message"), dict) else {}
    content = str(message.get("content") or first.get("text") or "").strip()
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    return content, dict(usage)


def expand_keywords(
    groups: list[list[str]],
    config: LLMConfig,
    *,
    model_name: str | None = None,
    topic: str = "",
    journals: list[str] | None = None,
    scope_label: str = "",
    scope_fields: str = "",
    language_hint: str = "",
) -> ExpansionResult:
    """同步调用 LLM 扩展关键词；失败抛 LLMError（调用方负责降级）。"""
    if not groups:
        raise LLMError("没有可扩展的关键词概念块。")

    model, provider = resolve_model(config, model_name)
    base_url = effective_base_url(model, provider)
    api_key = effective_api_key(model, provider)
    if not base_url:
        raise LLMError(f"模型「{model.name}」没有可用的 base_url，请在 config.json 的 llm 里补上。")
    if not api_key:
        raise LLMError(
            f"模型「{model.name}」没有配置 api_key。请在 config.json 的 llm.models 里填入 key，"
            "或设置环境变量后重载配置。"
        )

    user_prompt = build_user_prompt(
        groups,
        topic=topic,
        journals=journals or [],
        scope_label=scope_label,
        scope_fields=scope_fields,
        language_hint=language_hint,
    )
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]
    payload = build_request_payload(model, provider, messages)

    client = LLMClient(base_url=base_url, api_key=api_key, timeout=int(config.timeout_seconds or MODEL_TIMEOUT_SECONDS))
    started = time.monotonic()
    try:
        response = client.chat(payload)
    finally:
        client.close()
    elapsed = time.monotonic() - started

    content, usage = extract_content(response)
    parsed_groups = cap_total_terms(parse_expansion_payload(content, len(groups), groups))
    result = ExpansionResult(
        groups=parsed_groups,
        model=model.name,
        model_label=model.label or model.name,
        prompt_chars=len(user_prompt),
        elapsed_seconds=elapsed,
        usage=usage,
        raw_preview=_shorten(content, 400),
    )
    if not result.llm_term_count:
        result.warnings.append("LLM 没有给出可用的新关键词（可能都被判定为重复或格式不符）。")
    return result


def test_connection(  # noqa: D401  (名字保持与语义一致)
    config: LLMConfig, model_name: str | None = None
) -> dict[str, Any]:
    """用一个极小请求验证模型/key/base_url 是否可用。"""
    model, provider = resolve_model(config, model_name)
    base_url = effective_base_url(model, provider)
    api_key = effective_api_key(model, provider)
    if not base_url:
        raise LLMError(f"模型「{model.name}」没有可用的 base_url。")
    if not api_key:
        raise LLMError(f"模型「{model.name}」没有配置 api_key。")

    messages = [
        {"role": "system", "content": "你只会输出 JSON。"},
        {"role": "user", "content": '请输出 JSON：{"ok": true}'},
    ]
    payload = build_request_payload(model, provider, messages)
    payload["max_tokens"] = 64
    client = LLMClient(base_url=base_url, api_key=api_key, timeout=min(int(config.timeout_seconds or 60), 60))
    started = time.monotonic()
    try:
        response = client.chat(payload, attempts=1)
    finally:
        client.close()
    content, usage = extract_content(response)
    return {
        "ok": True,
        "model": model.name,
        "model_label": model.label or model.name,
        "provider": provider.name,
        "base_url": base_url,
        "endpoint": chat_endpoint(base_url),
        "elapsed_seconds": round(time.monotonic() - started, 2),
        "reply_preview": _shorten(content, 120),
        "usage": usage,
    }
