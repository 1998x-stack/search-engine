"""Small version-pinned OpenSearch HTTP adapter, with injectable transport for tests.

No user supplied arbitrary DSL or index names. TLS is verified by urllib; redirects
are refused to avoid credential forwarding to unrelated hosts.
"""
from __future__ import annotations
from dataclasses import dataclass
import base64
import json
from pathlib import Path
import re
import ssl
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import build_opener, HTTPSHandler, HTTPHandler, HTTPRedirectHandler, Request

NAME = re.compile(r'^[a-z][a-z0-9_-]{1,127}$')
READ_ALIAS = 'search-documents-read'
WRITE_ALIAS = 'search-documents-write'
DEFAULT_INDEX = 'search-documents-v001'


class SearchBackendError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, transient: bool = False):
        super().__init__(message)
        self.status = status
        self.transient = transient


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise SearchBackendError('OpenSearch endpoint attempted a redirect', status=code)


@dataclass
class OpenSearchClient:
    endpoint: str
    username: str | None = None
    password: str | None = None
    timeout_s: float = 12.0

    def __post_init__(self):
        parsed = urlsplit(self.endpoint)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError('Invalid OpenSearch base URL')
        if parsed.path not in ('', '/') or parsed.query or parsed.fragment:
            raise ValueError('OpenSearch base URL cannot contain path/query')
        if bool(self.username) != bool(self.password):
            raise ValueError('Username and password must be supplied together')
        if self.username and parsed.scheme != 'https':
            raise ValueError('HTTP Basic credentials require HTTPS')
        if self.timeout_s <= 0:
            raise ValueError('Timeout must be positive')
        self.endpoint = self.endpoint.rstrip('/')

    def request(self, method: str, path: str, body: dict | bytes | None = None, *, ndjson: bool = False) -> Any:
        if not path.startswith('/') or path.startswith('//') or '://' in path:
            raise ValueError('Unsafe OpenSearch path')
        raw = (body if isinstance(body,bytes) else json.dumps(body,separators=(',',':'),ensure_ascii=False).encode('utf-8')
               if body is not None else None)
        headers = {'Accept':'application/json'}
        if raw is not None:
            headers['Content-Type'] = 'application/x-ndjson' if ndjson else 'application/json'
        if self.username:
            auth = base64.b64encode(f'{self.username}:{self.password}'.encode()).decode()
            headers['Authorization'] = f'Basic {auth}'
        request = Request(self.endpoint+path, data=raw, method=method, headers=headers)
        opener = build_opener(HTTPHandler(), HTTPSHandler(context=ssl.create_default_context()), NoRedirect())
        try:
            with opener.open(request,timeout=self.timeout_s) as response:
                payload = response.read(10_000_000)
                return json.loads(payload) if payload else {}
        except HTTPError as exc:
            error = exc.read(4096).decode('utf-8',errors='replace')
            raise SearchBackendError(f'OpenSearch HTTP {exc.code}: {error[:200]}',status=exc.code,
                                     transient=exc.code in (408,429,500,502,503,504)) from exc
        except (URLError,TimeoutError,OSError) as exc:
            raise SearchBackendError(f'OpenSearch transport failure: {type(exc).__name__}',transient=True) from exc
        except ValueError as exc:
            raise SearchBackendError('Invalid OpenSearch JSON response',transient=True) from exc

    def bulk(self, actions: list[tuple[dict,dict]]) -> list[dict]:
        if not actions:
            return []
        lines=[]
        for meta,doc in actions:
            lines.append(json.dumps(meta,ensure_ascii=False,separators=(',',':')))
            lines.append(json.dumps(doc,ensure_ascii=False,separators=(',',':')))
        response=self.request('POST','/_bulk',('\n'.join(lines)+'\n').encode(),ndjson=True)
        items=response.get('items')
        if not isinstance(items,list) or len(items)!=len(actions):
            raise SearchBackendError('OpenSearch Bulk reply missing/mismatched items',transient=True)
        return [it.get('index') for it in items]

    def create_index(self,index: str,mapping: dict) -> None:
        require_index_name(index)
        self.request('PUT',f'/{index}',mapping)

    def ensure_initial_aliases(self,index: str) -> None:
        require_index_name(index)
        self.request('POST','/_aliases',{'actions':[
            {'add':{'index':index,'alias':READ_ALIAS}},
            {'add':{'index':index,'alias':WRITE_ALIAS,'is_write_index':True}}]})

    def alias_targets(self, alias: str) -> set[str]:
        require_index_name(alias)
        try:
            result=self.request('GET',f'/_alias/{alias}')
            return set(result.keys())
        except SearchBackendError as exc:
            if exc.status==404:
                return set()
            raise

    def swap_aliases(self,new_index: str,old: set[str]):
        require_index_name(new_index)
        actions=[]
        for alias in (READ_ALIAS,WRITE_ALIAS):
            for name in sorted(old):
                actions.append({'remove':{'index':name,'alias':alias,'must_exist':True}})
            actions.append({'add':{'index':new_index,'alias':alias,
                                   **({'is_write_index':True} if alias==WRITE_ALIAS else {})}})
        self.request('POST','/_aliases',{'actions':actions})


def require_index_name(name: str) -> str:
    if not isinstance(name,str) or not NAME.fullmatch(name) or name.startswith('_'):
        raise ValueError(f'Invalid index/alias name: {name!r}')
    return name


def mapping_from_file(path: str | Path) -> dict:
    with open(path,encoding='utf-8') as file:
        mapping=json.load(file)
    if mapping.get('mappings',{}).get('dynamic')!='strict':
        raise ValueError('Index must have strict mappings')
    return mapping
