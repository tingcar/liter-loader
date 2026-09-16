"""全文获取：候选链接排序 + 只下载合法可直接访问的全文。

严格遵守 skill 的原则：
- 只尝试开放获取（OpenAlex / Europe PMC / Unpaywall）与 DOI 解析出的
  合法直连链接；
- 检测到登录页、订阅页、付费墙、短跳转页时如实标记为不可访问，
  绝不伪装成下载成功；
- 不绕过任何访问控制。

关于本机网络的一个实测限制：`europepmc.org`、`pmc.ncbi.nlm.nih.gov`、
`www.ncbi.nlm.nih.gov` 的网页域会被拦截（403），因此这些域名的链接
默认跳过，不浪费尝试次数；Europe PMC 的全文改用
`www.ebi.ac.uk/europepmc/webservices/rest/.../fullTextXML`（REST 域可访问）。
"""

from __future__ import annotations

import hashlib
import html as html_module
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote, urlparse

import requests

from .config import DownloadConfig
from .models import Candidate, Status, normalize_doi

USER_AGENT = "literature-downloader/1.0 (+lawful-access-check; local research tool)"

PAYWALL_MARKERS = [
    "institutional login",
    "subscribe to access",
    "purchase access",
    "access through your institution",
    "sign in to access",
    "login required",
    "shibboleth",
    "ezproxy",
    "temporarily unavailable",
    "get access",
    "buy this article",
    "rent this article",
    "institutional access",
]

BLOCKED_HOST_SUFFIXES = (
    "europepmc.org",
    "pmc.ncbi.nlm.nih.gov",
    "www.ncbi.nlm.nih.gov",
    "ncbi.nlm.nih.gov",
)

EPMC_REST_BASE = "https://www.ebi.ac.uk/europepmc/webservices/rest"
UNPAYWALL_API = "https://api.unpaywall.org/v2"

HTML_STUB_BYTES = 12000
MAX_DOWNLOAD_BYTES = 60 * 1024 * 1024


@dataclass
class FetchCandidate:
    """一条待尝试的全文链接。"""

    url: str
    source: str
    kind: str = "auto"  # auto | pdf | xml | landing


@dataclass
class FetchResult:
    """一条文献的下载结果。"""

    record_id: str
    status: str = Status.METADATA_ONLY.value
    failure_reason: str = ""
    final_path: str = ""
    final_url: str = ""
    content_format: str = ""
    access_route_used: str = ""
    attempts: int = 0
    tried: list[str] = field(default_factory=list)
    pending_html: tuple[bytes, str, str] | None = None  # (payload, final_url, route)，优先试 PDF，失败后落盘

    @property
    def success(self) -> bool:
        return self.status in {"success_pdf", "success_html", "success_xml"}

    def to_log_row(self, candidate: Candidate) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "doi": candidate.doi,
            "title": candidate.title,
            "journal": candidate.journal,
            "final_path": self.final_path,
            "final_url": self.final_url,
            "download_status": self.status,
            "failure_reason": self.failure_reason,
            "access_route_used": self.access_route_used,
            "content_format": self.content_format,
            "attempt_count": str(self.attempts),
        }


def _is_blocked_host(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == suffix or host.endswith("." + suffix) for suffix in BLOCKED_HOST_SUFFIXES)


def _looks_like_pdf_url(url: str) -> bool:
    path = unquote(urlparse(url).path).lower()
    return path.endswith(".pdf") or "/pdf" in path


def build_fetch_candidates(candidate: Candidate, *, include_unpaywall_email: str = "") -> list[FetchCandidate]:
    """按可信度排序全部候选链接，并顺带加入 Unpaywall / Europe PMC 全文。"""
    items: list[FetchCandidate] = []

    def add(url: object, source: str, kind: str = "auto") -> None:
        value = str(url or "").strip()
        if not value.startswith(("http://", "https://")):
            return
        if _is_blocked_host(value):
            return
        if any(item.url == value for item in items):
            return
        items.append(FetchCandidate(url=value, source=source, kind=kind))

    add(candidate.pdf_url_candidate, "openalex_oa_pdf", "pdf")
    add(candidate.landing_page_url, "landing_page")

    pmcid = str(candidate.pmcid or "").strip()
    if pmcid and not pmcid.upper().startswith("PMC"):
        pmcid = f"PMC{pmcid}"
    if pmcid:
        add(f"{EPMC_REST_BASE}/{pmcid}/fullTextXML", "europepmc_rest_xml", "xml")

    doi = normalize_doi(candidate.doi)
    if doi and include_unpaywall_email:
        add(f"{UNPAYWALL_API}/{doi}?email={include_unpaywall_email}", "unpaywall_lookup", "landing")

    if doi:
        add(f"https://doi.org/{doi}", "doi_resolver", "landing")

    if str(candidate.article_url or "").startswith(("http://", "https://")):
        add(candidate.article_url, "article_url")
    return items


def classify_payload(payload: bytes, content_type: str, final_url: str) -> str:
    """判断响应体到底是 PDF / XML / HTML / 二进制。"""
    head = payload[:400].lstrip()
    if head.startswith(b"%PDF"):
        return "pdf"
    lowered = f"{content_type} {final_url}".lower()
    if "application/pdf" in lowered:
        return "pdf"
    if "xml" in content_type.lower() or head.startswith(b"<?xml"):
        return "xml"
    if (
        "html" in content_type.lower()
        or b"<html" in payload[:2000].lower()
        or b"<!doctype html" in payload[:2000].lower()
    ):
        return "html"
    return "binary"


def looks_paywalled(payload: bytes) -> bool:
    text = payload[:80000].decode("utf-8", errors="ignore").lower()
    return any(marker in text for marker in PAYWALL_MARKERS)


# 部分出版社（MDPI/Akamai/Cloudflare 等）会用 200 返回一个极短的拦截页或
# meta-refresh 人机校验页，而不是 403。这类响应必须按「平台拒绝/需要人工验证」
# 处理，不能当成全文成功。
BLOCK_PAGE_MARKERS = [
    "access denied",
    "you don't have permission to access",
    "reference #18",
    "errors.edgesuite.net",
    "request blocked",
    "403 forbidden",
    "attention required! | cloudflare",
]

CHALLENGE_PAGE_MARKERS = [
    "bm-verify",
    "http-equiv=\"refresh\"",
    "http-equiv='refresh'",
    "checking your browser",
    "cf-browser-verification",
    "just a moment",
    "enable javascript and cookies to continue",
]


def looks_block_page(payload: bytes) -> bool:
    text = payload[:4000].decode("utf-8", errors="ignore").lower()
    return any(marker in text for marker in BLOCK_PAGE_MARKERS)


def looks_challenge_page(payload: bytes) -> bool:
    """人机校验/跳转验证页：URL 或正文里出现校验指纹。"""
    text = payload[:4000].decode("utf-8", errors="ignore").lower()
    return any(marker in text for marker in CHALLENGE_PAGE_MARKERS)


CITATION_PDF_RE = re.compile(
    r"""<meta[^>]+(?:name|property)\s*=\s*["'](?:citation_pdf_url|og:pdf)["'][^>]*content\s*=\s*["']([^"']+)["']""",
    re.I,
)
CITATION_PDF_RE_REVERSED = re.compile(
    r"""<meta[^>]+content\s*=\s*["']([^"']+)["'][^>]*(?:name|property)\s*=\s*["'](?:citation_pdf_url|og:pdf)["']""",
    re.I,
)
HREF_PDF_RE = re.compile(r"""href\s*=\s*["']([^"']+\.pdf[^"']*)["']""", re.I)


def extract_pdf_links(html_text: str, base_url: str) -> list[str]:
    """从 HTML 全文页里找合法的 PDF 直链（citation_pdf_url 与 .pdf 链接）。"""
    if not html_text:
        return []
    links: list[str] = []
    for match in CITATION_PDF_RE.finditer(html_text):
        links.append(match.group(1))
    for match in CITATION_PDF_RE_REVERSED.finditer(html_text):
        links.append(match.group(1))
    for match in HREF_PDF_RE.finditer(html_text[:400000]):
        links.append(match.group(1))

    resolved: list[str] = []
    for raw in links:
        value = html_module.unescape(raw.strip())
        if value.startswith("//"):
            value = "https:" + value
        elif value.startswith("/"):
            parsed = urlparse(base_url)
            value = f"{parsed.scheme}://{parsed.netloc}{value}"
        elif not value.startswith(("http://", "https://")):
            continue
        if _is_blocked_host(value) or value in resolved:
            continue
        resolved.append(value)
    return resolved


def stable_stem(candidate: Candidate) -> str:
    """安全文件名主干：第一作者 + 年份 + 题名片段。"""
    title = candidate.title or "untitled"
    year = str(candidate.year or "unknown")
    first_author = ""
    if candidate.authors:
        first_author = re.split(r"[;,]", candidate.authors)[0].strip()
        first_author = first_author.split(" ")[0] if first_author else ""
    raw = f"{first_author}_{year}_{title[:80]}" if first_author else f"{year}_{title[:80]}"
    stem = re.sub(r"[^A-Za-z0-9._\u4e00-\u9fff-]+", "_", raw).strip("_")
    return stem[:110] or hashlib.sha1(title.encode("utf-8")).hexdigest()[:12]


def _error_status(status_code: int) -> tuple[str, str]:
    if status_code in (401, 402, 403):
        return Status.INACCESSIBLE.value, f"http_{status_code}"
    if status_code in (404, 410):
        return Status.BROKEN_LINK.value, f"http_{status_code}"
    if status_code == 429:
        return Status.RATE_LIMITED.value, "http_429"
    return Status.FAILED.value, f"http_{status_code}"


class FullTextFetcher:
    """按候选顺序尝试下载一条文献的合法全文。"""

    def __init__(
        self,
        config: DownloadConfig,
        *,
        unpaywall_email: str = "",
        log_path: Path | None = None,
    ) -> None:
        self.config = config
        self.unpaywall_email = unpaywall_email
        self.log_path = log_path
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "application/pdf,text/html,application/xml,text/xml,*/*;q=0.8",
                "Accept-Language": "en,zh-CN;q=0.8",
            }
        )

    def _log_attempt(
        self,
        candidate: Candidate,
        url: str,
        route: str,
        status: str,
        reason: str = "",
        content_format: str = "",
        http_status: int | None = None,
        final_url: str = "",
        file_path: str = "",
    ) -> None:
        """把每次尝试追加进 JSONL 日志（崩溃也能保留已尝试记录）。"""
        if not self.log_path:
            return
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            row = {
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "record_id": candidate.record_id,
                "doi": candidate.doi,
                "title": candidate.title,
                "journal": candidate.journal,
                "url": url,
                "final_url": final_url,
                "route": route,
                "http_status": http_status,
                "status": status,
                "reason": reason,
                "content_format": content_format,
                "file": file_path,
            }
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        except OSError:
            pass

    # ---------- 底层请求 ----------

    def _get(self, url: str) -> tuple[bytes, str, str]:
        response = self.session.get(url, timeout=self.config.timeout_seconds, allow_redirects=True)
        if response.status_code >= 400:
            raise requests.HTTPError(f"HTTP {response.status_code}", response=response)
        payload = response.content
        if len(payload) > MAX_DOWNLOAD_BYTES:
            payload = payload[:MAX_DOWNLOAD_BYTES]
        return payload, response.headers.get("Content-Type", ""), response.url

    def _resolve_unpaywall(self, candidate: Candidate) -> list[str]:
        if not self.unpaywall_email:
            return []
        doi = normalize_doi(candidate.doi)
        if not doi:
            return []
        try:
            response = self.session.get(
                f"{UNPAYWALL_API}/{doi}",
                params={"email": self.unpaywall_email},
                timeout=self.config.timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
        except Exception:  # noqa: BLE001
            return []
        links: list[str] = []
        best = payload.get("best_oa_location") or {}
        if isinstance(best, dict) and best.get("url_for_pdf"):
            links.append(str(best["url_for_pdf"]))
        for location in payload.get("oa_locations") or []:
            if isinstance(location, dict) and location.get("url_for_pdf"):
                links.append(str(location["url_for_pdf"]))
        return [link for link in links if link.startswith(("http://", "https://")) and not _is_blocked_host(link)]

    # ---------- 主流程 ----------

    def fetch(self, candidate: Candidate, papers_dir: Path) -> FetchResult:
        result = FetchResult(record_id=candidate.record_id)
        papers_dir.mkdir(parents=True, exist_ok=True)
        queue = build_fetch_candidates(candidate, include_unpaywall_email=self.unpaywall_email)
        attempted: set[str] = set()
        index = 0

        while index < len(queue) and result.attempts < self.config.max_attempts_per_record:
            item = queue[index]
            index += 1
            if item.url in attempted:
                continue
            attempted.add(item.url)

            # Unpaywall 是「查询」而不是文件，先换成一串真实链接再继续
            if item.source == "unpaywall_lookup":
                extra = [link for link in self._resolve_unpaywall(candidate) if link not in attempted]
                for link in reversed(extra):
                    queue.insert(index, FetchCandidate(url=link, source="unpaywall_pdf", kind="pdf"))
                continue

            result.attempts += 1
            result.tried.append(item.url)
            try:
                payload, content_type, final_url = self._get(item.url)
            except requests.HTTPError as exc:
                status_code = getattr(getattr(exc, "response", None), "status_code", 0) or 0
                result.status, result.failure_reason = _error_status(status_code)
                result.final_url = item.url
                result.access_route_used = item.source
                self._log_attempt(candidate, item.url, item.source, result.status, result.failure_reason, http_status=status_code)
                time.sleep(self.config.delay_seconds)
                continue
            except requests.Timeout:
                result.status, result.failure_reason = Status.METADATA_ONLY.value, "timeout"
                result.final_url = item.url
                result.access_route_used = item.source
                self._log_attempt(candidate, item.url, item.source, result.status, result.failure_reason)
                time.sleep(self.config.delay_seconds)
                continue
            except Exception as exc:  # noqa: BLE001
                result.status, result.failure_reason = Status.METADATA_ONLY.value, f"{type(exc).__name__}"
                result.final_url = item.url
                result.access_route_used = item.source
                self._log_attempt(candidate, item.url, item.source, result.status, result.failure_reason)
                time.sleep(self.config.delay_seconds)
                continue

            kind = classify_payload(payload, content_type, final_url)

            if kind == "html":
                if looks_paywalled(payload):
                    result.status, result.failure_reason = Status.INACCESSIBLE.value, "paywall_or_login_detected"
                    result.final_url = final_url
                    result.access_route_used = item.source
                    self._log_attempt(candidate, item.url, item.source, result.status, result.failure_reason, "html", final_url=final_url)
                    time.sleep(self.config.delay_seconds)
                    # 付费墙页面上如果还藏着 citation_pdf_url，仍可用于下一轮（合法 OA 场景）
                    text = payload[:200000].decode("utf-8", errors="ignore")
                    pdf_hints = extract_pdf_links(text, final_url)
                    for link in pdf_hints:
                        if link not in attempted:
                            queue.insert(index, FetchCandidate(url=link, source=f"{item.source}_pdf_hint", kind="pdf"))
                    continue
                if len(payload) < HTML_STUB_BYTES:
                    if looks_block_page(payload):
                        result.status, result.failure_reason = Status.INACCESSIBLE.value, "platform_blocked_direct_download"
                    elif looks_challenge_page(payload):
                        result.status, result.failure_reason = Status.INACCESSIBLE.value, "platform_bot_challenge"
                    else:
                        result.status, result.failure_reason = Status.INACCESSIBLE.value, "html_stub_not_fulltext"
                    result.final_url = final_url
                    result.access_route_used = item.source
                    self._log_attempt(candidate, item.url, item.source, result.status, result.failure_reason, "html", final_url=final_url)
                    time.sleep(self.config.delay_seconds)
                    continue
                text = payload[:400000].decode("utf-8", errors="ignore")
                hints = [link for link in extract_pdf_links(text, final_url) if link not in attempted]
                if hints:
                    # 先把 HTML 全文暂存到内存，优先继续尝试 PDF；若 PDF 成功，
                    # 就只保留 PDF，避免同一篇同时落两个文件。
                    if result.pending_html is None:
                        result.pending_html = (payload, final_url, item.source)
                    for link in reversed(hints):
                        queue.insert(index, FetchCandidate(url=link, source=f"{item.source}_pdf_hint", kind="pdf"))
                    time.sleep(self.config.delay_seconds)
                    continue
                self._store(candidate, payload, "html", papers_dir, result, final_url, item.source)
                return result
            if kind == "xml":
                if looks_paywalled(payload) and len(payload) < HTML_STUB_BYTES:
                    result.status, result.failure_reason = Status.INACCESSIBLE.value, "paywall_or_login_detected"
                    result.final_url = final_url
                    result.access_route_used = item.source
                    self._log_attempt(candidate, item.url, item.source, result.status, result.failure_reason, "xml", final_url=final_url)
                    time.sleep(self.config.delay_seconds)
                    continue
                self._store(candidate, payload, "xml", papers_dir, result, final_url, item.source)
                return result
            if kind == "pdf":
                self._store(candidate, payload, "pdf", papers_dir, result, final_url, item.source)
                return result

            result.status, result.failure_reason = Status.FAILED.value, "unexpected_binary_payload"
            result.final_url = final_url
            result.access_route_used = item.source
            self._log_attempt(candidate, item.url, item.source, result.status, result.failure_reason, kind, final_url=final_url)
            time.sleep(self.config.delay_seconds)

        if result.success:
            return result

        # PDF 没拿到，但过程里存下过 HTML 全文 → 作为「已保存网页全文」交付
        if result.pending_html is not None:
            payload, final_url, route = result.pending_html
            prior_status = result.status
            self._store(candidate, payload, "html", papers_dir, result, final_url, route)
            if prior_status and not result.success:
                result.status = prior_status
                result.final_path = ""
            return result

        if not attempted:
            result.status, result.failure_reason = Status.METADATA_ONLY.value, "no_candidate_url"
        return result

    def _store(
        self,
        candidate: Candidate,
        payload: bytes,
        kind: str,
        papers_dir: Path,
        result: FetchResult,
        final_url: str,
        route: str,
    ) -> None:
        extension = {"pdf": ".pdf", "html": ".html", "xml": ".xml"}[kind]
        stem = stable_stem(candidate)
        path = papers_dir / f"{stem}{extension}"
        counter = 1
        while path.exists() and path.stat().st_size > 0:
            path = papers_dir / f"{stem}_{counter}{extension}"
            counter += 1
        path.write_bytes(payload)
        result.status = {"pdf": Status.SUCCESS_PDF.value, "html": Status.SUCCESS_HTML.value, "xml": Status.SUCCESS_XML.value}[kind]
        result.failure_reason = ""
        result.final_path = str(path)
        result.final_url = final_url
        result.content_format = kind
        result.access_route_used = route
        self._log_attempt(
            candidate,
            final_url,
            route,
            result.status,
            content_format=kind,
            final_url=final_url,
            file_path=str(path),
        )

    def close(self) -> None:
        self.session.close()


def fetch_many(
    candidates: Iterable[Candidate],
    papers_dir: Path,
    config: DownloadConfig,
    *,
    unpaywall_email: str = "",
    on_result: Any = None,
    log_path: Path | None = None,
) -> list[FetchResult]:
    """依次下载多条文献（顺序执行，便于控制请求频率）。"""
    fetcher = FullTextFetcher(config, unpaywall_email=unpaywall_email, log_path=log_path)
    results: list[FetchResult] = []
    try:
        for candidate in candidates:
            result = fetcher.fetch(candidate, papers_dir)
            results.append(result)
            if callable(on_result):
                on_result(candidate, result)
    finally:
        fetcher.close()
    return results
