"""FastAPI 应用：本机网页版文献检索与下载服务。

启动：
    python -m app.server            # 默认 127.0.0.1:8765，端口被占用时自动向后找
    python -m app.server --port 9000 --reload
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from . import __version__
from .analysis import SOURCE_LABEL_ZH, apply_whitelist, merge_records, split_keywords
from .config import PROVIDER_PRESETS, AppConfig, ConfigError, load_config
from .config_store import ConfigWriteError, update_config_file
from .fulltext import fetch_many
from .journal_match import JournalMatcher
from .llm import LLMError, expand_keywords, test_connection
from .models import Candidate, Status
from .query_scope import (
    field_labels,
    get_scope,
    normalize_scope,
    scope_catalog,
    unscoped_sources,
)
from .report import generate_all, stats_payload
from .runs import ALLOWED_FILE_NAMES, RunState, RunStore, UnsafePathError, safe_join
from .sources.base import SearchContext
from .sources.runner import run_search

WEB_DIR = Path(__file__).resolve().parent / "web"


class KeywordTerm(BaseModel):
    term: str = Field(..., min_length=1)
    source: str = "user"  # user | llm


class KeywordGroup(BaseModel):
    terms: list[KeywordTerm] = Field(default_factory=list)


class SearchRequest(BaseModel):
    keywords: str = Field(..., min_length=1)
    # 用户在界面上确认过的「概念块 → 关键词（带来源标记）」
    keyword_groups: list[KeywordGroup] | None = None
    # 关键词扩展用的模型名（仅用于记录，便于在交付文件里写明来源）
    llm_model: str | None = None
    year_from: int | None = None
    year_to: int | None = None
    sources: dict[str, bool] | None = None
    max_results_per_source: int | None = None
    scope: str | None = None
    oa_only: bool = False


class ExpandRequest(BaseModel):
    keywords: str = Field(..., min_length=1)
    model: str | None = None
    scope: str | None = None
    topic: str | None = None
    language_hint: str | None = None
    max_new_terms_per_group: int | None = None


class LLMTestRequest(BaseModel):
    model: str | None = None


class ProviderEdit(BaseModel):
    name: str = Field(..., min_length=1)
    type: str | None = None
    base_url: str | None = None
    api_key: str | None = None


class ConfigEditRequest(BaseModel):
    """网页「配置」弹窗的入参。只提交要改的字段。"""

    source_keys: dict[str, str] | None = None
    sources: dict[str, bool] | None = None
    model_keys: dict[str, str] | None = None
    provider_keys: dict[str, str] | None = None
    provider: ProviderEdit | None = None
    email: str | None = None
    unpaywall_email: str | None = None


class DownloadRequest(BaseModel):
    record_ids: list[str] = Field(default_factory=list)


def resolve_keyword_groups(
    keywords: str,
    raw_groups: list[KeywordGroup] | None,
) -> tuple[list[list[str]], list[str], list[str], list[dict[str, Any]]]:
    """把前端确认过的关键词结构收敛成检索用的概念块。

    返回（概念块词表、用户词、LLM 生成词、带来源的展示结构）。

    - 前端没传结构时，退回按 `;` 拆分的用户关键词（保证兼容旧调用）；
    - 前端传了结构时，保留空块（用户在扩展预览里删光了某一块）以维持 AND 结构。
    """
    if not raw_groups:
        groups = split_keywords(keywords)
        user_terms = [term for group in groups for term in group]
        detail = [
            {"index": index, "terms": [{"term": term, "source": "user"} for term in group]}
            for index, group in enumerate(groups)
        ]
        return groups, user_terms, [], detail

    groups: list[list[str]] = []
    user_terms: list[str] = []
    llm_terms: list[str] = []
    detail: list[dict[str, Any]] = []
    for index, group in enumerate(raw_groups):
        seen: set[str] = set()
        terms: list[str] = []
        for item in group.terms:
            term = item.term.strip().strip('"').strip()
            if not term or term.casefold() in seen:
                continue
            seen.add(term.casefold())
            terms.append(term)
            if item.source == "llm":
                llm_terms.append(term)
            else:
                user_terms.append(term)
        groups.append(terms)
        detail.append(
            {"index": index, "terms": [{"term": item.term.strip(), "source": item.source} for item in group.terms]}
        )
    if not any(groups):
        groups = split_keywords(keywords)
        user_terms = [term for group in groups for term in group]
        detail = [
            {"index": index, "terms": [{"term": term, "source": "user"} for term in group]}
            for index, group in enumerate(groups)
        ]
        llm_terms = []
    return groups, user_terms, llm_terms, detail


def pick_port(host: str, preferred: int, attempts: int = 25) -> int:
    """从 preferred 开始找一个可用端口。"""
    for offset in range(attempts):
        port = preferred + offset
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind((host, port))
            except OSError:
                continue
            return port
    raise RuntimeError(f"在 {preferred}-{preferred + attempts} 范围内找不到可用端口。")


class DownloadManager:
    """后台下载任务：每个运行同时只允许一个下载任务。"""

    def __init__(self, store: RunStore, get_config: Any) -> None:
        self.store = store
        self.get_config = get_config
        self._jobs: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def submit(self, run: RunState, record_ids: list[str], config: AppConfig) -> tuple[str, int]:
        if not record_ids:
            raise ValueError("没有勾选任何文献。")
        candidates = [run.candidate_by_id()[rid] for rid in record_ids if rid in run.candidate_by_id()]
        if not candidates:
            raise ValueError("勾选的文献不在本次检索结果里，请重新检索。")
        # 记录本次勾选状态，让「文章地址总表.csv」的「是否勾选」列有意义。
        selected_ids = {item.record_id for item in candidates}
        for item in run.candidates:
            item.selected = item.record_id in selected_ids

        job_id = uuid.uuid4().hex[:12]
        payload: dict[str, Any] = {
            "job_id": job_id,
            "run_id": run.run_id,
            "record_ids": [item.record_id for item in candidates],
            "state": "queued",
            "total": len(candidates),
            "processed": 0,
            "current_title": "",
            "counts": {},
            "errors": [],
            "started_at": time.time(),
            "finished_at": None,
        }
        with self._lock:
            self._jobs[job_id] = payload

        thread = threading.Thread(
            target=self._run_job,
            args=(payload, run, candidates, config),
            name=f"lit-loader-download-{job_id}",
            daemon=True,
        )
        thread.start()
        return job_id, len(candidates)

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self._jobs.get(job_id)

    def _run_job(self, payload: dict[str, Any], run: RunState, candidates: list[Candidate], config: AppConfig) -> None:
        payload["state"] = "running"
        counts: dict[str, int] = {}

        def on_result(candidate: Candidate, result: Any) -> None:
            candidate.download_status = result.status
            candidate.failure_reason = result.failure_reason
            candidate.final_path = result.final_path
            candidate.content_format = result.content_format
            candidate.access_route_used = result.access_route_used
            candidate.attempt_count = result.attempts
            counts[result.status] = counts.get(result.status, 0) + 1
            payload["processed"] += 1
            payload["counts"] = dict(counts)
            payload["current_title"] = candidate.title

        try:
            results = fetch_many(
                candidates,
                run.papers_dir,
                config.download,
                unpaywall_email=config.search.unpaywall_email,
                on_result=on_result,
                log_path=run.directory / "下载尝试日志.jsonl",
            )
            for result in results:
                if not result.success and result.status == Status.METADATA_ONLY.value:
                    payload["errors"].append(f"{result.record_id}: {result.failure_reason}")
        except Exception as exc:  # noqa: BLE001
            payload["errors"].append(f"{type(exc).__name__}: {exc}")
            payload["state"] = "failed"
        else:
            payload["state"] = "done"

        for candidate in run.candidates:
            if candidate.download_status == Status.PENDING.value and candidate.record_id not in payload["record_ids"]:
                candidate.download_status = Status.NOT_SELECTED.value

        try:
            generate_all(run, config)
            self.store.save_run(run)
        except Exception as exc:  # noqa: BLE001
            payload["errors"].append(f"生成报告失败：{type(exc).__name__}: {exc}")
        payload["finished_at"] = time.time()

    def to_dict(self, payload: dict[str, Any], run: RunState, config: AppConfig) -> dict[str, Any]:
        data = dict(payload)
        data.pop("record_ids", None)
        data["elapsed_seconds"] = round((payload.get("finished_at") or time.time()) - payload["started_at"], 1)
        data["deliverables"] = run.deliverable_paths()
        data["summary"] = stats_payload(run, config)
        return data


def create_app(config_path: str | Path | None = None) -> FastAPI:
    app = FastAPI(title="文献下载器", version=__version__)

    state: dict[str, Any] = {
        "config_path": Path(config_path) if config_path else None,
        "config": None,
        "store": None,
    }

    def current_config(force: bool = False) -> AppConfig:
        """读取配置（每次检索/请求都重读，支持热更新白名单）。"""
        if force or state["config"] is None:
            try:
                state["config"] = load_config(state["config_path"])
            except ConfigError:
                raise
        config: AppConfig = state["config"]
        if state["store"] is None or state["store"].config is not config:
            state["store"] = RunStore(config)
        return config

    def get_store() -> RunStore:
        current_config()
        return state["store"]

    def load_or_400(force: bool = False) -> AppConfig:
        try:
            return current_config(force=force)
        except ConfigError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    manager = DownloadManager(get_store(), current_config)

    # ---------- 页面与静态资源 ----------

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(html)

    @app.get("/static/{name}")
    def static_file(name: str) -> FileResponse:
        try:
            target = safe_join(WEB_DIR, name)
        except UnsafePathError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not target.exists():
            raise HTTPException(status_code=404, detail="静态资源不存在")
        return FileResponse(target)

    # ---------- 配置 ----------

    @app.get("/api/config")
    def api_config() -> dict[str, Any]:
        try:
            config = load_or_400()
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=f"{type(exc).__name__}: {exc}") from exc
        return {
            "config": config.public_view(),
            "runs_root": str(config.output_root / "runs"),
            "version": __version__,
        }

    @app.post("/api/config/reload")
    def api_config_reload() -> dict[str, Any]:
        config = load_or_400(force=True)
        return {"ok": True, "config": config.public_view()}

    @app.post("/api/config/edit")
    def api_config_edit(request: ConfigEditRequest) -> dict[str, Any]:
        """把界面里填的 key/邮箱写回 config.json，并立即重载。

        只把「确实发生了哪些改动」与「某项是否已配置 key」返回给前端；
        提交的密钥内容不会被回显，日志里也不会打印。
        """
        config = load_or_400()
        new_provider: dict[str, Any] | None = None
        if request.provider is not None:
            new_provider = {"name": request.provider.name}
            for field_name in ("type", "base_url", "api_key"):
                value = getattr(request.provider, field_name)
                if value is not None:
                    new_provider[field_name] = value
        try:
            result = update_config_file(
                config.path,
                source_keys=request.source_keys,
                sources=request.sources,
                model_keys=request.model_keys,
                provider_keys=request.provider_keys,
                new_provider=new_provider,
                email=request.email,
                unpaywall_email=request.unpaywall_email,
            )
        except ConfigWriteError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        refreshed = load_or_400(force=True)
        return {
            "ok": True,
            "changed": result.changed,
            "message": result.message,
            "backup": Path(result.backup_path).name if result.backup_path else "",
            "config": refreshed.public_view(),
            "note": "密钥只写入本机 config.json（已加入 .gitignore），界面不会再回显明文。",
        }

    # ---------- LLM 关键词扩展 ----------

    @app.get("/api/llm/models")
    def api_llm_models() -> dict[str, Any]:
        config = load_or_400()
        llm = config.llm
        return {
            "enabled": llm.enabled,
            "auto_expand": llm.auto_expand,
            "default_model": llm.default_model,
            "timeout_seconds": llm.timeout_seconds,
            "max_new_terms_per_group": llm.max_new_terms_per_group,
            "providers": [provider.to_dict() for provider in llm.providers],
            "models": [model.to_dict() for model in llm.models],
            "provider_presets": dict(PROVIDER_PRESETS),
            "ready": any(model.has_api_key for model in llm.models) and bool(llm.models),
            "note": "api_key 只在后端用于请求头，不会返回给前端，也不会写进运行快照。",
        }

    @app.post("/api/llm/test")
    def api_llm_test(request: LLMTestRequest) -> dict[str, Any]:
        config = load_or_400(force=True)
        try:
            return test_connection(config.llm, request.model)
        except LLMError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=f"测试失败：{type(exc).__name__}: {exc}") from exc

    @app.post("/api/expand-keywords")
    def api_expand_keywords(request: ExpandRequest) -> dict[str, Any]:
        config = load_or_400(force=True)
        llm = config.llm
        keywords = request.keywords.strip()
        groups = split_keywords(keywords)
        if not groups:
            raise HTTPException(status_code=400, detail="请先填写关键词，多个关键词用 ; 分隔。")
        if not llm.enabled:
            raise HTTPException(
                status_code=400,
                detail="关键词扩展未启用：请在 config.json 的 llm 里把 enabled 设为 true，并配置 models。",
            )
        scope = normalize_scope(request.scope or config.search.scope)
        model_name = request.model or llm.default_model
        journals = [entry.name for entry in config.journals if entry.name]
        topic = (request.topic or llm.topic or "").strip()
        try:
            result = expand_keywords(
                groups,
                llm,
                model_name=model_name,
                topic=topic,
                journals=journals,
                scope_label=get_scope(scope).label,
                scope_fields=field_labels(list(get_scope(scope).fields)),
                language_hint=(request.language_hint or llm.language_hint or "").strip(),
            )
        except LLMError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=f"关键词扩展失败：{type(exc).__name__}: {exc}") from exc

        payload = result.to_dict()
        payload.update(
            {
                "scope": scope,
                "scope_label": get_scope(scope).label,
                "journals_used": journals[:20],
                "topic": topic,
                "original_groups": groups,
            }
        )
        return payload

    # ---------- 检索 ----------

    @app.post("/api/search")
    def api_search(request: SearchRequest) -> dict[str, Any]:
        config = load_or_400(force=True)
        keywords = request.keywords.strip()
        groups, user_terms, llm_terms, keyword_detail = resolve_keyword_groups(keywords, request.keyword_groups)
        if not any(groups):
            raise HTTPException(status_code=400, detail="请填写关键词，多个关键词用 ; 分隔。")
        if not config.journals:
            raise HTTPException(
                status_code=400,
                detail="期刊白名单为空：请先在 config.json 的 journals 里加入期刊，否则检索结果会被全部过滤。",
            )

        search_config = config.search
        if request.year_from is not None or request.year_to is not None:
            search_config.year_from = request.year_from or search_config.year_from
            search_config.year_to = request.year_to or search_config.year_to
        if search_config.year_from > search_config.year_to:
            search_config.year_from, search_config.year_to = search_config.year_to, search_config.year_from

        selection = request.sources or dict(search_config.sources)
        if request.max_results_per_source:
            search_config.max_results_per_source = max(1, min(int(request.max_results_per_source), 500))
        if request.scope:
            search_config.scope = normalize_scope(request.scope)

        context = SearchContext(
            keywords_raw=keywords,
            groups=groups,
            year_from=search_config.year_from,
            year_to=search_config.year_to,
            max_results_per_source=search_config.max_results_per_source,
            search=search_config,
            scope=search_config.scope,
        )

        outcomes = run_search(context, selection)
        records = [record for outcome in outcomes.values() for record in outcome.records]
        buckets, duplicates = merge_records(records)

        matcher = JournalMatcher(config.journals, config.journal_match)
        filtered = apply_whitelist(buckets, groups, config, matcher)

        if request.oa_only:
            filtered.candidates = [item for item in filtered.candidates if item.has_oa_signal]
        for item in filtered.candidates:
            if item.download_status == Status.PENDING.value:
                item.download_status = Status.PENDING.value

        warnings = list(config.warnings)
        for outcome in outcomes.values():
            warnings.extend(outcome.warnings)
        warnings.extend(filtered.warnings)
        narrow_sources = unscoped_sources(context.scope, list(outcomes))
        if narrow_sources:
            labels = "、".join(SOURCE_LABEL_ZH.get(name, name) for name in narrow_sources)
            warnings.append(
                f"{labels} 不支持严格限定标题/摘要字段，本次按相关度返回；"
                "这些来源里命中不在所选字段内的记录会在关键词打分阶段被降权或标为已排除。"
            )

        store = get_store()
        run = store.create_run(
            keywords=keywords,
            groups=groups,
            query_text=context.query_text,
            year_from=context.year_from,
            year_to=context.year_to,
            sources=[name for name in outcomes],
        )
        run.candidates = filtered.candidates
        run.filtered_candidates = filtered.filtered_candidates
        run.warnings = warnings
        run.stats = {
            "raw": len(records),
            "deduped": len(buckets),
            "duplicates": duplicates,
            "after_whitelist": len(filtered.candidates),
            "filtered_out": len(filtered.filtered_candidates),
            "by_source": {name: outcome.count for name, outcome in outcomes.items()},
            "source_errors": {name: outcome.error for name, outcome in outcomes.items() if outcome.error},
            "unmatched_journals": filtered.stats.unmatched_journals,
            "exclude_excluded": sum(1 for item in filtered.candidates if item.exclusion_reason_if_any),
            "scope": context.scope,
            "scope_label": get_scope(context.scope).label,
            "scope_fields": field_labels(list(get_scope(context.scope).fields)),
            "source_queries": {name: context.query_for(name) for name in outcomes},
            "unscoped_sources": narrow_sources,
            "user_terms": user_terms,
            "llm_terms": llm_terms,
            "keyword_groups": keyword_detail,
            "llm_used": bool(llm_terms),
            "llm_model": (request.llm_model or config.llm.default_model) if llm_terms else "",
        }
        if llm_terms:
            warnings.append(
                f"本次检索使用了 LLM 扩展的 {len(llm_terms)} 个新关键词（用户关键词 {len(user_terms)} 个）；"
                "清单里会同时列出两者，便于复现。"
            )

        # 检索完成即先生成一次三份交付文件（此时状态多为「待下载」）
        generate_all(run, config)
        store.save_run(run)

        return {
            "run": {
                "run_id": run.run_id,
                "directory": str(run.directory),
                "query": context.query_text,
                "groups": groups,
                "year_from": context.year_from,
                "year_to": context.year_to,
                "sources": [name for name in outcomes],
                "stats": run.stats,
                "warnings": warnings,
                "deliverables": run.deliverable_paths(),
            },
            "candidates": [item.to_dict() for item in run.candidates],
            "filtered_preview": run.stats["unmatched_journals"],
            "source_labels": SOURCE_LABEL_ZH,
            "scope": {
                "key": context.scope,
                "label": get_scope(context.scope).label,
                "fields": list(get_scope(context.scope).fields),
                "options": scope_catalog(),
            },
        }

    # ---------- 下载 ----------

    @app.post("/api/runs/{run_id}/download")
    def api_download(run_id: str, request: DownloadRequest) -> dict[str, Any]:
        config = load_or_400()
        store = get_store()
        run = store.get(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="找不到该运行记录，请重新检索。")
        with run.lock:
            if run.job and run.job.state in {"queued", "running"}:
                raise HTTPException(status_code=409, detail="该运行已有下载任务在进行中。")
            try:
                job_id, total = manager.submit(run, request.record_ids, config)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            job = manager.get(job_id)
            assert job is not None
            from .runs import DownloadJob

            run.job = DownloadJob(
                job_id=job_id,
                run_id=run.run_id,
                record_ids=list(request.record_ids),
                state="queued",
                total=total,
            )
        return {"job_id": job_id, "total": total, "run_id": run.run_id}

    @app.get("/api/runs/{run_id}/download/status")
    def api_download_status(run_id: str, job_id: str | None = None) -> dict[str, Any]:
        config = load_or_400()
        store = get_store()
        run = store.get(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="找不到该运行记录。")
        target_job = job_id or (run.job.job_id if run.job else None)
        if not target_job:
            raise HTTPException(status_code=404, detail="该运行还没有下载任务。")
        payload = manager.get(target_job)
        if payload is None:
            raise HTTPException(status_code=404, detail="找不到下载任务。")
        data = manager.to_dict(payload, run, config)
        data["candidates"] = [item.to_dict() for item in run.candidates]
        return data

    # ---------- 运行与文件 ----------

    @app.get("/api/runs/{run_id}")
    def api_run(run_id: str) -> dict[str, Any]:
        config = load_or_400()
        store = get_store()
        run = store.get(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="找不到该运行记录。")
        return {
            "run": run.to_dict(),
            "candidates": [item.to_dict() for item in run.candidates],
            "summary": stats_payload(run, config),
        }

    @app.get("/api/runs/{run_id}/file/{name}")
    def api_run_file(run_id: str, name: str) -> FileResponse:
        store = get_store()
        run = store.get(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="找不到该运行记录。")
        if name not in ALLOWED_FILE_NAMES:
            raise HTTPException(status_code=400, detail=f"只允许下载这几个文件：{'、'.join(sorted(ALLOWED_FILE_NAMES))}")
        try:
            target = safe_join(run.directory, name)
        except UnsafePathError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not target.exists():
            raise HTTPException(status_code=404, detail="文件还没生成。")
        return FileResponse(target, filename=target.name)

    @app.get("/api/runs/{run_id}/papers")
    def api_run_papers(run_id: str) -> dict[str, Any]:
        """列出本次运行已下载的全文文件（界面下载完成后展示）。"""
        store = get_store()
        run = store.get(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="找不到该运行记录。")
        files = []
        if run.papers_dir.exists():
            for path in sorted(run.papers_dir.iterdir()):
                if path.is_file():
                    files.append({"name": path.name, "size": path.stat().st_size})
        return {"papers": files, "directory": str(run.papers_dir), "count": len(files)}

    @app.post("/api/open-folder")
    def api_open_folder(payload: dict[str, Any]) -> dict[str, Any]:
        run_id = str((payload or {}).get("run_id") or "")
        store = get_store()
        run = store.get(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="找不到该运行记录。")
        try:
            if hasattr(os, "startfile"):
                os.startfile(str(run.directory))  # type: ignore[attr-defined]  # noqa: S606
                opened = True
            else:
                opened = False
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=f"打开文件夹失败：{exc}") from exc
        if not opened:
            raise HTTPException(status_code=400, detail=f"当前系统不支持自动打开文件夹，请手动打开：{run.directory}")
        return {"ok": True, "directory": str(run.directory)}

    @app.get("/api/health")
    def api_health() -> JSONResponse:
        return JSONResponse({"ok": True, "version": __version__, "time": time.time()})

    return app


def main() -> None:
    # Windows 控制台默认编码可能是 cp1252，中文启动提示会直接报错。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass

    parser = argparse.ArgumentParser(description="启动文献下载器网页版")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765, help="默认 8765；被占用时自动向后查找")
    parser.add_argument("--config", default=None, help="config.json 路径（默认工作区根目录）")
    parser.add_argument("--reload", action="store_true", help="开发模式：代码改动自动重载")
    args = parser.parse_args()

    app = create_app(args.config)
    import uvicorn

    port = pick_port(args.host, args.port)
    url = f"http://{args.host}:{port}/"
    print("=" * 68)
    print("  文献下载器（网页版）已启动")
    print(f"  请在浏览器打开：{url}")
    print(f"  期刊白名单配置文件：{args.config or (Path.cwd() / 'config.json')}")
    print("  提示：修改白名单后无需重启，下次检索立刻生效。按 Ctrl+C 停止。")
    print("=" * 68)
    if args.reload:
        uvicorn.run("app.server:create_app", host=args.host, port=port, reload=True, factory=True)
    else:
        uvicorn.run(app, host=args.host, port=port, log_level="info")


if __name__ == "__main__":
    main()
