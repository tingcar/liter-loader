"""关键词拆分、合并去重、打分与白名单过滤的单元测试。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.analysis import apply_whitelist, merge_records, score_bucket, split_keywords  # noqa: E402
from app.config import AppConfig, JournalMatchConfig, SearchConfig  # noqa: E402
from app.journal_match import JournalMatcher  # noqa: E402
from app.models import JournalEntry  # noqa: E402
from app.sources.base import RawRecord  # noqa: E402


def build_config() -> AppConfig:
    return AppConfig(
        search=SearchConfig(year_from=2021, year_to=2026, exclude_types=["editorial", "commentary"]),
        journal_match=JournalMatchConfig(),
        journals=[JournalEntry(name="Scientific Reports", issn=["2045-2322"])],
    )


def test_split_keywords_semicolon() -> None:
    groups = split_keywords("large language model|LLM|ChatGPT; environmental science|climate")
    assert groups == [["large language model", "LLM", "ChatGPT"], ["environmental science", "climate"]]


def test_split_keywords_fullwidth_and_comma() -> None:
    assert split_keywords("LLM；climate") == [["LLM"], ["climate"]]
    assert split_keywords("LLM, climate") == [["LLM"], ["climate"]]


def test_split_keywords_dedup_within_group() -> None:
    assert split_keywords("LLM|llm|ChatGPT") == [["LLM", "ChatGPT"]]


def test_merge_by_doi_and_title() -> None:
    records = [
        RawRecord(source="openalex", title="A study of LLMs", doi="10.1/ABC", year=2023, journal="Scientific Reports", oa_status="open", pdf_url="https://x/pdf"),
        RawRecord(source="crossref", title="A study of LLMs", doi="https://doi.org/10.1/abc", year=2023, issns=["2045-2322"]),
        RawRecord(source="pubmed", title="A different study", year=2022, journal="Scientific Reports"),
    ]
    buckets, duplicates = merge_records(records)
    assert duplicates == 1
    assert len(buckets) == 2
    first = buckets[0]
    assert first["doi"] == "10.1/abc"
    assert sorted(first["sources"]) == ["crossref", "openalex"]
    assert first["issns"] == ["2045-2322"]
    assert first["pdf_url"] == "https://x/pdf"


def test_merge_carries_keywords() -> None:
    records = [
        RawRecord(source="openalex", title="T", doi="10.1/k", keywords="LLM; climate"),
        RawRecord(source="europepmc", title="T", doi="10.1/k", keywords="short"),
    ]
    buckets, _ = merge_records(records)
    assert buckets[0]["keywords"] == "LLM; climate"


def test_merge_prefers_longest_abstract() -> None:
    records = [
        RawRecord(source="crossref", title="T", doi="10.1/x", abstract="short"),
        RawRecord(source="europepmc", title="T", doi="10.1/x", abstract="a much longer abstract text"),
    ]
    buckets, _ = merge_records(records)
    assert buckets[0]["abstract"] == "a much longer abstract text"


def test_score_bucket_group_hits() -> None:
    groups = [["LLM", "ChatGPT"], ["climate"]]
    bucket = {"title": "LLM for climate modelling", "abstract": "we use ChatGPT", "pdf_url": "https://x"}
    scored = score_bucket(bucket, groups, ["editorial"])
    assert len(scored["group_hits"]) == 2
    assert scored["field_hits"] == ["title", "abstract"]
    assert scored["score"] >= 6
    assert scored["priority"] == "high"
    assert scored["exclusion"] == ""


def test_score_bucket_single_group_is_low_priority() -> None:
    """只命中一个概念块 → 低优先级，避免单块噪声混进高优先级。"""
    groups = [["LLM"], ["climate"]]
    bucket = {"title": "LLM for medicine", "abstract": "text"}
    scored = score_bucket(bucket, groups, [])
    assert scored["score"] < 4
    assert scored["priority"] == "low"


def test_score_bucket_keyword_field_scope() -> None:
    """关键词只在作者关键词里命中时，应记录命中的字段。"""
    groups = [["LLM"]]
    bucket = {"title": "A study", "abstract": "unrelated", "keywords": "LLM; climate"}
    scored = score_bucket(bucket, groups, [])
    assert scored["group_hits"] == ["LLM"]
    assert scored["field_hits"] == ["keywords"]


def test_score_bucket_partial_term_match() -> None:
    """只命中一个同义词也要算命中该概念块。"""
    groups = [["large language model", "LLM"], ["climate"]]
    bucket = {"title": "Climate modelling with LLM", "abstract": "x"}
    scored = score_bucket(bucket, groups, [])
    assert len(scored["group_hits"]) == 2
    assert scored["field_hits"] == ["title"]


def test_score_bucket_exclusion() -> None:
    groups = [["LLM"]]
    bucket = {"title": "Editorial on something else", "abstract": "", "article_type": "editorial"}
    scored = score_bucket(bucket, groups, ["editorial"])
    assert scored["exclusion"] == "no_keyword_hit"
    assert scored["priority"] == "low"


def test_apply_whitelist_filters_and_reports() -> None:
    config = build_config()
    matcher = JournalMatcher(config.journals, config.journal_match)
    records = [
        RawRecord(source="openalex", title="LLM for climate", doi="10.1/a", year=2024, journal="Scientific Reports", issns=["2045-2322"], abstract="x", pdf_url="https://x.pdf"),
        RawRecord(source="openalex", title="LLM for climate", doi="10.1/b", year=2024, journal="Nature Medicine", issns=["1078-8956"], abstract="x"),
    ]
    buckets, _ = merge_records(records)
    result = apply_whitelist(buckets, [["LLM"], ["climate"]], config, matcher)
    assert len(result.candidates) == 1
    assert len(result.filtered_candidates) == 1
    assert result.filtered_candidates[0].exclusion_reason_if_any == "not_in_journal_whitelist"
    assert result.candidates[0].matched_by == "issn"
    assert result.candidates[0].keyword_field_hits == "标题"
    assert result.stats.total_before_filter == 2
    assert result.stats.total_after_filter == 1
    assert result.stats.unmatched_journals[0]["journal"] == "Nature Medicine"
    assert result.warnings, "白名单过滤后应有提示"


def test_apply_whitelist_excluded_candidate_kept_in_list() -> None:
    """关键词完全不命中的记录会保留在候选表里并标为已排除，方便人工复查。"""
    config = build_config()
    matcher = JournalMatcher(config.journals, config.journal_match)
    records = [
        RawRecord(source="openalex", title="Unrelated topic", doi="10.1/c", year=2024, journal="Scientific Reports", issns=["2045-2322"]),
    ]
    buckets, _ = merge_records(records)
    result = apply_whitelist(buckets, [["LLM"]], config, matcher)
    assert len(result.candidates) == 1
    assert result.candidates[0].download_status == "excluded"
    assert result.candidates[0].exclusion_reason_if_any == "no_keyword_hit"


def test_empty_whitelist_warns() -> None:
    config = build_config()
    config.journals = []
    matcher = JournalMatcher([], config.journal_match)
    records = [RawRecord(source="openalex", title="LLM", doi="10.1/d", year=2024, journal="Scientific Reports")]
    buckets, _ = merge_records(records)
    result = apply_whitelist(buckets, [["LLM"]], config, matcher)
    assert not result.candidates
    assert any("白名单为空" in warning for warning in result.warnings)


if __name__ == "__main__":
    import traceback

    failures = 0
    for name, func in sorted(globals().items()):
        if name.startswith("test_") and callable(func):
            try:
                func()
                print(f"PASS {name}")
            except Exception:  # noqa: BLE001
                failures += 1
                print(f"FAIL {name}")
                traceback.print_exc()
    print(f"\n{'OK' if failures == 0 else f'{failures} failed'}")
    raise SystemExit(1 if failures else 0)
