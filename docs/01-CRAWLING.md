# 01 — Crawling / Ingestion and Distributed Frontier

**Textbook trace:** Stanford IIR chapters 19–20 (Web search, crawling, URL frontier), 21 (link analysis). Policy standard: RFC 9309.

## 1. Sources and discovery

Supported ingress priority: licensed/first-party API -> sitemap -> RSS/Atom -> allowlisted seed links -> discovered in-page links -> explicitly permitted uploads. Track provenance for every discovered URL (source_id, parent_url_id, discovered_at, depth, anchor, policy). Do not recursively roam outside the allowlisted domain/scope; enforce scope after redirects as well.

URL normalization: lower-case host and scheme; normalize IDNA, default ports and dot segments; strip fragment; canonicalize percent-escapes safely. Remove only explicitly allowlisted tracking parameters (e.g. utm_* when confirmed irrelevant). **Never blindly sort/drop query parameters**, change case-sensitive paths, merge slash/no-slash variants or trust rel=canonical without verification. Keep both requested_url and final_url, plus canonical hint and redirect chain.

## 2. Politeness and access policy

Per host/authority maintain robots record (body digest, checked_at, TTL, fetch error class), max in-flight, next_allowed_at and circuit-breaker status. Identify the crawler honestly in User-Agent with an operator contact. Parse robots for the real crawler token per RFC 9309; use rule specificity. Treat 5xx/network failures of robots as disallowed temporarily; for ambiguity, fail closed; refresh cache regularly (normally within 24 h). HTTP robots 401/403 semantics differ by site; conservative operational policy is to stop and request access. Honor site-specific contractual and legal restrictions, noindex and X-Robots-Tag for indexing.

Prevent overload: per-host 1 concurrent fetch initially, configure min interval and global rate. Jitter request times to spread load, not to conceal identity. Request timeout, size cap, decompression cap and max redirects. Retry 429/503 with bounded exponential backoff, honor Retry-After, reduce per-host concurrency, and stop on repeated 403/CAPTCHA/challenge. Never recommend bypassing CAPTCHA, fingerprinting, IP blocklists, paywalls or logins, or rotating proxies to evade site limits. For protected sites seek permission or official APIs.

## 3. URL Frontier algorithms

Partition priority by host and schedule eligible host buckets. Host eligibility is an invariant, not just a best-effort timer. Two-tier scheduling:

1. Host queue: earliest next_allowed_at with fairness and quota; skip blocked/open-circuit hosts.
2. Per-host URL priority: source quality, freshness estimate, change probability, depth cap, revisit deadline, link value.

Illustrative score (not a real search engine formula):

P(url) = a * predicted_quality + b * expected_change + c * age_since_fetch - d * crawl_cost.

Store deterministic priority for tie-breaking and audit. Avoid starving new hosts and domains with small quotas. Distinguish seed crawl, recrawl, robots refresh and dead-link verification.

## 4. Fetch state machine

~~~text
DISCOVERED -> POLICY_CHECK -> READY -> LEASED -> FETCHED
                                |          |       |
                             DEFERRED    RETRY   PROCESSING
                                |          |       |
                              READY <------+     INDEX_PENDING -> INDEXED
                                |
                            BLOCKED / DEAD / REMOVED
~~~

- 200: verify content-type and size; raw sha256, store immutable bytes, enqueue parse.
- 301/302/307/308: bounded redirects; re-check robots and SSRF for each destination; persist source and final URL.
- 304: retain previous content/version; update last_seen/freshness without reindex.
- 404/410: policy-based tombstone after confirming scope and status; mark delete in outbox.
- 429/503: Retry-After + exponential backoff with cap and host circuit breaker.
- 401/403/challenge: stop and record permission/block reason; no escalation to evasive fetching.
- Network transient: retry under budget, then dead-letter with diagnostics.

Conditional GET: persist ETag and Last-Modified and supply If-None-Match/If-Modified-Since. Use 304 only when previous successful representation exists.

## 5. Safety

HTTP fetch gateway must prevent SSRF and DNS rebinding with policy on DNS results **and** actual connection destination. Block 127/8, RFC1918, link-local, loopback, multicast, cloud metadata and IPv6 ULA/link-local; reject non-HTTP schemes, credentials in URL and disallowed ports. Size/time limits apply to streaming compressed and decoded bodies. Never run fetched page code in the HTTP worker. Rendering is an optional sandboxed, opt-in capability.

## 6. Distributed crawler preregistration

P0: multiple fetcher processes on one host, centrally leasing SQLite tasks inside short BEGIN IMMEDIATE transactions. One scheduler controls host admission; work leases have epoch/fencing tokens and deadlines. **Do not** copy a WAL database to multiple worker hosts via NFS.

P2: PostgreSQL task coordinator (authoritative host budgets + frontier + lease tokens) and NATS JetStream/Redis Streams *as notification transport only*, workers claim with coordinator and ack after durable completion. Alternative: PostgreSQL SKIP LOCKED polling alone until throughput justifies a broker. Partition work using rendezvous hashing by registered domain (or host with shared-domain quotas), reassign on worker loss, and centralize final per-host token grants so two partitions cannot exceed limits. Shared IP / eTLD+1 fairness is an optional additional policy.

Use one crawl_id per run and explicit pagination/termination counters; abort/stop jobs must propagate cancellation and refrain from new fetches. Rollbacks/partial network errors must not duplicate irreversible side effects.

## 7. Verification plan

Fixtures: robots allow/disallow and failed retrieval; redirect to private IP; Unicode URL and query canonicalization; 304 with and without stored prior body; 429 Retry-After; site challenge stop; broken gzip; decompression bomb; oversized body; redirect loop; crawl priority fairness; no more than per-host concurrency; reclaimed lease after worker SIGKILL; multi-worker duplicate suppression; 404 soft-404 and deletion reconciliation.

## References

- https://nlp.stanford.edu/IR-book/ (chapters 19–20)
- https://www.rfc-editor.org/rfc/rfc9309.html
