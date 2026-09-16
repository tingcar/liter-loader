"""检索范围（scope）到各数据源字段语法的映射测试（不联网）。

这些断言锁定了四种范围在 OpenAlex / Europe PMC / PubMed / Crossref 上的
实际写法，避免以后改动时把字段前缀写错。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.query_scope import (  # noqa: E402
    DEFAULT_SCOPE,
    crossref_query,
    europepmc_query,
    field_labels,
    get_scope,
    normalize_scope,
    openalex_search,
    pubmed_term,
    scope_catalog,
    unscoped_sources,
)

GROUPS = [["large language model", "LLM"], ["climate"]]


def test_normalize_scope() -> None:
    assert normalize_scope(None) == DEFAULT_SCOPE
    assert normalize_scope("TITLE") == "title"
    assert normalize_scope("tiab") == "title_abstract"
    assert normalize_scope("title-abstract") == "title_abstract"
    assert normalize_scope("unknown-value") == DEFAULT_SCOPE
    assert normalize_scope("all") == "all"


def test_scope_catalog_shape() -> None:
    catalog = scope_catalog()
    assert {item["key"] for item in catalog} == {"title", "title_abstract", "title_abstract_keywords", "all"}
    assert sum(1 for item in catalog if item["default"]) == 1
    default = next(item for item in catalog if item["default"])
    assert default["key"] == DEFAULT_SCOPE


def test_field_labels() -> None:
    assert field_labels(["title", "abstract"]) == "标题 + 摘要"
    assert field_labels(list(get_scope("all").fields)) == "标题 + 摘要 + 关键词 + 全文"


def test_openalex_title_scope() -> None:
    expression = openalex_search("title", GROUPS)
    assert expression.startswith('(title:"large language model" OR title:LLM)')
    assert " AND " in expression
    assert "abstract:" not in expression


def test_openalex_title_abstract_scope() -> None:
    expression = openalex_search("title_abstract", GROUPS)
    assert 'title:"large language model"' in expression
    assert 'abstract:"large language model"' in expression
    assert "keyword:" not in expression


def test_openalex_all_scope_includes_fulltext() -> None:
    expression = openalex_search("all", GROUPS)
    assert "fulltext:" in expression and "keyword:" in expression


def test_europepmc_field_prefixes_and_year_clause() -> None:
    query = europepmc_query("title_abstract_keywords", GROUPS, (2023, 2026))
    assert "TITLE:" in query and "ABSTRACT:" in query and "KW:" in query
    assert "(FIRST_PDATE:[2023-01-01 TO 2026-12-31])" in query


def test_europepmc_all_scope_keeps_unprefixed_fulltext_terms() -> None:
    query = europepmc_query("all", GROUPS, (2023, 2026))
    assert "KW:" in query  # all 范围包含关键词
    assert '"large language model"' in query  # 全文用默认字段，无前缀
    assert "TITLE:" in query and "ABSTRACT:" in query


def test_pubmed_tags() -> None:
    assert '[ti]' in pubmed_term("title", GROUPS)
    term = pubmed_term("title_abstract", GROUPS)
    assert '[ti]' in term and '[tiab]' in term
    assert '[ot]' in pubmed_term("all", GROUPS) or '[tw]' in pubmed_term("all", GROUPS)
    assert '("2023/01/01"[dp]' in pubmed_term("title", GROUPS, (2023, 2026))


def test_pubmed_keyword_scope_uses_other_term_tag() -> None:
    term = pubmed_term("title_abstract_keywords", GROUPS)
    assert '[ot]' in term


def test_crossref_query_per_scope() -> None:
    assert "query.title" in crossref_query("title", GROUPS)
    assert "query.bibliographic" in crossref_query("title_abstract", GROUPS)
    assert "query.bibliographic" in crossref_query("title_abstract_keywords", GROUPS)
    assert "query" in crossref_query("all", GROUPS)


def test_crossref_is_not_field_scoped() -> None:
    assert get_scope("title").field_scoped_by_source["crossref"] is False
    assert get_scope("all").field_scoped_by_source["crossref"] is True
    assert unscoped_sources("title", ["openalex", "crossref", "pubmed"]) == ["crossref"]
    assert unscoped_sources("all", ["openalex", "crossref", "pubmed"]) == []


def test_empty_groups_produce_empty_queries() -> None:
    assert openalex_search("title_abstract", []) == ""
    assert crossref_query("title", [])["query.title"] == ""
    assert pubmed_term("title", [], (2021, 2026)).endswith('[dp] : "2026/12/31"[dp])')


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
