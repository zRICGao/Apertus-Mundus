"""Parties share one world and one clock, while keeping position and knowledge separate."""
import json
from contextlib import contextmanager
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from database import atomic, connect_db
from world import record_event, refresh_wiki

timeline_router=APIRouter(tags=['Party Actions'])
_db_file=''


def configure_timeline(db_file, generation_client=None, fn_tl_append_memory=None, memory_summary_limit=300):
    global _db_file
    _db_file=db_file


@contextmanager
def db():
    conn=connect_db(_db_file)
    try: yield conn
    finally: conn.close()


def require(conn,tl_id):
    row=conn.execute('SELECT * FROM timelines WHERE id=?',(tl_id,)).fetchone()
    if not row: raise HTTPException(404,'Party does not exist')
    return row


def members(conn,raw,excluding=None):
    try:
        ids=sorted({int(x.strip()) for x in raw.split(',') if x.strip()})
    except ValueError as exc: raise HTTPException(422,'Invalid party-member format') from exc
    for uid in ids:
        if not conn.execute('SELECT id FROM characters WHERE id=?',(uid,)).fetchone(): raise HTTPException(422,'Party member does not exist')
        for team in conn.execute("SELECT id,char_ids FROM timelines WHERE status='active'"):
            if team['id']!=excluding and str(uid) in team['char_ids'].split(','):
                raise HTTPException(409,'A character cannot belong to two active parties simultaneously')
    return ','.join(map(str,ids))


class TimelineCreateRequest(BaseModel):
    label:str='Party'
    color:str='#5b9cf5'
    char_ids:str=''
    current_node_id:int=0
    current_room_id:int|None=None


class TimelineUpdateRequest(BaseModel):
    label:str
    color:str='#5b9cf5'
    char_ids:str
    status:str='active'


class TimelineJumpRequest(BaseModel):
    node_id:int


class TimelineMergeRequest(BaseModel):
    source_id:int
    target_id:int


class StringContentRequest(BaseModel):
    content:str


@timeline_router.get('/api/timelines')
def get_timelines():
    with db() as conn:
        chars={r['id']:dict(r) for r in conn.execute('SELECT * FROM characters')}
        teams=[]
        for row in conn.execute('SELECT * FROM timelines ORDER BY id'):
            team=dict(row); team['characters']=[chars[int(x)] for x in row['char_ids'].split(',') if x.isdigit() and int(x) in chars]
            teams.append(team)
        return {'status':'success','timelines':teams}


@timeline_router.post('/api/timelines')
def create_timeline(req:TimelineCreateRequest):
    with db() as conn, atomic(conn):
        ids=members(conn,req.char_ids)
        if req.current_node_id and not conn.execute('SELECT id FROM nodes WHERE id=?',(req.current_node_id,)).fetchone(): raise HTTPException(422,'Scene does not exist')
        if req.current_room_id and not conn.execute('SELECT id FROM map_rooms WHERE id=?',(req.current_room_id,)).fetchone(): raise HTTPException(422,'Room does not exist')
        tick=int(conn.execute("SELECT value FROM system_state WHERE key='game_tick'").fetchone()[0])
        tl=conn.execute('INSERT INTO timelines(label,color,char_ids,current_node_id,current_room_id,game_tick) VALUES(?,?,?,?,?,?)',
                        (req.label[:100],req.color[:20],ids,req.current_node_id or None,req.current_room_id,tick)).lastrowid
        record_event(conn,'team_create',f'Party "{req.label}" started acting',{'char_ids':ids},tl)
        refresh_wiki(conn)
    return {'status':'success','id':tl}


@timeline_router.put('/api/timelines/{tl_id}')
def update_timeline(tl_id:int,req:TimelineUpdateRequest):
    with db() as conn, atomic(conn):
        require(conn,tl_id)
        if req.status not in {'active','paused','merged'}: raise HTTPException(422,'Invalid party status')
        ids=members(conn,req.char_ids,tl_id) if req.status=='active' else req.char_ids
        conn.execute('UPDATE timelines SET label=?,color=?,char_ids=?,status=? WHERE id=?',(req.label,req.color,ids,req.status,tl_id))
        refresh_wiki(conn)
    return {'status':'success'}


@timeline_router.delete('/api/timelines/{tl_id}')
def delete_timeline(tl_id:int):
    # Preserve knowledge scope and history; retire the party instead of reusing its identity.
    with db() as conn, atomic(conn):
        require(conn,tl_id)
        conn.execute("UPDATE timelines SET status='merged' WHERE id=?",(tl_id,))
        refresh_wiki(conn)
    return {'status':'success'}


@timeline_router.post('/api/timelines/{tl_id}/jump')
def timeline_jump(tl_id:int,req:TimelineJumpRequest):
    with db() as conn, atomic(conn):
        require(conn,tl_id)
        node=conn.execute('SELECT * FROM nodes WHERE id=?',(req.node_id,)).fetchone()
        if node is None: raise HTTPException(404,'Scene does not exist')
        if node['is_draft']: raise HTTPException(409,'Commit this branch first')
        conn.execute("UPDATE timelines SET current_node_id=?,memory=memory||? WHERE id=?",(req.node_id,f"\nEntered scene [{node['name']}]",tl_id))
        record_event(conn,'team_jump',node['name'],{'node_id':req.node_id},tl_id); refresh_wiki(conn)
    return {'status':'success'}


@timeline_router.get('/api/timelines/{tl_id}/memory')
def get_timeline_memory(tl_id:int):
    with db() as conn: return {'status':'success','content':require(conn,tl_id)['memory']}


@timeline_router.put('/api/timelines/{tl_id}/memory')
def update_timeline_memory(tl_id:int,req:StringContentRequest):
    with db() as conn, atomic(conn):
        require(conn,tl_id); conn.execute('UPDATE timelines SET memory=? WHERE id=?',(req.content,tl_id)); refresh_wiki(conn)
    return {'status':'success'}


@timeline_router.post('/api/timelines/merge')
def merge_timelines(req:TimelineMergeRequest):
    if req.source_id==req.target_id: raise HTTPException(422,'A party cannot merge with itself')
    with db() as conn, atomic(conn):
        src,tgt=require(conn,req.source_id),require(conn,req.target_id)
        if src['status']=='merged': return {'status':'already_applied','merged_memory':tgt['memory']}
        if src['current_room_id']!=tgt['current_room_id']: raise HTTPException(409,'Parties must reach the same location before merging')
        ids=','.join(sorted(set(src['char_ids'].split(',')+tgt['char_ids'].split(','))-{''},key=int))
        text=tgt['memory']+f"\n[Known events of merged party {src['label']}]\n"+src['memory']
        conn.execute("UPDATE timelines SET char_ids=?,memory=? WHERE id=?",(ids,text,req.target_id))
        conn.execute("UPDATE timelines SET status='merged' WHERE id=?",(req.source_id,))
        source,target=f'team:{req.source_id}',f'team:{req.target_id}'
        # Meeting transfers party knowledge, never entity-private memories.
        for table in ('entity_memories','wiki_sources','rag_documents','entity_relations','entity_states'):
            if table=='entity_states':
                # Physical state has one value; visibility becomes shared by the merged party.
                pass
            conn.execute(f'UPDATE {table} SET visibility=? WHERE visibility=?',(target,source))
        conn.execute('UPDATE rag_documents SET timeline_id=? WHERE timeline_id=?',(req.target_id,req.source_id))
        conn.execute('UPDATE memory_l1 SET timeline_id=? WHERE timeline_id=?',(req.target_id,req.source_id))
        record_event(conn,'team_merge',f"[{src['label']}] merged with [{tgt['label']}]",{},req.target_id); refresh_wiki(conn)
    return {'status':'success','merged_memory':text}


@timeline_router.put('/api/timelines/{tl_id}/room')
def set_timeline_room(tl_id:int,room_id:int|None=None):
    with db() as conn, atomic(conn):
        team=require(conn,tl_id)
        room=conn.execute('SELECT * FROM map_rooms WHERE id=?',(room_id,)).fetchone() if room_id else None
        if room_id and room is None: raise HTTPException(404,'Room does not exist')
        conn.execute('UPDATE timelines SET current_room_id=? WHERE id=?',(room_id,tl_id))
        for char_id in team['char_ids'].split(','):
            if char_id.isdigit(): conn.execute('UPDATE world_entities SET room_id=?,location=? WHERE uid=(SELECT entity_uid FROM characters WHERE id=?)',
                         (room_id,room['label'] if room else '',int(char_id)))
        record_event(conn,'team_move','GM updated party position',{'room_id':room_id},tl_id); refresh_wiki(conn)
    return {'status':'success'}
