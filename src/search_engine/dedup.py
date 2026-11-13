"""Stable SimHash LSH candidate generation and exact shingle Jaccard verification.

8 disjoint 8-bit bands guarantee >=1 matching band when Hamming distance <=7;
Jaccard verification still required. No transitive/union-find clustering is performed.
"""
from __future__ import annotations
from collections import Counter
import hashlib
import re

ALGORITHM = 'simhash8x8-shingle-v1'
MIN_TOKENS = 20
MIN_LENGTH_RATIO = 0.75
MAX_HAMMING = 7
MIN_JACCARD = 0.85


def tokens(content: str) -> list[str]:
    # Chinese single characters, Latin words/identifiers, numeric words.
    return re.findall(r'[\u3400-\u9fff]|[a-z0-9_+#.-]+', content.casefold())


def shingles(content: str, n: int = 3) -> set[str]:
    parts = tokens(content)
    return {'\x1f'.join(parts[i:i+n]) for i in range(len(parts)-n+1)} if len(parts) >= n else set()


def fingerprint(shingle_set: set[str]) -> str | None:
    if not shingle_set:
        return None
    vector = [0] * 64
    for token in sorted(shingle_set):
        bits = int.from_bytes(hashlib.sha256(token.encode('utf-8')).digest()[:8], 'big')
        for i in range(64):
            vector[i] += 1 if (bits >> i) & 1 else -1
    result = sum(1 << i for i, score in enumerate(vector) if score >= 0)
    return f'{result:016x}'


def bands(hex_fingerprint: str) -> tuple[int, ...]:
    return tuple((int(hex_fingerprint, 16) >> (i * 8)) & 255 for i in range(8))


def hamming(a: str, b: str) -> int:
    return (int(a, 16) ^ int(b, 16)).bit_count()


def jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a or b else 1.0


def near_match(body_a: str, body_b: str, fingerprint_a: str, fingerprint_b: str) -> float | None:
    a, b = tokens(body_a), tokens(body_b)
    if len(a) < MIN_TOKENS or len(b) < MIN_TOKENS:
        return None
    if min(len(a), len(b)) / max(len(a), len(b)) < MIN_LENGTH_RATIO:
        return None
    if hamming(fingerprint_a, fingerprint_b) > MAX_HAMMING:
        return None
    score = jaccard(shingles(body_a), shingles(body_b))
    return score if score >= MIN_JACCARD else None
