# Search Engine

**Status: Phase 2 document extraction/dedup/versioning/outbox implemented; OpenSearch indexing and search API are NOT implemented.**

Single-host domain-scoped crawler with SQLite WAL durable task queue, robots/host budgets and immutable raw content, now followed by a separately leased document processing worker: HTML/plain extraction, exact/near duplicate detection, versioned metadata and transactional indexing outbox. Architecture remains grounded in [Stanford Introduction to Information Retrieval](https://nlp.stanford.edu/IR-book/).

## Quick start (local Linux, Python >=3.12)

```bash
python -m pip install -e .
python -m search_engine.cli --database ./data/search.db init
python -m search_engine.cli --database ./data/search.db source demo https://example.org
python -m search_engine.cli --database ./data/search.db run demo https://example.org/
export CRAWLER_USER_AGENT='YourCrawler/0.1 (+mailto:you@example.org)'
python -m search_engine.cli --database ./data/search.db worker --max-steps 100
python -m search_engine.cli --database ./data/search.db process --max-steps 100
python -m search_engine.cli --database ./data/search.db status
```

Use only URLs under approved hosts. Default fetch safety rejects private IP destinations and nonstandard ports. Loopback tests explicitly opt into fixture-only private IP access; production CLI does not. A run defaults to 10,000 URL budget, depth 2 and 100 extracted links/page. It does **not** bypass CAPTCHA, paywalls, blocks or site restrictions.

For the local Compose stack, first set `CRAWLER_USER_AGENT`, run `docker compose up -d --build`, and start the opt-in worker pair via `docker compose --profile crawl up crawler processor`. Admin source/run commands can be executed inside the API container using `docker compose exec api python -m search_engine.cli ...`. The OpenSearch service is a development placeholder for later phases and is **not used** by the crawler. Do not deploy the dev Compose publicly: OpenSearch security is disabled.

## Verification

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
python scripts/validate_design.py
docker compose config --quiet
docker build -t search-engine:phase1 .
```

The first two steps run offline with Python/SQLite; Docker steps require Docker. Deterministic offline fixtures test crawler safety plus text processing, exact/near dedup, noindex/deletions, immutable versions, v2→v3 migration and rollback-safe index outbox. These are smoke/integration gates, not proof of internet-scale correctness.

## Project docs

| Document | Scope |
|---|---|
| [00](docs/00-ARCHITECTURE.md) | Architecture and decisions |
| [01](docs/01-CRAWLING.md) | Crawling/distributed frontier design |
| [02](docs/02-PROCESSING-DEDUP.md) | Future processing/dedup |
| [03](docs/03-INDEXING.md) | Future OpenSearch projection |
| [04](docs/04-QUERY-UNDERSTANDING.md) | Future zh/en query handling |
| [05](docs/05-RETRIEVAL-RANKING.md) | Future BM25/hybrid/reranker |
| [06](docs/06-BACKGROUND-JOBS.md) | Durable leases, retries and outbox design |
| [07](docs/07-DATA-MODEL.md) | Schema and entities |
| [08](docs/08-DEPLOYMENT.md) | Docker/Linux hardening |
| [09](docs/09-ROADMAP-EVALUATION.md) | Objective P0–P2 quality gates |
| [10](docs/10-STANFORD-IR-MAPPING.md) | Stanford IR chapter mapping |
| [Phase 2](docs/12-PHASE2-IMPLEMENTATION.md) | Processing, exact/near duplicate evidence, version/outbox contracts |
| [Phase 1](docs/11-PHASE1-IMPLEMENTATION.md) | Implemented features, exact limits and validation |
| [Gotchas](docs/gotchas.md) | Engineering hazards and observed fixes |

### Roles and boundaries

SQLite is canonical crawl/job/fetch metadata; raw blobs reside on a persistent volume; OpenSearch is a future disposable projection: `index_outbox` operations are persisted but not consumed yet. SQLite WAL must **not** be shared between hosts. Multi-host distributed workers are a later PostgreSQL coordinator migration.

### Not yet supported

Sitemap/RSS discovery, true distributed coordinator, full RFC9309 exhaustive compliance, JS rendering, PDF/HTML cleaning, exact/near document dedup, OpenSearch index writing, query understanding, search results, auth-protected admin web API, production HA and observability metrics. API `/v1/search` deliberately returns 501.
