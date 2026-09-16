"""并行执行各数据源检索，并做单源失败隔离。

单个数据源超时或报错只会写入 warnings，不会中断整体检索——这是刻意的：
OpenAlex / Crossref / Europe PMC / PubMed / Scopus / Web of Science 的可用性互相独立
（后两者还需要各自的 API key）。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any

from . import crossref, europepmc, openalex, pubmed, scopus, wos
from .base import SearchContext, SearchOutcome

SOURCE_MODULES: dict[str, Any] = {
    "openalex": openalex,
    "crossref": crossref,
    "europepmc": europepmc,
    "pubmed": pubmed,
    "scopus": scopus,
    "wos": wos,
}


def enabled_sources(selection: dict[str, bool] | None) -> list[str]:
    if not selection:
        return list(SOURCE_MODULES)
    return [name for name in SOURCE_MODULES if selection.get(name)]


def run_search(context: SearchContext, selection: dict[str, bool] | None = None) -> dict[str, SearchOutcome]:
    """并行检索所有启用的数据源，返回 {source: SearchOutcome}。"""
    names = enabled_sources(selection)
    if not names:
        return {}
    outcomes: dict[str, SearchOutcome] = {}
    with ThreadPoolExecutor(max_workers=min(len(names), 6)) as pool:
        futures = {pool.submit(SOURCE_MODULES[name].search, context): name for name in names}
        for future, name in futures.items():
            try:
                outcomes[name] = future.result()
            except Exception as exc:  # noqa: BLE001
                outcome = SearchOutcome(source=name)
                outcome.error = f"{type(exc).__name__}: {exc}"
                outcome.warnings.append(f"{name} 检索异常：{outcome.error}")
                outcomes[name] = outcome
    return outcomes
