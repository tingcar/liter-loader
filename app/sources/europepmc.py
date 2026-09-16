"""Europe PMC 数据源（REST API）。

用 `resultType=core` 拿到 PMCID、开放获取标记、摘要，以及最重要的
`fullTextUrlList`（里面含合法的 OA PDF / 全文页面地址）。

注意：本机网络无法访问 europepmc.org 网页域和 pmc.ncbi.nlm.nih.gov（403），
因此这里只使用 `www.ebi.ac.uk` 的 REST 接口。
"""

from __future__ import annotations

import time

from .base import RawRecord, SearchContext, SearchOutcome, SourceClient, excerpt, merge_authors

API_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
SOURCE_NAME = "europepmc"


def build_query(context: SearchContext) -> str:
    """Europe PMC 查询串（字段范围由 `context.scope` 决定）。"""
    return context.query_for(SOURCE_NAME)


def _keywords_of(result: dict) -> str:
    """Europe PMC 的作者关键词（keywordList.keyword[].term）。"""
    keyword_list = result.get("keywordList") or {}
    entries = keyword_list.get("keyword") if isinstance(keyword_list, dict) else None
    names: list[str] = []
    if isinstance(entries, list):
        for entry in entries:
            if isinstance(entry, dict):
                term = str(entry.get("term") or entry.get("keyword") or "").strip()
                if term:
                    names.append(term)
            elif entry:
                names.append(str(entry).strip())
    return "; ".join(names[:12])


def _fulltext_links(result: dict) -> tuple[list[str], str]:
    """返回（全部 OA 全文链接，优先 PDF 链接）。"""
    links: list[str] = []
    pdf_url = ""
    url_list = result.get("fullTextUrlList") or {}
    entries = url_list.get("fullTextUrl") if isinstance(url_list, dict) else None
    if not isinstance(entries, list):
        return links, pdf_url
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        url = str(entry.get("url") or "").strip()
        if not url or url in links:
            continue
        if str(entry.get("availabilityCode") or "").upper() != "OA":
            continue
        if str(entry.get("site") or "") == "DOI":
            continue
        links.append(url)
        style = str(entry.get("documentStyle") or "").lower()
        if not pdf_url and (style == "pdf" or "pdf" in url.lower()):
            pdf_url = url
    return links, pdf_url


def _to_record(result: dict) -> RawRecord:
    journal_info = result.get("journalInfo") or {}
    journal_block = journal_info.get("journal") if isinstance(journal_info, dict) else {}
    journal = str((journal_block or {}).get("title") or "").strip()
    short = str((journal_block or {}).get("medlineAbbreviation") or "").strip()
    issns = []
    for key in ("issn", "essn"):
        value = (journal_block or {}).get(key)
        if value:
            issns.append(str(value))
    author_list = result.get("authorList") or {}
    authors = []
    if isinstance(author_list, dict):
        for author in author_list.get("author") or []:
            if isinstance(author, dict) and author.get("fullName"):
                authors.append(str(author["fullName"]))
    if not authors and result.get("authorString"):
        authors = [part.strip() for part in str(result["authorString"]).split(",") if part.strip()]
    links, pdf_url = _fulltext_links(result)
    pmcid = str(result.get("pmcid") or "")
    landing = f"https://europepmc.org/article/MED/{result.get('pmid')}" if result.get("pmid") else ""
    in_epmc = str(result.get("inEPMC") or "").upper() == "Y"
    is_oa = str(result.get("isOpenAccess") or "").upper() == "Y"
    return RawRecord(
        source=SOURCE_NAME,
        title=str(result.get("title") or "").strip().rstrip("."),
        authors=merge_authors(authors),
        year=int(result["pubYear"]) if str(result.get("pubYear") or "").isdigit() else None,
        journal=journal,
        journal_short=short,
        issns=issns,
        doi=str(result.get("doi") or ""),
        pmid=str(result.get("pmid") or ""),
        pmcid=pmcid,
        abstract=excerpt(result.get("abstractText"), 1200),
        article_type=str(result.get("pubType") or ""),
        landing_page_url=landing,
        article_url=f"https://doi.org/{result.get('doi')}" if result.get("doi") else landing,
        pdf_url=pdf_url,
        oa_status="open" if (is_oa or in_epmc) else "closed",
        license=str(result.get("license") or ""),
        keywords=_keywords_of(result),
        fulltext_links=links,
        cited_by_count=int(result.get("citedByCount") or 0),
    )


def search(context: SearchContext) -> SearchOutcome:
    started = time.monotonic()
    outcome = SearchOutcome(source=SOURCE_NAME)
    if not context.groups:
        outcome.warnings.append("关键词为空，Europe PMC 未检索。")
        return outcome

    client = SourceClient(source=SOURCE_NAME)
    query = build_query(context)
    if not query:
        outcome.warnings.append("关键词为空，Europe PMC 未检索。")
        return outcome
    params: dict[str, object] = {
        "query": query,
        "format": "json",
        "resultType": "core",
        "pageSize": min(context.max_results_per_source, 1000),
        "sort": "CITED desc",
    }
    if context.mailto():
        params["email"] = context.mailto()

    try:
        payload = client.json(API_URL, params=params, timeout=40)
    except Exception as exc:  # noqa: BLE001
        outcome.error = f"{type(exc).__name__}: {exc}"
        outcome.warnings.append(f"Europe PMC 检索失败：{outcome.error}")
        outcome.elapsed_seconds = time.monotonic() - started
        return outcome

    results = ((payload or {}).get("resultList") or {}).get("result") if isinstance(payload, dict) else None
    if not isinstance(results, list):
        outcome.warnings.append("Europe PMC 返回结构异常，已跳过。")
        outcome.elapsed_seconds = time.monotonic() - started
        return outcome

    for result in results:
        if isinstance(result, dict):
            record = _to_record(result)
            if record.title:
                outcome.records.append(record)
    outcome.elapsed_seconds = time.monotonic() - started
    return outcome
