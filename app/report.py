"""生成三份交付文件：下载报告.html、文章地址总表.csv、文献清单.md。

设计原则（沿用 skill 的输出经验）：
- 面向用户的三份文件都直接包含期刊指标（影响因子、分区、指标年份、指标来源、
  索引情况），不让用户在文献清单和期刊指标表之间来回对照；
- 状态一律用中文解释，且「没下成」要写清楚原因，不把失败包装成成功；
- CSV 用 utf-8-sig，Excel 双击可直接正确显示中文。
"""

from __future__ import annotations

import csv
import html
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import AppConfig
from .models import Candidate, Status, next_step, reason_text, status_text
from .query_scope import DEFAULT_SCOPE, SOURCE_LABEL_ZH, field_labels, get_scope
from .runs import RunState

REPORT_NAME = "下载报告.html"
ADDRESS_TABLE_NAME = "文章地址总表.csv"
CHECKLIST_NAME = "文献清单.md"
DOWNLOAD_LOG_NAME = "下载日志.csv"

DOWNLOAD_LOG_COLUMNS = [
    "record_id",
    "doi",
    "title",
    "journal",
    "final_path",
    "final_url",
    "download_status",
    "failure_reason",
    "access_route_used",
    "content_format",
    "attempt_count",
]

ADDRESS_TABLE_COLUMNS = [
    "序号",
    "题名",
    "年份",
    "期刊或来源",
    "影响因子",
    "JCR分区",
    "指标年份",
    "指标来源",
    "索引情况",
    "DOI",
    "文章地址",
    "是否勾选",
    "是否下载成功",
    "全文状态",
    "文件或原因",
    "下一步建议",
]

PRIORITY_LABEL = {"high": "高优先级", "medium": "中优先级", "low": "低优先级"}

PENDING_STATUSES = {
    Status.PENDING.value,
    Status.NOT_SELECTED.value,
    Status.METADATA_ONLY.value,
    Status.INACCESSIBLE.value,
    Status.BROKEN_LINK.value,
    Status.RATE_LIMITED.value,
    Status.FAILED.value,
    Status.EXCLUDED.value,
}


def esc(value: object) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def article_address(candidate: Candidate) -> str:
    return (
        candidate.landing_page_url
        or candidate.article_url
        or candidate.pdf_url_candidate
        or (f"https://doi.org/{candidate.doi}" if candidate.doi else "")
    )


def metric_cell(value: str) -> str:
    return value.strip() if value and value.strip() else "待核验"


def file_or_reason(candidate: Candidate) -> str:
    if candidate.final_path:
        name = Path(candidate.final_path).name
        return f"{name}（{reason_text(candidate.failure_reason)}）" if candidate.failure_reason else name
    if candidate.failure_reason:
        return reason_text(candidate.failure_reason)
    return ""


def is_marked_metric(candidate: Candidate) -> bool:
    return any([candidate.impact_factor, candidate.jcr_quartile, candidate.metric_year, candidate.metric_source])


def build_address_rows(run: RunState) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for index, candidate in enumerate(run.candidates, start=1):
        status = candidate.download_status
        rows.append(
            {
                "序号": str(index),
                "题名": candidate.title,
                "年份": str(candidate.year or ""),
                "期刊或来源": candidate.journal or "（无期刊信息）",
                "影响因子": metric_cell(candidate.impact_factor),
                "JCR分区": metric_cell(candidate.jcr_quartile),
                "指标年份": metric_cell(candidate.metric_year),
                "指标来源": metric_cell(candidate.metric_source),
                "索引情况": metric_cell(candidate.indexing),
                "DOI": candidate.doi,
                "文章地址": article_address(candidate),
                "是否勾选": "是" if candidate.selected else "否",
                "是否下载成功": "是" if status.startswith("success") else "否",
                "全文状态": status_text(status),
                "文件或原因": file_or_reason(candidate),
                "下一步建议": next_step(status, candidate.failure_reason),
            }
        )
    return rows


def write_address_table(run: RunState) -> Path:
    path = run.directory / ADDRESS_TABLE_NAME
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=ADDRESS_TABLE_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(build_address_rows(run))
    return path


def _counts(run: RunState) -> dict[str, int]:
    counts = {
        "total": len(run.candidates),
        "selected": 0,
        "success_pdf": 0,
        "success_html": 0,
        "success_xml": 0,
        "inaccessible": 0,
        "metadata_only": 0,
        "broken_link": 0,
        "rate_limited": 0,
        "failed": 0,
        "not_selected": 0,
        "excluded": 0,
        "metrics": 0,
    }
    for candidate in run.candidates:
        if candidate.selected:
            counts["selected"] += 1
        status = candidate.download_status
        if status in counts:
            counts[status] += 1
        if is_marked_metric(candidate):
            counts["metrics"] += 1
    counts["success"] = counts["success_pdf"] + counts["success_html"] + counts["success_xml"]
    counts["unresolved"] = counts["total"] - counts["success"]
    return counts


def scope_summary(run: RunState) -> tuple[str, str]:
    """返回（检索范围中文名，字段列表中文）。"""
    stats = run.stats or {}
    label = str(stats.get("scope_label") or "").strip()
    fields = str(stats.get("scope_fields") or "").strip()
    if not label:
        key = str(stats.get("scope") or DEFAULT_SCOPE)
        definition = get_scope(key)
        label = definition.label
        fields = fields or field_labels(list(definition.fields))
    return label, fields


def build_keyword_lines(run: RunState) -> list[str]:
    """列出本次真正参与检索的关键词，区分用户输入与 AI 扩展。"""
    stats = run.stats or {}
    user_terms = [str(term) for term in (stats.get("user_terms") or [])]
    llm_terms = [str(term) for term in (stats.get("llm_terms") or [])]
    if not user_terms and not llm_terms:
        return []
    lines = ["## 本次检索关键词", ""]
    lines.append(f"- 用户输入（{len(user_terms)}）：{'、'.join(user_terms) if user_terms else '（无）'}")
    if llm_terms:
        model = stats.get("llm_model") or "（未记录）"
        lines.append(f"- AI 扩展（{len(llm_terms)}，模型 {model}）：{'、'.join(llm_terms)}")
    else:
        lines.append("- AI 扩展：本次未使用")
    lines.append("")

    detail = stats.get("keyword_groups") or []
    if detail:
        lines.append("| 概念块 | 用户关键词 | AI 扩展关键词 |")
        lines.append("| ---: | --- | --- |")
        for index, group in enumerate(detail, start=1):
            terms = group.get("terms") or []
            user_cell = "、".join(item["term"] for item in terms if item.get("source") == "user") or "—"
            llm_cell = "、".join(item["term"] for item in terms if item.get("source") == "llm") or "—"
            lines.append(f"| {index} | {user_cell} | {llm_cell} |")
        lines.append("")
    return lines


def build_checklist_markdown(run: RunState, config: AppConfig, counts: dict[str, int]) -> str:
    groups_text = " ； ".join(" / ".join(group) for group in run.groups)
    whitelist = "、".join(entry.name for entry in config.journals) or "（未配置）"
    scope_label, scope_fields = scope_summary(run)
    lines: list[str] = [
        "# 文献清单",
        "",
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 运行编号：`{run.run_id}`",
        f"- 关键词分组：{groups_text or '（未填写）'}",
        f"- 检索范围：{scope_label}（关键词匹配字段：{scope_fields}）",
        f"- 通用检索式：`{run.query_text}`",
        f"- 年份范围：{run.year_from}–{run.year_to}",
        f"- 数据源：{'、'.join(run.sources) or '（无）'}",
        f"- 期刊白名单（{len(config.journals)} 种）：{whitelist}",
        f"- 白名单外被过滤：{run.stats.get('filtered_out', 0)} 条",
        "",
        "## 结果概览",
        "",
        "| 指标 | 数量 |",
        "| --- | ---: |",
        f"| 白名单内候选文献 | {counts['total']} |",
        f"| 本次勾选 | {counts['selected']} |",
        f"| 已下载 PDF | {counts['success_pdf']} |",
        f"| 已保存网页全文 | {counts['success_html']} |",
        f"| 已保存 XML 全文 | {counts['success_xml']} |",
        f"| 需后续处理 | {counts['unresolved']} |",
        f"| 已标注期刊指标 | {counts['metrics']} |",
        "",
    ]

    source_queries = (run.stats or {}).get("source_queries") or {}
    if source_queries:
        lines.extend(["## 各数据源实际检索式", ""])
        for name, query in source_queries.items():
            lines.append(f"- **{name}**：`{query}`")
        lines.append("")

    lines.extend(build_keyword_lines(run))

    if run.filtered_candidates:
        lines.extend(["## 白名单外被过滤的期刊（前若干条）", ""])
        for item in run.stats.get("unmatched_journals", [])[:15]:
            lines.append(f"- {item['journal']}：{item['count']} 条（示例：{item['example']}）")
        lines.append("")

    for priority in ("high", "medium", "low"):
        bucket = [item for item in run.candidates if item.priority == priority]
        if not bucket:
            continue
        lines.extend([f"## {PRIORITY_LABEL[priority]}（{len(bucket)} 条）", ""])
        for index, candidate in enumerate(bucket, start=1):
            selection = "已勾选" if candidate.selected else "未勾选"
            lines.append(f"### {index}. {candidate.title or '（无题名）'}")
            lines.append("")
            lines.append(f"- 作者：{candidate.authors or '未知'}")
            lines.append(f"- 年份：{candidate.year or '未知'}")
            metrics = []
            if candidate.impact_factor:
                metrics.append(f"影响因子 {candidate.impact_factor}")
            if candidate.jcr_quartile:
                metrics.append(f"JCR {candidate.jcr_quartile}")
            if candidate.metric_year or candidate.metric_source:
                metrics.append(f"{candidate.metric_year or ''} {candidate.metric_source or ''}".strip())
            if candidate.indexing:
                metrics.append(f"索引：{candidate.indexing}")
            lines.append(
                f"- 期刊：{candidate.journal or '（无期刊信息）'}"
                + ("（" + "；".join(metrics) + "）" if metrics else "（指标待核验）")
            )
            if candidate.issn:
                lines.append(f"- ISSN：{candidate.issn}")
            if candidate.doi:
                lines.append(f"- DOI：https://doi.org/{candidate.doi}")
            address = article_address(candidate)
            if address:
                lines.append(f"- 文章地址：{address}")
            lines.append(f"- 来源库：{candidate.source_database or '未知'}")
            lines.append(f"- 白名单命中方式：{candidate.matched_by or '未命中'}")
            lines.append(f"- 关键词命中字段：{candidate.keyword_field_hits or '无'}")
            lines.append(f"- 开放获取：{candidate.oa_status or '未知'}")
            lines.append(f"- 关键词命中：{candidate.keyword_group_hits or candidate.keyword_include_hits or '无'}")
            lines.append(f"- 选择状态：{selection}")
            lines.append(f"- 全文状态：{status_text(candidate.download_status)}")
            if candidate.final_path:
                lines.append(f"- 文件：`{candidate.final_path}`")
            if candidate.failure_reason:
                lines.append(f"- 原因：{reason_text(candidate.failure_reason)}")
            lines.append(f"- 下一步：{next_step(candidate.download_status, candidate.failure_reason)}")
            lines.append("")

    lines.extend(["## 全部文献总表", "", "| # | 题名 | 年份 | 期刊 | IF | 分区 | DOI | 全文状态 |", "| ---: | --- | ---: | --- | --- | --- | --- | --- |"])
    for index, candidate in enumerate(run.candidates, start=1):
        lines.append(
            "| {index} | {title} | {year} | {journal} | {impact} | {quartile} | {doi} | {status} |".format(
                index=index,
                title=(candidate.title or "").replace("|", "\\|"),
                year=candidate.year or "",
                journal=(candidate.journal or "").replace("|", "\\|"),
                impact=metric_cell(candidate.impact_factor),
                quartile=metric_cell(candidate.jcr_quartile),
                doi=candidate.doi,
                status=status_text(candidate.download_status),
            )
        )
    lines.append("")

    unresolved = [item for item in run.candidates if not item.download_status.startswith("success")]
    if unresolved:
        lines.extend(["## 待处理文献与合法获取路线", ""])
        for candidate in unresolved[:60]:
            lines.append(
                f"- {candidate.title or '（无题名）'}（{candidate.year or '年份未知'}）："
                f"{status_text(candidate.download_status)} → {next_step(candidate.download_status, candidate.failure_reason)}"
            )
        lines.append("")

    lines.extend(
        [
            "## 检索日志",
            "",
            "| 日期 | 数据库 | 检索范围 | 检索式 | 过滤条件 | 命中数 | 保留数 | 备注 |",
            "| --- | --- | --- | --- | --- | ---: | ---: | --- |",
        ]
    )
    stats = run.stats
    per_source = stats.get("by_source") or {}
    scope_label, _scope_fields = scope_summary(run)
    for source in run.sources:
        source_query = (stats.get("source_queries") or {}).get(source) or run.query_text
        lines.append(
            f"| {datetime.now().strftime('%Y-%m-%d')} | {source} | {scope_label} | `{source_query}` | "
            f"白名单期刊 {len(config.journals)} 种；年份 {run.year_from}-{run.year_to} | "
            f"{per_source.get(source, 0)} | {stats.get('after_whitelist', 0)} | 白名单过滤 {stats.get('filtered_out', 0)} 条 |"
        )
    lines.append("")
    if run.warnings:
        lines.extend(["## 提示与警告", ""])
        for warning in run.warnings:
            lines.append(f"- {warning}")
        lines.append("")
    lines.extend(
        [
            "## 合规说明",
            "",
            "本清单只包含通过合法途径获取的开放获取全文或出版社直连全文。"
            "遇到需要订阅、登录或平台拒绝直连的文献，请使用学校图书馆、机构 VPN、馆际互借或直接联系作者。",
            "",
        ]
    )
    return "\n".join(lines)


def write_checklist(run: RunState, config: AppConfig, counts: dict[str, int]) -> Path:
    path = run.directory / CHECKLIST_NAME
    path.write_text(build_checklist_markdown(run, config, counts), encoding="utf-8")
    return path


def build_report_html(run: RunState, config: AppConfig) -> str:
    counts = _counts(run)
    cards = [
        ("白名单期刊", len(config.journals)),
        ("候选文献", counts["total"]),
        ("本次勾选", counts["selected"]),
        ("已下载 PDF", counts["success_pdf"]),
        ("网页/XML 全文", counts["success_html"] + counts["success_xml"]),
        ("需后续处理", counts["unresolved"]),
        ("已标注指标", counts["metrics"]),
    ]
    card_html = "\n".join(
        f"<div class='card'><div class='num'>{number}</div><div class='label'>{label}</div></div>"
        for label, number in cards
    )

    def table(rows: list[Candidate], unresolved: bool) -> str:
        body = []
        for index, candidate in enumerate(rows, start=1):
            status = candidate.download_status
            css = "ok" if status == "success_pdf" else ("warn" if status.startswith("success") else "bad")
            note = next_step(status, candidate.failure_reason) if unresolved else Path(candidate.final_path).name
            body.append(
                "<tr>"
                f"<td class='idx'>{index}</td>"
                f"<td class='year'>{esc(candidate.year or '')}</td>"
                f"<td><div class='paper-title'>{esc(candidate.title)}</div>"
                f"<div class='doi'>{esc(candidate.doi)}</div></td>"
                f"<td>{esc(candidate.journal)}</td>"
                f"<td>{esc(metric_text(candidate))}</td>"
                f"<td>{esc(candidate.keyword_field_hits or '—')}</td>"
                f"<td><span class='badge {css}'>{esc(status_text(status))}</span></td>"
                f"<td>{esc(note)}</td>"
                "</tr>"
            )
        return "\n".join(body) or "<tr><td colspan='7'>无</td></tr>"

    success_rows = [item for item in run.candidates if item.download_status.startswith("success")]
    pending_rows = [item for item in run.candidates if not item.download_status.startswith("success")]
    groups_text = " ； ".join(" / ".join(group) for group in run.groups)
    scope_label, scope_fields = scope_summary(run)
    source_queries = (run.stats or {}).get("source_queries") or {}
    source_query_html = "".join(
        f"<li><b>{esc(SOURCE_LABEL_ZH.get(name, name))}</b>：<code>{esc(query)}</code></li>"
        for name, query in source_queries.items()
    )
    stats = run.stats or {}
    user_terms = [str(term) for term in (stats.get("user_terms") or [])]
    llm_terms = [str(term) for term in (stats.get("llm_terms") or [])]
    keyword_html = ""
    if user_terms or llm_terms:
        llm_line = (
            f"{len(llm_terms)} 个（模型 {esc(stats.get('llm_model') or '未记录')}）：{esc('、'.join(llm_terms))}"
            if llm_terms
            else "本次未使用"
        )
        keyword_html = (
            "<div class='tipbox'><b>本次检索关键词</b><ul>"
            f"<li><b>用户输入</b>（{len(user_terms)}）：{esc('、'.join(user_terms) or '（无）')}</li>"
            f"<li><b>AI 扩展</b>：{llm_line}</li>"
            "</ul></div>"
        )
    filtered_summary = "".join(
        f"<li>{esc(item['journal'])}：{item['count']} 条（示例：{esc(item['example'])}）</li>"
        for item in (run.stats.get("unmatched_journals") or [])[:8]
    )
    warnings_html = "".join(f"<li>{esc(warning)}</li>" for warning in run.warnings)

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>文献下载报告 - {esc(run.keywords or run.run_id)}</title>
<style>
body {{ font-family: "Microsoft YaHei", "Noto Sans CJK SC", Arial, sans-serif; margin: 34px; color: #172033; line-height: 1.55; background: #ffffff; }}
h1 {{ font-size: 28px; margin: 0 0 6px; }}
h2 {{ margin-top: 30px; border-bottom: 2px solid #e5e7eb; padding-bottom: 7px; font-size: 19px; }}
.sub {{ color: #667085; margin-bottom: 18px; font-size: 13px; }}
.cards {{ display: grid; grid-template-columns: repeat(7, 1fr); gap: 10px; margin: 18px 0 22px; }}
.card {{ border: 1px solid #d7dee8; border-radius: 8px; padding: 12px; background: #f8fafc; }}
.num {{ font-size: 24px; font-weight: 800; color: #0f766e; }}
.label {{ color: #475467; font-size: 12px; }}
.note {{ background: #fff7ed; border: 1px solid #fdba74; padding: 12px 14px; border-radius: 8px; margin: 14px 0; font-size: 13px; }}
.tip {{ background: #ecfdf5; border: 1px solid #86efac; padding: 12px 14px; border-radius: 8px; margin: 14px 0; font-size: 13px; }}
table {{ width: 100%; border-collapse: collapse; font-size: 11.5px; table-layout: fixed; }}
th, td {{ border: 1px solid #d7dee8; padding: 7px; vertical-align: top; overflow-wrap: anywhere; }}
th {{ background: #eef2f7; text-align: left; color: #344054; }}
.idx {{ width: 30px; text-align: center; font-weight: 700; }}
.year {{ width: 44px; }}
.paper-title {{ font-weight: 700; color: #111827; }}
.doi {{ color: #667085; margin-top: 4px; font-size: 10.5px; }}
.badge {{ display: inline-block; border-radius: 999px; padding: 2px 7px; font-size: 11px; font-weight: 700; white-space: nowrap; }}
.ok {{ background: #dcfce7; color: #166534; }}
.warn {{ background: #fef3c7; color: #92400e; }}
.bad {{ background: #fee2e2; color: #991b1b; }}
code {{ background: #f3f4f6; padding: 2px 5px; border-radius: 4px; }}
.kv td {{ border: none; padding: 2px 8px 2px 0; font-size: 13px; }}
@media print {{
  body {{ margin: 12mm; }}
  .cards {{ grid-template-columns: repeat(4, 1fr); }}
  table {{ font-size: 9.5px; }}
  h2 {{ break-after: avoid; }}
  tr {{ break-inside: avoid; }}
}}
</style>
</head>
<body>
<h1>文献下载报告</h1>
<div class="sub">
运行编号 <code>{esc(run.run_id)}</code> ·
生成时间 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ·
关键词分组 {esc(groups_text or '（未填写）')} ·
检索范围 {esc(scope_label)}（{esc(scope_fields)}） ·
年份 {run.year_from}–{run.year_to} ·
数据源 {esc('、'.join(run.sources) or '（无）')}
</div>
<div class="cards">{card_html}</div>

<table class="kv">
<tr><td><b>检索范围</b></td><td>{esc(scope_label)} · 关键词匹配字段：{esc(scope_fields)}</td></tr>
<tr><td><b>通用检索式</b></td><td><code>{esc(run.query_text)}</code></td></tr>
<tr><td><b>期刊白名单</b></td><td>{len(config.journals)} 种 · {esc('、'.join(entry.name for entry in config.journals) or '（未配置）')}</td></tr>
<tr><td><b>白名单外过滤</b></td><td>{run.stats.get('filtered_out', 0)} 条</td></tr>
<tr><td><b>全文存放</b></td><td><code>{esc(run.papers_dir)}</code></td></tr>
</table>

{f'<div class="tipbox"><b>各数据源实际检索式</b><ul>{source_query_html}</ul></div>' if source_query_html else ''}
{keyword_html}

<div class="tip">只有期刊命中 <code>config.json</code> 白名单的文献才会出现在本报告里。需要新增期刊时，编辑 <code>config.json</code> 的 <code>journals</code> 字段后重新检索即可，无需重启服务。</div>
{f'<div class="note"><b>白名单外命中较多的期刊</b><ul>{filtered_summary}</ul>如果其中有你要的期刊，把它加进白名单后重新检索。</div>' if filtered_summary else ''}
{f'<div class="note"><b>提示</b><ul>{warnings_html}</ul></div>' if warnings_html else ''}
<div class="note">没下载成功通常不是程序坏了，而是出版社或数据库限制自动直连下载（ACS、Elsevier、SpringerLink 等尤其明显）。遇到 403、登录页、订阅页时，请使用学校图书馆/VPN、馆际互借、机构仓储或联系作者。本工具不绕过付费墙。</div>

<h2>已经拿到的全文（{len(success_rows)} 条）</h2>
<table>
<thead><tr><th>#</th><th>年份</th><th>题名 / DOI</th><th style="width:112px">期刊/来源</th><th style="width:112px">影响因子/分区</th><th style="width:74px">命中字段</th><th style="width:82px">状态</th><th>文件</th></tr></thead>
<tbody>{table(success_rows, False)}</tbody>
</table>

<h2>需要后续处理的文献（{len(pending_rows)} 条）</h2>
<table>
<thead><tr><th>#</th><th>年份</th><th>题名 / DOI</th><th style="width:112px">期刊/来源</th><th style="width:112px">影响因子/分区</th><th style="width:74px">命中字段</th><th style="width:82px">状态</th><th>建议</th></tr></thead>
<tbody>{table(pending_rows, True)}</tbody>
</table>

<h2>给学生的下一步</h2>
<ol>
<li>先读已下载 PDF 中和课题最贴近的 5–8 篇，边读边做笔记。</li>
<li>影响因子/分区只用于初筛，不要代替论文质量判断；重点看方法、数据、结论和可复现性。</li>
<li>对未下载成功但很重要的文献，用 DOI 走学校图书馆、机构 VPN 或馆际互借。</li>
<li>需要打开原文网页时，看 <code>文章地址总表.csv</code>；需要逐条备注时看 <code>文献清单.md</code>。</li>
<li>影响因子/分区字段在没核验时显示「待核验」，请在 <code>config.json</code> 的 journals 里补充并标注年份与来源。</li>
</ol>

<h2>合规说明</h2>
<p>本报告由本地 literature-downloader 网页版生成，只统计合法直连或开放获取的下载结果。工具不会绕过付费墙、不共享账号、不使用盗版来源。</p>
</body>
</html>
"""


def metric_text(candidate: Candidate) -> str:
    parts = []
    if candidate.impact_factor:
        parts.append(f"IF {candidate.impact_factor}")
    if candidate.jcr_quartile:
        parts.append(candidate.jcr_quartile)
    if candidate.indexing:
        parts.append(candidate.indexing)
    if candidate.metric_year or candidate.metric_source:
        source = " / ".join(value for value in [candidate.metric_year, candidate.metric_source] if value)
        if source:
            parts.append(source)
    return "；".join(parts) if parts else "待核验"


def write_report(run: RunState, config: AppConfig) -> str:
    (run.directory / REPORT_NAME).write_text(build_report_html(run, config), encoding="utf-8")
    return REPORT_NAME


def write_download_log(run: RunState) -> Path:
    """写出每次下载尝试的原始日志（技术追溯用）。"""
    path = run.directory / DOWNLOAD_LOG_NAME
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=DOWNLOAD_LOG_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for candidate in run.candidates:
            writer.writerow(
                {
                    "record_id": candidate.record_id,
                    "doi": candidate.doi,
                    "title": candidate.title,
                    "journal": candidate.journal,
                    "final_path": candidate.final_path,
                    "final_url": candidate.landing_page_url or candidate.article_url,
                    "download_status": candidate.download_status,
                    "failure_reason": candidate.failure_reason,
                    "access_route_used": candidate.access_route_used,
                    "content_format": candidate.content_format,
                    "attempt_count": str(candidate.attempt_count),
                }
            )
    return path


def generate_all(run: RunState, config: AppConfig) -> dict[str, str]:
    """生成三份交付文件（+ 一份技术日志），返回 {文件名: 路径}。"""
    counts = _counts(run)
    write_address_table(run)
    write_checklist(run, config, counts)
    write_report(run, config)
    write_download_log(run)
    return {
        REPORT_NAME: str(run.directory / REPORT_NAME),
        ADDRESS_TABLE_NAME: str(run.directory / ADDRESS_TABLE_NAME),
        CHECKLIST_NAME: str(run.directory / CHECKLIST_NAME),
    }


def stats_payload(run: RunState, config: AppConfig) -> dict[str, Any]:
    """给前端用的统计信息。"""
    counts = _counts(run)
    counts["filtered_out"] = run.stats.get("filtered_out", 0)
    counts["journals"] = len(config.journals)
    counts["unresolved"] = counts["total"] - counts["success"]
    return counts
