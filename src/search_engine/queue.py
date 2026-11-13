"""At-least-once SQLite task queue with leased jobs and fenced acknowledgements."""
from __future__ import annotations
from dataclasses import dataclass
import json
import sqlite3
import uuid
from .db import Database, clock_ms

@dataclass(frozen=True)
class Lease:
    job_id: str
    run_id: str | None
    kind: str
    entity_id: str
    payload: dict
    attempt: int
    max_attempts: int
    owner: str
    epoch: int

class Queue:
    def __init__(self, db: Database):
        self.db = db

    @staticmethod
    def enqueue_tx(con: sqlite3.Connection, kind: str, entity_id: str,
                   idempotency_key: str, *, run_id: str | None = None,
                   payload: dict | None = None, priority: int = 0,
                   scheduled_at_ms: int | None = None, now_ms: int | None = None) -> str:
        now = clock_ms() if now_ms is None else now_ms
        job_id = str(uuid.uuid4())
        con.execute('''INSERT OR IGNORE INTO jobs
            (job_id,run_id,kind,entity_id,payload_json,priority,scheduled_at_ms,
             idempotency_key,created_at_ms,updated_at_ms)
            VALUES (?,?,?,?,?,?,?,?,?,?)''',
            (job_id, run_id, kind, entity_id, json.dumps(payload or {}, ensure_ascii=False),
             priority, now if scheduled_at_ms is None else scheduled_at_ms,
             idempotency_key, now, now))
        row = con.execute('SELECT job_id,kind,entity_id FROM jobs WHERE idempotency_key=?',
                          (idempotency_key,)).fetchone()
        if row['kind'] != kind or row['entity_id'] != entity_id:
            raise ValueError('Idempotency key collision with a different task')
        return row['job_id']

    def enqueue(self, kind: str, entity_id: str, idempotency_key: str, **kwargs) -> str:
        with self.db.transaction() as con:
            return self.enqueue_tx(con, kind, entity_id, idempotency_key, **kwargs)

    def claim(self, owner: str, *, kinds: tuple[str, ...] = ('FETCH_URL',),
              now_ms: int | None = None, lease_ms: int = 30000) -> Lease | None:
        if not owner or not kinds or lease_ms <= 0:
            raise ValueError('owner, kinds and positive lease_ms are required')
        now = clock_ms() if now_ms is None else now_ms
        marks = ','.join('?' for _ in kinds)
        with self.db.transaction() as con:
            con.execute('''UPDATE jobs SET state='dead', lease_owner=NULL,
                leased_until_ms=NULL, last_error_class='attempt_budget_exhausted',
                updated_at_ms=? WHERE attempt>=max_attempts AND
                ((state IN ('ready','retry') AND scheduled_at_ms<=?) OR
                 (state='leased' AND leased_until_ms<=?))''', (now, now, now))
            row = con.execute(f'''SELECT j.* FROM jobs j LEFT JOIN crawl_runs r
                ON r.run_id=j.run_id
                WHERE j.kind IN ({marks}) AND j.attempt<j.max_attempts
                AND (j.run_id IS NULL OR r.state IN ('running','queued'))
                AND ((j.state IN ('ready','retry') AND j.scheduled_at_ms<=?)
                     OR (j.state='leased' AND j.leased_until_ms<=?))
                ORDER BY j.priority DESC,j.scheduled_at_ms ASC,j.job_id ASC LIMIT 1''',
                (*kinds, now, now)).fetchone()
            if not row:
                return None
            epoch = row['lease_epoch'] + 1
            con.execute('''UPDATE jobs SET state='leased',attempt=attempt+1,
                lease_owner=?,lease_epoch=?,leased_until_ms=?,updated_at_ms=?
                WHERE job_id=?''', (owner, epoch, now + lease_ms, now, row['job_id']))
            return Lease(job_id=row['job_id'], run_id=row['run_id'], kind=row['kind'],
                         entity_id=row['entity_id'], payload=json.loads(row['payload_json']),
                         attempt=row['attempt'] + 1, max_attempts=row['max_attempts'],
                         owner=owner, epoch=epoch)

    @staticmethod
    def _guard(con: sqlite3.Connection, lease: Lease) -> None:
        row = con.execute('SELECT state,lease_owner,lease_epoch FROM jobs WHERE job_id=?',
                          (lease.job_id,)).fetchone()
        if (not row or row['state'] != 'leased' or row['lease_owner'] != lease.owner
                or row['lease_epoch'] != lease.epoch):
            raise RuntimeError('Stale or invalid job lease')

    @classmethod
    def complete_tx(cls, con: sqlite3.Connection, lease: Lease, now_ms: int | None = None) -> None:
        cls._guard(con, lease)
        now = clock_ms() if now_ms is None else now_ms
        con.execute('''UPDATE jobs SET state='succeeded',lease_owner=NULL,
            leased_until_ms=NULL,updated_at_ms=? WHERE job_id=?''', (now, lease.job_id))

    def complete(self, lease: Lease, now_ms: int | None = None) -> None:
        with self.db.transaction() as con:
            self.complete_tx(con, lease, now_ms)

    def heartbeat(self, lease: Lease, *, now_ms: int | None = None,
                  extension_ms: int = 30000) -> None:
        now = clock_ms() if now_ms is None else now_ms
        with self.db.transaction() as con:
            self._guard(con, lease)
            con.execute('UPDATE jobs SET leased_until_ms=?,updated_at_ms=? WHERE job_id=?',
                        (now + extension_ms, now, lease.job_id))

    def defer(self, lease: Lease, due_ms: int, *, refund_attempt: bool = True,
              now_ms: int | None = None, reason: str = 'deferred') -> None:
        now = clock_ms() if now_ms is None else now_ms
        with self.db.transaction() as con:
            self._guard(con, lease)
            con.execute('''UPDATE jobs SET state='retry',scheduled_at_ms=?,
                attempt=CASE WHEN ?=1 AND attempt>0 THEN attempt-1 ELSE attempt END,
                lease_owner=NULL,leased_until_ms=NULL,last_error_class=?,updated_at_ms=?
                WHERE job_id=?''', (max(due_ms, now + 1), int(refund_attempt),
                                     reason, now, lease.job_id))

    def fail(self, lease: Lease, error: str, *, retryable: bool,
             retry_after_ms: int = 1000, now_ms: int | None = None) -> str:
        now = clock_ms() if now_ms is None else now_ms
        with self.db.transaction() as con:
            self._guard(con, lease)
            state = 'retry' if retryable and lease.attempt < lease.max_attempts else 'dead'
            con.execute('''UPDATE jobs SET state=?,scheduled_at_ms=?,
                lease_owner=NULL,leased_until_ms=NULL,last_error_class=?,updated_at_ms=?
                WHERE job_id=?''',
                (state, now + max(1, retry_after_ms), error[:160], now, lease.job_id))
            return state

    def cancel_run(self, run_id: str, *, now_ms: int | None = None) -> None:
        now = clock_ms() if now_ms is None else now_ms
        with self.db.transaction() as con:
            con.execute('UPDATE crawl_runs SET state=?,finished_at_ms=?,updated_at_ms=? WHERE run_id=?',
                        ('cancelled', now, now, run_id))
            con.execute('''UPDATE jobs SET state='cancelled',updated_at_ms=?,
                lease_owner=NULL,leased_until_ms=NULL
                WHERE run_id=? AND state IN ('ready','retry')''', (now, run_id))

    def finalize_runs(self, *, now_ms: int | None = None) -> int:
        """Set active runs terminal after *all* jobs are terminal; no empty runs."""
        now = clock_ms() if now_ms is None else now_ms
        with self.db.transaction() as con:
            rows = con.execute('''SELECT r.run_id,
                SUM(CASE WHEN j.state NOT IN ('succeeded','dead','cancelled') THEN 1 ELSE 0 END) AS remaining,
                SUM(CASE WHEN j.state='dead' THEN 1 ELSE 0 END) AS failed
                FROM crawl_runs r JOIN jobs j ON j.run_id=r.run_id
                WHERE r.state='running' GROUP BY r.run_id''').fetchall()
            count = 0
            for row in rows:
                if row['remaining'] == 0:
                    con.execute('''UPDATE crawl_runs SET state=?,finished_at_ms=?,updated_at_ms=?
                        WHERE run_id=? AND state='running' ''',
                        ('failed' if row['failed'] else 'completed',now,now,row['run_id']))
                    count += 1
            return count
