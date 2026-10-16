"""Versioned encyclopedia projections. Source records remain authoritative.

All readers use the same scope/version checks. Model output can select evidence,
never manufacture world facts. This module does not open a production database.
"""
import hashlib
import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field

COMPILER_VERSION = 'evidence-v1'
TABLES = ('knowledge_sources', 'source_revisions', 'evidence_blocks', 'wiki_revisions',
          'wiki_page_meta', 'wiki_dependencies', 'wiki_page_evidence', 'wiki_links', 'knowledge_jobs', 'wiki_conflicts','knowledge_changes')


def uid(prefix):
    return prefix + '_' + uuid.uuid4().hex


def pack(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def digest(value):
    return hashlib.sha256((value if isinstance(value, str) else pack(value)).encode()).hexdigest()


def init(conn):
    statements = [
        '''CREATE TABLE IF NOT EXISTS knowledge_sources(source_key TEXT PRIMARY KEY, source_uid TEXT UNIQUE NOT NULL,
        title TEXT NOT NULL, kind TEXT NOT NULL, visibility TEXT NOT NULL, timeline_id INTEGER,
        enabled INTEGER NOT NULL, active INTEGER NOT NULL, revision_uid TEXT NOT NULL, entity_uid TEXT,
        fingerprint TEXT NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS source_revisions(revision_uid TEXT PRIMARY KEY,source_uid TEXT NOT NULL,
        parent_uid TEXT,content TEXT NOT NULL,content_hash TEXT NOT NULL,recorded_at TEXT NOT NULL,
        effective_tick INTEGER,actor TEXT NOT NULL,reason TEXT NOT NULL,metadata TEXT NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS evidence_blocks(evidence_uid TEXT PRIMARY KEY,revision_uid TEXT NOT NULL,
        ordinal INTEGER NOT NULL,start_offset INTEGER NOT NULL,end_offset INTEGER NOT NULL,content TEXT NOT NULL,
        content_hash TEXT NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS wiki_revisions(revision_uid TEXT PRIMARY KEY,page_key TEXT NOT NULL,
        parent_uid TEXT,body TEXT NOT NULL,title TEXT NOT NULL,recorded_at TEXT NOT NULL,effective_tick INTEGER,
        input_revisions TEXT NOT NULL,compiler_version TEXT NOT NULL,model TEXT NOT NULL,actor TEXT NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS wiki_page_meta(page_key TEXT PRIMARY KEY,revision_uid TEXT NOT NULL,
        kind TEXT NOT NULL,timeline_id INTEGER,enabled INTEGER NOT NULL,owner_key TEXT NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS wiki_dependencies(page_key TEXT NOT NULL,source_key TEXT NOT NULL,
        revision_uid TEXT NOT NULL,PRIMARY KEY(page_key,source_key))''',
        '''CREATE TABLE IF NOT EXISTS wiki_page_evidence(page_key TEXT NOT NULL,evidence_uid TEXT NOT NULL,
        PRIMARY KEY(page_key,evidence_uid))''',
        '''CREATE TABLE IF NOT EXISTS wiki_links(link_uid TEXT PRIMARY KEY,from_key TEXT NOT NULL,
        target_kind TEXT NOT NULL,target_uid TEXT NOT NULL,link_type TEXT NOT NULL,origin TEXT NOT NULL,
        status TEXT NOT NULL,evidence_uid TEXT NOT NULL,revision_uid TEXT NOT NULL,visibility TEXT NOT NULL,
        timeline_id INTEGER)''',
        '''CREATE TABLE IF NOT EXISTS knowledge_jobs(job_uid TEXT PRIMARY KEY,source_key TEXT NOT NULL,
        revision_uid TEXT NOT NULL,compiler_version TEXT NOT NULL,epoch TEXT NOT NULL,status TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,next_attempt REAL NOT NULL DEFAULT 0,lease_until REAL NOT NULL DEFAULT 0,
        last_error TEXT NOT NULL DEFAULT '',UNIQUE(source_key,revision_uid,compiler_version))''',
        '''CREATE TABLE IF NOT EXISTS wiki_conflicts(conflict_uid TEXT PRIMARY KEY,page_key TEXT NOT NULL,
        revision_uid TEXT NOT NULL,kind TEXT NOT NULL,detail TEXT NOT NULL,status TEXT NOT NULL,
        recorded_at TEXT NOT NULL)''',
        '''CREATE TABLE IF NOT EXISTS knowledge_changes(change_uid TEXT PRIMARY KEY,recorded_at TEXT NOT NULL,
        subject TEXT NOT NULL,kind TEXT NOT NULL,before_value TEXT NOT NULL,after_value TEXT NOT NULL,actor TEXT NOT NULL)''',
        'CREATE INDEX IF NOT EXISTS evidence_revision ON evidence_blocks(revision_uid,ordinal)',
        'CREATE INDEX IF NOT EXISTS wiki_revision_page ON wiki_revisions(page_key,recorded_at)',
        'CREATE INDEX IF NOT EXISTS wiki_link_from ON wiki_links(from_key)',
        'CREATE INDEX IF NOT EXISTS wiki_link_to ON wiki_links(target_kind,target_uid)',
        'CREATE INDEX IF NOT EXISTS wiki_dependency_source ON wiki_dependencies(source_key)',
    ]
    for sql in statements:
        conn.execute(sql)


def epoch(conn):
    row = conn.execute("SELECT value FROM system_state WHERE key='world_epoch'").fetchone()
    return row[0] if row else ''


def log_change(conn,subject,kind,before,after,actor='system'):
    from world import now
    conn.execute('INSERT INTO knowledge_changes VALUES(?,?,?,?,?,?,?)',
                 (uid('change'),now(),subject,kind,pack(before),pack(after),actor))


def allowed(scope, team, viewer='gm', timeline_id=None):
    from world import visible
    if not visible(scope, viewer, timeline_id):
        return False
    # A GM can browse all teams, but a team-context simulation cannot borrow others' knowledge.
    if timeline_id is not None and team not in (None, timeline_id):
        return False
    if scope.startswith('team:') and timeline_id is not None and scope != f'team:{timeline_id}':
        return False
    return not (viewer != 'gm' and team is not None and team != timeline_id)


def terms(text):
    result = set(re.findall(r'[a-z0-9_]+', text.lower()))
    for seq in re.findall(r'[\u4e00-\u9fff]+', text):
        result.update(seq[i:i+2] for i in range(len(seq)-1))
        if len(seq) == 1:
            result.add(seq)
    return result


def collect(conn):
    """Compatibility adapters: every accepted source enters the same revision pipeline."""
    from world import visible, entity_state_context
    result = {}
    def add(key, title, body, kind='lore', scope='gm', team=None, enabled=True, entity=None, events=None, tick=None):
        result[key] = dict(key=key,title=title,body=body,kind=kind,scope=scope,team=team,
                           enabled=int(enabled),entity=entity,events=events or [],tick=tick)
    for r in conn.execute('SELECT * FROM lorebook'):
        add(f"lore/{r['id']}",r['keywords'],r['content'])
    for r in conn.execute('SELECT * FROM rag_documents'):
        # Raw text is saved on new uploads; legacy overlapping chunks are losslessly joined.
        chunks = [x[0] for x in conn.execute('SELECT chunk_text FROM rag_chunks WHERE doc_id=? ORDER BY chunk_index',(r['id'],))]
        raw = r['raw_text'] if 'raw_text' in r.keys() and r['raw_text'] else join_chunks(chunks)
        add(f"document/{r['id']}",r['title'],raw,'history' if r['source']=='memory_l3_eviction' else 'lore',
            r['visibility'],r['timeline_id'],not r['hidden'])
    for r in conn.execute('SELECT * FROM wiki_sources'):
        if r['source_key'].startswith(('document/','lore/')):
            continue
        team = int(r['visibility'][5:]) if r['visibility'].startswith('team:') else None
        add(r['source_key'],r['title'],r['content'],'history' if r['source_key'].startswith('memory/') else 'lore',r['visibility'],team)
    for e in conn.execute('SELECT * FROM world_entities'):
        scopes = {'public','gm',e['visibility']}
        for table, fieldname in [('entity_states','entity_uid'),('entity_memories','entity_uid'),('entity_relations','subject_uid')]:
            scopes.update(x[0] for x in conn.execute(f'SELECT visibility FROM {table} WHERE {fieldname}=?',(e['uid'],)))
        contexts=[(scope,int(scope[5:]) if scope.startswith('team:') else None) for scope in sorted(scopes)]
        contexts.extend(('gm',t[0]) for t in conn.execute('SELECT id FROM timelines'))
        for scope,team in contexts:
            if not visible(e['visibility'],scope):
                continue
            states = entity_state_context(conn,e['uid'],scope,team)
            states=[s for s in states if allowed(s['visibility'],None,scope,team)]
            body = f"# {e['name']}\n实体ID：{e['uid']}\n类型：{e['entity_type']}\n位置：{e['location']}\n状态：{e['status']}\n"
            try:
                desc = json.loads(e['state_desc'] or '{}').get('desc','')
            except (ValueError,AttributeError):
                desc = e['state_desc']
            body += str(desc) + '\n' + '\n'.join(f"{s['name']}：{pack(s['value'])}；{s['meaning']}；{s['effects']}"+
                (f" [事件:{s['source_event_id']}]" if scope=='gm' and s['source_event_id'] is not None else '') for s in states)
            events = []
            for r in conn.execute('SELECT * FROM entity_states WHERE entity_uid=?',(e['uid'],)):
                if allowed(r['visibility'],None,scope,team) and r['source_event_id'] is not None:
                    events.append(r['source_event_id'])
            for r in conn.execute('SELECT * FROM entity_memories WHERE entity_uid=? ORDER BY id',(e['uid'],)):
                if allowed(r['visibility'],r['timeline_id'],scope,team):
                    body += f"\n已确认记忆：{r['content']}"+(f" [事件:{r['source_event_id']}]" if scope=='gm' and r['source_event_id'] is not None else '')
                    if r['source_event_id'] is not None:
                        events.append(r['source_event_id'])
            for r in conn.execute('SELECT * FROM entity_relations WHERE subject_uid=?',(e['uid'],)):
                if allowed(r['visibility'],None,scope,team):
                    body += f"\n关系：{r['predicate']} → {r['object_uid']}；{r['description']} [关系:{r['uid']}]"
            key=f"entity/{e['uid']}/{scope}"+(f'/team:{team}' if scope=='gm' and team is not None else '')
            # Event IDs are also knowledge: do not disclose GM event existence in public pages.
            if scope!='gm':
                events=[]
            add(key,e['name'],body,'entities',scope,team,entity=e['uid'],events=sorted(set(events)))
    for e in conn.execute('SELECT * FROM world_events WHERE valid=1 ORDER BY id'):
        scope = f"team:{e['timeline_id']}" if e['timeline_id'] and e['kind'] in ('branch_committed','scene_committed') else 'gm'
        add(f"event/{e['event_uid']}",f"事件 {e['id']}",e['content'],'history',scope,e['timeline_id'],events=[e['id']],tick=e['game_tick'])
    for r in conn.execute("SELECT * FROM system_state WHERE key IN ('worldview','session_memory')"):
        add('system/'+r['key'],'世界观' if r['key']=='worldview' else '近期剧情记忆',r['value'],
            'rules' if r['key']=='worldview' else 'history')
    for r in conn.execute('SELECT * FROM map_rooms'):
        add(f"map/{r['id']}",r['label'],r['description'] or '', 'map')
    for r in conn.execute('SELECT * FROM map_edges'):
        add(f"map_edge/{r['id']}",f"地图连接 {r['id']}",pack(dict(r)), 'map')
    for r in conn.execute('SELECT * FROM nodes WHERE is_draft=0'):
        add(f"scene/{r['id']}",r['name'],r['content'] or '', 'history')
    for r in conn.execute('SELECT * FROM timelines'):
        add(f"team_memory/{r['id']}",r['label']+' · 剧情记忆',r['memory'] or '', 'history',f"team:{r['id']}",r['id'])
    for r in conn.execute('SELECT * FROM triggers'):
        add(f"rule/{r['id']}",r['label'],pack(dict(r)),'rules')
    return result


def join_chunks(chunks):
    raw = ''
    for chunk in chunks:
        overlap = next((i for i in range(min(len(raw),len(chunk)),0,-1) if raw.endswith(chunk[:i])),0)
        raw += chunk[overlap:] if overlap else ('\n' if raw else '')+chunk
    return raw


def blocks(content, max_size=600):
    # Offsets always refer to the immutable source, including whitespace.
    start = 0
    while start < len(content):
        end = min(start+max_size,len(content))
        if end < len(content):
            boundary = max(content.rfind(c,start+max_size//2,end) for c in ('\n','。','！','？','. '))
            if boundary >= 0:
                end = boundary+1
        if content[start:end].strip():
            yield start,end,content[start:end]
        start = end


def publish(conn, key, title, body, inputs, scope, team, kind, owner, entity=None, events=None, model='', actor='projection', tick=None):
    from world import now
    old = conn.execute('SELECT * FROM wiki_page_meta WHERE page_key=?',(key,)).fetchone()
    if old:
        rev = conn.execute('SELECT * FROM wiki_revisions WHERE revision_uid=?',(old['revision_uid'],)).fetchone()
        if rev and rev['body']==body and rev['title']==title and rev['input_revisions']==pack(inputs):
            return old['revision_uid']
    rev_uid = uid('wrev')
    stamp = now()
    conn.execute('INSERT INTO wiki_revisions VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                 (rev_uid,key,old['revision_uid'] if old else None,body,title,stamp,tick,pack(inputs),COMPILER_VERSION,model,actor))
    enabled = all(conn.execute('SELECT enabled FROM knowledge_sources WHERE source_key=?',(k,)).fetchone()[0] for k in inputs)
    conn.execute('INSERT OR REPLACE INTO wiki_pages VALUES(?,?,?,?,?,?,?,?,?)',
                 (key,title,body,entity,scope,pack(events or []),digest(inputs),int(bool(model)),stamp))
    conn.execute('INSERT OR REPLACE INTO wiki_page_meta VALUES(?,?,?,?,?,?)',(key,rev_uid,kind,team,int(enabled),owner))
    conn.execute('DELETE FROM wiki_dependencies WHERE page_key=?',(key,))
    conn.executemany('INSERT INTO wiki_dependencies VALUES(?,?,?)',[(key,k,v) for k,v in inputs.items()])
    conn.execute('DELETE FROM wiki_page_evidence WHERE page_key=?',(key,))
    conn.execute('INSERT OR IGNORE INTO wiki_page_evidence SELECT ?,b.evidence_uid FROM evidence_blocks b JOIN wiki_dependencies d ON d.revision_uid=b.revision_uid WHERE d.page_key=?',(key,key))
    return rev_uid


def sync(conn):
    from world import now
    init(conn)
    records = collect(conn)
    changed = False
    for key, r in records.items():
        fp = digest(r)
        old = conn.execute('SELECT * FROM knowledge_sources WHERE source_key=?',(key,)).fetchone()
        if old and old['active'] and old['fingerprint']==fp:
            continue
        changed = True
        if r['kind']=='entities' and r['tick'] is None:
            tick=conn.execute("SELECT value FROM system_state WHERE key='game_tick'").fetchone()
            r['tick']=int(tick[0]) if tick else 0
        source_uid = old['source_uid'] if old and old['active'] else uid('src')
        revision = uid('srev')
        parent = old['revision_uid'] if old and old['active'] else None
        conn.execute('INSERT INTO source_revisions VALUES(?,?,?,?,?,?,?,?,?,?)',
                     (revision,source_uid,parent,r['body'],digest(r['body']),now(),r['tick'],'source','source_changed',pack(r)))
        conn.execute('INSERT OR REPLACE INTO knowledge_sources VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                     (key,source_uid,r['title'],r['kind'],r['scope'],r['team'],r['enabled'],1,revision,r['entity'],fp))
        log_change(conn,key,'source_revision',parent,revision)
        for i,(start,end,text) in enumerate(blocks(r['body'])):
            conn.execute('INSERT INTO evidence_blocks VALUES(?,?,?,?,?,?,?)',(uid('ev'),revision,i,start,end,text,digest(text)))
        conn.execute("UPDATE knowledge_jobs SET status='obsolete' WHERE source_key=? AND status IN ('pending','running','failed')",(key,))
        conn.execute('INSERT OR IGNORE INTO knowledge_jobs(job_uid,source_key,revision_uid,compiler_version,epoch,status) VALUES(?,?,?,?,?,?)',
                     (uid('job'),key,revision,COMPILER_VERSION,epoch(conn),'pending' if r['body'].strip() else 'done'))
        publish(conn,key,r['title'],r['body'],{key:revision},r['scope'],r['team'],r['kind'],key,r['entity'],r['events'],tick=r['tick'])
    for old in conn.execute('SELECT * FROM knowledge_sources WHERE active=1').fetchall():
        if old['source_key'] not in records:
            changed = True
            conn.execute('UPDATE knowledge_sources SET active=0 WHERE source_key=?',(old['source_key'],))
            log_change(conn,old['source_key'],'source_removed',old['revision_uid'],None)
            conn.execute("UPDATE knowledge_jobs SET status='obsolete' WHERE source_key=?",(old['source_key'],))
    for page in conn.execute('SELECT page_key FROM wiki_pages').fetchall():
        deps = conn.execute('SELECT d.*,s.active,s.revision_uid AS current_rev FROM wiki_dependencies d LEFT JOIN knowledge_sources s ON s.source_key=d.source_key WHERE d.page_key=?',(page[0],)).fetchall()
        if not deps or any(not d['active'] or d['current_rev']!=d['revision_uid'] for d in deps):
            changed = True
            for table in ('wiki_pages','wiki_page_meta','wiki_dependencies','wiki_page_evidence'):
                conn.execute(f'DELETE FROM {table} WHERE page_key=?',(page[0],))
    if changed:
        # A compiled source page may depend on a changed neighbor. Restore its own
        # current raw projection immediately; recompile without losing availability.
        for key,r in records.items():
            if not conn.execute('SELECT 1 FROM wiki_pages WHERE page_key=?',(key,)).fetchone():
                source=conn.execute('SELECT revision_uid FROM knowledge_sources WHERE source_key=?',(key,)).fetchone()
                publish(conn,key,r['title'],r['body'],{key:source[0]},r['scope'],r['team'],r['kind'],key,r['entity'],r['events'],tick=r['tick'])
                conn.execute("UPDATE knowledge_jobs SET status='pending',attempts=0,next_attempt=0 WHERE source_key=? AND revision_uid=?",(key,source[0]))
        rebuild_links(conn)


def page_allowed(conn, key, viewer='gm', timeline_id=None, retrieval=False):
    p = conn.execute('SELECT p.*,m.timeline_id,m.enabled,m.kind FROM wiki_pages p JOIN wiki_page_meta m USING(page_key) WHERE page_key=?',(key,)).fetchone()
    if not p or not allowed(p['visibility'],p['timeline_id'],viewer,timeline_id) or (retrieval and not p['enabled']):
        return None
    if p['entity_uid'] and p['visibility']=='gm' and timeline_id is not None and p['timeline_id'] is None:
        return None
    deps = conn.execute('SELECT d.revision_uid AS expected,s.* FROM wiki_dependencies d JOIN knowledge_sources s ON s.source_key=d.source_key WHERE d.page_key=?',(key,)).fetchall()
    if not deps or any(not s['active'] or s['expected']!=s['revision_uid'] or not allowed(s['visibility'],s['timeline_id'],viewer,timeline_id) or (retrieval and not s['enabled']) for s in deps):
        return None
    return dict(p)


def entity_page(conn, entity_uid, viewer, timeline_id, retrieval=False):
    keys = [r[0] for r in conn.execute('SELECT page_key FROM wiki_pages WHERE entity_uid=?',(entity_uid,))]
    # Prefer the viewer's own projection; never encode GM page keys into a public link.
    preferred='/gm/team:'+str(timeline_id) if viewer=='gm' and timeline_id is not None else '/'+viewer
    keys.sort(key=lambda k: (not k.endswith(preferred),not k.endswith('/public'),k))
    return next((p for k in keys if (p:=page_allowed(conn,k,viewer,timeline_id,retrieval))),None)


def rebuild_links(conn):
    conn.execute("DELETE FROM wiki_links WHERE origin!='llm'")
    names = {}
    for e in conn.execute('SELECT uid,name,aliases FROM world_entities'):
        for name in [e['name'],*json.loads(e['aliases'] or '[]')]:
            if name.strip():
                names.setdefault(name.casefold(),set()).add(e['uid'])
    source_names={}
    for s in conn.execute("SELECT * FROM knowledge_sources WHERE active=1 AND entity_uid IS NULL AND kind IN ('lore','map')"):
        labels=s['title'].split(',') if s['source_key'].startswith('lore/') else [s['title']]
        for name in labels:
            name=name.strip().casefold()
            if name:
                source_names.setdefault(name,set()).add(s['source_uid'])
    for p in conn.execute('SELECT p.*,m.timeline_id FROM wiki_pages p JOIN wiki_page_meta m USING(page_key)').fetchall():
        evidence = conn.execute('SELECT b.* FROM evidence_blocks b JOIN wiki_page_evidence d USING(evidence_uid) WHERE d.page_key=?',(p['page_key'],)).fetchall()
        for b in evidence:
            text = b['content'].casefold()
            for name, candidates in names.items():
                match = name in text if re.search(r'[\u4e00-\u9fff]',name) else re.search(r'(?<!\w)'+re.escape(name)+r'(?!\w)',text)
                if len(candidates)==1 and match:
                    target = next(iter(candidates))
                    if target != p['entity_uid']:
                        put_link(conn,p,target,'entity','mention','deterministic',b)
            for name,targets in source_names.items():
                match=name in text if re.search(r'[\u4e00-\u9fff]',name) else re.search(r'(?<!\w)'+re.escape(name)+r'(?!\w)',text)
                if len(targets)==1 and match and name not in names:
                    target=next(iter(targets))
                    dest=conn.execute('SELECT source_key FROM knowledge_sources WHERE source_uid=?',(target,)).fetchone()[0]
                    if dest!=p['page_key']:
                        put_link(conn,p,target,'source','mention','deterministic',b)
            for r in conn.execute('SELECT * FROM entity_relations WHERE subject_uid=?',(p['entity_uid'],)):
                if f"[关系:{r['uid']}]" in b['content']:
                    put_link(conn,p,r['object_uid'],'entity','confirmed_relation','authority',b)
            for event_id in re.findall(r'\[事件:(\d+)\]',b['content']):
                ev = conn.execute('SELECT event_uid FROM world_events WHERE id=? AND valid=1',(event_id,)).fetchone()
                if ev:
                    put_link(conn,p,'event/'+ev[0],'page','source','authority',b)
    conn.execute('DELETE FROM wiki_links WHERE from_key NOT IN (SELECT page_key FROM wiki_pages) OR revision_uid NOT IN (SELECT revision_uid FROM knowledge_sources WHERE active=1)')
    conn.execute("DELETE FROM wiki_links WHERE target_kind='entity' AND target_uid NOT IN (SELECT uid FROM world_entities)")
    conn.execute("DELETE FROM wiki_links WHERE target_kind='source' AND target_uid NOT IN (SELECT source_uid FROM knowledge_sources WHERE active=1)")
    conn.execute("DELETE FROM wiki_links WHERE target_kind='page' AND target_uid NOT IN (SELECT page_key FROM wiki_pages)")


def put_link(conn, p, target, target_kind, kind, origin, evidence, status='valid'):
    values = (p['page_key'],target_kind,target,kind,origin,status,evidence['evidence_uid'],evidence['revision_uid'],p['visibility'],p['timeline_id'])
    conn.execute('INSERT OR IGNORE INTO wiki_links VALUES(?,?,?,?,?,?,?,?,?,?,?)',(digest(values),*values))


def links(conn, key, viewer='gm', timeline_id=None, retrieval=False):
    page = page_allowed(conn,key,viewer,timeline_id,retrieval)
    if not page:
        return []
    output = []
    source=conn.execute('SELECT source_uid FROM knowledge_sources WHERE source_key=? AND active=1',(key,)).fetchone()
    for edge in conn.execute('SELECT * FROM wiki_links WHERE status=\'valid\' AND (from_key=? OR (target_kind=\'page\' AND target_uid=?) OR (target_kind=\'entity\' AND target_uid=?) OR (target_kind=\'source\' AND target_uid=?))',(key,key,page['entity_uid'],source[0] if source else '')).fetchall():
        if not allowed(edge['visibility'],edge['timeline_id'],viewer,timeline_id):
            continue
        start = page_allowed(conn,edge['from_key'],viewer,timeline_id,retrieval)
        if edge['target_kind']=='entity':
            end=entity_page(conn,edge['target_uid'],viewer,timeline_id,retrieval)
        elif edge['target_kind']=='source':
            target=conn.execute('SELECT source_key FROM knowledge_sources WHERE source_uid=? AND active=1',(edge['target_uid'],)).fetchone()
            end=page_allowed(conn,target[0],viewer,timeline_id,retrieval) if target else None
        else:
            end=page_allowed(conn,edge['target_uid'],viewer,timeline_id,retrieval)
        evidence = conn.execute('SELECT b.*,s.visibility,s.timeline_id,s.enabled,s.active,s.revision_uid AS current_rev FROM evidence_blocks b JOIN source_revisions r USING(revision_uid) JOIN knowledge_sources s ON s.source_uid=r.source_uid WHERE b.evidence_uid=?',(edge['evidence_uid'],)).fetchone()
        if not start or not end or not evidence or not evidence['active'] or evidence['revision_uid']!=evidence['current_rev']:
            continue
        if not allowed(evidence['visibility'],evidence['timeline_id'],viewer,timeline_id) or (retrieval and not evidence['enabled']):
            continue
        outgoing = edge['from_key']==key
        other = end if outgoing else start
        output.append({**dict(edge),'direction':'outgoing' if outgoing else 'incoming','page_key':other['page_key'],
                       'title':other['title'],'evidence':evidence['content']})
    return output


@dataclass
class ContextBundle:
    evidence: list = field(default_factory=list)
    trace: list = field(default_factory=list)
    budget_chars: int = 10000
    diagnostics: dict = field(default_factory=dict)

    @property
    def text(self):
        return '\n\n'.join('[证据:'+','.join(e['evidence_ids'])+' 来源:'+ ' / '.join(e['titles'])+']\n'+e['content'] for e in self.evidence)


def _latest_world_token(conn):
    """Use a fresh reader to detect commits/rollback while the network was running."""
    from world import state_token
    import sqlite3
    from contextlib import closing
    from pathlib import Path
    path=next((r[2] for r in conn.execute('PRAGMA database_list') if r[1]=='main'),'')
    if not path:return state_token(conn)
    with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True)) as reader:
        reader.row_factory=sqlite3.Row
        reader.execute('BEGIN')
        return state_token(reader)


def retrieve(conn, query, *, viewer='gm', timeline_id=None, sources=None, limit=6, budget=10000, one_hop=None, rerank=None, candidate_audit=False):
    from dataclasses import replace
    from providers import generation
    import reranking
    settings=replace(generation.settings)
    use_rerank=rerank is not False and reranking.configured(settings)
    # Network work never holds a world write transaction.
    can_request=use_rerank and not conn.in_transaction
    guard=_latest_world_token(conn) if can_request else None
    if one_hop is None:
        one_hop=os.getenv('WIKI_LINK_EXPANSION','false').lower()=='true'
    selected = set(sources) if sources is not None else {'lore','history','entities','map'}
    bundle = ContextBundle(budget_chars=budget)
    query_terms = terms(query)
    candidates = []
    eligible_pages={}
    # All paths converge on immutable original evidence; generated prose is not a second fact copy.
    for p in conn.execute('SELECT page_key FROM wiki_pages'):
        page = page_allowed(conn,p[0],viewer,timeline_id,True)
        if not page:
            if viewer=='gm':
                bundle.trace.append({'page_key':p[0],'included':False,'reason':'scope_disabled_or_stale'})
            continue
        if page['kind'] not in selected:
            bundle.trace.append({'page_key':p[0],'included':False,'reason':'route'})
            continue
        if page['entity_uid'] and entity_page(conn,page['entity_uid'],viewer,timeline_id,True)['page_key'] != p[0]:
            continue
        eligible_pages[p[0]]=page
    bundle.diagnostics['state_filter']={'eligible_pages':len(eligible_pages),
        'checks':['visibility','timeline','enabled','current_revision','active_source','viewer_projection']}
    for key,page in eligible_pages.items():
        rows = conn.execute('SELECT b.* FROM evidence_blocks b JOIN wiki_page_evidence d USING(evidence_uid) WHERE d.page_key=?',(key,)).fetchall()
        for b in rows:
            score = sum(t in (page['title']+' '+b['content']).lower() for t in query_terms)/max(1,len(query_terms))
            if score:
                candidates.append(dict(b,score=score,page_key=key,title=page['title'],path=[key]))
    # Reuse validated RAG vectors, but map scores back to the same source evidence.
    from rag import _hybrid_retrieve
    doc_ids={int(key.split('/')[1]) for key in eligible_pages if re.fullmatch(r'document/\d+',key)}
    for hit in _hybrid_retrieve(conn,query,top_k=80 if use_rerank or candidate_audit else 30,
            viewer=viewer,timeline_id=timeline_id,sources=selected,doc_ids=doc_ids,
            candidate_cap=80 if use_rerank or candidate_audit else 30):
        key = f"document/{hit['doc_id']}"
        page = page_allowed(conn,key,viewer,timeline_id,True)
        if not page:
            continue
        for b in conn.execute('SELECT b.* FROM evidence_blocks b JOIN knowledge_sources s ON s.revision_uid=b.revision_uid WHERE s.source_key=?',(key,)):
            if b['content'] in hit['chunk_text'] or hit['chunk_text'] in b['content']:
                candidates.append(dict(b,score=hit['score'],page_key=key,title=page['title'],path=[key,'rag']))
    candidates.sort(key=lambda c:(-c['score'],c['page_key'],c['ordinal']))
    seeds = list(dict.fromkeys(c['page_key'] for c in candidates))[:4]
    if one_hop:
        if not (use_rerank or candidate_audit):
            for c in candidates:
                if c['page_key'] not in seeds:
                    bundle.trace.append(dict(page_key=c['page_key'],evidence_uid=c['evidence_uid'],score=c['score'],path=c['path'],included=False,reason='seed_limit'))
            candidates=[c for c in candidates if c['page_key'] in seeds]
        neighbors = []
        for key in seeds:
            for edge in links(conn,key,viewer,timeline_id,True)[:8]:
                dest = edge['page_key']
                page = page_allowed(conn,dest,viewer,timeline_id,True)
                if dest in seeds or not page or page['kind'] not in selected:
                    continue
                for b in conn.execute('SELECT b.* FROM evidence_blocks b JOIN wiki_page_evidence d USING(evidence_uid) WHERE d.page_key=?',(dest,)):
                    score = sum(t in b['content'].lower() for t in query_terms)/max(1,len(query_terms))
                    if score:
                        neighbors.append(dict(b,score=score+0.1,page_key=dest,title=page['title'],path=[key,edge['link_uid'],dest]))
        neighbors.sort(key=lambda c:(-c['score'],c['page_key']))
        selected_pages = set(list(dict.fromkeys(c['page_key'] for c in neighbors))[:4])
        for c in neighbors:
            if c['page_key'] not in selected_pages:
                bundle.trace.append(dict(page_key=c['page_key'],evidence_uid=c['evidence_uid'],score=c['score'],path=c['path'],included=False,reason='neighbor_limit'))
        candidates.extend(c for c in neighbors if c['page_key'] in selected_pages)
    candidates.sort(key=lambda c:(-c['score'],len(c['path']),c['page_key'],c['ordinal']))
    if candidate_audit or can_request:
        if can_request and guard!=_latest_world_token(conn):
            bundle.diagnostics['rerank']={'status':'stale_world','reason':'changed_before_request'}
            return bundle
        result=reranking.rerank(query,candidates,settings,audit_only=candidate_audit)
        bundle.diagnostics['rerank']=result.diagnostics
        if can_request and (guard!=_latest_world_token(conn) or settings!=generation.settings):
            bundle.diagnostics['rerank'].update(status='stale_world',reason='world_or_config_changed')
            return bundle
        if result.diagnostics['status']=='ok':
            selected_ids={c['evidence_uid'] for c in result.candidates}
            for rank,c in enumerate(candidates,1):
                if c['evidence_uid'] not in selected_ids:
                    bundle.trace.append(dict(page_key=c['page_key'],evidence_uid=c['evidence_uid'],
                        rank_before=rank,path=c['path'],included=False,reason='rerank_candidate_limit'))
        candidates=result.candidates
    else:
        bundle.diagnostics['rerank']={'status':'transaction_active' if use_rerank else
            'unconfigured' if settings.rerank_enabled and rerank is not False else 'disabled'}
    groups = {}
    for rank,c in enumerate(candidates,1):
        normalized = re.sub(r'\s+','',c['content'])
        trace = dict(page_key=c['page_key'],evidence_uid=c['evidence_uid'],rank=rank,score=c['score'],path=c['path'],included=False)
        if 'rerank_score' in c:
            trace.update(rerank_score=c['rerank_score'],coarse_score=c['coarse_score'],
                rank_before=c['rank_before'],rerank_input_index=c['rerank_input_index'])
        bundle.trace.append(trace)
        if normalized in groups:
            g = groups[normalized]
            previous_ids=list(g['evidence_ids']); previous_titles=list(g['titles'])
            if c['evidence_uid'] not in g['evidence_ids']:
                g['evidence_ids'].append(c['evidence_uid'])
            if c['title'] not in g['titles']:
                g['titles'].append(c['title'])
            if len(bundle.text)>budget:
                g['evidence_ids']=previous_ids; g['titles']=previous_titles
                trace.update(included=False,reason='provenance_budget')
                continue
            trace.update(included=True,reason='deduplicated')
            continue
        group = dict(content=c['content'],evidence_ids=[c['evidence_uid']],titles=[c['title']],path=c['path'])
        size = len(c['content'])+len(c['title'])+len(c['evidence_uid'])+32
        if len(bundle.evidence)>=limit or sum(len(x['content'])+sum(map(len,x['titles']))+sum(map(len,x['evidence_ids']))+32 for x in bundle.evidence)+size>budget:
            trace['reason']='budget_or_limit'
            continue
        groups[normalized]=group
        bundle.evidence.append(group)
        if len(bundle.text)>budget:
            bundle.evidence.pop(); groups.pop(normalized)
            trace['reason']='budget'
            continue
        trace.update(included=True,reason='selected')
    return bundle
