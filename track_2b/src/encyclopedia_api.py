"""GM encyclopedia workspace; player publication is intentionally a separate future API."""
import json
from pathlib import Path
from contextlib import closing
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from database import atomic, connect_db
import knowledge as k

router=APIRouter(tags=['Encyclopedia'])
_db_file=''


def configure(db_file):
    global _db_file
    _db_file=db_file


def page_detail(conn,key,viewer='gm',timeline_id=None):
    p=k.page_allowed(conn,key,viewer,timeline_id)
    if not p:
        raise HTTPException(404,'Encyclopedia page not found')
    meta=dict(conn.execute('SELECT * FROM wiki_page_meta WHERE page_key=?',(key,)).fetchone())
    sources=[]
    for s in conn.execute('SELECT s.*,r.content,r.recorded_at,r.effective_tick FROM wiki_dependencies d JOIN knowledge_sources s ON s.source_key=d.source_key JOIN source_revisions r ON r.revision_uid=d.revision_uid WHERE d.page_key=?',(key,)):
        sources.append(dict(s))
    current=conn.execute('SELECT * FROM wiki_revisions WHERE revision_uid=?',(meta['revision_uid'],)).fetchone()
    # Historical contents may have been more privileged than the current page.
    revisions=[]
    for r in conn.execute('SELECT * FROM wiki_revisions WHERE page_key=? ORDER BY rowid DESC',(key,)):
        if viewer!='gm' and r['revision_uid']!=meta['revision_uid']:
            continue
        revisions.append(dict(r))
    conflicts=[]
    candidates=[]
    if viewer=='gm':
        conflicts=[dict(r) for r in conn.execute('SELECT * FROM wiki_conflicts WHERE page_key=? AND revision_uid IN (SELECT revision_uid FROM knowledge_sources WHERE active=1) AND status=\'pending\'',(key,))]
        for e in conn.execute("SELECT * FROM wiki_links WHERE from_key=? AND status='candidate'",(key,)):
            if k.entity_page(conn,e['target_uid'],viewer,timeline_id):
                candidates.append(dict(e))
    return {'page':{**p,**meta,'effective_tick':current['effective_tick']},'sources':sources,
            'revisions':revisions,'links':k.links(conn,key,viewer,timeline_id),'conflicts':conflicts,'candidates':candidates,
            'evidence':[dict(r) for r in conn.execute('SELECT b.* FROM evidence_blocks b JOIN wiki_page_evidence d USING(evidence_uid) WHERE d.page_key=?',(key,))],
            'changes':[dict(r) for r in conn.execute('SELECT * FROM knowledge_changes WHERE subject=? ORDER BY rowid DESC',(key,))] if viewer=='gm' else [],
            'epoch':k.epoch(conn)}


class SourceEdit(BaseModel):
    title:str=Field(min_length=1,max_length=200)
    content:str=Field(min_length=1,max_length=500000)
    visibility:str='gm'
    timeline_id:int|None=None
    retrieval_enabled:bool=True
    expected_revision:str|None=None
    reason:str='GM edit'


def edit_source(conn,key,req):
    from world import validate_visibility, now
    validate_visibility(req.visibility)
    old=conn.execute('SELECT * FROM knowledge_sources WHERE source_key=? AND active=1',(key,)).fetchone() if key else None
    if key and (not old or old['revision_uid']!=req.expected_revision):
        raise HTTPException(409,'The source has been updated; reload and compare changes')
    if key and key.startswith('document/'):
        from rag import chunk_text
        doc_id=int(key.split('/')[1])
        chunks=chunk_text(req.content)
        conn.execute("UPDATE rag_documents SET title=?,raw_text=?,visibility=?,timeline_id=?,hidden=?,chunk_size=?,embedding_model='' WHERE id=?",
                     (req.title,req.content,req.visibility,req.timeline_id,int(not req.retrieval_enabled),len(chunks),doc_id))
        conn.execute('DELETE FROM rag_chunks WHERE doc_id=?',(doc_id,))
        conn.executemany('INSERT INTO rag_chunks(doc_id,chunk_index,chunk_text) VALUES(?,?,?)',[(doc_id,i,c) for i,c in enumerate(chunks)])
    elif key and key.startswith('lore/'):
        if req.visibility!='gm' or req.timeline_id is not None or not req.retrieval_enabled:
            raise HTTPException(422,'Legacy entries remain GM-only; create a separate source for a different scope')
        conn.execute('UPDATE lorebook SET keywords=?,content=? WHERE id=?',(req.title,req.content,int(key.split('/')[1])))
    elif not key or key.startswith('manual/'):
        # Documents are the compatibility storage for new uploads and authored sources.
        from rag import chunk_text
        chunks=chunk_text(req.content)
        doc=conn.execute('INSERT INTO rag_documents(title,source,raw_text,visibility,timeline_id,hidden,chunk_size) VALUES(?,?,?,?,?,?,?)',
                         (req.title,'encyclopedia',req.content,req.visibility,req.timeline_id,int(not req.retrieval_enabled),len(chunks))).lastrowid
        conn.executemany('INSERT INTO rag_chunks(doc_id,chunk_index,chunk_text) VALUES(?,?,?)',[(doc,i,c) for i,c in enumerate(chunks)])
        key=f'document/{doc}'
    else:
        raise HTTPException(422,'This page comes from world records; edit the entity, rule, or event source instead')
    k.sync(conn)
    revision=conn.execute('SELECT revision_uid FROM knowledge_sources WHERE source_key=?',(key,)).fetchone()[0]
    if not old or revision!=old['revision_uid']:
        conn.execute('UPDATE source_revisions SET actor=?,reason=? WHERE revision_uid=?',('gm',req.reason,revision))
    return key


@router.post('/api/wiki/sources')
def create_source(req:SourceEdit):
    with closing(connect_db(_db_file)) as conn, atomic(conn):
        key=edit_source(conn,None,req)
        return {'status':'success','page_key':key}


@router.put('/api/wiki/source')
def update_source(key:str,req:SourceEdit):
    with closing(connect_db(_db_file)) as conn, atomic(conn):
        k.sync(conn)
        key=edit_source(conn,key,req)
        return {'status':'success','page_key':key}


@router.delete('/api/wiki/source')
def delete_source(key:str,expected_revision:str):
    with closing(connect_db(_db_file)) as conn, atomic(conn):
        k.sync(conn)
        old=conn.execute('SELECT * FROM knowledge_sources WHERE source_key=? AND active=1',(key,)).fetchone()
        if not old or old['revision_uid']!=expected_revision:
            raise HTTPException(409,'Source data has changed; refresh before proceeding')
        if key.startswith('document/'):
            doc_id=int(key.split('/')[1])
            conn.execute('DELETE FROM rag_chunks WHERE doc_id=?',(doc_id,))
            conn.execute('DELETE FROM rag_documents WHERE id=?',(doc_id,))
        elif key.startswith('lore/'):
            conn.execute('DELETE FROM lorebook WHERE id=?',(int(key.split('/')[1]),))
        else:
            raise HTTPException(422,'Delete this entry through its world-record source')
        k.sync(conn)
        return {'status':'success'}


class RestoreRevision(BaseModel):
    revision_uid:str
    expected_revision:str


@router.post('/api/wiki/source/restore')
def restore_revision(key:str,req:RestoreRevision):
    with closing(connect_db(_db_file)) as conn, atomic(conn):
        k.sync(conn)
        source=conn.execute('SELECT * FROM knowledge_sources WHERE source_key=? AND active=1',(key,)).fetchone()
        rev=conn.execute('SELECT * FROM source_revisions WHERE revision_uid=?',(req.revision_uid,)).fetchone()
        if not source or not rev or source['source_uid']!=rev['source_uid']:
            raise HTTPException(404,'Source revision does not exist')
        metadata=json.loads(rev['metadata'])
        edit_source(conn,key,SourceEdit(title=metadata['title'],content=rev['content'],visibility=metadata['scope'],timeline_id=metadata['team'],
                                       retrieval_enabled=bool(metadata['enabled']),expected_revision=req.expected_revision,reason='Restore source revision '+req.revision_uid))
        return {'status':'success','page_key':key}


@router.get('/api/wiki/source/history')
def source_history(key:str):
    with closing(connect_db(_db_file)) as conn:
        s=conn.execute('SELECT * FROM knowledge_sources WHERE source_key=? AND active=1',(key,)).fetchone()
        if not s:
            raise HTTPException(404,'Source not found')
        return {'revisions':[dict(r) for r in conn.execute('SELECT * FROM source_revisions WHERE source_uid=? ORDER BY rowid DESC',(s['source_uid'],))]}


@router.post('/api/wiki/retry')
def retry(key:str):
    with closing(connect_db(_db_file)) as conn, atomic(conn):
        k.sync(conn)
        result=conn.execute("UPDATE knowledge_jobs SET status='pending',attempts=0,next_attempt=0,last_error='' WHERE source_key=? AND revision_uid=(SELECT revision_uid FROM knowledge_sources WHERE source_key=?) AND status!='running'",(key,key))
        return {'status':'success','queued':result.rowcount}


class LinkReview(BaseModel):
    link_uid:str
    approve:bool


@router.post('/api/wiki/links/review')
def review_link(req:LinkReview):
    with closing(connect_db(_db_file)) as conn, atomic(conn):
        k.sync(conn)
        link=conn.execute("SELECT * FROM wiki_links WHERE link_uid=? AND status='candidate'",(req.link_uid,)).fetchone()
        if not link or not k.page_allowed(conn,link['from_key']) or not k.entity_page(conn,link['target_uid'],'gm',link['timeline_id']):
            raise HTTPException(409,'Candidate has expired; refresh')
        conn.execute('UPDATE wiki_links SET status=? WHERE link_uid=?',('valid' if req.approve else 'rejected',req.link_uid))
        k.log_change(conn,link['from_key'],'link_review',dict(link),{'status':'valid' if req.approve else 'rejected'},'gm')
        # A reviewed mention is still not an entity_relations fact.
        return {'status':'success'}


@router.post('/api/wiki/conflicts/dismiss')
def dismiss(conflict_uid:str):
    with closing(connect_db(_db_file)) as conn, atomic(conn):
        conflict=conn.execute('SELECT * FROM wiki_conflicts WHERE conflict_uid=?',(conflict_uid,)).fetchone()
        if conflict:
            conn.execute("UPDATE wiki_conflicts SET status='dismissed' WHERE conflict_uid=?",(conflict_uid,))
            k.log_change(conn,conflict['page_key'],'conflict_dismissed',conflict_uid,None,'gm')
        return {'status':'success'}


@router.get('/api/wiki/retrieval')
def retrieval(query:str,timeline_id:int|None=None,one_hop:bool=False,rerank:bool|None=None):
    with closing(connect_db(_db_file)) as conn:
        with atomic(conn):k.sync(conn)
        result=k.retrieve(conn,query,timeline_id=timeline_id,one_hop=one_hop,rerank=rerank)
        return {'evidence':result.evidence,'trace':result.trace,'prompt':result.text,'budget_chars':result.budget_chars,'diagnostics':result.diagnostics}


def scan(conn,folder):
    root=Path(__file__).resolve().parent/'campaigns'
    target=(root/folder).resolve()
    if not target.is_relative_to(root.resolve()) or not (target/'campaign.json').is_file():
        raise HTTPException(422,'Select a valid campaign directory under campaigns')
    from campaigns import is_campaign_file
    if not is_campaign_file(target/'campaign.json'):
        raise HTTPException(422,'Invalid campaign')
    items=[]
    for path in sorted((target/'knowledge').glob('*')):
        if path.suffix.lower() not in ('.txt','.md') or not path.is_file() or not path.resolve().is_relative_to(target):
            continue
        content=path.read_text(encoding='utf-8')
        matches=conn.execute('SELECT * FROM rag_documents WHERE source IN (?,?)',(path.name,str(path.relative_to(root)))).fetchall()
        old=matches[0] if len(matches)==1 else None
        key=f"document/{old['id']}" if old else None
        source=conn.execute('SELECT * FROM knowledge_sources WHERE source_key=?',(key,)).fetchone() if key else None
        revision=conn.execute('SELECT content FROM source_revisions WHERE revision_uid=?',(source['revision_uid'],)).fetchone() if source else None
        items.append(dict(filename=path.name,title=path.stem,content=content,hash=k.digest(content),key=key,
                          expected_revision=source['revision_uid'] if source else None,
                          state='ambiguous' if len(matches)>1 else 'unchanged' if revision and revision[0]==content else 'changed' if old else 'new'))
    return items


@router.get('/api/wiki/scan')
def scan_directory(folder:str):
    with closing(connect_db(_db_file)) as conn, atomic(conn):
        k.sync(conn)
        return {'files':scan(conn,folder)}


class ImportFile(BaseModel):
    filename:str
    content_hash:str
    expected_revision:str|None=None


@router.post('/api/wiki/scan/import')
def import_file(folder:str,req:ImportFile):
    with closing(connect_db(_db_file)) as conn, atomic(conn):
        k.sync(conn)
        item=next((r for r in scan(conn,folder) if r['filename']==req.filename),None)
        if not item or item['hash']!=req.content_hash or item['expected_revision']!=req.expected_revision or item['state']=='ambiguous':
            raise HTTPException(409,'File or save source has changed; compare again')
        old=conn.execute('SELECT * FROM knowledge_sources WHERE source_key=?',(item['key'],)).fetchone() if item['key'] else None
        key=edit_source(conn,item['key'],SourceEdit(title=item['title'],content=item['content'],expected_revision=item['expected_revision'],
                       visibility=old['visibility'] if old else 'gm',timeline_id=old['timeline_id'] if old else None,
                       retrieval_enabled=bool(old['enabled']) if old else True,reason='Imported from knowledge file'))
        conn.execute('UPDATE rag_documents SET source=? WHERE id=?',(str(Path(folder)/'knowledge'/req.filename),int(key.split('/')[1])))
        return {'status':'success','page_key':key}
