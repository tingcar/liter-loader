"""Crossref 数据源（https://api.crossref.org/works）。

主要补充 DOI 元数据、出版社、期刊缩写（short-container-title）和 ISSN。
"""

from __future__ import annotations

import time

from .base import RawRecord, SearchContext, SearchOutcome, SourceClient, excerpt, merge_authors
from ..query_scope import crossref_query, get_scope

API_URL = "https://api.crossref.org/works"
SOURCE_NAME = "crossref"


def _year_of(item: dict) -> int | None:
    for key in ("published-print", "published-online", "issued", "created"):
        block = item.get(key)
        if isinstance(block, dict):
            parts = block.get("date-parts")
            if isinstance(parts, list) and parts and isinstance(parts[0], list) and parts[0]:
                value = parts[0][0]
                if isinstance(value, int):
                    return value
    return None


def _to_record(item: dict) -> RawRecord:
    titles = item.get("title") or []
    title = " ".join(str(value) for value in titles if value).strip() if isinstance(titles, list) else str(titles)
    containers = item.get("container-title") or []
    journal = " ".join(str(value) for value in containers if value).strip() if isinstance(containers, list) else ""
    shorts = item.get("short-container-title") or []
    journal_short = " ".join(str(value) for value in shorts if value).strip() if isinstance(shorts, list) else ""
    issns = [str(value) for value in (item.get("ISSN") or []) if value]
    authors = []
    for author in item.get("author") or []:
        if not isinstance(author, dict):
            continue
        name = " ".join(part for part in [author.get("given"), author.get("family")] if part)
        if name.strip():
            authors.append(name.strip())
    links = []
    for link in item.get("link") or []:
        if isinstance(link, dict) and link.get("URL"):
            links.append(str(link["URL"]))
    pdf_url = next((link for link in links if link.lower().endswith(".pdf") or "pdf" in link.lower()), "")
    subjects = item.get("subject") or []
    keywords = "; ".join(str(value) for value in subjects if value)[:600] if isinstance(subjects, list) else ""
    return RawRecord(
        source=SOURCE_NAME,
        title=title,
        authors=merge_authors(authors),
        year=_year_of(item),
        journal=journal,
        journal_short=journal_short,
        issns=issns,
        doi=str(item.get("DOI") or ""),
        abstract=excerpt(item.get("abstract"), 1200),
        article_type=str(item.get("type") or ""),
        publisher=str(item.get("publisher") or ""),
        landing_page_url=str(item.get("URL") or ""),
        article_url=f"https://doi.org/{item.get('DOI')}" if item.get("DOI") else str(item.get("URL") or ""),
        pdf_url=pdf_url,
        oa_status="open" if pdf_url else "",
        keywords=keywords,
        fulltext_links=[link for link in [pdf_url, str(item.get("URL") or "")] if link],
        cited_by_count=int(item.get("is-referenced-by-count") or 0),
    )


def search(context: SearchContext) -> SearchOutcome:
    started = time.monotonic()
    outcome = SearchOutcome(source=SOURCE_NAME)
    if not context.groups:
        outcome.warnings.append("关键词为空，Crossref 未检索。")
        return outcome

    client = SourceClient(source=SOURCE_NAME)
    query_params = crossref_query(context.scope, context.groups)
    if not query_params or not any(value.strip() for value in query_params.values()):
        outcome.warnings.append("关键词为空，Crossref 未检索。")
        return outcome
    if not get_scope(context.scope).field_scoped_by_source.get(SOURCE_NAME, True):
        outcome.warnings.append(
            "Crossref 不支持严格限定标题/摘要字段，本次按相关度返回；"
            "命中不在所选字段内的记录会在关键词打分阶段被降权或标为已排除。"
        )
    params: dict[str, object] = {
        **query_params,
        "filter": f"from-pub-date:{context.year_from}-01-01,until-pub-date:{context.year_to}-12-31,type:journal-article",
        "rows": min(context.max_results_per_source, 200),
        "select": (
            "DOI,title,container-title,short-container-title,ISSN,author,published-print,"
            "published-online,issued,type,publisher,URL,link,abstract,subject,is-referenced-by-count"
        ),
        "sort": "relevance",
    }
    if context.mailto():
        params["mailto"] = context.mailto()

    try:
        payload = client.json(API_URL, params=params, timeout=40)
    except Exception as exc:  # noqa: BLE001
        outcome.error = f"{type(exc).__name__}: {exc}"
        outcome.warnings.append(f"Crossref 检索失败：{outcome.error}")
        outcome.elapsed_seconds = time.monotonic() - started
        return outcome

    items = ((payload or {}).get("message") or {}).get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        outcome.warnings.append("Crossref 返回结构异常，已跳过。")
        outcome.elapsed_seconds = time.monotonic() - started
        return outcome

    for item in items:
        if isinstance(item, dict):
            record = _to_record(item)
            if record.title:
                outcome.records.append(record)
    outcome.elapsed_seconds = time.monotonic() - started
    return outcome
