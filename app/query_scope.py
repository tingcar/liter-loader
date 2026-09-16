"""检索范围（search scope）：把「关键词匹配论文的哪些字段」翻译成各数据源的写法。

支持四种范围：

- `title`：只匹配标题
- `title_abstract`：匹配标题 + 摘要（默认）
- `title_abstract_keywords`：匹配标题 + 摘要 + 关键词
- `all`：匹配标题 + 摘要 + 关键词 + 全文（数据源支持时）

各数据源的字段语法（已实测）：

| 数据源 | 标题 | 摘要 | 关键词 | 组合方式 |
| --- | --- | --- | --- | --- |
| OpenAlex | `title:` | `abstract:` | `keyword:` | `search=` 里用 `AND`/`OR` 嵌套括号 |
| Europe PMC | `TITLE:` | `ABSTRACT:` | `KW:` | 查询串里嵌套括号 |
| PubMed | `[ti]` | `[tiab]` | `[ot]` | 逐词加字段标签后括号组合 |
| Scopus | `TITLE()` | `ABS()` | `AUTHKEY()` | 字段函数 + `AND`/`OR` 括号，年份用 `PUBYEAR` |
| Web of Science | `TI=` | `AB=` | `AK=` | `TS=`/`ALL=` 与布尔括号，年份用 `publishTimeSpan` |
| Crossref | — | — | — | 只支持相关度匹配（`query.title`/`query.bibliographic`/`query`），**不能限定字段** |

Crossref 因此被标记为 `field_scoped=False`：当用户选择了较窄的范围时，前端会
提示「Crossref 只能按相关度返回，可能出现命中不在所选字段内的记录」，并且这些
记录在我们自己的关键词打分阶段仍会被检查。

Scopus 与 Web of Science 都**需要机构订阅的 API key**：没有 key 时它们会直接
返回明确提示，不参与检索，其余数据源照常工作。

Scopus 没有独立的「全文」字段，`all` 范围退化为 `ALL()`（整条记录，含参考文献）；
Web of Science 的 `ALL=` 同样是全记录字段。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ScopeDefinition:
    key: str
    label: str
    description: str
    fields: tuple[str, ...]
    # 数据源标记：True 表示该源支持严格限定字段；False 表示只能相关度匹配
    field_scoped_by_source: dict[str, bool]
    default: bool = False


SCOPES: dict[str, ScopeDefinition] = {
    "title": ScopeDefinition(
        key="title",
        label="仅标题",
        description="关键词必须出现在论文标题里，最精确但结果最少",
        fields=("title",),
        field_scoped_by_source={
            "openalex": True,
            "europepmc": True,
            "pubmed": True,
            "scopus": True,
            "wos": True,
            "crossref": False,
        },
    ),
    "title_abstract": ScopeDefinition(
        key="title_abstract",
        label="标题 + 摘要",
        description="关键词出现在标题或摘要里（默认，兼顾精确与召回）",
        fields=("title", "abstract"),
        field_scoped_by_source={
            "openalex": True,
            "europepmc": True,
            "pubmed": True,
            "scopus": True,
            "wos": True,
            "crossref": False,
        },
        default=True,
    ),
    "title_abstract_keywords": ScopeDefinition(
        key="title_abstract_keywords",
        label="标题 + 摘要 + 关键词",
        description="再加上作者关键词，适合抓主题词写得多的论文",
        fields=("title", "abstract", "keywords"),
        field_scoped_by_source={
            "openalex": True,
            "europepmc": True,
            "pubmed": True,
            "scopus": True,
            "wos": True,
            "crossref": False,
        },
    ),
    "all": ScopeDefinition(
        key="all",
        label="全部字段（含全文）",
        description="标题、摘要、关键词以及数据源可提供的全文，召回最多但噪声也最多",
        fields=("title", "abstract", "keywords", "fulltext"),
        field_scoped_by_source={
            "openalex": True,
            "europepmc": True,
            "pubmed": True,
            "scopus": True,
            "wos": True,
            "crossref": True,
        },
    ),
}

DEFAULT_SCOPE = "title_abstract"

FIELD_ZH: dict[str, str] = {
    "title": "标题",
    "abstract": "摘要",
    "keywords": "关键词",
    "fulltext": "全文",
}

SOURCE_LABEL_ZH: dict[str, str] = {
    "openalex": "OpenAlex",
    "crossref": "Crossref",
    "europepmc": "Europe PMC",
    "pubmed": "PubMed",
    "scopus": "Scopus",
    "wos": "Web of Science",
}

# 数据源元信息：是否需要 API key、去哪申请、可用性说明。前端据此做提示与置灰。
SOURCE_CATALOG: dict[str, dict[str, object]] = {
    "openalex": {
        "label": "OpenAlex",
        "requires_key": False,
        "free": True,
        "note": "完全开放，无需账号",
    },
    "crossref": {
        "label": "Crossref",
        "requires_key": False,
        "free": True,
        "note": "完全开放；填写邮箱可进入更稳的 polite pool",
    },
    "europepmc": {
        "label": "Europe PMC",
        "requires_key": False,
        "free": True,
        "note": "完全开放",
    },
    "pubmed": {
        "label": "PubMed",
        "requires_key": False,
        "free": True,
        "note": "开放；可选填 NCBI api_key 提高限流额度",
        "optional_key": "pubmed",
    },
    "scopus": {
        "label": "Scopus",
        "requires_key": True,
        "optional": True,
        "free": False,
        "key_field": "scopus",
        "key_label": "Elsevier Scopus API key（X-ELS-APIKey）",
        "how_to": "可选渠道：需要 Elsevier 开发者 API key，且所在机构有 Scopus 订阅。不填也能用其它渠道。",
    },
    "wos": {
        "label": "Web of Science",
        "requires_key": True,
        "optional": True,
        "free": False,
        "key_field": "wos",
        "key_label": "Clarivate API key（X-ApiKey）",
        "how_to": "可选渠道：需要 Clarivate 开发者门户（WoS Starter 计划）的 API key。不填也能用其它渠道。",
    },
}

SOURCE_ORDER: list[str] = ["openalex", "crossref", "europepmc", "pubmed", "scopus", "wos"]


def source_catalog(api_keys: dict[str, str] | None = None) -> list[dict[str, object]]:
    """给前端的检索渠道清单（含是否需要 key、是否可选、当前是否已配置）。"""
    keys = api_keys or {}
    catalog: list[dict[str, object]] = []
    for name in SOURCE_ORDER:
        entry = dict(SOURCE_CATALOG[name])
        requires_key = bool(entry.get("requires_key"))
        optional = bool(entry.get("optional"))
        key_field = str(entry.get("key_field") or name)
        has_key = bool(str(keys.get(key_field) or "").strip())
        entry.update(
            {
                "name": name,
                "has_key": has_key,
                # 需要 key 且没配 → 用不了；可选渠道缺 key 只是「暂时跳过」，不算错误
                "available": True if not requires_key else has_key,
                "key_configured": has_key,
                "optional": optional or not requires_key,
            }
        )
        catalog.append(entry)
    return catalog


def normalize_scope(value: object) -> str:
    """把用户/前端传进来的范围值收敛到合法取值。"""
    key = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if key in SCOPES:
        return key
    aliases = {
        "ti": "title",
        "tab": "title_abstract",
        "tiab": "title_abstract",
        "abstract": "title_abstract",
        "title_abstract_keyword": "title_abstract_keywords",
        "keyword": "title_abstract_keywords",
        "full": "all",
        "default": DEFAULT_SCOPE,
    }
    return aliases.get(key, DEFAULT_SCOPE)


def get_scope(value: object) -> ScopeDefinition:
    return SCOPES[normalize_scope(value)]


def scope_catalog() -> list[dict[str, Any]]:
    """给前端用的范围清单。"""
    return [
        {
            "key": definition.key,
            "label": definition.label,
            "description": definition.description,
            "fields": [FIELD_ZH.get(field, field) for field in definition.fields],
            "field_scoped_by_source": dict(definition.field_scoped_by_source),
            "default": definition.default,
        }
        for definition in SCOPES.values()
    ]


def field_labels(fields: list[str]) -> str:
    return " + ".join(FIELD_ZH.get(field, field) for field in fields)


def unscoped_sources(scope: object, enabled_sources: list[str]) -> list[str]:
    """返回在当前范围下「无法严格限定字段」的数据源。"""
    definition = get_scope(scope)
    return [
        name
        for name in enabled_sources
        if not definition.field_scoped_by_source.get(name, True)
    ]


def _quote(term: str) -> str:
    term = term.strip()
    if not term:
        return ""
    if " " in term and not (term.startswith('"') and term.endswith('"')):
        return f'"{term}"'
    return term


def openalex_field(field: str) -> str:
    return {
        "title": "title",
        "abstract": "abstract",
        "keywords": "keyword",
        "fulltext": "fulltext",
    }.get(field, "default")


def openalex_search(scope: object, groups: list[list[str]]) -> str:
    """OpenAlex 的 `search=` 表达式：块间 AND、块内 OR，带字段前缀。"""
    definition = get_scope(scope)
    field_names = [openalex_field(field) for field in definition.fields]
    and_parts: list[str] = []
    for group in groups:
        or_parts: list[str] = []
        for term in group:
            quoted = _quote(term)
            if not quoted:
                continue
            for field_name in field_names:
                if field_name == "default":
                    or_parts.append(quoted)
                else:
                    or_parts.append(f"{field_name}:{quoted}")
        if or_parts:
            and_parts.append("(" + " OR ".join(or_parts) + ")")
    return " AND ".join(and_parts)


def europepmc_field(field: str) -> str:
    return {
        "title": "TITLE",
        "abstract": "ABSTRACT",
        "keywords": "KW",
        "fulltext": "",  # Europe PMC 的全文检索就是默认的 query，不加前缀
    }.get(field, "")


def europepmc_query(scope: object, groups: list[list[str]], years: tuple[int, int] | None = None) -> str:
    definition = get_scope(scope)
    field_names = [europepmc_field(field) for field in definition.fields]
    and_parts: list[str] = []
    for group in groups:
        or_parts: list[str] = []
        for term in group:
            quoted = _quote(term)
            if not quoted:
                continue
            for field_name in field_names:
                or_parts.append(f"{field_name}:{quoted}" if field_name else quoted)
        if or_parts:
            and_parts.append("(" + " OR ".join(or_parts) + ")")
    query = " AND ".join(and_parts)
    if years:
        date_clause = f"(FIRST_PDATE:[{years[0]}-01-01 TO {years[1]}-12-31])"
        query = f"({query}) AND {date_clause}" if query else date_clause
    return query


def pubmed_field(field: str) -> str:
    return {
        "title": "ti",
        "abstract": "tiab",
        "keywords": "ot",
        "fulltext": "tw",
    }.get(field, "tiab")


def pubmed_term(scope: object, groups: list[list[str]], years: tuple[int, int] | None = None) -> str:
    definition = get_scope(scope)
    tags = [pubmed_field(field) for field in definition.fields]
    and_parts: list[str] = []
    for group in groups:
        or_parts: list[str] = []
        for term in group:
            quoted = _quote(term)
            if not quoted:
                continue
            for tag in tags:
                or_parts.append(f"{quoted}[{tag}]")
        if or_parts:
            and_parts.append("(" + " OR ".join(or_parts) + ")")
    term = " AND ".join(and_parts)
    if years:
        date_clause = f'("{years[0]}/01/01"[dp] : "{years[1]}/12/31"[dp])'
        term = f"{term} AND {date_clause}" if term else date_clause
    return term


def crossref_query(scope: object, groups: list[list[str]]) -> dict[str, str]:
    """Crossref 不支持字段限定，只能选一个相关度检索参数。

    - `title`：`query.title`（把词当作标题线索，Crossref 仍是 OR/词袋语义）
    - 其它范围：`query.bibliographic`（题录）+ `query`（全字段）
    """
    definition = get_scope(scope)
    naive = " ".join(
        _quote(term).strip('"') for group in groups for term in group if term.strip()
    )
    if definition.fields == ("title",):
        return {"query.title": naive}
    if definition.key == "all":
        return {"query": naive}
    return {"query.bibliographic": naive}


# ---------------------------------------------------------------- Scopus


def scopus_field(field: str) -> str:
    return {
        "title": "TITLE",
        "abstract": "ABS",
        "keywords": "AUTHKEY",
        # Scopus 没有独立全文字段，ALL() 是整条记录（含参考文献）
        "fulltext": "ALL",
    }.get(field, "TITLE-ABS-KEY")


def scopus_field(field: str) -> str:
    return {
        "title": "TITLE",
        "abstract": "ABS",
        # Scopus 只有 KEY()：同时覆盖作者关键词与 Scopus 索引关键词
        "keywords": "KEY",
        # Scopus 没有独立全文字段，ALL() 是整条记录（含参考文献）
        "fulltext": "ALL",
    }.get(field, "TITLE-ABS-KEY")


def _broadest_fields(fields: tuple[str, ...], mapping: dict[str, str]) -> list[str]:
    """从范围字段里挑出最宽的字段，避免「窄字段 OR 宽字段」的冗余操作数。

    例如 `all` 范围同时含 title/abstract/keywords/fulltext，而 `ALL()`/`ALL=`
    本身就覆盖了前面所有字段，所以只保留 `ALL`。
    """
    names: list[str] = []
    fallback = ""
    for field in fields:
        name = mapping.get(field, "")
        if not name:
            continue
        if field == "fulltext":
            return [name]
        if not fallback:
            fallback = name
        if name not in names:
            names.append(name)
    if not names and fallback:
        names.append(fallback)
    return names


def scopus_query(scope: object, groups: list[list[str]], years: tuple[int, int] | None = None) -> str:
    """Scopus 布尔检索式：字段函数 + 括号，年份用 PUBYEAR。"""
    definition = get_scope(scope)
    field_names = _broadest_fields(
        definition.fields,
        {
            "title": "TITLE",
            "abstract": "ABS",
            "keywords": "KEY",
            "fulltext": "ALL",
        },
    )
    and_parts: list[str] = []
    for group in groups:
        or_parts: list[str] = []
        for term in group:
            quoted = _quote(term)
            if not quoted:
                continue
            for field_name in field_names:
                or_parts.append(f"{field_name}({quoted})")
        if or_parts:
            and_parts.append("(" + " OR ".join(or_parts) + ")")
    query = " AND ".join(and_parts)
    if years:
        year_clause = f"(PUBYEAR > {years[0] - 1} AND PUBYEAR < {years[1] + 1})"
        query = f"({query}) AND {year_clause}" if query else year_clause
    return query


def wos_query(scope: object, groups: list[list[str]]) -> str:
    """Web of Science Starter API 的 `q` 参数（字段标签 = 值）。

    注意：字段标签不能直接套括号（`TI=(a OR b)` 不合法），必须写成
    `(TI=a OR TI=b)`；年份由 `publishTimeSpan` 参数控制，不写进 q。
    """
    definition = get_scope(scope)
    field_names = _broadest_fields(
        definition.fields,
        {
            "title": "TI",
            "abstract": "AB",
            "keywords": "AK",
            "fulltext": "ALL",
        },
    )
    and_parts: list[str] = []
    for group in groups:
        or_parts: list[str] = []
        for term in group:
            quoted = _quote(term)
            if not quoted:
                continue
            for field_name in field_names:
                or_parts.append(f"{field_name}={quoted}")
        if or_parts:
            and_parts.append("(" + " OR ".join(or_parts) + ")")
    return " AND ".join(and_parts)
