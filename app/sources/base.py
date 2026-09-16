"""数据源公共设施：HTTP 会话、重试、检索上下文与原始记录模型。"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from threading import Lock
from typing import Any, Callable

import requests

from ..config import SearchConfig
from ..query_scope import (
    DEFAULT_SCOPE,
    crossref_query,
    europepmc_query,
    field_labels,
    get_scope,
    openalex_search,
    pubmed_term,
    scopus_query,
    wos_query,
)

USER_AGENT = "liter-loader/1.0 (+lawful-access-check; local research tool)"


@dataclass
class SearchContext:
    """一次检索的输入参数。"""

    keywords_raw: str
    groups: list[list[str]]
    year_from: int
    year_to: int
    max_results_per_source: int
    search: SearchConfig
    scope: str = DEFAULT_SCOPE

    @property
    def abbreviation(self) -> str:
        return self.keywords_raw

    @property
    def field_text(self) -> str:
        """当前检索范围里包含的字段（中文）。"""
        return field_labels(list(get_scope(self.scope).fields))

    def query_for(self, source: str) -> str:
        """按数据源生成实际使用的检索式（给用户看，便于复现）。"""
        if source == "openalex":
            return openalex_search(self.scope, self.groups)
        if source == "europepmc":
            return europepmc_query(self.scope, self.groups, (self.year_from, self.year_to))
        if source == "pubmed":
            return pubmed_term(self.scope, self.groups, (self.year_from, self.year_to))
        if source == "scopus":
            return scopus_query(self.scope, self.groups, (self.year_from, self.year_to))
        if source == "wos":
            query = wos_query(self.scope, self.groups)
            return f"{query}（年份：publishTimeSpan={self.year_from}-01-01+{self.year_to}-12-31）"
        if source == "crossref":
            params = crossref_query(self.scope, self.groups)
            body = " & ".join(f"{key}={value}" for key, value in params.items())
            return f"{body}（Crossref 不支持字段限定，按相关度匹配）"
        return self.query_text

    @property
    def query_text(self) -> str:
        """用于展示的通用 Boolean 检索式。"""
        parts: list[str] = []
        for group in self.groups:
            formatted = [f'"{term}"' if " " in term and not term.startswith('"') else term for term in group]
            parts.append("(" + " OR ".join(formatted) + ")")
        base = " AND ".join(parts) if parts else self.keywords_raw
        return f"{base} ({self.year_from}-{self.year_to})" if base else f"({self.year_from}-{self.year_to})"

    @property
    def terms(self) -> list[str]:
        return [term for group in self.groups for term in group]

    def mailto(self) -> str:
        return self.search.email or ""


@dataclass
class RawRecord:
    """数据源返回的一条未归一化记录。"""

    source: str
    title: str = ""
    authors: str = ""
    year: int | None = None
    journal: str = ""
    journal_short: str = ""
    issns: list[str] = field(default_factory=list)
    doi: str = ""
    pmid: str = ""
    pmcid: str = ""
    abstract: str = ""
    article_type: str = ""
    publisher: str = ""
    landing_page_url: str = ""
    article_url: str = ""
    pdf_url: str = ""
    oa_status: str = ""
    license: str = ""
    keywords: str = ""
    fulltext_links: list[str] = field(default_factory=list)
    cited_by_count: int = 0


@dataclass
class SearchOutcome:
    """一个数据源的检索结果。"""

    source: str
    records: list[RawRecord] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error: str = ""
    elapsed_seconds: float = 0.0

    @property
    def count(self) -> int:
        return len(self.records)


class RateLimiter:
    """进程内共享的简单限速器（同一域名请求间隔不小于 delay）。"""

    def __init__(self, delay: float) -> None:
        self.delay = max(0.0, delay)
        self._locks: dict[str, tuple[Lock, float]] = {}
        self._registry_lock = Lock()

    def _lock_for(self, host: str) -> tuple[Lock, float]:
        with self._registry_lock:
            if host not in self._locks:
                self._locks[host] = (Lock(), 0.0)
            return self._locks[host]

    def wait(self, host: str) -> None:
        lock, _ = self._lock_for(host)
        with lock:
            bucket = self._locks[host]
            last = bucket[1]
            now = time.monotonic()
            remaining = self.delay - (now - last)
            if remaining > 0:
                time.sleep(remaining)
            self._locks[host] = (bucket[0], time.monotonic())


def build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "application/json, application/xml, text/xml, */*;q=0.8",
        }
    )
    return session


# 进程内共享的默认限速器：同一域名两次请求至少间隔 0.5 秒。
DEFAULT_LIMITER = RateLimiter(0.5)


@dataclass
class SourceClient:
    """一个数据源共用的会话 + 限速器。"""

    source: str
    session: requests.Session = field(default_factory=build_session)
    limiter: RateLimiter = field(default_factory=lambda: DEFAULT_LIMITER)

    def json(self, url: str, *, params: dict[str, Any] | None = None, timeout: int = 30, attempts: int = 3) -> Any:
        return get_json(self.session, url, params=params, timeout=timeout, limiter=self.limiter, attempts=attempts)

    def text(self, url: str, *, params: dict[str, Any] | None = None, timeout: int = 30, attempts: int = 3) -> str:
        return get_text(self.session, url, params=params, timeout=timeout, limiter=self.limiter, attempts=attempts)


def get_json(
    session: requests.Session,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    timeout: int = 30,
    limiter: RateLimiter | None = None,
    attempts: int = 3,
) -> Any:
    """带重试的 JSON GET。失败抛最后一个异常。"""
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        if limiter is not None:
            limiter.wait(_host_of(url))
        try:
            response = session.get(url, params=params, timeout=timeout)
            if response.status_code in {429, 500, 502, 503, 504} and attempt < attempts:
                time.sleep(min(2.0 * attempt, 6.0))
                continue
            response.raise_for_status()
            return response.json()
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt < attempts:
                time.sleep(min(1.0 * attempt, 4.0))
    if last_error is not None:
        raise last_error
    raise RuntimeError("unreachable")


def get_text(
    session: requests.Session,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    timeout: int = 30,
    limiter: RateLimiter | None = None,
    attempts: int = 3,
) -> str:
    """带重试的文本 GET（用于 PubMed efetch）。"""
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        if limiter is not None:
            limiter.wait(_host_of(url))
        try:
            response = session.get(url, params=params, timeout=timeout)
            if response.status_code in {429, 500, 502, 503, 504} and attempt < attempts:
                time.sleep(min(2.0 * attempt, 6.0))
                continue
            response.raise_for_status()
            return response.text
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            if attempt < attempts:
                time.sleep(min(1.0 * attempt, 4.0))
    if last_error is not None:
        raise last_error
    raise RuntimeError("unreachable")


def _host_of(url: str) -> str:
    without_scheme = url.split("//", 1)[-1]
    return without_scheme.split("/", 1)[0].lower()


def excerpt(value: object, limit: int = 600) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit]


def merge_authors(names: list[str], limit: int = 12) -> str:
    cleaned = [name.strip() for name in names if name and name.strip()]
    return "; ".join(cleaned[:limit])


def search_function(module: Any) -> Callable[[SearchContext], SearchOutcome] | None:
    """取出模块里的 search 函数（统一约定）。"""
    func = getattr(module, "search", None)
    return func if callable(func) else None
