"""PubMed 数据源（NCBI E-utilities）。

esearch 取 PMID 列表 → efetch(xml) 取题录。DOI 从 `ArticleIdList` 中取，
PMCID 也从那里取（本机无法访问 utils/idconv，所以不调用它）。
"""

from __future__ import annotations

import time
import xml.etree.ElementTree as ElementTree

from .base import RawRecord, SearchContext, SearchOutcome, SourceClient, excerpt, merge_authors

ESEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
EFETCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
SOURCE_NAME = "pubmed"


def build_term(context: SearchContext) -> str:
    """PubMed 检索式（字段标签由 `context.scope` 决定）。"""
    return context.query_for(SOURCE_NAME)


def _keywords(article: ElementTree.Element) -> str:
    """PubMed 的 KeywordList（作者关键词 + MeSH 之外的主题词）。"""
    names: list[str] = []
    for node in article.findall("./MedlineCitation/KeywordList/Keyword"):
        value = _text(node).strip()
        if value:
            names.append(value)
    return "; ".join(names[:12])



def _text(node: ElementTree.Element | None) -> str:
    if node is None:
        return ""
    parts: list[str] = []

    def walk(element: ElementTree.Element) -> None:
        if element.text:
            parts.append(element.text)
        for child in element:
            walk(child)
            if child.tail:
                parts.append(child.tail)

    walk(node)
    return " ".join(" ".join(parts).split())


def _abstract(article: ElementTree.Element) -> str:
    chunks: list[str] = []
    for node in article.findall(".//Abstract/AbstractText"):
        label = node.attrib.get("Label")
        body = _text(node)
        if body:
            chunks.append(f"{label}: {body}" if label else body)
    return excerpt(" ".join(chunks), 1200)


def _authors(article: ElementTree.Element) -> str:
    names: list[str] = []
    for author in article.findall(".//AuthorList/Author"):
        collective = author.findtext("CollectiveName")
        if collective:
            names.append(collective.strip())
            continue
        last = (author.findtext("LastName") or "").strip()
        fore = (author.findtext("ForeName") or author.findtext("Initials") or "").strip()
        name = " ".join(part for part in [fore, last] if part).strip()
        if name:
            names.append(name)
    return merge_authors(names)


def _year(article: ElementTree.Element) -> int | None:
    for path in (".//Journal/JournalIssue/PubDate/Year", ".//ArticleDate/Year", ".//PubMedPubDate/Year"):
        value = article.findtext(path)
        if value and value.strip().isdigit():
            return int(value.strip())
    medline_date = article.findtext(".//Journal/JournalIssue/PubDate/MedlineDate")
    if medline_date:
        token = medline_date.strip()[:4]
        if token.isdigit():
            return int(token)
    return None


def _records_from_xml(xml_text: str, limit: int) -> list[RawRecord]:
    root = ElementTree.fromstring(xml_text)
    records: list[RawRecord] = []
    for article in root.findall(".//PubmedArticle")[:limit]:
        title = _text(article.find(".//Article/ArticleTitle"))
        journal = (article.findtext(".//Journal/Title") or "").strip()
        short = (article.findtext(".//Journal/ISOAbbreviation") or "").strip()
        issn = (article.findtext(".//Journal/ISSN") or "").strip()
        volume = (article.findtext(".//Journal/JournalIssue/Volume") or "").strip()
        issue = (article.findtext(".//Journal/JournalIssue/Issue") or "").strip()
        pages = (article.findtext(".//Pagination/MedlinePgn") or article.findtext(".//Pagination/StartPage") or "").strip()
        pii = (article.findtext(".//Article/ELocationID[@EIdType='pii']") or "").strip()
        doi = ""
        pmcid = ""
        # 注意：不能写成 .//ArticleIdList/ArticleId —— 参考文献里也嵌着
        # ArticleIdList/ArticleId，那样会把别人文献的 DOI/PMCID 抓进来。
        top_ids = article.find("./PubmedData/ArticleIdList")
        if top_ids is None:
            citation = article.find("./MedlineCitation")
            top_ids = citation.find("./ArticleIdList") if citation is not None else None
        if top_ids is not None:
            for article_id in top_ids.findall("./ArticleId"):
                id_type = str(article_id.attrib.get("IdType") or "").lower()
                value = (article_id.text or "").strip()
                if id_type == "doi" and not doi:
                    doi = value
                elif id_type == "pmc" and not pmcid:
                    pmcid = value
        pmid = (article.findtext("MedlineCitation/PMID") or "").strip()
        if not doi:
            for elocation in article.findall(".//Article/ELocationID"):
                if str(elocation.attrib.get("EIdType") or "").lower() == "doi":
                    doi = (elocation.text or "").strip()
        article_url = f"https://doi.org/{doi}" if doi else (f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else "")
        records.append(
            RawRecord(
                source=SOURCE_NAME,
                title=title,
                authors=_authors(article),
                year=_year(article),
                journal=journal,
                journal_short=short,
                issns=[issn] if issn else [],
                doi=doi,
                pmid=pmid,
                pmcid=pmcid,
                abstract=_abstract(article),
                article_type="journal-article",
                landing_page_url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else "",
                article_url=article_url,
                oa_status="",
                keywords=_keywords(article),
                fulltext_links=[],
            )
        )
        if pages or volume or issue:
            records[-1].publisher = " ".join(part for part in [volume, issue, pages] if part)
    return records


def search(context: SearchContext) -> SearchOutcome:
    started = time.monotonic()
    outcome = SearchOutcome(source=SOURCE_NAME)
    if not context.groups:
        outcome.warnings.append("关键词为空，PubMed 未检索。")
        return outcome

    client = SourceClient(source=SOURCE_NAME)
    limit = min(context.max_results_per_source, 200)
    term = build_term(context)
    if not term:
        outcome.warnings.append("关键词为空，PubMed 未检索。")
        return outcome
    common: dict[str, object] = {"db": "pubmed", "tool": "lit-loader"}
    if context.mailto():
        common["email"] = context.mailto()
    api_key = context.search.api_keys.get("pubmed")
    if api_key:
        common["api_key"] = api_key

    try:
        payload = client.json(
            ESEARCH_URL,
            params={**common, "term": term, "retmode": "json", "retmax": limit, "sort": "relevance"},
            timeout=40,
        )
    except Exception as exc:  # noqa: BLE001
        outcome.error = f"{type(exc).__name__}: {exc}"
        outcome.warnings.append(f"PubMed esearch 失败：{outcome.error}")
        outcome.elapsed_seconds = time.monotonic() - started
        return outcome

    ids = ((payload or {}).get("esearchresult") or {}).get("idlist") if isinstance(payload, dict) else None
    if not isinstance(ids, list) or not ids:
        outcome.elapsed_seconds = time.monotonic() - started
        return outcome

    try:
        xml_text = client.text(EFETCH_URL, params={**common, "id": ",".join(str(item) for item in ids), "retmode": "xml"}, timeout=60)
        outcome.records = _records_from_xml(xml_text, limit)
    except Exception as exc:  # noqa: BLE001
        outcome.error = f"{type(exc).__name__}: {exc}"
        outcome.warnings.append(f"PubMed efetch 失败：{outcome.error}")
        outcome.elapsed_seconds = time.monotonic() - started
        return outcome

    outcome.elapsed_seconds = time.monotonic() - started
    return outcome
