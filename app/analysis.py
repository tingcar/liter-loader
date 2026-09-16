"""关键词拆分、多源合并去重、相关性打分、期刊白名单过滤。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable

from .config import AppConfig
from .journal_match import JournalMatcher
from .models import Candidate, make_record_id, normalize_doi, normalize_title
from .query_scope import FIELD_ZH, SOURCE_LABEL_ZH, get_scope  # noqa: F401  (SOURCE_LABEL_ZH 供其它模块转引)
from .sources.base import RawRecord

PRIORITY_HIGH = "high"
PRIORITY_MEDIUM = "medium"
PRIORITY_LOW = "low"

PRIORITY_ZH = {"high": "高", "medium": "中", "low": "低"}


def split_keywords(raw: str) -> list[list[str]]:
    """按 `;` 拆分概念块，块内按 `|` 或 `/` 拆分同义词。

    与 skill 的 `prepare_harvest_run.py::split_groups` 语义保持一致，
    因此既支持 `;` 也兼容原来的 `,` 分隔写法。
    """
    groups: list[list[str]] = []
    if not raw:
        return groups
    normalized = raw.replace("\uff1b", ";").replace("\uff0c", ",")
    outer = normalized.split(";") if ";" in normalized else normalized.split(",")
    for chunk in outer:
        terms = [
            term.strip().strip('"').strip("'")
            for term in chunk.replace("/", "|").split("|")
            if term.strip().strip('"').strip("'")
        ]
        # 同一块内的同义词去重但保留顺序
        seen: set[str] = set()
        unique: list[str] = []
        for term in terms:
            key = term.casefold()
            if key not in seen:
                seen.add(key)
                unique.append(term)
        if unique:
            groups.append(unique)
    return groups


@dataclass
class MergeStats:
    raw_counts: dict[str, int] = field(default_factory=dict)
    duplicate_records: int = 0
    total_before_filter: int = 0
    total_after_filter: int = 0
    unmatched_journals: list[dict[str, Any]] = field(default_factory=list)
    filtered_out_records: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class FilteredResult:
    candidates: list[Candidate] = field(default_factory=list)
    filtered_candidates: list[Candidate] = field(default_factory=list)
    stats: MergeStats = field(default_factory=MergeStats)
    warnings: list[str] = field(default_factory=list)


def merge_records(records: Iterable[RawRecord]) -> tuple[list[dict[str, Any]], int]:
    """把多源记录按 DOI（缺失时按归一化题名）合并。

    返回（合并后的字典列表，重复记录数）。
    """
    buckets: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    duplicates = 0

    for record in records:
        doi = normalize_doi(record.doi)
        key = f"doi:{doi}" if doi else f"title:{normalize_title(record.title)}|{record.year or ''}"
        if key not in buckets:
            bucket: dict[str, Any] = {
                "sources": [],
                "records": [],
                "title": "",
                "authors": "",
                "year": None,
                "journal": "",
                "journal_short": "",
                "issns": [],
                "doi": doi,
                "pmid": "",
                "pmcid": "",
                "abstract": "",
                "article_type": "",
                "publisher": "",
                "landing_page_url": "",
                "article_url": "",
                "pdf_url": "",
                "oa_status": "",
                "license": "",
                "keywords": "",
                "fulltext_links": [],
                "cited_by_count": 0,
            }
            buckets[key] = bucket
            order.append(key)
        else:
            duplicates += 1
        bucket = buckets[key]
        bucket["records"].append(record)
        source = record.source
        if source and source not in bucket["sources"]:
            bucket["sources"].append(source)

        # 按数据源优先级补字段：OpenAlex > Europe PMC > Crossref > PubMed
        if not bucket["title"] and record.title:
            bucket["title"] = record.title
        if not bucket["authors"] and record.authors:
            bucket["authors"] = record.authors
        if not bucket["year"] and record.year:
            bucket["year"] = record.year
        if not bucket["journal"] and record.journal:
            bucket["journal"] = record.journal
        if not bucket["journal_short"] and record.journal_short:
            bucket["journal_short"] = record.journal_short
        for issn in record.issns:
            if issn and issn not in bucket["issns"]:
                bucket["issns"].append(issn)
        if not bucket["doi"] and record.doi:
            bucket["doi"] = normalize_doi(record.doi)
        if not bucket["pmid"] and record.pmid:
            bucket["pmid"] = record.pmid
        if not bucket["pmcid"] and record.pmcid:
            bucket["pmcid"] = record.pmcid
        if len(record.abstract or "") > len(bucket["abstract"] or ""):
            bucket["abstract"] = record.abstract
        if not bucket["article_type"] and record.article_type:
            bucket["article_type"] = record.article_type
        if not bucket["publisher"] and record.publisher:
            bucket["publisher"] = record.publisher
        if not bucket["landing_page_url"] and record.landing_page_url:
            bucket["landing_page_url"] = record.landing_page_url
        if not bucket["article_url"] and record.article_url:
            bucket["article_url"] = record.article_url
        if not bucket["pdf_url"] and record.pdf_url:
            bucket["pdf_url"] = record.pdf_url
        if record.oa_status == "open" or not bucket["oa_status"]:
            if record.oa_status:
                bucket["oa_status"] = record.oa_status
        if not bucket["license"] and record.license:
            bucket["license"] = record.license
        if record.keywords and len(record.keywords) > len(bucket["keywords"]):
            bucket["keywords"] = record.keywords
        for link in record.fulltext_links:
            if link and link not in bucket["fulltext_links"]:
                bucket["fulltext_links"].append(link)
        bucket["cited_by_count"] = max(bucket["cited_by_count"], int(record.cited_by_count or 0))

    for bucket in buckets.values():
        # 标题优先取最完整的一条
        bucket["title"] = (
            max((record.title for record in bucket["records"] if record.title), key=len, default="") or bucket["title"]
        )
        if not bucket["authors"]:
            bucket["authors"] = max((record.authors for record in bucket["records"] if record.authors), key=len, default="")
        if not bucket["abstract"]:
            bucket["abstract"] = max((record.abstract for record in bucket["records"] if record.abstract), key=len, default="")
        if not bucket["keywords"]:
            bucket["keywords"] = max((record.keywords for record in bucket["records"] if record.keywords), key=len, default="")

    return [buckets[key] for key in order], duplicates


DEFAULT_SCORE_FIELDS: tuple[str, ...] = ("title", "abstract", "keywords")
"""打分阶段实际可判断的字段（全文范围没有本地全文，退化为这三个）。"""


def _field_texts(bucket: dict[str, Any]) -> dict[str, str]:
    """把题名/摘要/关键词归一化成小写文本，供按字段匹配使用。"""
    return {
        "title": normalize_title(bucket.get("title")),
        "abstract": normalize_title(bucket.get("abstract")),
        "keywords": normalize_title(bucket.get("keywords")),
    }


def score_bucket(
    bucket: dict[str, Any],
    groups: list[list[str]],
    exclude_terms: list[str],
    fields: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """关键词相关性打分。

    `fields` 是本次检索范围覆盖的可判字段（默认标题/摘要/关键词）。只在这个
    集合内统计命中——否则「仅标题」范围下，Crossref 之类不能限定字段的数据源
    返回的摘要命中会被错算成命中，把范围约束架空了。

    规则参考 skill 的 `rank_candidates.py` 并扩展：
    - 每个概念块命中 ≥1 个同义词 → 命中块数 × 2（上限 8）；
    - 同一块内命中的同义词越多、命中字段越多 → 加分（上限 3）；
    - 有开放获取/直连 PDF 信号 +1；缺摘要 −1；类型词命中 −3。
    """
    all_texts = _field_texts(bucket)
    scope_fields = tuple(fields) if fields else DEFAULT_SCORE_FIELDS
    effective_fields = [name for name in all_texts if name in scope_fields]
    evidence = " ".join(all_texts.get(name, "") for name in effective_fields)
    exclude_hits = [
        term for term in exclude_terms if normalize_title(term) and normalize_title(term) in evidence
    ]

    group_hits: list[str] = []
    term_hits: list[str] = []
    matched_fields: list[str] = []
    for group in groups:
        hit_terms: list[str] = []
        hit_fields: list[str] = []
        for term in group:
            needle = normalize_title(term)
            if not needle:
                continue
            found_in = [name for name in effective_fields if needle in all_texts.get(name, "")]
            if found_in:
                hit_terms.append(term)
                for field_name in found_in:
                    if field_name not in hit_fields:
                        hit_fields.append(field_name)
                    if field_name not in matched_fields:
                        matched_fields.append(field_name)
        if hit_terms:
            group_hits.append(" | ".join(hit_terms))
            term_hits.extend(hit_terms)

    score = 0
    if group_hits:
        score += min(len(group_hits) * 2, 8)
        score += min(len(term_hits), 2)
        score += min(max(len(matched_fields) - 1, 0), 1)
    has_oa = bool(bucket.get("pdf_url")) or str(bucket.get("oa_status") or "").lower().startswith("open")
    if has_oa:
        score += 1
    if str(bucket.get("article_type") or "").casefold() in {"editorial", "commentary", "news", "letter"}:
        score -= 3
    if not bucket.get("abstract") and "abstract" in scope_fields:
        score -= 1
    if exclude_hits and not group_hits:
        score -= 2

    exclusion = ""
    title = str(bucket.get("title") or "")
    if not title:
        exclusion = "missing_title"
    elif not group_hits:
        exclusion = "no_keyword_hit"
    elif exclude_hits and not group_hits:
        exclusion = "excluded_by_title_abstract_terms"

    priority = PRIORITY_HIGH if score >= 6 else (PRIORITY_MEDIUM if score >= 4 else PRIORITY_LOW)
    return {
        "group_hits": group_hits,
        "term_hits": term_hits,
        "field_hits": matched_fields,
        "exclude_hits": exclude_hits,
        "score": score,
        "priority": priority,
        "exclusion": exclusion,
    }


def _bucket_to_candidate(bucket: dict[str, Any], scored: dict[str, Any], matcher: JournalMatcher) -> Candidate:
    doi = normalize_doi(bucket.get("doi"))
    title = str(bucket.get("title") or "")
    year = bucket.get("year")
    match = matcher.match(bucket.get("journal"), bucket.get("issns"))
    entry = match.entry

    candidate_urls = []
    for url in [bucket.get("pdf_url"), bucket.get("landing_page_url"), *(bucket.get("fulltext_links") or [])]:
        if url and str(url).startswith(("http://", "https://")) and url not in candidate_urls:
            candidate_urls.append(str(url))
    if doi:
        doi_url = f"https://doi.org/{doi}"
        if doi_url not in candidate_urls:
            candidate_urls.append(doi_url)

    article_url = (
        bucket.get("landing_page_url")
        or bucket.get("article_url")
        or (f"https://doi.org/{doi}" if doi else "")
    )

    candidate = Candidate(
        record_id=make_record_id(doi, title, year),
        title=title,
        authors=str(bucket.get("authors") or ""),
        year=year if isinstance(year, int) else None,
        journal=str(bucket.get("journal") or ""),
        journal_short=str(bucket.get("journal_short") or "") or (entry.name if entry else ""),
        issn="; ".join(bucket.get("issns") or []),
        doi=doi,
        pmid=str(bucket.get("pmid") or ""),
        pmcid=str(bucket.get("pmcid") or ""),
        source_database="; ".join(bucket.get("sources") or []),
        abstract_if_available=str(bucket.get("abstract") or "")[:1200],
        article_type_guess=str(bucket.get("article_type") or ""),
        keyword_include_hits="; ".join(scored["term_hits"][:8]),
        keyword_group_hits=" || ".join(scored["group_hits"]),
        keyword_field_hits="; ".join(FIELD_ZH.get(field, field) for field in scored["field_hits"]),
        keyword_exclude_hits="; ".join(scored["exclude_hits"]),
        keyword_relevance_score=int(scored["score"]),
        priority=scored["priority"],
        oa_status=str(bucket.get("oa_status") or ""),
        pdf_url_candidate=str(bucket.get("pdf_url") or ""),
        landing_page_url=str(bucket.get("landing_page_url") or ""),
        article_url=str(article_url),
        candidate_urls_json=json.dumps(candidate_urls, ensure_ascii=False),
        download_status="pending",
        exclusion_reason_if_any=scored["exclusion"],
        quality_notes=f"被引 {bucket.get('cited_by_count') or 0}" if bucket.get("cited_by_count") else "",
        impact_factor=entry.impact_factor if entry else "",
        jcr_quartile=entry.jcr_quartile if entry else "",
        metric_year=entry.metric_year if entry else "",
        metric_source=entry.metric_source if entry else "",
        indexing=entry.indexing if entry else "",
        venue_note=entry.note if entry else "",
        matched_by=match.method,
    )
    return candidate


def apply_whitelist(
    buckets: list[dict[str, Any]],
    groups: list[list[str]],
    config: AppConfig,
    matcher: JournalMatcher | None = None,
) -> FilteredResult:
    """打分 + 白名单过滤，产出最终候选列表。

    只有期刊命中 `config.json` 白名单的文献会进入结果页；白名单外的记录
    不落表、不下 PDF，但会把数量和代表性期刊写进 stats/warnings。
    """
    matcher = matcher or JournalMatcher(config.journals, config.journal_match)
    result = FilteredResult()
    result.stats.total_before_filter = len(buckets)
    unmatched_counter: dict[str, int] = {}
    unmatched_examples: dict[str, str] = {}
    # 只按本次检索范围覆盖的字段打分（全文范围退化为标题/摘要/关键词）
    score_fields = tuple(field for field in get_scope(config.search.scope).fields if field in DEFAULT_SCORE_FIELDS)
    if not score_fields:
        score_fields = DEFAULT_SCORE_FIELDS

    for bucket in buckets:
        scored = score_bucket(bucket, groups, config.search.exclude_types, score_fields)
        candidate = _bucket_to_candidate(bucket, scored, matcher)
        candidate.selected = False
        if not candidate.matched_by:
            candidate.exclusion_reason_if_any = "not_in_journal_whitelist"
            candidate.download_status = "excluded"
            result.filtered_candidates.append(candidate)
            label = candidate.journal or "未标注期刊（预印本/仓储记录）"
            unmatched_counter[label] = unmatched_counter.get(label, 0) + 1
            unmatched_examples.setdefault(label, candidate.title)
            continue
        if candidate.exclusion_reason_if_any:
            candidate.download_status = "excluded"
        result.candidates.append(candidate)

    # 高优先级在前，其次按得分与年份
    result.candidates.sort(
        key=lambda item: (
            0 if item.priority == PRIORITY_HIGH else (1 if item.priority == PRIORITY_MEDIUM else 2),
            -item.keyword_relevance_score,
            -(item.year or 0),
            item.title,
        )
    )
    result.stats.total_after_filter = len(result.candidates)
    result.stats.unmatched_journals = [
        {"journal": journal, "count": count, "example": unmatched_examples.get(journal, "")}
        for journal, count in sorted(unmatched_counter.items(), key=lambda kv: (-kv[1], kv[0]))[:30]
    ]
    result.stats.filtered_out_records = [
        {"journal": item.journal, "title": item.title, "doi": item.doi} for item in result.filtered_candidates[:50]
    ]

    if matcher.is_empty:
        result.warnings.append("期刊白名单为空：本次结果全部被过滤。请先编辑 config.json 的 journals 字段。")
    elif result.filtered_candidates:
        top = result.stats.unmatched_journals[:3]
        preview = "、".join(f"{item['journal']}（{item['count']} 条）" for item in top)
        result.warnings.append(
            f"已按期刊白名单过滤掉 {len(result.filtered_candidates)} 条清单外文献。命中较多的清单外期刊：{preview}。"
            "如果其中有关注的期刊，把它加进 config.json 的 journals 后重新检索。"
        )
    return result
