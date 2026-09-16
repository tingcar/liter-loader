"""期刊白名单匹配。

只有期刊命中 `config.json` 的 `journals` 白名单，文献才会进入检索结果页。
匹配策略（按可信度从高到低）：

1. `issn`：ISSN 精确命中（去掉连字符、统一大小写）。
2. `name`：期刊名与白名单 `name`/`aliases` 归一化后完全一致。
3. `abbrev`：去掉停用词后逐词取前缀拼接，比较缩写串（容忍
   `Sci. Total Environ.` ↔ `Science of the Total Environment`）。
4. `fuzzy`：词集合 Jaccard 相似度 ≥ 0.8。

匹配失败时返回最相近的白名单条目，方便在界面上提示用户补白名单。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher

from .config import JournalMatchConfig
from .models import JournalEntry

STOPWORDS = {
    "of",
    "the",
    "and",
    "for",
    "in",
    "on",
    "to",
    "at",
    "a",
    "an",
    "de",
    "la",
    "le",
    "der",
    "die",
    "das",
    "und",
    "et",
    "du",
    "des",
}

FUZZY_THRESHOLD = 0.72
ABBREV_TOKEN_LEN = 3

_DASHES = dict.fromkeys(map(ord, "‐‑‒–—―−"), "-")


def normalize_issn(value: object) -> str:
    """归一化 ISSN：只保留字母数字，统一大写。"""
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(char for char in text if not unicodedata.combining(char))
    return re.sub(r"[^0-9A-Za-z]", "", text).upper()


def normalize_journal_name(value: object) -> str:
    """期刊名归一化：去变音符号、去标点、小写、合并空白。"""
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = text.translate(_DASHES).casefold()
    text = text.replace("&", " and ")
    text = re.sub(r"[\u2018\u2019\u201c\u201d]", " ", text)
    text = re.sub(r"[^0-9a-z\u4e00-\u9fff]+", " ", text)
    return " ".join(text.split())


def tokens(value: object) -> list[str]:
    """去掉停用词后的词序列（用于缩写比较）。"""
    words = [word for word in normalize_journal_name(value).split() if word]
    meaningful = [word for word in words if word not in STOPWORDS]
    return meaningful or words


def abbrev_signature(value: object) -> str:
    """逐词取前缀拼接出的缩写签名。"""
    parts = []
    for word in tokens(value):
        parts.append(word[:ABBREV_TOKEN_LEN] if len(word) > ABBREV_TOKEN_LEN else word)
    return "".join(parts)


def _token_set(value: object) -> set[str]:
    return set(tokens(value))


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


@dataclass
class MatchResult:
    """一次白名单匹配的结果。"""

    matched: bool
    method: str = ""
    entry: JournalEntry | None = None
    reason: str = ""
    closest: str = ""
    closest_score: float = 0.0

    @property
    def entry_name(self) -> str:
        return self.entry.name if self.entry else ""

    def to_dict(self) -> dict[str, object]:
        return {
            "matched": self.matched,
            "method": self.method,
            "journal": self.entry_name,
            "reason": self.reason,
            "closest": self.closest,
            "closest_score": round(self.closest_score, 3),
        }


def _entry_candidates(entry: JournalEntry) -> list[str]:
    names = [entry.name, *entry.aliases]
    return [name for name in names if name]


class JournalMatcher:
    """预计算好归一化索引的白名单匹配器。"""

    def __init__(self, journals: list[JournalEntry], options: JournalMatchConfig | None = None) -> None:
        self.options = options or JournalMatchConfig()
        self.journals = list(journals)
        self._by_issn: dict[str, JournalEntry] = {}
        self._by_norm_name: dict[str, JournalEntry] = {}
        self._abbrev_index: list[tuple[str, str, JournalEntry]] = []
        self._token_index: list[tuple[set[str], JournalEntry]] = []

        for entry in self.journals:
            for issn in entry.issn:
                key = normalize_issn(issn)
                if key:
                    self._by_issn.setdefault(key, entry)
            for name in _entry_candidates(entry):
                norm = normalize_journal_name(name)
                if not norm:
                    continue
                self._by_norm_name.setdefault(norm, entry)
                signature = abbrev_signature(name)
                if signature:
                    self._abbrev_index.append((signature, norm, entry))
                word_set = _token_set(name)
                if word_set:
                    self._token_index.append((word_set, entry))

    @property
    def is_empty(self) -> bool:
        return not self.journals

    def match(self, journal: object, issn: object = None, extra_issns: list[str] | None = None) -> MatchResult:
        """判断一个期刊是否在白名单内。"""
        if self.is_empty:
            return MatchResult(matched=False, reason="whitelist_empty")

        candidate_issns: list[str] = []
        if isinstance(issn, (list, tuple, set)):
            candidate_issns.extend(str(item) for item in issn)
        elif issn:
            candidate_issns.append(str(issn))
        if extra_issns:
            candidate_issns.extend(str(item) for item in extra_issns)

        for raw in candidate_issns:
            key = normalize_issn(raw)
            if key and key in self._by_issn:
                return MatchResult(matched=True, method="issn", entry=self._by_issn[key])

        norm_name = normalize_journal_name(journal)
        if not norm_name:
            closest, score = self._closest("")
            return MatchResult(matched=False, reason="journal_name_missing", closest=closest, closest_score=score)

        entry = self._by_norm_name.get(norm_name)
        if entry is not None:
            return MatchResult(matched=True, method="name", entry=entry)

        if self.options.allow_abbrev_prefix:
            signature = abbrev_signature(journal)
            if signature:
                for candidate_signature, _norm, candidate_entry in self._abbrev_index:
                    if signature == candidate_signature:
                        return MatchResult(matched=True, method="abbrev", entry=candidate_entry)
                for candidate_signature, _norm, candidate_entry in self._abbrev_index:
                    shorter, longer = sorted((signature, candidate_signature), key=len)
                    if len(shorter) >= 6 and shorter in longer:
                        return MatchResult(matched=True, method="abbrev", entry=candidate_entry)

        if self.options.allow_fuzzy_tokens:
            word_set = _token_set(journal)
            best_entry: JournalEntry | None = None
            best_score = 0.0
            for candidate_words, candidate_entry in self._token_index:
                score = _jaccard(word_set, candidate_words)
                if score > best_score:
                    best_score, best_entry = score, candidate_entry
            if best_entry is not None and best_score >= FUZZY_THRESHOLD:
                return MatchResult(matched=True, method="fuzzy", entry=best_entry)

        closest, score = self._closest(norm_name)
        return MatchResult(matched=False, reason="not_in_journal_whitelist", closest=closest, closest_score=score)

    def _closest(self, norm_name: str) -> tuple[str, float]:
        if not norm_name:
            return "", 0.0
        best_name = ""
        best_score = 0.0
        for candidate in self._by_norm_name:
            if not candidate:
                continue
            score = SequenceMatcher(None, norm_name, candidate).ratio()
            if score > best_score:
                best_score, best_name = score, candidate
        return best_name, best_score
