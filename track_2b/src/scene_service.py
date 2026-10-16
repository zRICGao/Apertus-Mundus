"""One transactional entry point for preset choices and generated branches."""
import json
from fastapi import APIRouter, HTTPException, BackgroundTasks
from pydantic import BaseModel, Field
from database import atomic, connect_db
from contextlib import closing
from world import state_token, encode, record_event, refresh_wiki
from engine import apply_branch

scene_router=APIRouter(tags=['Scene commits'])
_db_file=''

def configure_scene(db_file):
    global _db_file
    _db_file=db_file

class SceneCommitRequest(BaseModel):
    node_id:int
    from_node_id:int|None=None
    timeline_id:int|None=None
    option_text:str=''
    operation_key:str=Field(min_length=1,max_length=200)
    expected_token:str|None=None
    confirm_npc_additions:bool=False

def commit_scene(conn, req):
    from trigger import evaluate_triggers, party_members
    from memory_service import append_to_memory, _tl_append_memory, _l1_append
    with atomic(conn):
        node=conn.execute('SELECT * FROM nodes WHERE id=?',(req.node_id,)).fetchone()
        if not node: raise HTTPException(404,'Scene not found')
        if node['is_draft'] or conn.execute('SELECT id FROM world_events WHERE operation_key=?',
                (f'branch:{req.node_id}',)).fetchone():
            return apply_branch(conn,req.node_id,req.expected_token,expected_timeline=req.timeline_id,
                                confirm_npc_additions=req.confirm_npc_additions)
        key='scene:'+req.operation_key
        identity={'node_id':req.node_id,'from_node_id':req.from_node_id,'timeline_id':req.timeline_id,'option_text':req.option_text}
        previous=conn.execute('SELECT payload FROM world_events WHERE operation_key=?',(key,)).fetchone()
        if previous:
            saved=json.loads(previous['payload'])
            if saved['request']!=identity: raise HTTPException(409,'Commit key was already used for a different choice')
            return {**saved['result'],'status':'already_applied'}
        if req.expected_token and state_token(conn)!=req.expected_token:
            raise HTTPException(409,'World changed; refresh before committing')
        members=party_members(conn,req.timeline_id)
        if req.timeline_id is not None:
            current=conn.execute('SELECT current_node_id FROM timelines WHERE id=?',(req.timeline_id,)).fetchone()[0]
        else:
            row=conn.execute("SELECT value FROM system_state WHERE key='player_current_scene_id'").fetchone()
            current=int(row[0]) if row and str(row[0]).isdigit() else None
        if current and req.from_node_id is not None and current!=req.from_node_id:
            raise HTTPException(409,'Scene changed; this choice is stale')
        event_id=record_event(conn,'scene_committed',node['content'],{'request':identity},req.timeline_id,key)
        if req.timeline_id is None:
            conn.execute("INSERT OR REPLACE INTO system_state VALUES('player_current_scene_id',?)",(str(req.node_id),))
        else:
            conn.execute('UPDATE timelines SET current_node_id=? WHERE id=?',(req.node_id,req.timeline_id))
        from spatial import scene_room,set_position
        room=scene_room(conn,req.node_id)
        if room: set_position(conn,room['id'],req.timeline_id)
        text=f"Player selected {req.option_text} → {node['name']}"
        if req.timeline_id is None: append_to_memory(conn,text)
        else: _tl_append_memory(conn,req.timeline_id,text)
        _l1_append(conn,node['name'],req.option_text,node['content'],timeline_id=req.timeline_id)
        conn.execute('INSERT INTO chronicle_log(scene_id,scene_name,scene_content,player_action) VALUES(?,?,?,?)',
                     (req.node_id,node['name'],node['content'],req.option_text))
        rules=evaluate_triggers(conn,req.node_id,node['name'],node['content'],req.timeline_id,event_id)
        result={'status':'success','event_id':event_id,'node_id':req.node_id,'timeline_id':req.timeline_id,'trigger_results':rules}
        conn.execute('UPDATE world_events SET payload=? WHERE id=?',(encode({'request':identity,'result':result}),event_id))
        refresh_wiki(conn)
        from wiki_jobs import enqueue
        enqueue(conn,event_id)
        return result

@scene_router.post('/api/game/commit-scene')
def scene_commit(req:SceneCommitRequest, background_tasks:BackgroundTasks):
    from memory_service import maintain_memory
    with closing(connect_db(_db_file)) as conn:
        try: result=commit_scene(conn,req)
        except ValueError as exc: raise HTTPException(422,str(exc)) from exc
    if result['status']=='success':
        background_tasks.add_task(maintain_memory,req.timeline_id)
    return result
