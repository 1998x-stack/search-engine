"""Public-only BM25 search with safe bilingual QueryPlan.

All optional rewrites are bounded and **never** weaken mandatory filters.
OpenSearch analyzer config owns actual query-time word segmentation.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any

from .opensearch import OpenSearchClient, READ_ALIAS, require_index_name
from .query import plan_query, QueryPlan

FIELDS = [
    "title^3", "title.zh^4", "title.en^4",
    "headings^2", "headings.zh^3", "headings.en^3",
    "content", "content.zh", "content.en",
]


def _search_payload(plan: QueryPlan, *, limit: int, offset: int) -> dict:
    filters: list[dict] = [
        {"term": {"visibility": "public"}},
        {"term": {"is_indexable": True}},
    ]
    if plan.language is not None:
        filters.append({"term": {"language": plan.language}})
    if plan.source_id is not None:
        filters.append({"term": {"source_id": plan.source_id}})
    if plan.host is not None:
        filters.append({"term": {"host": plan.host}})
    if plan.after_ms is not None or plan.before_ms is not None:
        bound = {}
        if plan.after_ms is not None:
            bound["gte"] = plan.after_ms
        if plan.before_ms is not None:
            bound["lt"] = plan.before_ms
        filters.append({"range": {"updated_at_ms": bound}})

    # Retain a literal lexical baseline as first should clause (existing test
    # fakes rely on this), then explicit title phrase/exact match.
    needle = plan.retrieval_text
    should: list[dict] = [
        {"multi_match": {"query": needle, "fields": FIELDS, "type": "best_fields"}},
        {"match_phrase": {"title": {"query": needle, "boost": 6}}},
        {"term": {"title.raw": {"value": needle, "boost": 12}}},
    ]
    for expansion in plan.expansions:
        should.append({"multi_match": {
            "query": expansion, "fields": FIELDS, "type": "best_fields",
            "boost": 0.2,
        }})
    boolean: dict[str, Any] = {
        "filter": filters,
        "should": should,
        "minimum_should_match": 1,
    }
    if plan.phrases:
        boolean["must"] = [
            {"multi_match": {
                "query": phrase, "fields": FIELDS, "type": "phrase",
                "slop": 0,
            }} for phrase in plan.phrases
        ]
    if plan.excluded:
        boolean["must_not"] = [
            {"multi_match": {"query": excluded, "fields": FIELDS,
                             "type": "phrase" if " " in excluded else "best_fields"}}
            for excluded in plan.excluded
        ]
    return {
        "track_total_hits": True,
        "from": offset, "size": limit,
        "_source": ["document_id", "title", "description", "canonical_url",
                    "source_id", "language", "revision", "updated_at_ms"],
        "highlight": {
            "pre_tags": [""], "post_tags": [""],
            "fields": {"content": {"fragment_size": 180, "number_of_fragments": 1}},
        },
        "query": {"bool": boolean},
        "sort": [{"_score": "desc"}, {"document_id": "asc"}],
    }


def search_query(q: str, *, limit: int = 10, language: str | None = None,
                 source_id: str | None = None, offset: int = 0,
                 rewrite_mode: str = "suggest") -> dict:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
        raise ValueError("Limit must be 1–20")
    if isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset <= 200:
        raise ValueError("Offset must be 0–200; deep paging is not supported")
    plan = plan_query(q, language=language, source_id=source_id,
                      rewrite_mode=rewrite_mode)
    return _search_payload(plan, limit=limit, offset=offset)


@dataclass
class SearchService:
    client: OpenSearchClient
    read_alias: str = READ_ALIAS

    def __post_init__(self):
        self.read_alias = require_index_name(self.read_alias)

    def search(self, q: str, *, limit: int = 10, language: str | None = None,
               source_id: str | None = None, offset: int = 0,
               rewrite_mode: str = "suggest") -> dict[str, Any]:
        plan = plan_query(q, language=language, source_id=source_id,
                          rewrite_mode=rewrite_mode)
        # Apply the same pagination validation as callers of search_query.
        payload = search_query(q, limit=limit, language=language,
                               source_id=source_id, offset=offset,
                               rewrite_mode=rewrite_mode)
        response = self.client.request("POST", f"/{self.read_alias}/_search", payload)
        hits = response.get("hits", {})
        rows = []
        for item in hits.get("hits", []):
            source = item.get("_source", {})
            content = (source.get("description")
                       or " ".join(item.get("highlight", {}).get("content", []))
                       or "")
            rows.append({
                "document_id": source.get("document_id", item.get("_id")),
                "title": source.get("title", ""), "url": source.get("canonical_url", ""),
                "source_id": source.get("source_id", ""),
                "language": source.get("language", "und"),
                "revision": source.get("revision"),
                "score": item.get("_score", 0),
                "snippet": str(content)[:240],
            })
        total = hits.get("total", 0)
        return {
            "query": q,
            "total": total.get("value", 0) if isinstance(total, dict) else total,
            "offset": offset, "limit": limit,
            "analysis": plan.public_summary(),
            "results": rows,
        }
