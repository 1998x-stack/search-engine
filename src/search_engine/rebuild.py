"""Offline, operator-gated full index generation and atomic alias switch.

Workers must be stopped. We fail if SQLite's version snapshot changes; the final
alias operation is performed while a short BEGIN IMMEDIATE fence prevents writes.
This is not a zero-downtime online migration facility.
"""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
from .db import Database,clock_ms
from .indexing import IndexLease,OutboxIndexer
from .opensearch import OpenSearchClient,READ_ALIAS,WRITE_ALIAS,SearchBackendError,require_index_name,mapping_from_file
from .store import BlobStore


REBUILD_BATCH_BYTES=4_000_000

def _snapshot(con):
    rows=con.execute('SELECT document_id,latest_revision FROM documents WHERE latest_revision>0 ORDER BY document_id').fetchall()
    digest=hashlib.sha256()
    for r in rows:
        digest.update(f'{r["document_id"]}:{r["latest_revision"]}\n'.encode())
    return digest.hexdigest(),[(r['document_id'],r['latest_revision']) for r in rows]


class IndexManager:
    def __init__(self,db:Database,store:BlobStore,client:OpenSearchClient,mapping_path:Path):
        self.db,self.store,self.client,self.mapping_path=db,store,client,Path(mapping_path)

    def initialize(self,index:str)->None:
        require_index_name(index)
        if self.client.alias_targets(READ_ALIAS) or self.client.alias_targets(WRITE_ALIAS):
            raise ValueError('Existing aliases: refusing to initialize blindly')
        self.client.create_index(index,mapping_from_file(self.mapping_path))
        self.client.ensure_initial_aliases(index)

    def rebuild_offline(self,new_index:str,*,confirmed_quiescent:bool=False)->dict:
        require_index_name(new_index)
        if not confirmed_quiescent:
            raise ValueError('Stop all processors/indexers and explicitly confirm quiescence')
        old=self.client.alias_targets(READ_ALIAS)
        write=self.client.alias_targets(WRITE_ALIAS)
        if len(old)!=1 or old!=write or new_index in old:
            raise ValueError('Expected one shared read/write alias to old index')
        with self.db.connect() as con:
            snapshot,versions=_snapshot(con)
            pending=con.execute("SELECT COUNT(*) FROM index_outbox WHERE state!='acked'").fetchone()[0]
        if pending:
            raise ValueError('Outbox must be fully ACKed before offline rebuild')
        self.client.create_index(new_index,mapping_from_file(self.mapping_path))
        indexer=OutboxIndexer(self.db,self.store,self.client,write_alias=new_index)
        now=clock_ms()
        actions=[]
        action_bytes=0
        def flush() -> None:
            nonlocal actions,action_bytes
            if not actions:
                return
            responses=self.client.bulk(actions)
            for (meta,_),response in zip(actions,responses):
                if (not isinstance(response,dict) or response.get('_id')!=meta['index']['_id']
                        or not 200<=int(response.get('status',0))<300):
                    raise SearchBackendError('Rebuild bulk failed; old alias preserved')
            actions=[]
            action_bytes=0
        for doc_id,rev in versions:
            with self.db.connect() as con:
                row=con.execute('''SELECT is_indexable FROM document_versions
                    WHERE document_id=? AND revision=?''',(doc_id,rev)).fetchone()
            if not row:
                raise RuntimeError('Missing document version during rebuild')
            lease=IndexLease('rebuild',doc_id,rev,'upsert' if row['is_indexable'] else 'delete',
                             0,0,'rebuild')
            prepared=indexer._materialize(lease,now)
            if prepared is None:
                raise RuntimeError('Document changed during offline rebuild')
            meta,body=prepared
            payload_bytes=len(json.dumps(body,ensure_ascii=False).encode())+300
            if payload_bytes>REBUILD_BATCH_BYTES:
                raise ValueError('Single document too large for rebuild Bulk budget')
            if actions and (len(actions)>=32 or action_bytes+payload_bytes>REBUILD_BATCH_BYTES):
                flush()
            actions.append((meta,body))
            action_bytes+=payload_bytes
        flush()
        # Index refresh is required before checking count. For full P0 indexes all
        # logical tombstones are retained, so physical count = number of documents.
        self.client.request('POST',f'/{new_index}/_refresh')
        counted=self.client.request('GET',f'/{new_index}/_count').get('count',-1)
        if counted!=len(versions):
            raise RuntimeError('Rebuild count mismatch; old alias preserved')
        # SQLite writer fence blocks a late processor commit until after alias switch.
        # Operator must also have stopped indexer; it is a remote writer not fenced by SQL.
        with self.db.transaction() as con:
            check,_=_snapshot(con)
            if snapshot!=check:
                raise RuntimeError('Document versions changed during rebuild')
            pending=con.execute("SELECT COUNT(*) FROM index_outbox WHERE state!='acked'").fetchone()[0]
            if pending:
                raise RuntimeError('Pending outbox changes during rebuild')
            if (self.client.alias_targets(READ_ALIAS)!=old or
                    self.client.alias_targets(WRITE_ALIAS)!=old):
                raise RuntimeError('Aliases changed during rebuild')
            self.client.swap_aliases(new_index,old)
        return {'index':new_index,'documents':len(versions),'snapshot_sha256':snapshot,
                'previous_index':next(iter(old))}
