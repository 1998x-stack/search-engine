"""Safe, deterministic multilingual query planning.

Tokenizer tokens are diagnostic hints, NOT a substitute for OpenSearch analyzers.
No LLM, network lookup, or untrusted raw OpenSearch DSL. Rewrites cannot remove
operator-specified ACL/source/host/time constraints.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timezone
import re
import unicodedata
from typing import Literal

ALLOWED_LANGUAGES = frozenset({"zh", "en", "mixed", "und"})
REWRITE_MODES = frozenset({"off", "suggest", "expand"})
MAX_ATOMS = 32
MAX_PHRASES = 8
MAX_EXCLUDED = 8
MAX_EXPANSIONS = 4

# Conservative demonstration lexicon for token diagnostics only;
# index-side CJK analysis remains the authoritative tokenizer.
ZH_TERMS = frozenset({
    "搜索引擎", "搜索", "引擎", "索引", "倒排索引", "检索", "中文", "英文",
    "中文分词", "分词", "推荐系统", "机器学习", "深度学习", "爬虫",
    "分布式", "查询", "排序", "算法", "怎么", "如何", "教程",
    "入门", "最新", "安装", "性能", "比较", "与", "数据", "系统",
})
SPELL_VOCAB = frozenset({
    "python", "opensearch", "elasticsearch", "sqlite", "search", "engine",
    "index", "retrieval", "ranking", "document", "crawler", "tokenizer",
    "latency", "benchmark", "inverted",
})
# Explicitly bounded synonyms; not translations of arbitrary user text.
SYNONYMS = {
    "搜索引擎": ("检索系统",),
    "倒排索引": ("inverted index",),
    "爬虫": ("网页抓取",),
    "query understanding": ("查询理解",),
    "分词": ("tokenization",),
}
TOKEN_RE = re.compile(
    r"c\+\+|c#|\.net\b|[a-zA-Z][a-zA-Z0-9._+#-]*|\d+(?:\.\d+)*|[\u3400-\u9fff]+",
    re.IGNORECASE,
)
HAN_RE = re.compile(r"[\u3400-\u9fff]")
LATIN_RE = re.compile(r"[A-Za-z]")
SOURCE_RE = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")
HOST_RE = re.compile(r"^(?=.{1,253}$)[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$")


@dataclass(frozen=True)
class QueryPlan:
    original: str
    normalized: str
    positive: tuple[str, ...]
    phrases: tuple[str, ...]
    excluded: tuple[str, ...]
    language: str | None
    detected_language: str
    source_id: str | None
    host: str | None
    after_ms: int | None
    before_ms: int | None
    intent: str
    tokens: tuple[str, ...]
    rewrite_mode: Literal["off", "suggest", "expand"]
    suggestion: dict[str, str] | None
    expansions: tuple[str, ...]

    @property
    def retrieval_text(self) -> str:
        return " ".join((*self.positive, *self.phrases))

    def public_summary(self) -> dict:
        return {
            "normalized": self.normalized,
            "detected_language": self.detected_language,
            "intent": self.intent,
            "tokens": list(self.tokens),
            "suggestion": self.suggestion,
            "expansions": list(self.expansions),
            "rewrite_mode": self.rewrite_mode,
            "filters": {key: val for key, val in (
                ("language", self.language),
                ("source_id", self.source_id), ("site", self.host),
                ("after_ms", self.after_ms), ("before_ms", self.before_ms)
            ) if val is not None},
        }


def _atoms(q: str) -> list[tuple[str, bool]]:
    """Quote-aware bounded lexer: -\"negative phrase\" remains a single atom."""
    out: list[tuple[str, bool]] = []
    i = 0
    while i < len(q):
        if q[i].isspace():
            i += 1
            continue
        neg = q[i] == "-"
        if neg:
            i += 1
            if i >= len(q) or q[i].isspace():
                raise ValueError("Dangling negative query term")
        quoted = q[i] == '"'
        if quoted:
            i += 1
            start = i
            while i < len(q) and q[i] != '"':
                i += 1
            if i == len(q):
                raise ValueError("Unterminated quoted phrase")
            content = q[start:i]
            i += 1
            if i < len(q) and not q[i].isspace():
                raise ValueError("Quoted phrases must end at a token boundary")
        else:
            start = i
            while i < len(q) and not q[i].isspace():
                if q[i] == '"':
                    raise ValueError("Quote must start a phrase")
                i += 1
            content = q[start:i]
        if not content:
            raise ValueError("Empty query token/phrase")
        out.append(("-" + content if neg else content, quoted))
        if len(out) > MAX_ATOMS:
            raise ValueError("Query has too many clauses")
    return out


def _segment_han(run: str) -> list[str]:
    tokens: list[str] = []
    i = 0
    while i < len(run):
        match = next((term for term in sorted(ZH_TERMS, key=lambda term: (-len(term), term))
                      if run.startswith(term, i)), None)
        if match:
            tokens.append(match)
            i += len(match)
        elif i + 1 < len(run):
            tokens.append(run[i:i + 2])
            i += 1  # overlapping bigrams for unknown Chinese words
        else:
            tokens.append(run[i])
            i += 1
    return tokens


def diagnostic_tokens(text: str) -> tuple[str, ...]:
    out = []
    for m in TOKEN_RE.finditer(text):
        raw = m.group(0)
        if HAN_RE.search(raw):
            out.extend(_segment_han(raw))
        else:
            out.append(raw.casefold())
    # Deduplicate while preserving order, avoiding uncontrolled repeated tokens.
    return tuple(dict.fromkeys(out[:64]))


def _spell_distance(a: str, b: str) -> int:
    """Damerau-Levenshtein OSA for typo suggestions; tokens capped at 20 chars."""
    rows = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(len(a) + 1):
        rows[i][0] = i
    for j in range(len(b) + 1):
        rows[0][j] = j
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            rows[i][j] = min(rows[i - 1][j] + 1, rows[i][j - 1] + 1,
                             rows[i - 1][j - 1] + (a[i - 1] != b[j - 1]))
            if (i > 1 and j > 1 and a[i - 1] == b[j - 2]
                    and a[i - 2] == b[j - 1]):
                rows[i][j] = min(rows[i][j], rows[i - 2][j - 2] + 1)
    return rows[-1][-1]


def suggest_typo(terms: tuple[str, ...]) -> dict[str, str] | None:
    for word in terms:
        lowered = word.casefold()
        if not re.fullmatch(r"[a-z]{4,20}", lowered) or lowered in SPELL_VOCAB:
            continue
        choices = [( _spell_distance(lowered, cand), cand)
                   for cand in SPELL_VOCAB if abs(len(lowered) - len(cand)) <= 1]
        choices = [(dist, cand) for dist, cand in choices if dist == 1]
        if len(choices) == 1:
            return {"original": word, "candidate": choices[0][1]}
    return None


def _date_ms(value: str) -> int:
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError
        return int(datetime.combine(parsed, time.min, timezone.utc).timestamp() * 1000)
    except (ValueError, OverflowError) as exc:
        raise ValueError("Date filter must be YYYY-MM-DD") from exc


def _site(value: str) -> str:
    if any(c in value for c in "/:@?#") or ".." in value:
        raise ValueError("site: expects an exact hostname, not a URL or domain suffix")
    try:
        value = value.rstrip(".").encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ValueError("Invalid site: hostname") from exc
    if not HOST_RE.fullmatch(value) or any(not label or label.startswith("-") or label.endswith("-")
                                          or len(label) > 63 for label in value.split(".")):
        raise ValueError("Invalid site: hostname")
    return value


def plan_query(q: str, *, language: str | None = None, source_id: str | None = None,
               rewrite_mode: str = "suggest") -> QueryPlan:
    if not isinstance(q, str) or not q.strip() or len(q) > 200:
        raise ValueError("Query must have 1–200 characters")
    if any(unicodedata.category(c) in {"Cc", "Cs", "Cf"} for c in q):
        raise ValueError("Query contains control or format characters")
    if rewrite_mode not in REWRITE_MODES:
        raise ValueError("Unsupported rewrite mode")
    if language is not None and language not in ALLOWED_LANGUAGES:
        raise ValueError("Unsupported language filter")
    if source_id is not None and not SOURCE_RE.fullmatch(source_id):
        raise ValueError("Invalid source_id filter")

    normalized = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", q)).strip()
    positive: list[str] = []
    phrases: list[str] = []
    excluded: list[str] = []
    filters: dict[str, str] = {}
    for atom, quoted in _atoms(normalized):
        if not quoted and ":" in atom and not atom.startswith("-"):
            key, val = atom.split(":", 1)
            if key in ("lang", "source", "site", "after", "from", "before"):
                if not val:
                    raise ValueError(f"Empty {key}: filter")
                k = {"lang":"language", "source":"source_id", "from":"after"}.get(key, key)
                if k in filters:
                    raise ValueError(f"Duplicate {key}: filter")
                filters[k] = val
                continue
        if atom.startswith("-"):
            excluded.append(atom[1:])
        elif quoted:
            phrases.append(atom)
        else:
            positive.append(atom)
    if not positive and not phrases:
        raise ValueError("Query must contain at least one positive search term")
    if len(phrases) > MAX_PHRASES or len(excluded) > MAX_EXCLUDED:
        raise ValueError("Too many phrases or exclusions")
    embedded_lang = filters.get("language")
    if embedded_lang is not None and embedded_lang not in ALLOWED_LANGUAGES:
        raise ValueError("Unsupported lang: filter")
    if language and embedded_lang and language != embedded_lang:
        raise ValueError("Conflicting language filters")
    embedded_source = filters.get("source_id")
    if embedded_source and not SOURCE_RE.fullmatch(embedded_source):
        raise ValueError("Invalid source: filter")
    if source_id and embedded_source and source_id != embedded_source:
        raise ValueError("Conflicting source filters")

    site = _site(filters["site"]) if "site" in filters else None
    after = _date_ms(filters["after"]) if "after" in filters else None
    before = _date_ms(filters["before"]) if "before" in filters else None
    if after is not None and before is not None and after >= before:
        raise ValueError("Date window is empty or reversed")
    terms = tuple(positive)
    qs = " ".join((*positive, *phrases))
    has_han = bool(HAN_RE.search(qs))
    has_latin = bool(LATIN_RE.search(qs))
    detected = "mixed" if has_han and has_latin else "zh" if has_han else "en" if has_latin else "und"
    low = qs.casefold()
    intent = ("troubleshooting" if any(x in low for x in ("error", "exception", "报错", "修复", "故障"))
              else "tutorial" if any(x in low for x in ("how to", "tutorial", "guide", "教程", "怎么", "如何"))
              else "comparison" if any(x in low for x in (" vs ", " versus ", "对比", "比较"))
              else "navigation" if site or phrases or re.search(r"\b(?:cve-\d{4}-\d+|[a-z]+\d+[a-z]*)\b",low)
              else "informational")
    suggestion = suggest_typo(terms) if rewrite_mode != "off" else None
    expansions: list[str] = []
    if rewrite_mode == "expand" and intent in ("informational", "tutorial", "comparison"):
        for token in (*positive, *phrases):
            for syn in SYNONYMS.get(token.casefold(), ()):
                if syn != token and syn not in expansions:
                    expansions.append(syn)
                if len(expansions) == MAX_EXPANSIONS:
                    break
    return QueryPlan(
        original=q, normalized=normalized, positive=terms, phrases=tuple(phrases),
        excluded=tuple(excluded), language=language or embedded_lang,
        detected_language=detected, source_id=source_id or embedded_source,
        host=site, after_ms=after, before_ms=before, intent=intent,
        tokens=diagnostic_tokens(qs), rewrite_mode=rewrite_mode,
        suggestion=suggestion, expansions=tuple(expansions),
    )
