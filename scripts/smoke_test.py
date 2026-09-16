#!/usr/bin/env python
"""端到端冒烟测试（需要联网）。

流程：读取/构造配置 → 真实多源检索 → 白名单过滤 → 自动勾选开放获取条目
→ 真实下载 → 生成三份交付文件 → 逐项断言。

用法：
    python scripts/smoke_test.py                    # 用根目录 config.json
    python scripts/smoke_test.py --keywords "..." --limit 3
退出码 0 表示全部通过。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(WORKSPACE))

# Windows 控制台默认编码可能是 cp1252，中文输出会报错，这里强制 UTF-8。
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass

from app.analysis import apply_whitelist, merge_records, split_keywords  # noqa: E402
from app.config import load_config  # noqa: E402
from app.fulltext import fetch_many  # noqa: E402
from app.journal_match import JournalMatcher  # noqa: E402
from app.report import ADDRESS_TABLE_COLUMNS, generate_all  # noqa: E402
from app.query_scope import field_labels, get_scope, normalize_scope  # noqa: E402
from app.runs import RunStore, read_rows  # noqa: E402
from app.sources.base import SearchContext  # noqa: E402
from app.sources.runner import run_search  # noqa: E402

DEFAULT_KEYWORDS = "large language model|LLM|ChatGPT; environmental science|climate|sustainability"


def main() -> int:
    parser = argparse.ArgumentParser(description="liter-loader 端到端冒烟测试")
    parser.add_argument("--config", default=str(WORKSPACE / "config.json"))
    parser.add_argument("--keywords", default=DEFAULT_KEYWORDS)
    parser.add_argument("--limit", type=int, default=3, help="实际尝试下载的条数")
    parser.add_argument("--per-source", type=int, default=50, help="每个数据源取多少条（默认 50）")
    parser.add_argument(
        "--scope",
        default="title_abstract",
        help="关键词匹配范围：title / title_abstract / title_abstract_keywords / all",
    )
    parser.add_argument(
        "--no-auto-expand",
        action="store_true",
        help="白名单内没有可下载条目时，不自动临时扩充白名单（默认会自动扩充以验证下载链路）",
    )
    args = parser.parse_args()

    failures: list[str] = []

    def check(condition: bool, message: str) -> None:
        if condition:
            print(f"  [OK] {message}")
        else:
            failures.append(message)
            print(f"  [FAIL] {message}")

    config = load_config(args.config)
    print(f"配置文件：{config.path}")
    print(f"期刊白名单：{len(config.journals)} 种")
    for warning in config.warnings:
        print(f"  [warn] {warning}")
    check(bool(config.journals), "配置里有期刊白名单")

    if args.per_source:
        config.search.max_results_per_source = args.per_source

    groups = split_keywords(args.keywords)
    check(bool(groups), f"关键词拆分成功：{groups}")
    scope = normalize_scope(args.scope)
    # 打分阶段读取 config.search.scope，这里保持一致，避免「范围」与「打分字段」不一致。
    config.search.scope = scope
    context = SearchContext(
        keywords_raw=args.keywords,
        groups=groups,
        year_from=config.search.year_from,
        year_to=config.search.year_to,
        max_results_per_source=config.search.max_results_per_source,
        search=config.search,
        scope=scope,
    )
    definition = get_scope(scope)
    print(f"检索范围：{definition.label}（{field_labels(list(definition.fields))}）")
    print(f"通用检索式：{context.query_text}")
    for name in config.search.sources:
        if config.search.sources.get(name):
            print(f"  {name:10s}: {context.query_for(name)[:150]}")

    print("\n[1/6] 多源检索")
    outcomes = run_search(context, config.search.sources)
    for name, outcome in outcomes.items():
        print(f"  {name:10s} {outcome.count:4d} 条  {outcome.elapsed_seconds:5.2f}s  {outcome.error or ''}")
        for warning in outcome.warnings:
            print(f"      [warn] {warning}")
    total_raw = sum(outcome.count for outcome in outcomes.values())
    check(total_raw > 0, f"至少一个数据源返回结果（共 {total_raw} 条）")

    records = [record for outcome in outcomes.values() for record in outcome.records]
    buckets, duplicates = merge_records(records)
    print(f"\n[2/6] 合并去重：{total_raw} → {len(buckets)} 条（合并 {duplicates} 条重复）")

    print("\n[3/6] 关键词打分 + 期刊白名单过滤")
    matcher = JournalMatcher(config.journals, config.journal_match)
    filtered = apply_whitelist(buckets, groups, config, matcher)
    for warning in filtered.warnings:
        print(f"  [warn] {warning}")
    print(f"  白名单内 {len(filtered.candidates)} 条；白名单外过滤 {len(filtered.filtered_candidates)} 条")
    check(
        all(candidate.matched_by for candidate in filtered.candidates),
        "结果页所有文献都命中期刊白名单",
    )
    if filtered.filtered_candidates:
        for item in filtered.filtered_candidates[:3]:
            check(
                item.matched_by == "",
                f"被过滤的文献确实没命中白名单：{item.journal or '(无期刊信息)'}",
            )
    check(bool(filtered.candidates), "白名单内至少有一条候选文献")
    if scope == "title":
        title_hits = [
            item
            for item in filtered.candidates
            if item.keyword_group_hits and item.keyword_field_hits not in {"", "标题"}
        ]
        check(not title_hits, f"仅标题范围下命中字段只应是「标题」（异常 {len(title_hits)} 条）")
    if filtered.candidates:
        check(
            all(item.keyword_field_hits for item in filtered.candidates if item.keyword_group_hits),
            "命中的关键词都标注了命中字段",
        )

    downloadable = [item for item in filtered.candidates if item.has_oa_signal and not item.exclusion_reason_if_any]
    expanded_journals = False
    if not downloadable and not args.no_auto_expand:
        # 默认 config.json 里的示例白名单很窄，可能一条 OA 都命中不到。
        # 这里临时扩充白名单（不写回 config.json），只为验证「下载 → 交付文件」链路。
        from app.models import JournalEntry

        print("\n  [说明] 当前白名单内没有可下载的开放获取条目，临时扩充白名单以验证下载链路。")
        for extra in [
            JournalEntry(name="Sustainability", issn=["2071-1050"], indexing="SCIE"),
            JournalEntry(name="Environmental Science & Technology", issn=["0013-936X", "1520-5851"], indexing="SCIE"),
            JournalEntry(name="Journal of Environmental Management", issn=["0301-4797"], indexing="SCIE"),
            JournalEntry(name="Environmental Science and Ecotechnology", issn=["2666-4984"]),
            JournalEntry(name="BMC Psychology", issn=["2050-7283"]),
        ]:
            config.journals.append(extra)
        matcher = JournalMatcher(config.journals, config.journal_match)
        filtered = apply_whitelist(buckets, groups, config, matcher)
        expanded_journals = True
        downloadable = [item for item in filtered.candidates if item.has_oa_signal and not item.exclusion_reason_if_any]
        print(f"  扩充后白名单内 {len(filtered.candidates)} 条，其中可下载 {len(downloadable)} 条")

    store = RunStore(config)
    run = store.create_run(
        keywords=args.keywords,
        groups=groups,
        query_text=context.query_text,
        year_from=config.search.year_from,
        year_to=config.search.year_to,
        sources=list(outcomes.keys()),
    )
    run.candidates = filtered.candidates
    run.filtered_candidates = filtered.filtered_candidates
    run.warnings = filtered.warnings + [w for outcome in outcomes.values() for w in outcome.warnings]
    run.stats = {
        "raw": total_raw,
        "deduped": len(buckets),
        "duplicates": duplicates,
        "after_whitelist": len(filtered.candidates),
        "filtered_out": len(filtered.filtered_candidates),
        "by_source": {name: outcome.count for name, outcome in outcomes.items()},
        "unmatched_journals": filtered.stats.unmatched_journals,
        "whitelist_expanded_for_test": expanded_journals,
        "scope": scope,
        "scope_label": definition.label,
        "scope_fields": field_labels(list(definition.fields)),
        "source_queries": {name: context.query_for(name) for name in outcomes},
        "unscoped_sources": [],
        "user_terms": [term for group in groups for term in group],
        "llm_terms": [],
        "keyword_groups": [
            {"index": index, "terms": [{"term": term, "source": "user"} for term in group]}
            for index, group in enumerate(groups)
        ],
        "llm_used": False,
        "llm_model": "",
    }
    print(f"  运行目录：{run.directory}")

    print("\n[4/6] 自动勾选开放获取条目并下载")
    selection = downloadable[: args.limit]
    for item in selection:
        item.selected = True
    check(bool(selection), f"选中 {len(selection)} 条待下载")
    if selection:
        for item in selection:
            print(f"  - {item.title[:70]}  [{item.journal}]")
        results = fetch_many(
            selection,
            run.papers_dir,
            config.download,
            unpaywall_email=config.search.unpaywall_email,
            log_path=run.directory / "下载尝试日志.jsonl",
        )
        for item, result in zip(selection, results):
            item.download_status = result.status
            item.failure_reason = result.failure_reason
            item.final_path = result.final_path
            item.content_format = result.content_format
            item.access_route_used = result.access_route_used
            item.attempt_count = result.attempts
            print(f"  → {result.status:14s} {result.failure_reason or '-'} {Path(result.final_path).name if result.final_path else ''}")
        check(any(result.success for result in results), "至少下载成功一条全文")
        check(
            all(result.failure_reason != "no_candidate_url" for result in results),
            "每条都至少尝试过一个全文入口",
        )
    for item in run.candidates:
        if item.download_status == "pending":
            item.download_status = "not_selected"

    print("\n[5/6] 生成三份交付文件")
    paths = generate_all(run, config)
    for name, path in paths.items():
        size = Path(path).stat().st_size if Path(path).exists() else 0
        print(f"  {name}  ({size} 字节)")
        check(size > 100, f"{name} 已生成且非空")

    print("\n[6/6] 校验交付文件内容")
    rows = read_rows(Path(paths["文章地址总表.csv"]))
    check(bool(rows), "文章地址总表.csv 有数据行")
    check(list(rows[0].keys()) == ADDRESS_TABLE_COLUMNS, "文章地址总表.csv 列名与约定一致")
    check(all(row["文章地址"] for row in rows), "每行都有文章地址")
    check(
        all(row["影响因子"] for row in rows),
        "每行都带影响因子字段（未核验时为「待核验」）",
    )
    checklist = Path(paths["文献清单.md"]).read_text(encoding="utf-8")
    check("# 文献清单" in checklist and "## 结果概览" in checklist, "文献清单.md 结构完整")
    check("## 本次检索关键词" in checklist and "用户输入" in checklist, "文献清单.md 记录了本次使用的关键词")
    report = Path(paths["下载报告.html"]).read_text(encoding="utf-8")
    check("<!doctype html>" in report and "文献下载报告" in report, "下载报告.html 是完整 HTML")
    papers = sorted(path.name for path in run.papers_dir.iterdir()) if run.papers_dir.exists() else []
    check(bool(papers) or not selection, f"papers/ 里有下载到的全文文件：{papers[:3]}")

    store.save_run(run)
    meta = json.loads((run.directory / "run.meta.json").read_text(encoding="utf-8"))
    check(meta["run_id"] == run.run_id, "run.meta.json 记录了本次运行")
    check((run.directory / "候选文献总表.csv").exists(), "候选文献总表.csv 已落盘")
    check((run.directory / "config.snapshot.json").exists(), "config.snapshot.json 已落盘（可复现本次白名单）")
    log_csv = run.directory / "下载日志.csv"
    check(log_csv.exists(), "下载日志.csv 已生成")
    if log_csv.exists():
        log_rows = read_rows(log_csv)
        check(len(log_rows) == len(run.candidates), f"下载日志.csv 每个候选一行（{len(log_rows)}/{len(run.candidates)}）")
    attempt_log = run.directory / "下载尝试日志.jsonl"
    check(attempt_log.exists() and attempt_log.stat().st_size > 0, "下载尝试日志.jsonl 记录了每次尝试的 URL")

    print(f"\n交付文件目录：{run.directory}")
    if failures:
        print(f"\n{len(failures)} 项未通过：")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("\n全部检查通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
