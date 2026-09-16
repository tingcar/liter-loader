"""全文分类、链接抽取与候选排序的单元测试（不联网）。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.fulltext import (  # noqa: E402
    build_fetch_candidates,
    classify_payload,
    extract_pdf_links,
    looks_block_page,
    looks_challenge_page,
    looks_paywalled,
    stable_stem,
)
from app.models import Candidate, next_step, reason_text, status_text  # noqa: E402


def test_classify_pdf_magic_bytes() -> None:
    assert classify_payload(b"%PDF-1.7\n...", "application/octet-stream", "https://x/y") == "pdf"


def test_classify_by_content_type_and_extension() -> None:
    assert classify_payload(b"binary", "application/pdf", "https://x/y") == "pdf"
    assert classify_payload(b"hello", "text/html; charset=utf-8", "https://x/y") == "html"
    assert classify_payload(b'<?xml version="1.0"?>', "application/xml", "https://x/y") == "xml"
    assert classify_payload(b"\x00\x01", "application/octet-stream", "https://x/y") == "binary"


def test_paywall_detection() -> None:
    assert looks_paywall_payload(b"<html>Access through your institution</html>")
    assert not looks_paywall_payload(b"<html><body>" + b"full text " * 500 + b"</body></html>")


def looks_paywall_payload(payload: bytes) -> bool:
    return looks_paywalled(payload)


def test_block_and_challenge_detection() -> None:
    assert looks_block_page(b"<HTML><HEAD><TITLE>Access Denied</TITLE></HEAD>")
    assert not looks_block_page(b"<html><body>normal article page</body></html>")
    assert looks_challenge_page(b'<meta http-equiv="refresh" content="5; URL=/x?bm-verify=AAQ">')
    assert not looks_challenge_page(b"<html><body>normal article page</body></html>")


def test_extract_pdf_links_meta_and_relative() -> None:
    html = (
        '<meta name="citation_pdf_url" content="https://pub.example.org/a.pdf">'
        '<a href="/files/b.pdf">pdf</a>'
        '<meta content="https://pub.example.org/c.pdf" property="og:pdf">'
    )
    links = extract_pdf_links(html, "https://pub.example.org/article/1")
    assert "https://pub.example.org/a.pdf" in links
    assert "https://pub.example.org/files/b.pdf" in links
    assert "https://pub.example.org/c.pdf" in links


def test_build_fetch_candidates_skips_blocked_hosts() -> None:
    candidate = Candidate(
        record_id="x1",
        title="T",
        doi="10.1000/abc",
        pmcid="PMC1234567",
        pdf_url_candidate="https://europepmc.org/articles/PMC1234567?pdf=render",
        landing_page_url="https://example.org/landing",
        article_url="https://example.org/article",
    )
    items = build_fetch_candidates(candidate)
    urls = [item.url for item in items]
    assert all("europepmc.org" not in url for url in urls), urls
    assert any("ebi.ac.uk/europepmc/webservices/rest/PMC1234567/fullTextXML" in url for url in urls)
    assert any("doi.org/10.1000/abc" in url for url in urls)
    assert urls[0] == "https://example.org/landing", "landing_page 应排在 doi 解析之前"


def test_build_fetch_candidates_normalizes_pmcid() -> None:
    candidate = Candidate(record_id="x2", title="T", pmcid="1234567")
    urls = [item.url for item in build_fetch_candidates(candidate)]
    assert any("/PMC1234567/fullTextXML" in url for url in urls)


def test_stable_stem_sanitizes() -> None:
    candidate = Candidate(record_id="x3", title='A/B: "test" <paper>', authors="Zhang, San", year=2024)
    stem = stable_stem(candidate)
    assert "/" not in stem and ":" not in stem and '"' not in stem
    assert "2024" in stem


def test_status_and_reason_text() -> None:
    assert status_text("success_pdf") == "已下载 PDF"
    assert reason_text("platform_bot_challenge") == "平台要求人机验证，无法自动直连，请在浏览器里打开或走学校库"
    assert "学校" in next_step("inaccessible", "http_403")
    assert next_step("success_pdf") == "已拿到 PDF，优先阅读"


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
