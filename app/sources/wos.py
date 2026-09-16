"""Web of Science 数据源（Clarivate Web of Science Starter API）。

- 端点：`https://api.clarivate.com/apis/wos-starter/v1/documents`（GET）
- 认证：请求头 `X-ApiKey: <key>`
- 关键参数：`q`（字段标签检索式）、`db=WOS`、`limit`、`page`、`sortField`、`publishTimeSpan`
- 需要 Clarivate 开发者 key（Starter 计划需要在开发者门户申请）；没有 key 时给出明确提示

字段标签（四种检索范围，见 `query_scope.py`）：
`TI=` 标题 / `AB=` 摘要 / `AK=` 作者关键词 / `TS=` 主题（标题+摘要+关键词）/
`ALL=` 全记录字段。

为便于离线自测，端点可用环境变量 `LITLOADER_WOS_ENDPOINT` 覆盖。
"""

from __future__ import annotations

import os
import time

from .base import RawRecord, SearchContext, SearchOutcome, SourceClient, excerpt, merge_authors

API_URL = os.environ.get(
    "LITLOADER_WOS_ENDPOINT",
    "https://api.clarivate.com/apis/wos-starter/v1/documents",
)
SOURCE_NAME = "wos"
LABEL = "Web of Science"


def _records(payload: object) -> list[dict]:
    if not isinstance(payload, dict):
        return []
    data = payload.get("Data") if isinstance(payload.get("Data"), dict) else payload.get("data")
    if isinstance(data, dict):
        records = data.get("Records") or data.get("records")
    else:
        records = payload.get("Records") or payload.get("records")
    if isinstance(records, list):
        return [item for item in records if isinstance(item, dict)]
    return []


def _year(record: dict) -> int | None:
    source = record.get("Source") if isinstance(record.get("Source"), dict) else {}
    for container in (source, record):
        value = container.get("Published.BiblioYear") or container.get("Published.Year") or container.get("publishYear")
        if value is None:
            continue
        token = str(value).strip()[:4]
        if token.isdigit():
            return int(token)
    return None


def _journal(record: dict) -> tuple[str, str]:
    source = record.get("Source") if isinstance(record.get("Source"), dict) else {}
    name = str(source.get("SourceTitle") or source.get("sourceTitle") or "").strip()
    short = str(source.get("SourceAbbrev") or "").strip()
    return name, short


def _identifiers(record: dict) -> list[str]:
    """Doi / Issn 字段既可能是字符串也可能是数组。"""
    identifiers: list[str] = []

    def collect(value: object) -> None:
        if isinstance(value, list):
            for item in value:
                collect(item)
        elif isinstance(value, dict):
            for key in ("value", "Value", "doi", "issn"):
                if value.get(key):
                    collect(value[key])
        elif value:
            identifiers.append(str(value).strip())

    for key in ("Doi", "Identifiers", "Issn"):
        collect(record.get(key))
    return [item for item in identifiers if item]


def _to_record(record: dict) -> RawRecord:
    title = str(record.get("Title") or record.get("title") or "").strip()
    if isinstance(record.get("Title"), dict):  # 个别版本包一层
        title = str(record["Title"].get("Title") or record["Title"].get("title") or "").strip()

    authors: list[str] = []
    for author in record.get("Names") or record.get("names") or []:
        if not isinstance(author, dict):
            continue
        display = str(author.get("DisplayName") or author.get("displayName") or "").strip()
        if not display:
            display = " ".join(
                part for part in [str(author.get("FirstName") or "").strip(), str(author.get("LastName") or "").strip()] if part
            )
        if display:
            authors.append(display)

    identifiers = _identifiers(record)
    doi = next((item for item in identifiers if item.lower().startswith("10.")), "")
    issns = [item for item in identifiers if item != doi and len(item) in {8, 9} and any(char.isdigit() for char in item)]
    journal, journal_short = _journal(record)
    uid = str(record.get("UID") or record.get("uid") or "").strip()
    links = record.get("Links") if isinstance(record.get("Links"), dict) else {}
    landing = str(
        (links or {}).get("Record") or (links or {}).get("record") or (record.get("OtherInformation") or {}).get("Identifier") or ""
    ).strip() or (f"https://www.webofscience.com/wos/woscc/full-record/{uid}" if uid else "")
    citations = record.get("Citations")
    cited = 0
    if isinstance(citations, list) and citations:
        first = citations[0]
        if isinstance(first, dict):
            try:
                cited = int(str(first.get("Count") or 0))
            except (TypeError, ValueError):
                cited = 0
    return RawRecord(
        source=SOURCE_NAME,
        title=title,
        authors=merge_authors(authors),
        year=_year(record),
        journal=journal,
        journal_short=journal_short,
        issns=issns,
        doi=doi,
        article_type=str(record.get("DocumentType") or record.get("documentType") or ""),
        publisher=str((record.get("Source") or {}).get("Publisher") or "") if isinstance(record.get("Source"), dict) else "",
        landing_page_url=landing,
        article_url=f"https://doi.org/{doi}" if doi else landing,
        cited_by_count=cited,
        abstract=excerpt(record.get("Abstract") or record.get("abstract"), 1200),
    )


def search(context: SearchContext) -> SearchOutcome:
    started = time.monotonic()
    outcome = SearchOutcome(source=SOURCE_NAME)
    api_key = str(context.search.api_keys.get(SOURCE_NAME) or "").strip()
    if not api_key:
        outcome.error = "missing_api_key"
        outcome.warnings.append(
            "Web of Science 需要 Clarivate API key 才能检索：请在网页右上角「配置」里填入，"
            "或写进 config.json 的 search.api_keys.wos。"
        )
        outcome.elapsed_seconds = time.monotonic() - started
        return outcome

    query = context.query_for(SOURCE_NAME)
    if not query:
        outcome.warnings.append("关键词为空，Web of Science 未检索。")
        outcome.elapsed_seconds = time.monotonic() - started
        return outcome

    client = SourceClient(source=SOURCE_NAME)
    limit = min(context.max_results_per_source, 50)  # Starter 计划单页上限较小
    params: dict[str, object] = {
        "q": query,
        "db": "WOS",
        "limit": limit,
        "page": 1,
        "sortField": "RS",
        "sortOrder": "desc",
        "publishTimeSpan": f"{context.year_from}-01-01+{context.year_to}-12-31",
    }
    headers = {"X-ApiKey": api_key, "Accept": "application/json"}

    try:
        response = client.session.get(API_URL, params=params, headers=headers, timeout=45)
        if response.status_code in {401, 403}:
            outcome.error = f"http_{response.status_code}"
            outcome.warnings.append(
                f"Web of Science 拒绝了这次请求（HTTP {response.status_code}）：API key 无效、未启用 WoS Starter 访问权限，或该 key 没有订阅范围。"
            )
            outcome.elapsed_seconds = time.monotonic() - started
            return outcome
        if response.status_code == 429:
            outcome.error = "http_429"
            outcome.warnings.append("Web of Science 触发限流（HTTP 429），请稍后重试或减少每源条数。")
            outcome.elapsed_seconds = time.monotonic() - started
            return outcome
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:  # noqa: BLE001
        outcome.error = f"{type(exc).__name__}: {exc}"
        outcome.warnings.append(f"Web of Science 检索失败：{outcome.error}")
        outcome.elapsed_seconds = time.monotonic() - started
        return outcome

    records = _records(payload)
    if not records:
        outcome.warnings.append("Web of Science 没有匹配结果（可能是检索式过窄、年份区间内无收录，或 key 的订阅范围有限）。")
    for record in records:
        parsed = _to_record(record)
        if parsed.title:
            outcome.records.append(parsed)
    outcome.elapsed_seconds = time.monotonic() - started
    return outcome
