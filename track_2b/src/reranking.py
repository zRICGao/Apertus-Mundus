"""Bounded reranking of already authorized original evidence, never generated prose.

No service/key is inherited from generation or embeddings. An unavailable or invalid
response preserves the caller's original ranking. No document text/response is logged.
"""
import math
import re
import time
from collections import Counter
from dataclasses import dataclass
from urllib.parse import urlsplit
import httpx

POOL_SIZE=80
PER_SOURCE=12
MAX_DOCUMENT_CHARS=1200


@dataclass
class RerankResult:
    candidates:list
    diagnostics:dict


def tokens(text):
    result=re.findall(r'[a-z0-9_]+',text.lower())
    for part in re.findall(r'[\u4e00-\u9fff]+',text):
        result.extend(part[i:i+2] for i in range(len(part)-1))
        if len(part)==1:result.append(part)
    return result


def shortlist(query,candidates):
    """BM25 block scores, weak title hint, source diversity, exact-text dedup.

    This coarse pool is independent from the old top-six ranking. It cannot recover
    evidence with no lexical/dense match; parent context and dense recall remain useful.
    """
    unique={}
    for row in candidates:
        key=re.sub(r'\s+','',row['content'])
        if key not in unique:unique[key]=row
    rows=list(unique.values())
    counts=[Counter(tokens(r['content'])) for r in rows]
    q=set(tokens(query)); n=len(rows)
    average=sum(sum(c.values()) for c in counts)/max(1,n)
    df=Counter(t for c in counts for t in c if t in q)
    scored=[]
    for row,count in zip(rows,counts):
        length=sum(count.values());score=0
        for term in q:
            frequency=count[term]
            idf=math.log(1+(n-df[term]+0.5)/(df[term]+0.5))
            if frequency:
                score+=idf*frequency*2.2/(frequency+1.2*(.25+.75*length/max(1,average)))
            if term in row['title'].lower():score+=.08*idf
        scored.append(dict(row,coarse_score=score))
    scored.sort(key=lambda r:(-r['coarse_score'],-r['score'],r['page_key'],r['ordinal']))
    pool=[]; per_source=Counter()
    for row in scored:
        if per_source[row['page_key']]>=PER_SOURCE:continue
        pool.append(row);per_source[row['page_key']]+=1
        if len(pool)>=POOL_SIZE:break
    return pool


def configured(settings):
    try: host=urlsplit(settings.rerank_base_url).hostname
    except ValueError: return False
    return bool(settings.rerank_enabled and settings.rerank_base_url.strip() and settings.rerank_model.strip()
                and (settings.rerank_api_key or host in {'localhost','127.0.0.1','::1'}))


def request_scores(settings,query,documents):
    parsed=urlsplit(settings.rerank_base_url)
    if parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('invalid_endpoint')
    endpoint=settings.rerank_base_url.rstrip('/')
    mixedbread=parsed.hostname in ('api.mixedbread.com','api.mixedbread.ai') or parsed.path.rstrip('/').endswith('/reranking')
    if mixedbread:
        if parsed.path.rstrip('/')=='':endpoint+='/v1/reranking'
        elif parsed.path.rstrip('/')=='/v1':endpoint+='/reranking'
        elif not endpoint.endswith('/reranking'):raise ValueError('invalid_mixedbread_endpoint')
    elif not endpoint.endswith('/rerank'):endpoint+='/rerank'
    timeout=float(settings.rerank_timeout)
    if not math.isfinite(timeout) or not .1<=timeout<=30:raise ValueError('invalid_timeout')
    headers={'Authorization':'Bearer '+settings.rerank_api_key} if settings.rerank_api_key else {}
    payload={'model':settings.rerank_model,'query':query}
    payload.update({'input':documents,'top_k':len(documents),'return_input':False,'rewrite_query':False} if mixedbread else
                   {'documents':documents,'top_n':len(documents),'return_documents':False})
    # No redirects or retries: a key and authorized material stay at the configured endpoint.
    with httpx.Client(timeout=timeout,follow_redirects=False) as client:
        response=client.post(endpoint,headers=headers,json=payload)
        response.raise_for_status()
        data=response.json()
    rows=data.get('data' if mixedbread else 'results') if isinstance(data,dict) else None
    if mixedbread and isinstance(data,dict):
        actual=data.get('model','')
        if not isinstance(actual,str) or actual.removeprefix('mixedbread-ai/')!=settings.rerank_model.removeprefix('mixedbread-ai/'):
            raise ValueError('unexpected_model')
    if not isinstance(rows,list) or len(rows)!=len(documents):raise ValueError('incomplete_results')
    scores={}
    for row in rows:
        if not isinstance(row,dict):raise ValueError('invalid_result')
        index=row.get('index'); score=row.get('score' if mixedbread else 'relevance_score')
        if type(index) is not int or not 0<=index<len(documents) or index in scores:
            raise ValueError('invalid_index')
        if type(score) not in (int,float) or not math.isfinite(score):raise ValueError('invalid_score')
        scores[index]=score
    usage=data.get('usage',data.get('meta',{}))
    # Only numeric token counters, never arbitrary upstream strings.
    def numeric(value,depth=0):
        if depth>3:return {}
        return {k:v if type(v) in (int,float) and math.isfinite(v) else numeric(v,depth+1)
                for k,v in value.items() if k in ('tokens','input_tokens','output_tokens','total_tokens','prompt_tokens','completion_tokens') and
                (type(v) in (int,float) and math.isfinite(v) or isinstance(v,dict))} if isinstance(value,dict) else {}
    return scores,numeric(usage)


def rerank(query,candidates,settings,*,audit_only=False):
    started=time.perf_counter();pool=shortlist(query,candidates)
    pool_texts={re.sub(r'\s+','',p['content']) for p in pool}
    diagnostics={'status':'candidate_audit' if audit_only else 'pending','model':settings.rerank_model,
        'input_candidates':len(candidates),'candidate_count':len(pool),
        'candidate_evidence_ids':list(dict.fromkeys(r['evidence_uid'] for r in candidates
            if re.sub(r'\s+','',r['content']) in pool_texts))}
    if audit_only:return RerankResult(candidates,diagnostics)
    if not pool:
        diagnostics['status']='empty';return RerankResult(candidates,diagnostics)
    try:
        documents=[(r['title']+'\n'+r['content'])[:MAX_DOCUMENT_CHARS] for r in pool]
        scores,usage=request_scores(settings,query,documents)
        positions={index:rank for rank,index in enumerate(scores)}
        ranked={re.sub(r'\s+','',row['content']):dict(row,rerank_score=scores[i],rerank_input_index=i,service_rank=positions[i])
                for i,row in enumerate(pool)}
        ordered=[dict(row,rerank_score=ranked[key]['rerank_score'],coarse_score=ranked[key]['coarse_score'],
            rank_before=i+1,rerank_input_index=ranked[key]['rerank_input_index'],service_rank=ranked[key]['service_rank']) for i,row in enumerate(candidates)
            if (key:=re.sub(r'\s+','',row['content'])) in ranked]
        ordered.sort(key=lambda r:(-r['rerank_score'],r['service_rank'],r['page_key'],r['ordinal']))
        diagnostics.update(status='ok',usage=usage)
        return RerankResult(ordered,diagnostics)
    except (httpx.HTTPError,ValueError,TypeError,KeyError):
        diagnostics.update(status='fallback',reason='service_error_or_invalid_response')
        return RerankResult(candidates,diagnostics)
    finally:
        diagnostics['latency_ms']=round((time.perf_counter()-started)*1000,3)
