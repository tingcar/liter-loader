"""运行目录管理、路径安全校验、内存运行态与下载任务进度。

每次检索生成一个运行目录：

    <output_root>/runs/<run_id>/
        下载报告.html
        文章地址总表.csv
        文献清单.md
        候选文献总表.csv
        下载日志.csv
        config.snapshot.json
        papers/

`<output_root>` 默认是工作区下的 `paperdown/`（可在 config.json 里改）。
"""

from __future__ import annotations

import csv
import json
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import AppConfig
from .models import CANDIDATE_COLUMNS, Candidate

RUN_ID_RE = re.compile(r"^[A-Za-z0-9_\-]+$")
DELIVERABLE_NAMES = ("下载报告.html", "文章地址总表.csv", "文献清单.md")
ALLOWED_FILE_NAMES = set(DELIVERABLES_NAMES := DELIVERABLE_NAMES) | {
    "候选文献总表.csv",
    "下载日志.csv",
    "config.snapshot.json",
}


class UnsafePathError(ValueError):
    """请求的文件名不在允许范围内，或试图跳出运行目录。"""


def safe_join(root: Path, relative: str) -> Path:
    """把 `relative` 解析到 `root` 下，并阻止路径穿越。"""
    candidate_name = str(relative or "").strip().replace("\\", "/")
    if not candidate_name:
        raise UnsafePathError("文件名不能为空。")
    if candidate_name.startswith("/") or ":" in candidate_name:
        raise UnsafePathError(f"不允许的路径：{relative}")
    parts = [part for part in candidate_name.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise UnsafePathError(f"不允许的路径：{relative}")
    if len(parts) != 1:
        raise UnsafePathError(f"只允许访问运行目录根下的文件：{relative}")
    resolved_root = root.resolve()
    target = (resolved_root / parts[0]).resolve()
    if resolved_root != target and resolved_root not in target.parents:
        raise UnsafePathError(f"不允许的路径：{relative}")
    return target


def slugify(text: str, limit: int = 24) -> str:
    """生成适合放进目录名的短标签（保留中文）。"""
    cleaned = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "-", str(text or "")).strip("-")
    return cleaned[:limit] or "search"


def make_run_id(keywords: str) -> str:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{stamp}_{slugify(keywords, 20)}"


@dataclass
class DownloadJob:
    """一次「下载文献」任务的进度。"""

    job_id: str
    run_id: str
    record_ids: list[str]
    state: str = "queued"  # queued | running | done | failed | cancelled
    total: int = 0
    processed: int = 0
    current_title: str = ""
    counts: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None

    @property
    def elapsed_seconds(self) -> float:
        return (self.finished_at or time.time()) - self.started_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "run_id": self.run_id,
            "state": self.state,
            "total": self.total,
            "processed": self.processed,
            "current_title": self.current_title,
            "counts": dict(self.counts),
            "errors": list(self.errors[-20:]),
            "elapsed_seconds": round(self.elapsed_seconds, 1),
        }


@dataclass
class RunState:
    """一次检索的运行态。"""

    run_id: str
    directory: Path
    keywords: str
    groups: list[list[str]]
    query_text: str
    year_from: int
    year_to: int
    sources: list[str]
    candidates: list[Candidate]
    filtered_candidates: list[Candidate]
    stats: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    job: DownloadJob | None = None
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def papers_dir(self) -> Path:
        return self.directory / "papers"

    def candidate_by_id(self) -> dict[str, Candidate]:
        return {item.record_id: item for item in self.candidates}

    def deliverable_paths(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for name in DELIVERABLE_NAMES:
            path = self.directory / name
            if path.exists():
                result[name] = str(path)
        return result

    def summary_counts(self) -> dict[str, int]:
        counts = {"total": len(self.candidates), "selected": 0, "success_pdf": 0, "success_html": 0, "success_xml": 0}
        counts["inaccessible"] = 0
        counts["metadata_only"] = 0
        counts["failed"] = 0
        counts["not_selected"] = 0
        counts["excluded"] = 0
        for item in self.candidates:
            if item.selected:
                counts["selected"] += 1
            status = item.download_status
            if status in counts:
                counts[status] += 1
        return counts

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "directory": str(self.directory),
            "keywords": self.keywords,
            "groups": self.groups,
            "query": self.query_text,
            "year_from": self.year_from,
            "year_to": self.year_to,
            "sources": self.sources,
            "stats": dict(self.stats),
            "warnings": list(self.warnings),
            "created_at": self.created_at,
            "counts": self.summary_counts(),
            "deliverables": self.deliverable_paths(),
            "job": self.job.to_dict() if self.job else None,
        }


def write_rows(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def read_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


class RunStore:
    """内存运行态 + `paperdown/index.json` 落盘索引。"""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self._runs: dict[str, RunState] = {}
        self._lock = threading.Lock()

    # ---------- 目录与索引 ----------

    @property
    def root(self) -> Path:
        return self.config.output_root

    @property
    def runs_root(self) -> Path:
        return self.root / "runs"

    @property
    def index_path(self) -> Path:
        return self.root / "index.json"

    def ensure_dirs(self) -> None:
        self.runs_root.mkdir(parents=True, exist_ok=True)

    def create_run(
        self,
        *,
        keywords: str,
        groups: list[list[str]],
        query_text: str,
        year_from: int,
        year_to: int,
        sources: list[str],
    ) -> RunState:
        self.ensure_dirs()
        run_id = make_run_id(keywords)
        if (self.runs_root / run_id).exists():
            run_id = f"{run_id}_{uuid.uuid4().hex[:4]}"
        directory = self.runs_root / run_id
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "papers").mkdir(exist_ok=True)
        state = RunState(
            run_id=run_id,
            directory=directory,
            keywords=keywords,
            groups=groups,
            query_text=query_text,
            year_from=year_from,
            year_to=year_to,
            sources=sources,
            candidates=[],
            filtered_candidates=[],
        )
        with self._lock:
            self._runs[run_id] = state
        return state

    def get(self, run_id: str) -> RunState | None:
        if not RUN_ID_RE.match(str(run_id or "")):
            return None
        with self._lock:
            state = self._runs.get(run_id)
        if state is not None:
            return state
        directory = self.runs_root / run_id
        if not directory.is_dir():
            return None
        state = self._load_state(run_id, directory)
        if state is None:
            return None
        with self._lock:
            self._runs.setdefault(run_id, state)
            return self._runs[run_id]

    def _load_state(self, run_id: str, directory: Path) -> RunState | None:
        """从磁盘恢复一个历史运行（用于刷新页面后仍能看报告）。"""
        rows = read_rows(directory / "候选文献总表.csv")
        candidates = [Candidate.from_dict(row) for row in rows]
        meta: dict[str, Any] = {}
        meta_path = directory / "run.meta.json"
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                meta = {}
        return RunState(
            run_id=run_id,
            directory=directory,
            keywords=str(meta.get("keywords") or ""),
            groups=meta.get("groups") or [],
            query_text=str(meta.get("query") or ""),
            year_from=int(meta.get("year_from") or 0),
            year_to=int(meta.get("year_to") or 0),
            sources=list(meta.get("sources") or []),
            candidates=candidates,
            filtered_candidates=[],
            stats=meta.get("stats") or {},
            warnings=list(meta.get("warnings") or []),
            created_at=float(meta.get("created_at") or time.time()),
        )

    def save_run(self, state: RunState) -> None:
        """写出候选总表、运行元信息与索引。"""
        state.directory.mkdir(parents=True, exist_ok=True)
        write_rows(
            state.directory / "候选文献总表.csv",
            CANDIDATE_COLUMNS,
            [item.to_dict() for item in state.candidates],
        )
        meta = state.to_dict()
        meta["filtered_out"] = [
            {"journal": item.journal, "title": item.title, "doi": item.doi} for item in state.filtered_candidates[:200]
        ]
        (state.directory / "run.meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        config_snapshot = {
            "snapshot_of": str(self.config.path),
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "search": {
                "year_from": state.year_from,
                "year_to": state.year_to,
                "sources": state.sources,
                "exclude_types": list(self.config.search.exclude_types),
                "scope": self.config.search.scope,
                "email": self.config.search.email,
                "unpaywall_email": self.config.search.unpaywall_email,
            },
            # 注意：这里必须用脱敏版本，绝不能把 api_key 写进运行目录
            "llm": self.config.llm.sanitized(),
            "journals": [entry.to_dict() for entry in self.config.journals],
        }
        (state.directory / "config.snapshot.json").write_text(
            json.dumps(config_snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self._update_index(state)

    def _update_index(self, state: RunState) -> None:
        self.ensure_dirs()
        entries: list[dict[str, Any]] = []
        if self.index_path.exists():
            try:
                existing = json.loads(self.index_path.read_text(encoding="utf-8"))
                if isinstance(existing, list):
                    entries = [item for item in existing if isinstance(item, dict)]
            except json.JSONDecodeError:
                entries = []
        counts = state.summary_counts()
        entry = {
            "run_id": state.run_id,
            "keywords": state.keywords,
            "created_at": datetime.fromtimestamp(state.created_at).strftime("%Y-%m-%d %H:%M:%S"),
            "total": counts["total"],
            "success": counts["success_pdf"] + counts["success_html"] + counts["success_xml"],
            "directory": str(state.directory),
        }
        entries = [item for item in entries if item.get("run_id") != state.run_id]
        entries.append(entry)
        entries.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        self.index_path.write_text(json.dumps(entries[:200], ensure_ascii=False, indent=2), encoding="utf-8")

    def list_runs(self) -> list[dict[str, Any]]:
        if not self.index_path.exists():
            return []
        try:
            entries = json.loads(self.index_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return []
        return entries if isinstance(entries, list) else []

    def known_run_ids(self) -> set[str]:
        return {str(item.get("run_id")) for item in self.list_runs() if item.get("run_id")}
