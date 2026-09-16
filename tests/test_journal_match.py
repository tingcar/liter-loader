"""期刊白名单匹配的单元测试。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import JournalMatchConfig  # noqa: E402
from app.journal_match import (  # noqa: E402
    JournalMatcher,
    abbrev_signature,
    normalize_issn,
    normalize_journal_name,
)
from app.models import JournalEntry  # noqa: E402


def build_matcher() -> JournalMatcher:
    journals = [
        JournalEntry(
            name="Science of the Total Environment",
            aliases=["Sci Total Environ", "Sci. Total Environ.", "STOTEN"],
            issn=["0048-9697", "1879-1026"],
        ),
        JournalEntry(name="Scientific Reports", aliases=["Sci Rep"], issn=["2045-2322"]),
        JournalEntry(name="Journal of Cleaner Production", issn=["0959-6526"]),
    ]
    return JournalMatcher(journals, JournalMatchConfig())


def test_normalize_issn() -> None:
    assert normalize_issn("0048-9697") == "00489697"
    assert normalize_issn(" 2045–2322 ") == "20452322"
    assert normalize_issn(None) == ""


def test_normalize_journal_name() -> None:
    assert normalize_journal_name("Sci. Total Environ.") == "sci total environ"
    assert normalize_journal_name("Science  of the   Total Environment") == "science of the total environment"
    assert normalize_journal_name("Science & Nature") == "science and nature"


def test_issn_match() -> None:
    matcher = build_matcher()
    result = matcher.match("Some Unknown Journal Name", issn=["0048-9697"])
    assert result.matched
    assert result.method == "issn"
    assert result.entry_name == "Science of the Total Environment"


def test_issn_list_match() -> None:
    matcher = build_matcher()
    result = matcher.match("Unknown", issn=["1234-5678", "2045-2322"])
    assert result.matched and result.entry_name == "Scientific Reports"


def test_exact_name_and_punctuation() -> None:
    matcher = build_matcher()
    assert matcher.match("Science of the Total Environment").method == "name"
    assert matcher.match("science   of the total environment").matched


def test_alias_abbreviation() -> None:
    matcher = build_matcher()
    result = matcher.match("Sci. Total Environ.")
    assert result.matched
    assert result.method in {"name", "abbrev"}


def test_abbrev_signature_stable() -> None:
    assert abbrev_signature("Science of the Total Environment") == abbrev_signature("Sci. Total Environ.")


def test_fuzzy_token_match() -> None:
    matcher = build_matcher()
    result = matcher.match("Journal of Cleaner Productions")
    assert result.matched, result
    assert result.entry_name == "Journal of Cleaner Production"
    assert result.method in {"abbrev", "fuzzy"}


def test_reject_unlisted_journal() -> None:
    matcher = build_matcher()
    result = matcher.match("Nature Medicine", issn=["1078-8956"])
    assert not result.matched
    assert result.reason == "not_in_journal_whitelist"


def test_reject_empty_journal_name() -> None:
    matcher = build_matcher()
    result = matcher.match("")
    assert not result.matched
    assert result.reason == "journal_name_missing"


def test_empty_whitelist_rejects_everything() -> None:
    matcher = JournalMatcher([], JournalMatchConfig())
    result = matcher.match("Scientific Reports", issn=["2045-2322"])
    assert not result.matched
    assert result.reason == "whitelist_empty"


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
