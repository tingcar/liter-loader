"""Scopus 数据源（Elsevier Scopus Search API）。

- 端点：`https://api.elsevier.com/content/search/scopus`（GET）
- 认证：请求头 `X-ELS-APIKey: <key>`（也可用查询参数 `apiKey`，这里用请求头）
- 关键参数：`query`（Scopus 布尔检索式）、`count`、`start`、`sort=relevancy`、`date`
- 需要 Elsevier 开发者 key，且机构/账号需有 Scopus 订阅权限；没有 key 时给出明确提示

字段语法（四种检索范围，见 `query_scope.py`）：
`TITLE()` / `ABS()` / `AUTHKEY()` / `TITLE-ABS-KEY()` / `ALL()`；
年份用 `PUBYEAR > 2019 AND PUBYEAR < 2027`。

为便于离线自测，端点可用环境变量 `LITLOADER_SCOPUS_ENDPOINT` 覆盖。
"""

from __future__ import annotations

import os
import time

from .base import RawRecord, SearchContext, SearchOutcome, SourceClient, excerpt, merge_authors

API_URL = os.environ.get("LITLOADER_SCOPUS_ENDPOINT", "https://api.elsevier.com/content/search/scopus")
SOURCE_NAME = "scopus"
LABEL = "Scopus"

# STANDARD 视图已包含题名/期刊/作者/DOI/被引等字段；COMPLETE 还会多带摘要
DEFAULT_VIEW = "STANDARD"
COMPLETE_VIEW = "COMPLETE"


def _entries(payload: object) -> list[dict]:
    if not isinstance(payload, dict):
        return []
    results = payload.get("search-results")
    if not isinstance(results, dict):
        return []
    entry = results.get("entry")
    if isinstance(entry, list):
        return [item for item in entry if isinstance(item, dict)]
    if isinstance(entry, dict):
        return [entry]
    return []


def _total(payload: object) -> int:
    if not isinstance(payload, dict):
        return 0
    results = payload.get("search-results")
    if not isinstance(results, dict):
        return 0
    try:
        return int(results.get("opensearch:totalResults") or 0)
    except (TypeError, ValueError):
        return 0


def _issn(entry: dict) -> list[str]:
    raw = entry.get("prism:issn") or entry.get("prism:eIssn") or ""
    return [item.strip() for item in str(raw).split(",") if item.strip()]


def _year(entry: dict) -> int | None:
    for key in ("prism:coverDate", "prism:coverDisplayDate", "prism:publicationDate"):
        value = str(entry.get(key) or "")
        token = value[:4]
        if token.isdigit():
            return int(token)
    return None


def _to_record(entry: dict) -> RawRecord:
    creators = entry.get("author")
    if isinstance(creators, dict):  # 单作者时 Scopus 可能返回对象而不是数组
        creators = [creators]
    authors: list[str] = []
    if isinstance(creators, list):
        for author in creators:
            if isinstance(author, dict):
                # authname 形如 "Doe J."，优先用它；缺失时用 given-name + surname
                name = str(author.get("authname") or "").strip()
                if not name:
                    given = str(author.get("given-name") or "").strip()
                    surname = str(author.get("surname") or "").strip()
                    name = " ".join(part for part in [given, surname] if part)
                if name:
                    authors.append(name)
    doi = str(entry.get("prism:doi") or "").strip()
    scopus_id = str(entry.get("dc:identifier") or "").strip()
    landing = str(entry.get("prism:url") or "").strip()
    if not landing and scopus_id:
        landing = f"https://www.scopus.com/record/display.uri?eid={scopus_id}"
    journal = str(entry.get("prism:publicationName") or "").strip()
    openaccess = str(entry.get("openaccess") or "").strip()
    return RawRecord(
        source=SOURCE_NAME,
        title=str(entry.get("dc:title") or "").strip(),
        authors=merge_authors(authors),
        year=_year(entry),
        journal=journal,
        journal_short=str(entry.get("prism:publicationName") or "").strip(),
        issns=_issn(entry),
        doi=doi,
        article_type=str(entry.get("subtypeDescription") or entry.get("prism:aggregationType") or ""),
        publisher=str(entry.get("prism:publisher") or ""),
        landing_page_url=landing,
        article_url=f"https://doi.org/{doi}" if doi else landing,
        oa_status="open" if openaccess.isdigit() and int(openaccess) > 0 else "",
        cited_by_count=int(str(entry.get("citedby-count") or 0).strip() or 0),
        abstract=excerpt(entry.get("dc:description"), 1200),
    )


def search(context: SearchContext) -> SearchOutcome:
    started = time.monotonic()
    outcome = SearchOutcome(source=SOURCE_NAME)
    api_key = str(context.search.api_keys.get(SOURCE_NAME) or "").strip()
    if not api_key:
        outcome.error = "missing_api_key"
        outcome.warnings.append(
            "Scopus 需要 Elsevier 开发者 API key 才能检索：请在网页右上角「配置」里填入，"
            "或写进 config.json 的 search.api_keys.scopus。"
        )
        outcome.elapsed_seconds = time.monotonic() - started
        return outcome

    query = context.query_for(SOURCE_NAME)
    if not query:
        outcome.warnings.append("关键词为空，Scopus 未检索。")
        outcome.elapsed_seconds = time.monotonic() - started
        return outcome

    client = SourceClient(source=SOURCE_NAME)
    limit = min(context.max_results_per_source, 200)
    params: dict[str, object] = {
        "query": query,
        "count": limit,
        "start": 0,
        "sort": "relevancy",
        "view": COMPLETE_VIEW,
    }
    headers = {"X-ELS-APIKey": api_key, "Accept": "application/json"}
    if context.mailto():
        params["mailTo"] = context.mailto()

    try:
        response = client.session.get(API_URL, params=params, headers=headers, timeout=45)
        if response.status_code in {401, 403}:
            outcome.error = f"http_{response.status_code}"
            outcome.warnings.append(
                f"Scopus 拒绝了这次请求（HTTP {response.status_code}）：API key 无效、或该 key 对应的账号没有 Scopus 订阅权限。"
            )
            outcome.elapsed_seconds = time.monotonic() - started
            return outcome
        if response.status_code == 429:
            outcome.error = "http_429"
            outcome.warnings.append("Scopus 触发限流（HTTP 429），请稍后重试或减少每源条数。")
            outcome.elapsed_seconds = time.monotonic() - started
            return outcome
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:  # noqa: BLE001
        outcome.error = f"{type(exc).__name__}: {exc}"
        outcome.warnings.append(f"Scopus 检索失败：{outcome.error}")
        outcome.elapsed_seconds = time.monotonic() - started
        return outcome

    entries = _entries(payload)
    if not entries:
        total = _total(payload)
        if total == 0:
            outcome.warnings.append("Scopus 没有匹配结果（可能是检索式过窄或该机构未收录）。")
    for entry in entries:
        record = _to_record(entry)
        if record.title:
            outcome.records.append(record)
    outcome.elapsed_seconds = time.monotonic() - started
    return outcome
