"""Crawl run/source admission, durable URL frontier, and per-host HTTP permits."""
from __future__ import annotations
from dataclasses import dataclass
import hashlib
import json
import sqlite3
import uuid
from urllib.parse import urlsplit
from .db import Database, clock_ms
from .queue import Queue
from .urls import normalize_url, origin, approved_host, URLRejected

@dataclass(frozen=True)
class Permit:
    token: str
    host_id: str

class Frontier:
    def __init__(self, db: Database, *, allowed_ports: set[int] | None = None,
                 min_interval_ms: int = 1000, max_inflight: int = 1):
        self.db = db
        self.allowed_ports = allowed_ports or {80, 443}
        self.min_interval_ms = min_interval_ms
        self.max_inflight = max_inflight

    def create_source(self, source_id: str, root_url: str,
                      *, allowed_hosts: list[str] | None = None,
                      now_ms: int | None = None) -> None:
        now = clock_ms() if now_ms is None else now_ms
        normalized = normalize_url(root_url, allowed_ports=self.allowed_ports)
        hosts = sorted(set(h.lower() for h in (allowed_hosts or [urlsplit(normalized).hostname])))
        if not approved_host(normalized, set(hosts)):
            raise URLRejected('Root URL outside source scope')
        with self.db.transaction() as con:
            con.execute('''INSERT INTO sources
                (source_id,name,root_url,allowed_hosts_json,policy_json,created_at_ms,updated_at_ms)
                VALUES(?,?,?,?,?,?,?)''',
                (source_id, source_id, normalized, json.dumps(hosts), '{}', now, now))

    def create_run(self, source_id: str, *, config: dict | None = None,
                   run_id: str | None = None, now_ms: int | None = None) -> str:
        now = clock_ms() if now_ms is None else now_ms
        run_id = run_id or str(uuid.uuid4())
        with self.db.transaction() as con:
            con.execute('''INSERT INTO crawl_runs
                (run_id,source_id,state,config_json,created_at_ms,started_at_ms,updated_at_ms)
                VALUES(?,?, 'running',?,?,?,?)''',
                (run_id,source_id,json.dumps(config or {}),now,now,now))
        return run_id

    def add_url(self, source_id: str, url: str, *, run_id: str,
                depth: int = 0, parent_url_id: str | None = None,
                redirect_hops: int = 0, now_ms: int | None = None) -> str:
        with self.db.transaction() as con:
            return self.add_url_tx(con, source_id, url, run_id=run_id,
                                   depth=depth, parent_url_id=parent_url_id,
                                   redirect_hops=redirect_hops, now_ms=now_ms)

    def add_url_tx(self, con: sqlite3.Connection, source_id: str, url: str, *,
                   run_id: str, depth: int = 0,
                   parent_url_id: str | None = None, redirect_hops: int = 0,
                   now_ms: int | None = None) -> str:
        now = clock_ms() if now_ms is None else now_ms
        normalized = normalize_url(url, allowed_ports=self.allowed_ports)
        source = con.execute('SELECT allowed_hosts_json,enabled FROM sources WHERE source_id=?',
                             (source_id,)).fetchone()
        if not source or source['enabled'] != 1:
            raise URLRejected('Source disabled or unknown')
        if not approved_host(normalized, set(json.loads(source['allowed_hosts_json']))):
            raise URLRejected('URL host is outside approved source')
        linked_run = con.execute('SELECT source_id,state,config_json FROM crawl_runs WHERE run_id=?',
                                 (run_id,)).fetchone()
        if not linked_run or linked_run['source_id'] != source_id or linked_run['state'] != 'running':
            raise ValueError('Crawl run not active or mismatched to source')
        config = json.loads(linked_run['config_json'])
        max_urls = max(1, min(100_000, int(config.get('max_urls', 10_000))))
        url_id = hashlib.sha256(f'{source_id}\0{normalized}'.encode()).hexdigest()
        # Budget is per crawl run even for URLs already known from older runs.
        in_this_run = con.execute("""SELECT 1 FROM jobs WHERE run_id=?
            AND kind='FETCH_URL' AND entity_id=?""",(run_id,url_id)).fetchone()
        if not in_this_run:
            total = con.execute("""SELECT COUNT(*) FROM jobs j
                WHERE j.run_id=? AND j.kind='FETCH_URL'""",(run_id,)).fetchone()[0]
            if total >= max_urls:
                raise URLRejected('Run URL budget exhausted')
        host_id = origin(normalized)
        con.execute('''INSERT OR IGNORE INTO hosts
            (host_id,hostname,max_inflight,min_interval_ms,updated_at_ms)
            VALUES(?,?,?,?,?)''',
            (host_id,urlsplit(normalized).hostname,self.max_inflight,self.min_interval_ms,now))
        con.execute('''INSERT OR IGNORE INTO urls
            (url_id,source_id,host_id,normalized_url,original_url,parent_url_id,
             depth,first_seen_at_ms) VALUES (?,?,?,?,?,?,?,?)''',
            (url_id,source_id,host_id,normalized,url,parent_url_id,depth,now))
        Queue.enqueue_tx(con,'FETCH_URL',url_id,
                         f'fetch:{run_id}:{url_id}',run_id=run_id,
                         payload={'redirect_hops':redirect_hops},now_ms=now)
        return url_id

    def reserve_host(self, host_id: str, job_id: str, *, now_ms: int | None = None,
                     lease_ms: int = 30000) -> tuple[Permit | None,int]:
        now = clock_ms() if now_ms is None else now_ms
        with self.db.transaction() as con:
            con.execute('DELETE FROM host_leases WHERE leased_until_ms<=?', (now,))
            host = con.execute('SELECT * FROM hosts WHERE host_id=?', (host_id,)).fetchone()
            if not host:
                raise ValueError('Unknown host')
            active = con.execute('''SELECT COUNT(*) AS cnt,MIN(leased_until_ms) AS earliest
                FROM host_leases WHERE host_id=?''', (host_id,)).fetchone()
            due = max(host['next_allowed_at_ms'],host['circuit_open_until_ms'])
            if active['cnt'] >= host['max_inflight']:
                due = max(due, active['earliest'])
            if active['cnt'] >= host['max_inflight'] or now < due:
                return None, max(now + 1, due)
            token = str(uuid.uuid4())
            con.execute('''INSERT INTO host_leases
                (token,host_id,job_id,leased_until_ms) VALUES (?,?,?,?)''',
                (token,host_id,job_id,now+lease_ms))
            con.execute('UPDATE hosts SET next_allowed_at_ms=?,updated_at_ms=? WHERE host_id=?',
                        (now+host['min_interval_ms'],now,host_id))
            return Permit(token,host_id),now

    def release_host(self, permit: Permit) -> None:
        with self.db.transaction() as con:
            con.execute('DELETE FROM host_leases WHERE token=? AND host_id=?',
                        (permit.token,permit.host_id))

    def defer_host(self, host_id: str, until_ms: int) -> None:
        with self.db.transaction() as con:
            con.execute('''UPDATE hosts SET circuit_open_until_ms=MAX(circuit_open_until_ms,?),
                updated_at_ms=? WHERE host_id=?''',(until_ms,clock_ms(),host_id))
