"""数据模型与状态枚举。

字段命名尽量与 `literature-downloader` skill 的候选表保持一致，方便和
skill 的既有脚本、报告模板互相对照。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class Status(str, Enum):
    """全文状态标签，与 skill 的 download_status 取值一致。"""

    PENDING = "pending"
    NOT_SELECTED = "not_selected"
    SUCCESS_PDF = "success_pdf"
    SUCCESS_HTML = "success_html"
    SUCCESS_XML = "success_xml"
    METADATA_ONLY = "metadata_only"
    INACCESSIBLE = "inaccessible"
    BROKEN_LINK = "broken_link"
    RATE_LIMITED = "rate_limited"
    EXCLUDED = "excluded"
    FAILED = "failed"

    @property
    def is_success(self) -> bool:
        return self.value in {"success_pdf", "success_html", "success_xml"}


STATUS_ZH: dict[str, str] = {
    "pending": "待下载",
    "not_selected": "未勾选",
    "success_pdf": "已下载 PDF",
    "success_html": "已保存网页全文",
    "success_xml": "已保存 XML 全文",
    "metadata_only": "只找到题录",
    "inaccessible": "平台限制直连下载",
    "broken_link": "链接失效",
    "rate_limited": "被限流，稍后重试",
    "excluded": "已排除",
    "failed": "尝试失败，需人工复查入口",
}

REASON_ZH: dict[str, str] = {
    "http_401": "需要登录或机构权限",
    "http_402": "需要付费或订阅",
    "http_403": "平台拒绝直连下载，建议学校库/VPN/馆际互借",
    "http_404": "链接不存在或已变更",
    "http_410": "链接已失效",
    "http_429": "被限流，稍后重试",
    "paywall_or_login_detected": "检测到登录页或付费墙",
    "html_stub_not_fulltext": "只拿到跳转页/占位页，不是真正全文",
    "platform_blocked_direct_download": "平台拦截了直接下载（反爬/权限限制），需要用学校库或人工打开",
    "platform_bot_challenge": "平台要求人机验证，无法自动直连，请在浏览器里打开或走学校库",
    "no_candidate_url": "没有找到可尝试的全文链接",
    "not_in_journal_whitelist": "期刊不在 config.json 白名单内",
    "not_selected": "本次未勾选下载",
    "timeout": "请求超时",
    "connection_error": "网络连接失败",
}


def status_text(status: str) -> str:
    """把英文状态转成中文说明。"""
    return STATUS_ZH.get(status, status or "待处理")


def reason_text(reason: str) -> str:
    if not reason:
        return ""
    return REASON_ZH.get(reason, reason)


def next_step(status: str, reason: str = "") -> str:
    """按状态给出下一步合法获取建议（沿用 skill 的说法）。"""
    if status == "success_pdf":
        return "已拿到 PDF，优先阅读"
    if status in {"success_html", "success_xml"}:
        return "已保存全文页面，可人工继续追 PDF"
    if status == "inaccessible" or reason in {
        "http_401",
        "http_402",
        "http_403",
        "paywall_or_login_detected",
        "platform_blocked_direct_download",
        "platform_bot_challenge",
    }:
        return "用学校图书馆/VPN；不行就馆际互借或联系作者"
    if reason == "html_stub_not_fulltext":
        return "只拿到跳转页/占位页；用 DOI 走学校库或人工打开出版社页"
    if status == "broken_link":
        return "用 DOI 重新打开出版社页面"
    if status == "rate_limited":
        return "稍后重试，或直接用 DOI 走学校库"
    if status == "not_selected":
        return "本次未勾选；需要时重新检索并勾选下载"
    return "人工复查 DOI、作者主页、机构仓储、Unpaywall"


DOI_PREFIX_RE = re.compile(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", re.I)


def normalize_doi(value: Any) -> str:
    """把 DOI 统一成裸格式（小写、无 URL 前缀）。"""
    text = str(value or "").strip()
    text = DOI_PREFIX_RE.sub("", text)
    return text.strip().strip(".").lower()


def normalize_title(value: Any) -> str:
    """题名归一化，用于无 DOI 时的去重。"""
    text = str(value or "").casefold()
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", " ", text)
    return " ".join(text.split())


def make_record_id(doi: str, title: str, year: Any = "") -> str:
    """生成稳定、可排序的 record_id。"""
    key = normalize_doi(doi) or f"{normalize_title(title)}|{year}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:10]


@dataclass
class JournalEntry:
    """config.json 里的一条期刊白名单。"""

    name: str
    aliases: list[str] = field(default_factory=list)
    issn: list[str] = field(default_factory=list)
    impact_factor: str = ""
    jcr_quartile: str = ""
    metric_year: str = ""
    metric_source: str = ""
    indexing: str = ""
    note: str = ""

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "JournalEntry":
        def as_str_list(value: Any) -> list[str]:
            if value is None or value == "":
                return []
            if isinstance(value, str):
                return [item.strip() for item in value.split(";") if item.strip()]
            if isinstance(value, (list, tuple)):
                return [str(item).strip() for item in value if str(item).strip()]
            return [str(value).strip()]

        return cls(
            name=str(raw.get("name") or "").strip(),
            aliases=as_str_list(raw.get("aliases")),
            issn=as_str_list(raw.get("issn")),
            impact_factor=str(raw.get("impact_factor") or "").strip(),
            jcr_quartile=str(raw.get("jcr_quartile") or "").strip(),
            metric_year=str(raw.get("metric_year") or "").strip(),
            metric_source=str(raw.get("metric_source") or "").strip(),
            indexing=str(raw.get("indexing") or "").strip(),
            note=str(raw.get("note") or "").strip(),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Candidate:
    """一条候选文献。字段对齐 skill 的候选表，并加上本应用的扩展字段。"""

    record_id: str
    title: str = ""
    authors: str = ""
    year: int | None = None
    journal: str = ""
    journal_short: str = ""
    issn: str = ""
    doi: str = ""
    pmid: str = ""
    pmcid: str = ""
    source_database: str = ""
    abstract_if_available: str = ""
    article_type_guess: str = ""
    keyword_include_hits: str = ""
    keyword_group_hits: str = ""
    keyword_field_hits: str = ""
    keyword_exclude_hits: str = ""
    keyword_relevance_score: int = 0
    priority: str = ""
    oa_status: str = ""
    pdf_url_candidate: str = ""
    landing_page_url: str = ""
    article_url: str = ""
    candidate_urls_json: str = ""
    download_status: str = Status.PENDING.value
    failure_reason: str = ""
    final_path: str = ""
    content_format: str = ""
    access_route_used: str = ""
    attempt_count: int = 0
    exclusion_reason_if_any: str = ""
    quality_notes: str = ""
    impact_factor: str = ""
    jcr_quartile: str = ""
    metric_year: str = ""
    metric_source: str = ""
    indexing: str = ""
    venue_note: str = ""
    matched_by: str = ""
    selected: bool = False

    @property
    def is_success(self) -> bool:
        return self.download_status in {"success_pdf", "success_html", "success_xml"}

    @property
    def has_oa_signal(self) -> bool:
        return bool(self.pdf_url_candidate) or self.oa_status in {"open", "oa", "true", "yes"}

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "Candidate":
        """从字典恢复（用于内存缓存与运行态持久化）。"""
        values = dict(raw)
        year = values.get("year")
        if isinstance(year, str):
            values["year"] = int(year) if year.strip().isdigit() else None
        for numeric in ("keyword_relevance_score", "attempt_count"):
            value = values.get(numeric)
            if isinstance(value, str):
                values["attempt_count"] = int(value) if value.strip().isdigit() else 0
                values[numeric] = values["attempt_count"]
        known = {key: values.get(key) for key in cls.__dataclass_fields__ if key in values}
        known["selected"] = bool(known.get("selected", False))
        return cls(**known)  # type: ignore[arg-type]


CANDIDATE_COLUMNS: list[str] = [
    "record_id",
    "title",
    "authors",
    "year",
    "journal",
    "journal_short",
    "issn",
    "doi",
    "pmid",
    "pmcid",
    "source_database",
    "abstract_if_available",
    "article_type_guess",
    "keyword_include_hits",
    "keyword_group_hits",
    "keyword_field_hits",
    "keyword_exclude_hits",
    "keyword_relevance_score",
    "priority",
    "oa_status",
    "pdf_url_candidate",
    "landing_page_url",
    "article_url",
    "candidate_urls_json",
    "download_status",
    "failure_reason",
    "final_path",
    "content_format",
    "access_route_used",
    "attempt_count",
    "exclusion_reason_if_any",
    "quality_notes",
    "impact_factor",
    "jcr_quartile",
    "metric_year",
    "metric_source",
    "indexing",
    "venue_note",
    "matched_by",
]
