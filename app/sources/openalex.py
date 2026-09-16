"""OpenAlex 数据源（https://api.openalex.org/works）。

OpenAlex 是主力数据源：题录字段最全（期刊名、ISSN、年份、作者、摘要倒排索引、
开放获取状态与 OA PDF 直链）。
"""

from __future__ import annotations

import time

from .base import RawRecord, SearchContext, SearchOutcome, SourceClient, excerpt, merge_authors

API_URL = "https://api.openalex.org/works"
SOURCE_NAME = "openalex"

SELECT_FIELDS = ",".join(
    [
        "id",
        "doi",
        "title",
        "display_name",
        "publication_year",
        "publication_date",
        "type",
        "type_crossref",
        "authorships",
        "primary_location",
        "best_oa_location",
        "locations",
        "open_access",
        "abstract_inverted_index",
        "keywords",
        "cited_by_count",
        "ids",
        "language",
    ]
)


def _keywords_of(work: dict) -> str:
    """OpenAlex 的 keywords 字段（算法抽取的主题词）。"""
    names: list[str] = []
    for keyword in work.get("keywords") or []:
        if isinstance(keyword, dict):
            name = str(keyword.get("display_name") or "").strip()
            if name:
                names.append(name)
    return "; ".join(names[:12])


def _abstract_from_inverted_index(index: object, limit: int = 800) -> str:
    """OpenAlex 的摘要是倒排索引，这里还原成文本。"""
    if not isinstance(index, dict):
        return ""
    positions: list[tuple[int, str]] = []
    for word, spots in index.items():
        if not isinstance(spots, list):
            continue
        for spot in spots:
            if isinstance(spot, int):
                positions.append((spot, str(word)))
    if not positions:
        return ""
    positions.sort(key=lambda item: item[0])
    return excerpt(" ".join(word for _spot, word in positions[:limit]), 1200)


def _journal_fields(work: dict) -> tuple[str, str, list[str]]:
    journal = ""
    journal_short = ""
    issns: list[str] = []
    for key in ("primary_location", "best_oa_location", "host_venue"):
        location = work.get(key) or {}
        if not isinstance(location, dict):
            continue
        source = location.get("source") or {}
        if not isinstance(source, dict):
            continue
        display_name = str(source.get("display_name") or "").strip()
        if display_name and not journal:
            journal = display_name
        short = str(source.get("abbreviated_title") or "").strip()
        if short and not journal_short:
            journal_short = short
        for issn in [source.get("issn_l"), *(source.get("issn") or [])]:
            if issn and str(issn) not in issns:
                issns.append(str(issn))
        if journal:
            break
    return journal, journal_short, issns


def _oa_pdf_url(work: dict) -> str:
    best = work.get("best_oa_location") or {}
    if isinstance(best, dict):
        for key in ("pdf_url", "landing_page_url"):
            value = str(best.get(key) or "").strip()
            if value:
                return value
    return ""


def _fulltext_links(work: dict) -> list[str]:
    links: list[str] = []
    for key in ("best_oa_location", "primary_location"):
        location = work.get(key) or {}
        if not isinstance(location, dict):
            continue
        for field_name in ("pdf_url", "landing_page_url"):
            value = str(location.get(field_name) or "").strip()
            if value and value not in links:
                links.append(value)
    return links


def _to_record(work: dict) -> RawRecord:
    journal, journal_short, issns = _journal_fields(work)
    open_access = work.get("open_access") or {}
    is_oa = bool(open_access.get("is_oa")) if isinstance(open_access, dict) else False
    oa_status = str(open_access.get("oa_status") or "") if isinstance(open_access, dict) else ""
    authors = []
    for authorship in work.get("authorships") or []:
        if not isinstance(authorship, dict):
            continue
        author = authorship.get("author") or {}
        if isinstance(author, dict) and author.get("display_name"):
            authors.append(str(author["display_name"]))
    ids = work.get("ids") or {}
    primary = work.get("primary_location") or {}
    landing = str(primary.get("landing_page_url") or "") if isinstance(primary, dict) else ""
    return RawRecord(
        source=SOURCE_NAME,
        title=str(work.get("display_name") or work.get("title") or "").strip(),
        authors=merge_authors(authors),
        year=work.get("publication_year") if isinstance(work.get("publication_year"), int) else None,
        journal=journal,
        journal_short=journal_short,
        issns=issns,
        doi=str(work.get("doi") or ""),
        pmid=str(ids.get("pmid") or "").rsplit("/", 1)[-1] if ids.get("pmid") else "",
        pmcid=str(ids.get("pmcid") or "").rsplit("/", 1)[-1] if ids.get("pmcid") else "",
        abstract=_abstract_from_inverted_index(work.get("abstract_inverted_index")),
        article_type=str(work.get("type_crossref") or work.get("type") or ""),
        landing_page_url=landing,
        article_url=str(work.get("id") or ""),
        pdf_url=_oa_pdf_url(work),
        oa_status=("open" if is_oa else oa_status or "closed"),
        keywords=_keywords_of(work),
        fulltext_links=_fulltext_links(work),
        cited_by_count=int(work.get("cited_by_count") or 0),
    )


def search(context: SearchContext) -> SearchOutcome:
    started = time.monotonic()
    outcome = SearchOutcome(source=SOURCE_NAME)
    if not context.groups:
        outcome.warnings.append("关键词为空，OpenAlex 未检索。")
        return outcome

    client = SourceClient(source=SOURCE_NAME)
    search_expression = context.query_for(SOURCE_NAME)
    if not search_expression:
        outcome.warnings.append("关键词为空，OpenAlex 未检索。")
        return outcome
    params: dict[str, object] = {
        "search": search_expression,
        "filter": (
            f"from_publication_date:{context.year_from}-01-01,"
            f"to_publication_date:{context.year_to}-12-31,"
            "type:article|review"
        ),
        "per-page": min(context.max_results_per_source, 200),
        "select": SELECT_FIELDS,
    }
    if context.mailto():
        params["mailto"] = context.mailto()

    try:
        payload = client.json(API_URL, params=params, timeout=40)
    except Exception as exc:  # noqa: BLE001
        outcome.error = f"{type(exc).__name__}: {exc}"
        outcome.warnings.append(f"OpenAlex 检索失败：{outcome.error}")
        outcome.elapsed_seconds = time.monotonic() - started
        return outcome

    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list):
        outcome.warnings.append("OpenAlex 返回结构异常，已跳过。")
        outcome.elapsed_seconds = time.monotonic() - started
        return outcome

    for work in results:
        if isinstance(work, dict):
            record = _to_record(work)
            if record.title:
                outcome.records.append(record)
    outcome.elapsed_seconds = time.monotonic() - started
    return outcome
