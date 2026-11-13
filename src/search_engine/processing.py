"""Phase 2: durable fetch -> extracted text -> version/outbox transaction.

OpenSearch delivery deliberately deferred. Re-running a lease is safe: only the
latest persisted content revision is authoritative, never the worker's timing.
"""
from __future__ import annotations
from dataclasses import dataclass
import hashlib
import sqlite3
import uuid

from .db import Database, clock_ms
from .dedup import (ALGORITHM, MIN_TOKENS, bands, fingerprint,
                    near_match, shingles, tokens)
from .extract import VERSION, Extracted, extract
from .queue import Lease, Queue
from .store import BlobStore


class CorruptedBlob(ValueError):
    pass


@dataclass(frozen=True)
class Candidate:
    document_id: str
    revision: int
    similarity: float


class DocumentProcessor:
    def __init__(self, db: Database, store: BlobStore, *, worker_id: str = 'processor-1'):
        self.db, self.store, self.worker_id = db, store, worker_id
        self.queue = Queue(db)

    def run_once(self, *, now_ms: int | None = None) -> str:
        now = clock_ms() if now_ms is None else now_ms
        lease = self.queue.claim(self.worker_id,
                                 kinds=('EXTRACT_CONTENT', 'DELETE_DOCUMENT'),
                                 now_ms=now, lease_ms=60_000)
        if not lease:
            return 'idle'
        with self.db.connect() as con:
            row = con.execute('''SELECT f.*, f.rowid AS fetch_order,
                u.source_id,u.normalized_url,u.document_id AS attached_document_id,
                r.state AS run_state
                FROM fetches f JOIN urls u ON u.url_id=f.url_id
                LEFT JOIN crawl_runs r ON r.run_id=f.run_id WHERE f.fetch_id=?''',
                (lease.entity_id,)).fetchone()
            item = dict(row) if row else None
        if not item:
            self.queue.fail(lease, 'missing_fetch', retryable=False, now_ms=now)
            return 'dead'
        if item['run_state'] != 'running':
            self.queue.complete(lease, now_ms=now)
            return 'cancelled'
        if lease.kind == 'DELETE_DOCUMENT':
            return self._commit(lease, item, None, None, None, None, now)
        if item['http_status'] != 200 or not item['raw_blob_ref']:
            self.queue.fail(lease, 'invalid_fetch_representation', retryable=False, now_ms=now)
            return 'dead'
        try:
            raw = self.store.get(item['raw_blob_ref'])
            if hashlib.sha256(raw).hexdigest() != item['raw_sha256']:
                raise CorruptedBlob('Stored bytes do not match fetch SHA-256')
            parsed = extract(raw,item['content_type'],item['normalized_url'],
                             x_robots_tag=item['x_robots_tag'] or '')
            content_hash = hashlib.sha256(parsed.normalized_body.encode('utf-8')).hexdigest()
            # Immutable text artifact exists before relational version commit.
            _, text_blob = self.store.put(parsed.normalized_body.encode('utf-8'))
            my_shingles = shingles(parsed.normalized_body) if parsed.indexable else set()
            simhash = fingerprint(my_shingles) if len(tokens(parsed.normalized_body)) >= MIN_TOKENS else None
            near = self._near_candidates(item['source_id'],parsed,simhash) if simhash else []
            return self._commit(lease,item,parsed,content_hash,text_blob,(simhash,near),now)
        except (OSError, ValueError, UnicodeError) as exc:
            # No silent empty text on errors, and no index event on partial extraction.
            self.queue.fail(lease, type(exc).__name__, retryable=isinstance(exc, OSError),
                            now_ms=now, retry_after_ms=1000)
            return 'retry' if isinstance(exc, OSError) and lease.attempt < lease.max_attempts else 'dead'

    def _near_candidates(self, source_id: str, parsed: Extracted, simhash: str) -> list[Candidate]:
        byte_bands = bands(simhash)
        with self.db.connect() as con:
            candidates = con.execute('''SELECT DISTINCT s.document_id,s.revision,
                s.simhash_hex,v.text_blob_ref
                FROM signature_bands b
                JOIN document_signatures s
                  ON s.document_id=b.document_id AND s.revision=b.revision
                  AND s.algorithm_version=b.algorithm_version
                JOIN documents d ON d.document_id=s.document_id AND d.latest_revision=s.revision
                JOIN document_versions v ON v.document_id=s.document_id AND v.revision=s.revision
                WHERE d.source_id=? AND d.is_deleted=0 AND v.is_indexable=1
                  AND b.algorithm_version=? AND (
                  (b.band_number=0 AND b.band_value=?) OR
                  (b.band_number=1 AND b.band_value=?) OR
                  (b.band_number=2 AND b.band_value=?) OR
                  (b.band_number=3 AND b.band_value=?) OR
                  (b.band_number=4 AND b.band_value=?) OR
                  (b.band_number=5 AND b.band_value=?) OR
                  (b.band_number=6 AND b.band_value=?) OR
                  (b.band_number=7 AND b.band_value=?))
                ORDER BY s.document_id LIMIT 200''',
                (source_id,ALGORITHM,*byte_bands)).fetchall()
        near: list[Candidate] = []
        for candidate in candidates:
            try:
                text = self.store.get(candidate['text_blob_ref']).decode('utf-8')
                score = near_match(parsed.normalized_body,text,simhash,candidate['simhash_hex'])
            except (OSError,UnicodeError,ValueError):
                continue  # failed candidate cannot become a false duplicate
            if score is not None:
                near.append(Candidate(candidate['document_id'],candidate['revision'],score))
        return near

    @staticmethod
    def _relation(con: sqlite3.Connection, current: str, other: str,
                  method: str, version: str, score: float, now: int) -> None:
        if current == other:
            return
        left,right = sorted((current,other))
        con.execute('''INSERT INTO duplicate_relations
            (left_document_id,right_document_id,method,algorithm_version,score,decision,examined_at_ms)
            VALUES (?,?,?,?,?,'duplicate',?)
            ON CONFLICT(left_document_id,right_document_id,method,algorithm_version)
            DO UPDATE SET score=excluded.score,decision='duplicate',examined_at_ms=excluded.examined_at_ms''',
            (left,right,method,version,score,now))

    def _commit(self, lease: Lease, item: dict, parsed: Extracted | None,
                content_hash: str | None, text_blob: str | None,
                similarity: tuple[str | None,list[Candidate]] | None, now: int) -> str:
        doc_id = hashlib.sha256((item['source_id']+'\0'+item['normalized_url']).encode()).hexdigest()
        with self.db.transaction() as con:
            Queue._guard(con,lease)
            active = con.execute('SELECT state FROM crawl_runs WHERE run_id=?',
                                 (lease.run_id,)).fetchone()
            if not active or active['state'] != 'running':
                Queue.complete_tx(con,lease,now)
                return 'cancelled'
            doc = con.execute('SELECT * FROM documents WHERE document_id=?',
                              (doc_id,)).fetchone()
            prior = None
            if doc and doc['latest_revision']:
                prior = con.execute('''SELECT v.*,f.rowid AS prior_fetch_order FROM document_versions v
                  LEFT JOIN fetches f ON v.fetch_id=f.fetch_id
                  WHERE v.document_id=? AND v.revision=?''',
                  (doc_id,doc['latest_revision'])).fetchone()
            # Old queued work cannot revert a more recently recorded fetch.
            if prior and prior['prior_fetch_order'] and item['fetch_order'] <= prior['prior_fetch_order']:
                Queue.complete_tx(con,lease,now)
                return 'stale'
            deleting = lease.kind == 'DELETE_DOCUMENT'
            if deleting and (not doc or doc['is_deleted']):
                Queue.complete_tx(con,lease,now)
                return 'unchanged'
            if not deleting and parsed is None:
                raise ValueError('Expected parsed content')
            if not deleting and prior and not prior['is_tombstone'] and all((
                prior['normalized_sha256'] == content_hash,
                prior['title'] == parsed.title,
                prior['language'] == parsed.language,
                prior['description'] == parsed.description,
                prior['canonical_hint'] == parsed.canonical_hint,
                prior['is_indexable'] == int(parsed.indexable),
                prior['parser_version'] == VERSION,
            )):
                Queue.complete_tx(con,lease,now)
                return 'unchanged'
            if not doc:
                con.execute('''INSERT INTO documents
                    (document_id,source_id,canonical_url,latest_revision,is_deleted,created_at_ms,updated_at_ms)
                    VALUES (?,?,?,0,0,?,?)''',
                    (doc_id,item['source_id'],item['normalized_url'],now,now))
            revision = (doc['latest_revision'] if doc else 0) + 1
            is_indexable = int(bool(parsed and parsed.indexable) and not deleting)
            con.execute('''INSERT INTO document_versions
                (document_id,revision,fetch_id,normalized_sha256,text_blob_ref,title,language,
                 parser_version,is_indexable,is_tombstone,created_at_ms,canonical_hint,description)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (doc_id,revision,item['fetch_id'],None if deleting else content_hash,
                 None if deleting else text_blob,None if deleting else parsed.title,
                 None if deleting else parsed.language,VERSION,is_indexable,int(deleting),now,
                 None if deleting else parsed.canonical_hint,
                 None if deleting else parsed.description))
            con.execute('''UPDATE documents SET latest_revision=?,is_deleted=?,updated_at_ms=?
                WHERE document_id=?''',(revision,int(deleting),now,doc_id))
            con.execute('UPDATE urls SET document_id=? WHERE url_id=?',
                        (doc_id,item['url_id']))
            op = 'upsert' if is_indexable else 'delete'
            con.execute('''INSERT INTO index_outbox
                (op_id,document_id,revision,op_type,payload_ref,state,attempt,scheduled_at_ms,created_at_ms,updated_at_ms)
                VALUES (?,?,?,?,?,'pending',0,?,?,?)''',
                (str(uuid.uuid4()),doc_id,revision,op,text_blob if is_indexable else None,now,now,now))
            # Similarity is a relation between current contents. Invalidate stale
            # duplicate links when a document changes/deletes/noindexes.
            con.execute('''UPDATE duplicate_relations SET decision='distinct',examined_at_ms=?
                WHERE (left_document_id=? OR right_document_id=?) AND decision='duplicate' ''',
                (now,doc_id,doc_id))
            if is_indexable:
                exact_matches = con.execute('''SELECT d.document_id FROM documents d
                    JOIN document_versions v ON v.document_id=d.document_id
                      AND v.revision=d.latest_revision
                    WHERE d.source_id=? AND d.document_id<>? AND d.is_deleted=0
                      AND v.is_indexable=1 AND v.normalized_sha256=? LIMIT 100''',
                    (item['source_id'],doc_id,content_hash)).fetchall()
                exact_ids = {m['document_id'] for m in exact_matches}
                for match in exact_matches:
                    self._relation(con,doc_id,match['document_id'],'exact','norm-v1',1.0,now)
                if similarity and similarity[0]:
                    simhash, near = similarity
                    con.execute('''INSERT INTO document_signatures
                        (document_id,revision,algorithm_version,simhash_hex,token_count)
                        VALUES(?,?,?,?,?)''',
                        (doc_id,revision,ALGORITHM,simhash,len(tokens(parsed.normalized_body))))
                    con.executemany('''INSERT INTO signature_bands
                        (document_id,revision,algorithm_version,band_number,band_value)
                        VALUES (?,?,?,?,?)''',
                        [(doc_id,revision,ALGORITHM,n,value) for n,value in enumerate(bands(simhash))])
                    for candidate in near:
                        # Candidate was read outside write transaction; reject stale revisions.
                        fresh = con.execute('''SELECT d.latest_revision,v.is_indexable
                            FROM documents d JOIN document_versions v
                            ON v.document_id=d.document_id AND v.revision=d.latest_revision
                            WHERE d.document_id=? AND d.is_deleted=0''',
                            (candidate.document_id,)).fetchone()
                        if (fresh and fresh['latest_revision'] == candidate.revision
                                and fresh['is_indexable'] and candidate.document_id not in exact_ids
                                and candidate.document_id != doc_id):
                            self._relation(con,doc_id,candidate.document_id,'near',ALGORITHM,candidate.similarity,now)
            Queue.complete_tx(con,lease,now)
            if deleting:
                return 'deleted'
            if not is_indexable:
                return 'noindex'
            return 'updated' if doc else 'created'
