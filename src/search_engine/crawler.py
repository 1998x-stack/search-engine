"""Bounded, compliant single-host crawl worker. No indexing in Phase 1."""
from __future__ import annotations
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import urljoin
import uuid

from .db import Database, clock_ms
from .fetch import FetchError, SafeFetcher, FetchResponse
from .frontier import Frontier
from .queue import Queue, Lease
from .robots import RobotsPolicy
from .store import BlobStore
from .urls import URLRejected, origin

class LinkParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == 'a':
            for key, value in attrs:
                if key.lower() == 'href' and value:
                    self.hrefs.append(value)
                    break

def retry_after_ms(header: str | None, now_ms: int) -> int | None:
    if not header:
        return None
    try:
        return min(3_600_000, max(1000, int(header) * 1000))
    except ValueError:
        try:
            target = parsedate_to_datetime(header)
            return min(3_600_000, max(1000, int((target - datetime.fromtimestamp(now_ms / 1000, timezone.utc)).total_seconds() * 1000)))
        except (ValueError, TypeError, OverflowError):
            return None

class Crawler:
    def __init__(self, db: Database, frontier: Frontier, fetcher: SafeFetcher,
                 store: BlobStore, *, worker_id: str = 'worker-1',
                 robots_ttl_ms: int = 24 * 3600 * 1000,
                 max_depth: int = 2, max_links_per_page: int = 100):
        if not 0 <= max_depth <= 10 or not 0 <= max_links_per_page <= 1000:
            raise ValueError('Bounded depth and link count required')
        self.db, self.frontier, self.fetcher, self.store = db, frontier, fetcher, store
        self.queue = Queue(db)
        self.worker_id = worker_id
        self.robots_ttl_ms = robots_ttl_ms
        self.max_depth = max_depth
        self.max_links_per_page = max_links_per_page

    def run_once(self, *, now_ms: int | None = None) -> str:
        now = clock_ms() if now_ms is None else now_ms
        lease = self.queue.claim(self.worker_id, now_ms=now)
        if lease is None:
            return 'idle'
        with self.db.connect() as con:
            row = con.execute('''SELECT urls.*,crawl_runs.config_json FROM urls
                JOIN crawl_runs ON crawl_runs.run_id=? WHERE urls.url_id=?''',
                (lease.run_id,lease.entity_id)).fetchone()
            if row is None:
                self.queue.fail(lease,'missing_url',retryable=False,now_ms=now)
                return 'dead'
            item = dict(row)
        host_id = item['host_id']
        permit, due_ms = self.frontier.reserve_host(host_id,lease.job_id,now_ms=now,
                                                    lease_ms=max(30000,int(self.fetcher.timeout_s*1000)+10000))
        if permit is None:
            self.queue.defer(lease,due_ms,now_ms=now,reason='host_budget')
            return 'deferred'
        try:
            with self.db.connect() as con:
                robot = con.execute('''SELECT hr.*,h.robots_status FROM host_robots hr
                    JOIN hosts h ON h.host_id=hr.host_id WHERE hr.host_id=?''',
                    (host_id,)).fetchone()
            if not robot or robot['expires_at_ms'] <= now:
                return self._refresh_robots(lease,host_id,item['normalized_url'],now)
            if robot['robots_status'] != 'allowed' or robot['status_code'] not in (200,404,410):
                self._update_url_state(lease,'blocked',now)
                return 'blocked'
            if robot['status_code'] == 200 and not RobotsPolicy.parse(robot['body']).allowed(
                    self.fetcher.user_agent,item['normalized_url']):
                self._update_url_state(lease,'blocked',now)
                return 'blocked'
            return self._fetch_url(lease,item,now)
        finally:
            self.frontier.release_host(permit)

    def _refresh_robots(self, lease: Lease, host_id: str, url: str, now: int) -> str:
        robots_url = origin(url) + '/robots.txt'
        try:
            response = self.fetcher.fetch(robots_url)
        except FetchError:
            # Unreachable robots: fail closed and retry under budget.
            self.queue.fail(lease,'robots_unreachable',retryable=True,
                            retry_after_ms=5000,now_ms=now)
            return 'retry'
        allowed = response.status in (200,404,410)
        if response.status == 200:
            body = response.body.decode('utf-8',errors='replace')[:512_000]
            # A CDN/login challenge may be sent as HTTP 200 HTML. It is NOT
            # permission to crawl; reject challenge-shaped/incorrect MIME.
            if (response.content_type not in ('text/plain', 'text/robots')
                    or '<html' in body[:512].lower()
                    or len(response.body) > 512_000):
                body, allowed = '', False
        else:
            body = ''
        ttl = self.robots_ttl_ms if allowed else min(self.robots_ttl_ms,60_000)
        with self.db.transaction() as con:
            Queue._guard(con, lease)
            con.execute('''INSERT INTO host_robots
                (host_id,body,status_code,fetched_at_ms,expires_at_ms)
                VALUES(?,?,?,?,?) ON CONFLICT(host_id) DO UPDATE SET
                body=excluded.body,status_code=excluded.status_code,
                fetched_at_ms=excluded.fetched_at_ms,expires_at_ms=excluded.expires_at_ms''',
                (host_id,body,response.status,now,now+ttl))
            con.execute('''UPDATE hosts SET robots_status=?,robots_checked_at_ms=?,
                updated_at_ms=? WHERE host_id=?''',
                ('allowed' if allowed else 'disallowed',now,now,host_id))
        if not allowed:
            if response.status in (429,500,502,503,504):
                self.queue.fail(lease,'robots_unavailable',retryable=True,
                                retry_after_ms=retry_after_ms(response.headers.get('retry-after'),now) or 5000,
                                now_ms=now)
                return 'retry'
            self._update_url_state(lease,'blocked',now)
            return 'blocked'
        self.queue.defer(lease,now+1,reason='robots_refreshed',now_ms=now)
        return 'robots'

    def _update_url_state(self, lease: Lease, state: str, now: int) -> None:
        with self.db.transaction() as con:
            Queue._guard(con,lease)
            con.execute('UPDATE urls SET status=?,last_seen_at_ms=? WHERE url_id=?',
                        (state,now,lease.entity_id))
            Queue.complete_tx(con,lease,now)

    def _audit_fetch_tx(self, con, lease: Lease,
                        response: FetchResponse | None, now: int, *,
                        digest: str | None = None, blob_ref: str | None = None,
                        error_class: str | None = None) -> None:
        con.execute('''INSERT INTO fetches
            (fetch_id,url_id,run_id,fetched_at_ms,finished_at_ms,http_status,
             final_url,content_type,raw_sha256,raw_blob_ref,bytes_received,
             etag,last_modified,error_class)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (str(uuid.uuid4()),lease.entity_id,lease.run_id,now,now,
             response.status if response else None,response.url if response else None,
             response.content_type if response else None,digest,blob_ref,
             len(response.body) if response else 0,
             response.headers.get('etag') if response else None,
             response.headers.get('last-modified') if response else None,error_class))

    def _fetch_url(self, lease: Lease, item: dict, now: int) -> str:
        with self.db.connect() as con:
            prior = con.execute('''SELECT etag,last_modified,raw_blob_ref FROM fetches
                WHERE url_id=? AND http_status=200 AND raw_blob_ref IS NOT NULL
                ORDER BY fetched_at_ms DESC, rowid DESC LIMIT 1''',
                (lease.entity_id,)).fetchone()
        try:
            response = self.fetcher.fetch(item['normalized_url'],
                                          etag=prior['etag'] if prior else None,
                                          last_modified=prior['last_modified'] if prior else None)
        except FetchError:
            with self.db.transaction() as con:
                Queue._guard(con,lease)
                self._audit_fetch_tx(con,lease,None,now,error_class='transport_error')
            self.queue.fail(lease,'fetch_transport',retryable=True,
                            retry_after_ms=min(60000,1000*(2**min(lease.attempt,6))),now_ms=now)
            return 'retry'
        status = response.status
        if status in (429,500,502,503,504):
            delay = retry_after_ms(response.headers.get('retry-after'),now)
            delay = delay or min(60000,1000*(2**min(lease.attempt,6)))
            with self.db.transaction() as con:
                Queue._guard(con,lease)
                self._audit_fetch_tx(con,lease,response,now,error_class=f'http_{status}')
            if status in (429,503):
                self.frontier.defer_host(item['host_id'],now+delay)
            self.queue.fail(lease,f'http_{status}',retryable=True,retry_after_ms=delay,now_ms=now)
            return 'retry'
        if status == 304 and not prior:
            self.queue.fail(lease,'invalid_304_without_body',retryable=False,now_ms=now)
            return 'dead'
        if status in (401,403):
            with self.db.transaction() as con:
                Queue._guard(con,lease)
                self._audit_fetch_tx(con,lease,response,now)
                con.execute("UPDATE urls SET status='blocked',last_seen_at_ms=? WHERE url_id=?",
                            (now,lease.entity_id))
                Queue.complete_tx(con,lease,now)
            return 'blocked'
        if status not in (200,301,302,303,307,308,304,404,410):
            with self.db.transaction() as con:
                Queue._guard(con,lease)
                self._audit_fetch_tx(con,lease,response,now,error_class=f'http_{status}')
            self.queue.fail(lease,f'http_{status}',retryable=False,now_ms=now)
            return 'dead'
        digest = blob_ref = None
        if status == 200:
            if response.content_type not in ('text/html','text/plain','application/xhtml+xml',
                                             'application/xml','text/xml'):
                self.queue.fail(lease,'unsupported_content_type',retryable=False,now_ms=now)
                return 'dead'
            digest,blob_ref = self.store.put(response.body)
        with self.db.transaction() as con:
            Queue._guard(con,lease)
            self._audit_fetch_tx(con,lease,response,now,digest=digest,blob_ref=blob_ref)
            con.execute('UPDATE urls SET status=?,last_seen_at_ms=? WHERE url_id=?',
                        ('deleted' if status in (404,410) else 'fetched',now,lease.entity_id))
            # Redirects enqueue separate scoped tasks, each requiring host permission.
            if status in (301,302,303,307,308):
                location = response.headers.get('location')
                hops = int(lease.payload.get('redirect_hops',0))
                if location and hops < 5:
                    try:
                        self.frontier.add_url_tx(con,item['source_id'],
                            urljoin(response.url,location),run_id=lease.run_id,
                            depth=item['depth'],parent_url_id=lease.entity_id,
                            redirect_hops=hops+1,now_ms=now)
                    except (URLRejected,ValueError):
                        pass
            if (status == 200 and item['depth'] < self.max_depth
                    and response.content_type in ('text/html','application/xhtml+xml')):
                parser = LinkParser()
                parser.feed(response.body.decode('utf-8',errors='replace'))
                for href in parser.hrefs[:self.max_links_per_page]:
                    try:
                        self.frontier.add_url_tx(con,item['source_id'],
                            urljoin(response.url,href),run_id=lease.run_id,
                            depth=item['depth']+1,parent_url_id=lease.entity_id,
                            now_ms=now)
                    except (URLRejected,ValueError):
                        continue
            Queue.complete_tx(con,lease,now)
        return ('not_modified' if status == 304 else 'deleted' if status in (404,410)
                else 'redirect' if status in (301,302,303,307,308) else 'fetched')
