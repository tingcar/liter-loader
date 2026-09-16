"""运行目录与路径安全的单元测试。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import AppConfig, DownloadConfig  # noqa: E402
from app.models import Candidate  # noqa: E402
from app.runs import (  # noqa: E402
    RunStore,
    UnsafePathError,
    make_run_id,
    safe_join,
    slugify,
)


def test_slugify_keeps_chinese() -> None:
    assert slugify("LLM 在环境领域中的应用") == "LLM-在环境领域中的应用"
    assert slugify("///") == "search"


def test_make_run_id_format() -> None:
    run_id = make_run_id("LLM; climate")
    assert len(run_id.split("_")) >= 3
    assert "LLM-climate" in run_id


def test_safe_join_allows_plain_names(tmp: Path) -> None:
    target = safe_join(tmp, "下载报告.html")
    assert target == (tmp / "下载报告.html").resolve()


def test_safe_join_rejects_traversal(tmp: Path) -> None:
    for bad in ["../x", "..\\x", "a/../../b", "sub/file.csv", "/etc/passwd", "C:/windows/system32", ""]:
        try:
            safe_join(tmp, bad)
        except UnsafePathError:
            continue
        raise AssertionError(f"应当拒绝：{bad!r}")


def test_store_creates_run_and_index(tmp: Path) -> None:
    config = AppConfig(download=DownloadConfig(output_root=str(tmp)))
    store = RunStore(config)
    state = store.create_run(
        keywords="LLM; climate",
        groups=[["LLM"], ["climate"]],
        query_text="(LLM) AND (climate)",
        year_from=2021,
        year_to=2026,
        sources=["openalex"],
    )
    assert state.directory.is_dir()
    assert (state.directory / "papers").is_dir()
    state.candidates = [
        Candidate(record_id="abc123", title="Test paper", year=2024, journal="Scientific Reports"),
    ]
    store.save_run(state)
    assert (state.directory / "候选文献总表.csv").exists()
    assert (state.directory / "config.snapshot.json").exists()
    assert store.list_runs(), "索引应记录本次运行"


def test_store_reloads_run_from_disk(tmp: Path) -> None:
    config = AppConfig(download=DownloadConfig(output_root=str(tmp)))
    store = RunStore(config)
    state = store.create_run(
        keywords="LLM",
        groups=[["LLM"]],
        query_text="(LLM)",
        year_from=2021,
        year_to=2026,
        sources=["openalex"],
    )
    state.candidates = [Candidate(record_id="rid1", title="Reloaded", year=2023, journal="Scientific Reports")]
    store.save_run(state)

    fresh = RunStore(config)
    loaded = fresh.get(state.run_id)
    assert loaded is not None
    assert len(loaded.candidates) == 1
    assert loaded.candidates[0].title == "Reloaded"
    assert loaded.keywords == "LLM"


def test_store_rejects_bad_run_id(tmp: Path) -> None:
    config = AppConfig(download=DownloadConfig(output_root=str(tmp)))
    store = RunStore(config)
    assert store.get("../../etc") is None
    assert store.get("nope") is None


def test_summary_counts(tmp: Path) -> None:
    config = AppConfig(download=DownloadConfig(output_root=str(tmp)))
    store = RunStore(config)
    state = store.create_run(
        keywords="x", groups=[["x"]], query_text="x", year_from=2021, year_to=2026, sources=[]
    )
    state.candidates = [
        Candidate(record_id="a", title="A", download_status="success_pdf", selected=True),
        Candidate(record_id="b", title="B", download_status="inaccessible"),
        Candidate(record_id="c", title="C", download_status="not_selected"),
    ]
    counts = state.summary_counts()
    assert counts["total"] == 3
    assert counts["selected"] == 1
    assert counts["success_pdf"] == 1
    assert counts["inaccessible"] == 1


def main() -> int:
    import shutil
    import traceback

    # 注意：DSH 文件沙箱不允许写系统临时目录，因此在工作区内建临时目录。
    workspace = Path(__file__).resolve().parent.parent
    tmp_root = workspace / ".tmp_tests"
    if tmp_root.exists():
        shutil.rmtree(tmp_root, ignore_errors=True)
    tmp_root.mkdir(parents=True, exist_ok=True)

    failures = 0
    for name, func in sorted(globals().items()):
        if not (name.startswith("test_") and callable(func)):
            continue
        target = tmp_root / name
        target.mkdir(parents=True, exist_ok=True)
        try:
            if func.__code__.co_argcount == 1:
                func(target)
            else:
                func()
            print(f"PASS {name}")
        except Exception:  # noqa: BLE001
            failures += 1
            print(f"FAIL {name}")
            traceback.print_exc()
    shutil.rmtree(tmp_root, ignore_errors=True)
    print(f"\n{'OK' if failures == 0 else f'{failures} failed'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
