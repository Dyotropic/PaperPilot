"""Validated search filters shared by source adapters and the search UI."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from html import unescape
import math
import re
import unicodedata


_SPACE_RE = re.compile(r"\s+")
_WORD_RE = re.compile(r"[^\w]+", re.UNICODE)


def _plain_text(value: object) -> str:
    text = unescape(str(value or ""))
    return _SPACE_RE.sub(" ", unicodedata.normalize("NFKC", text)).strip()


def normalize_name(value: object) -> str:
    """Normalize a person/venue name while retaining word boundaries."""
    return _SPACE_RE.sub(" ", _WORD_RE.sub(" ", _plain_text(value).casefold())).strip()


def _author_aliases(paper: dict) -> list[str]:
    aliases = paper.get("_author_names") or []
    if isinstance(aliases, str):
        aliases = [aliases]
    if not aliases:
        aliases = re.split(r"\s*(?:,|;|\band\b)\s*", str(paper.get("authors") or ""))
    return [normalize_name(v) for v in aliases if normalize_name(v)]


def author_matches(paper: dict, author: str) -> bool:
    """Match a normalized phrase inside one author name, on whole words only."""
    wanted = normalize_name(author)
    if not wanted:
        return True
    needle = wanted.split()
    for alias in _author_aliases(paper):
        words = alias.split()
        width = len(needle)
        if any(words[i:i + width] == needle for i in range(len(words) - width + 1)):
            return True
    return False


def journal_matches(value: object, journal: str, *, allow_citation_suffix: bool = False) -> bool:
    """Match a complete venue name, allowing only citation details after it.

    For example, ``Nature, 2024, 1(2)`` matches ``Nature`` while
    ``Nature Communications`` does not.
    """
    actual = normalize_name(value)
    wanted = normalize_name(journal)
    if not wanted:
        return True
    if actual == wanted:
        return True
    if not allow_citation_suffix or not actual.startswith(wanted + " "):
        return False
    raw = _plain_text(value).casefold()
    raw_wanted = _plain_text(journal).casefold()
    suffix = raw[len(raw_wanted):].lstrip()
    return bool(re.match(
        r"^(?:(?:[,;:]\s*)?(?:\d|vol\.?\b|volume\b|no\.?\b|pp?\.?\b)|\(|\[)",
        suffix,
    ))


@dataclass(frozen=True, slots=True)
class SearchFilters:
    """Immutable, validated filters. Empty fields mean unrestricted."""

    year_from: int | None = None
    year_to: int | None = None
    author: str = ""
    journal: str = ""

    def __post_init__(self) -> None:
        limit = datetime.now().year + 1
        for label, value in (("起始年份", self.year_from), ("结束年份", self.year_to)):
            if value is not None and (type(value) is not int or not 1 <= value <= limit):
                raise ValueError(f"{label}必须在 1 到 {limit} 之间")
        if self.year_from is not None and self.year_to is not None and self.year_from > self.year_to:
            raise ValueError("起始年份不能晚于结束年份")
        object.__setattr__(self, "author", _plain_text(self.author))
        object.__setattr__(self, "journal", _plain_text(self.journal))
        for label, value in (("作者", self.author), ("期刊", self.journal)):
            if len(value) > 200:
                raise ValueError(f"{label}条件不能超过 200 个字符")
            if value and not normalize_name(value):
                raise ValueError(f"{label}条件必须包含文字或数字")

    @classmethod
    def from_raw(cls, year_from: object = "", year_to: object = "",
                 author: object = "", journal: object = "") -> "SearchFilters":
        def parse_year(value: object, label: str) -> int | None:
            if value is None:
                return None
            if isinstance(value, bool):
                raise ValueError(f"{label}必须是整数")
            text = str(value).strip()
            if not text:
                return None
            if len(text) > 6 or not text.isdecimal():
                raise ValueError(f"{label}必须是整数")
            return int(text)

        return cls(
            year_from=parse_year(year_from, "起始年份"),
            year_to=parse_year(year_to, "结束年份"),
            author=str(author or ""),
            journal=str(journal or ""),
        )

    @property
    def active(self) -> bool:
        return any((self.year_from is not None, self.year_to is not None,
                    self.author, self.journal))

    @property
    def cache_key(self) -> str:
        return "|".join((str(self.year_from or ""), str(self.year_to or ""),
                         normalize_name(self.author), normalize_name(self.journal)))

    def matches(self, paper: dict) -> bool:
        if self.year_from is not None or self.year_to is not None:
            year = paper.get("year")
            if type(year) is not int:
                return False
            if self.year_from is not None and year < self.year_from:
                return False
            if self.year_to is not None and year > self.year_to:
                return False
        if self.author and not author_matches(paper, self.author):
            return False
        if self.journal:
            journal = paper.get("journal")
            if not journal or not journal_matches(
                    journal, self.journal,
                    allow_citation_suffix=paper.get("source") == "arxiv"):
                return False
        return True


def coerce_filters(filters: SearchFilters | None, year_min: str = "",
                   year_max: str = "") -> SearchFilters | None:
    """Merge legacy year arguments with the new filter object consistently."""
    if filters is None:
        legacy = SearchFilters.from_raw(year_min, year_max)
        return legacy if legacy.active else None
    legacy = SearchFilters.from_raw(year_min, year_max)
    from_values = [v for v in (filters.year_from, legacy.year_from) if v is not None]
    to_values = [v for v in (filters.year_to, legacy.year_to) if v is not None]
    legacy_from = max(from_values) if from_values else None
    legacy_to = min(to_values) if to_values else None
    merged = SearchFilters(legacy_from, legacy_to, filters.author, filters.journal)
    return merged if merged.active else None


def search_limits(max_pages: object = None, request_timeout: object = None) -> tuple[int, float]:
    """Read and clamp network controls to safe finite ranges."""
    from paperpilot.config import config
    search = config.get("search", {}) or {}
    try:
        pages = int(max_pages if max_pages is not None else search.get("max_pages", 5))
    except (TypeError, ValueError, OverflowError):
        pages = 5
    try:
        timeout = float(request_timeout if request_timeout is not None
                        else search.get("request_timeout", 15))
    except (TypeError, ValueError, OverflowError):
        timeout = 15.0
    if not math.isfinite(timeout):
        timeout = 15.0
    return max(1, min(20, pages)), max(1.0, min(120.0, timeout))
