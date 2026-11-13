"""Phase 3 lexical search, no untrusted Query DSL, no semantic features yet."""
from __future__ import annotations
from dataclasses import dataclass
import re
from typing import Any

from .opensearch import OpenSearchClient,READ_ALIAS,require_index_name

_ALLOWED_LANG={'zh','en','mixed','und'}


def search_query(q:str,*,limit:int=10,language:str|None=None,
                 source_id:str|None=None,offset:int=0)->dict:
    if not isinstance(q,str) or not q.strip() or len(q)>200 or any(ord(c)<32 for c in q):
        raise ValueError('Query must have 1–200 printable characters')
    if not isinstance(limit,int) or not 1<=limit<=20:
        raise ValueError('Limit must be 1–20')
    if not isinstance(offset,int) or not 0<=offset<=200:
        raise ValueError('Offset must be 0–200; deep paging is not supported')
    if language is not None and language not in _ALLOWED_LANG:
        raise ValueError('Unsupported language filter')
    if source_id is not None and (len(source_id)>128 or not re.fullmatch(r'[a-zA-Z0-9_\-]+',source_id)):
        raise ValueError('Invalid source_id filter')
    query=q.strip()
    fields=['title^3','title.zh^4','title.en^4',
            'headings^2','headings.zh^3','headings.en^3',
            'content','content.zh','content.en']
    filters=[{'term':{'visibility':'public'}},{'term':{'is_indexable':True}}]
    if language:
        filters.append({'term':{'language':language}})
    if source_id:
        filters.append({'term':{'source_id':source_id}})
    return {
        'track_total_hits':True,
        'from':offset,'size':limit,
        '_source':['document_id','title','description','canonical_url',
                   'source_id','language','revision','updated_at_ms'],
        'highlight':{'pre_tags':[''],'post_tags':[''],
                     'fields':{'content':{'fragment_size':180,'number_of_fragments':1}}},
        'query':{'bool':{
            'filter':filters,
            'should':[
                {'multi_match':{'query':query,'fields':fields,'type':'best_fields'}},
                {'match_phrase':{'title':{'query':query,'boost':6}}},
                {'term':{'title.raw':{'value':query,'boost':12}}}
            ],
            'minimum_should_match':1}},
        'sort':[{'_score':'desc'},{'document_id':'asc'}]
    }


@dataclass
class SearchService:
    client: OpenSearchClient
    read_alias: str = READ_ALIAS

    def __post_init__(self):
        self.read_alias=require_index_name(self.read_alias)

    def search(self,q:str,*,limit:int=10,language:str|None=None,
               source_id:str|None=None,offset:int=0)->dict[str,Any]:
        payload=search_query(q,limit=limit,language=language,source_id=source_id,offset=offset)
        response=self.client.request('POST',f'/{self.read_alias}/_search',payload)
        hits=response.get('hits',{})
        rows=[]
        for item in hits.get('hits',[]):
            source=item.get('_source',{})
            content=source.get('description') or ' '.join(item.get('highlight',{}).get('content',[])) or ''
            rows.append({'document_id':source.get('document_id',item.get('_id')),
                         'title':source.get('title',''),
                         'url':source.get('canonical_url',''),
                         'source_id':source.get('source_id',''),
                         'language':source.get('language','und'),
                         'revision':source.get('revision'),
                         'score':item.get('_score',0),
                         'snippet':str(content)[:240]})
        total=hits.get('total',0)
        return {'query':q,'total':total.get('value',0) if isinstance(total,dict) else total,
                'offset':offset,'limit':limit,'results':rows}
