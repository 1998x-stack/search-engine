"""Durable, per-item OpenSearch projection from Phase 2 document versions.

Delete/noindex events index a **content-free logical tombstone** using external_gte
instead of physical deletion: this preserves version ordering across retries.
The read query always constrains is_indexable=true and visibility=public.
"""
from __future__ import annotations
from dataclasses import dataclass
import json
from pathlib import Path
import uuid
from urllib.parse import urlsplit
from typing import Any

from .db import Database,clock_ms
from .opensearch import OpenSearchClient,SearchBackendError,WRITE_ALIAS,READ_ALIAS,require_index_name
from .store import BlobStore

MAX_ATTEMPTS=8
MAX_BULK_BYTES=4_000_000
MAX_BATCH=32
LEASE_MS=60_000


@dataclass(frozen=True)
class IndexLease:
    op_id: str
    document_id: str
    revision: int
    op_type: str
    attempt: int
    lease_epoch: int
    lease_owner: str


class OutboxIndexer:
    def __init__(self, db:Database,store:BlobStore,client:OpenSearchClient,
                 *, worker_id:str='indexer-1',write_alias:str=WRITE_ALIAS):
        self.db,self.store,self.client,self.worker_id=db,store,client,worker_id
        self.write_alias=require_index_name(write_alias)

    def claim_batch(self,*,now_ms:int|None=None,count:int=MAX_BATCH)->list[IndexLease]:
        now=clock_ms() if now_ms is None else now_ms
        if count<1 or count>MAX_BATCH:
            raise ValueError('Invalid batch size')
        with self.db.transaction() as con:
            # Exhausted leases can't loop infinitely; keep failures inspectable.
            con.execute('''UPDATE index_outbox SET state='dead',lease_owner=NULL,
                leased_until_ms=NULL,last_error_class='attempt_budget_exhausted',updated_at_ms=?
                WHERE attempt>=? AND ((state IN ('pending','retry') AND scheduled_at_ms<=?)
                OR (state='leased' AND leased_until_ms<=?))''',(now,MAX_ATTEMPTS,now,now))
            rows=con.execute('''SELECT * FROM index_outbox WHERE attempt<?
                AND ((state IN ('pending','retry') AND scheduled_at_ms<=?)
                OR (state='leased' AND leased_until_ms<=?))
                ORDER BY scheduled_at_ms ASC,revision ASC,op_id ASC LIMIT ?''',
                (MAX_ATTEMPTS,now,now,count)).fetchall()
            leased=[]
            for row in rows:
                epoch=row['lease_epoch']+1
                con.execute('''UPDATE index_outbox SET state='leased',attempt=attempt+1,
                    lease_owner=?,lease_epoch=?,leased_until_ms=?,updated_at_ms=?
                    WHERE op_id=?''',(self.worker_id,epoch,now+LEASE_MS,now,row['op_id']))
                leased.append(IndexLease(row['op_id'],row['document_id'],row['revision'],
                    row['op_type'],row['attempt']+1,epoch,self.worker_id))
            return leased

    @staticmethod
    def _guard(con,lease:IndexLease):
        row=con.execute('SELECT state,lease_owner,lease_epoch FROM index_outbox WHERE op_id=?',
                        (lease.op_id,)).fetchone()
        if not row or (row['state'],row['lease_owner'],row['lease_epoch']) != (
            'leased',lease.lease_owner,lease.lease_epoch):
            raise RuntimeError('Stale outbox lease')

    def _finish(self,lease:IndexLease,*,now:int,success:bool=False,
                superseded:bool=False,error:str='',transient:bool=True):
        with self.db.transaction() as con:
            self._guard(con,lease)
            if success:
                con.execute('''INSERT INTO index_receipts
                    (index_alias,document_id,revision,op_id,indexed_at_ms)
                    VALUES(?,?,?,?,?) ON CONFLICT(index_alias,document_id) DO UPDATE SET
                    revision=excluded.revision,op_id=excluded.op_id,
                    indexed_at_ms=excluded.indexed_at_ms
                    WHERE excluded.revision >= index_receipts.revision''',
                    (self.write_alias,lease.document_id,lease.revision,lease.op_id,now))
            if success or superseded:
                state='acked'
                delay=0
            else:
                state='retry' if transient and lease.attempt<MAX_ATTEMPTS else 'dead'
                delay=min(300_000,1000*(2**min(lease.attempt,8)))
            con.execute('''UPDATE index_outbox SET state=?,scheduled_at_ms=?,lease_owner=NULL,
                leased_until_ms=NULL,last_error_class=?,updated_at_ms=? WHERE op_id=?''',
                (state,now+delay,None if success else ('superseded' if superseded else error[:160]),now,lease.op_id))
            return state

    def _materialize(self,lease:IndexLease,now:int)->tuple[dict,dict]|None:
        with self.db.connect() as con:
            row=con.execute('''SELECT d.source_id,d.canonical_url,d.latest_revision,d.updated_at_ms,
                v.title,v.description,v.language,v.normalized_sha256,v.text_blob_ref,
                v.is_indexable,v.is_tombstone,v.revision
                FROM documents d JOIN document_versions v
                ON v.document_id=d.document_id AND v.revision=?
                WHERE d.document_id=?''',(lease.revision,lease.document_id)).fetchone()
        if not row:
            raise ValueError('Missing authoritative document/version')
        if row['latest_revision'] != lease.revision:
            return None
        if (lease.op_type=='upsert') != bool(row['is_indexable']):
            raise ValueError('Outbox event and document state disagree')
        active=bool(row['is_indexable'])
        content=''
        if active:
            if not row['text_blob_ref']:
                raise ValueError('Indexable revision has no text blob')
            content=self.store.get(row['text_blob_ref']).decode('utf-8')
            if len(content.encode('utf-8')) > MAX_BULK_BYTES - 10_000:
                raise ValueError('Index document exceeds per-item payload limit')
        body={
            'document_id':lease.document_id,'source_id':row['source_id'],
            'version_id':f'{lease.document_id}:{lease.revision}',
            'revision':lease.revision,'canonical_url':row['canonical_url'],
            'host':urlsplit(row['canonical_url']).hostname or '',
            'language':row['language'] or 'und' if active else 'und',
            'visibility':'public','allowed_principals':[],
            'title':row['title'] or '' if active else '',
            'description':row['description'] or '' if active else '',
            'content':content,'normalized_sha256':row['normalized_sha256'] or '' if active else '',
            'is_indexable':active,'updated_at_ms':row['updated_at_ms'],
            'indexed_at_ms':now,
        }
        meta={'index':{'_index':self.write_alias,'_id':lease.document_id,
                       'version':lease.revision,'version_type':'external_gte'}}
        return meta,body

    def run_once(self,*,now_ms:int|None=None,count:int=MAX_BATCH)->dict[str,int]:
        now=clock_ms() if now_ms is None else now_ms
        leases=self.claim_batch(now_ms=now,count=count)
        totals={'claimed':len(leases),'acked':0,'superseded':0,'retry':0,'dead':0}
        if not leases:
            return totals
        pending: list[tuple[IndexLease,dict,dict]]=[]
        for lease in leases:
            try:
                prepared=self._materialize(lease,now)
                if prepared is None:
                    self._finish(lease,now=now,superseded=True)
                    totals['superseded']+=1
                else:
                    meta,body=prepared
                    # Bound the serialized request; individual huge items are dead-lettered.
                    if len(json.dumps(body,ensure_ascii=False).encode()) > MAX_BULK_BYTES:
                        raise ValueError('Index document exceeds bulk payload limit')
                    pending.append((lease,meta,body))
            except (OSError,ValueError,UnicodeError) as exc:
                state=self._finish(lease,now=now,error=type(exc).__name__,transient=isinstance(exc,OSError))
                totals[state]+=1
        # Each batch is bounded by MAX_BULK_BYTES, preserving per-item acknowledgements.
        batches=[]
        group=[];size=0
        for lease,meta,body in pending:
            size_of=len(json.dumps(body,ensure_ascii=False).encode())+300
            if group and size+size_of>MAX_BULK_BYTES:
                batches.append(group);group=[];size=0
            group.append((lease,meta,body));size+=size_of
        if group:
            batches.append(group)
        for batch in batches:
            try:
                responses=self.client.bulk([(meta,body) for _,meta,body in batch])
            except SearchBackendError as exc:
                for lease,_,_ in batch:
                    state=self._finish(lease,now=now,error=f'bulk_http_{exc.status or "transport"}',
                                       transient=exc.transient)
                    totals[state]+=1
                continue
            for (lease,meta,body),result in zip(batch,responses):
                # Malformed or reordered response is NOT a successful ACK.
                if not isinstance(result,dict) or result.get('_id')!=lease.document_id:
                    state=self._finish(lease,now=now,error='bulk_response_mismatch',transient=True)
                else:
                    status=int(result.get('status',0))
                    if 200<=status<300:
                        state=self._finish(lease,now=now,success=True)
                    elif status == 409:
                        # A higher revision might have overtaken this in-flight item.
                        with self.db.connect() as con:
                            latest=con.execute('SELECT latest_revision FROM documents WHERE document_id=?',
                                               (lease.document_id,)).fetchone()
                        if latest and latest['latest_revision']>lease.revision:
                            state=self._finish(lease,now=now,superseded=True)
                            totals['superseded']+=1
                            continue
                        state=self._finish(lease,now=now,error='bulk_item_409',transient=True)
                    elif status in (408,429,500,502,503,504):
                        state=self._finish(lease,now=now,error=f'bulk_item_{status}',transient=True)
                    else:
                        state=self._finish(lease,now=now,error=f'bulk_item_{status}',transient=False)
                totals[state]+=1
        return totals

    def retry_dead(self,*,document_id:str|None=None,now_ms:int|None=None)->int:
        now=clock_ms() if now_ms is None else now_ms
        with self.db.transaction() as con:
            if document_id:
                result=con.execute('''UPDATE index_outbox SET state='pending',attempt=0,
                   scheduled_at_ms=?,lease_owner=NULL,leased_until_ms=NULL,updated_at_ms=?
                   WHERE state='dead' AND document_id=?''',(now,now,document_id))
            else:
                result=con.execute('''UPDATE index_outbox SET state='pending',attempt=0,
                   scheduled_at_ms=?,lease_owner=NULL,leased_until_ms=NULL,updated_at_ms=?
                   WHERE state='dead' ''',(now,now))
            return result.rowcount

    def audit_local(self)->dict[str,int]:
        with self.db.connect() as con:
            result=con.execute('''SELECT
              (SELECT COUNT(*) FROM documents WHERE latest_revision>0) AS total,
              (SELECT COUNT(*) FROM documents d LEFT JOIN index_receipts i
               ON i.document_id=d.document_id AND i.index_alias=?
               WHERE d.latest_revision>0 AND COALESCE(i.revision,0)<d.latest_revision) AS lagging,
              (SELECT COUNT(*) FROM index_outbox WHERE state='dead') AS dead,
              (SELECT COUNT(*) FROM index_outbox WHERE state IN ('pending','retry','leased')) AS pending''',
              (self.write_alias,)).fetchone()
            return dict(result)
