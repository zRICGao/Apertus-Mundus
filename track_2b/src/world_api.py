"""World state, disambiguation and wiki endpoints."""
import json
from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel, Field

from database import atomic, connect_db
from providers import generation, route_action
from world import (apply_state_changes, entity_state_context, known_definitions, record_event,
                   refresh_wiki, resolve_mention, state_token, synthesize_wiki, visible)

world_router = APIRouter(tags=["世界与Wiki"])
_db_file = ""


def configure_world(db_file):
    global _db_file
    _db_file = db_file
    from encyclopedia_api import configure
    configure(db_file)


class StatePatch(BaseModel):
    definitions: list[dict] = Field(default_factory=list)
    changes: list[dict] = Field(default_factory=list)
    expected_token: str
    operation_key: str
    timeline_id: int | None = None


class ResolveRequest(BaseModel):
    mention: str
    context: str = ""
    timeline_id: int | None = None


@world_router.get("/api/world")
def world_state():
    conn = connect_db(_db_file)
    try:
        return {"status": "success", "state_token": state_token(conn), "definitions": known_definitions(conn),
                "game_tick": int(conn.execute("SELECT value FROM system_state WHERE key='game_tick'").fetchone()[0]),
                "decision_provider": generation.settings.decision_provider,
                "features": {"image_generation": False, "speech": False, "wiki": True, "dynamic_states": True}}
    finally:
        conn.close()


@world_router.get("/api/world/entities/{uid}/states")
def entity_states(uid: str):
    conn = connect_db(_db_file)
    try:
        return {"status": "success", "states": entity_state_context(conn, uid)}
    finally:
        conn.close()


@world_router.post("/api/world/states")
def patch_states(req: StatePatch):
    if not req.operation_key.strip():
        raise HTTPException(422, "operation_key 不能为空")
    conn = connect_db(_db_file)
    try:
        with atomic(conn):
            old = conn.execute("SELECT id FROM world_events WHERE operation_key=?", (req.operation_key,)).fetchone()
            if old:
                return {"status": "already_applied", "event_id": old[0]}
            if state_token(conn) != req.expected_token:
                raise HTTPException(409, "世界状态已变化，请刷新后提交")
            event_id = record_event(conn, "state_patch", "状态更新", req.model_dump(), req.timeline_id, req.operation_key)
            apply_state_changes(conn, req.definitions, req.changes, event_id)
            refresh_wiki(conn)
        return {"status": "success", "event_id": event_id, "state_token": state_token(conn)}
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    finally:
        conn.close()


@world_router.post("/api/world/resolve")
def resolve(req: ResolveRequest):
    conn = connect_db(_db_file)
    try:
        return resolve_mention(conn, req.mention, req.context, timeline_id=req.timeline_id)
    finally:
        conn.close()


class RouteRequest(BaseModel):
    player_action: str
    scene_content: str = ""


@world_router.post("/api/ai/decision")
def decision(req: RouteRequest):
    return {"status": "success", **route_action(req.model_dump())}


@world_router.get("/api/wiki")
def wiki_index(viewer: str = "gm", timeline_id: int | None = None, query: str = "", kind: str = ""):
    import knowledge as k
    from contextlib import closing
    with closing(connect_db(_db_file)) as conn, atomic(conn):
        refresh_wiki(conn)
        pages=[]
        for row in conn.execute('SELECT page_key FROM wiki_pages'):
            page=k.page_allowed(conn,row[0],viewer,timeline_id)
            if not page or (kind and page['kind']!=kind):
                continue
            raw=' '.join(r[0] for r in conn.execute('SELECT r.content FROM wiki_dependencies d JOIN source_revisions r USING(revision_uid) WHERE d.page_key=?',(row[0],)))
            if query and query.casefold() not in (page['title']+' '+page['body']+' '+raw).casefold():
                continue
            job=conn.execute('SELECT status,last_error FROM knowledge_jobs WHERE source_key=? AND revision_uid=(SELECT revision_uid FROM knowledge_sources WHERE source_key=?) ORDER BY rowid DESC LIMIT 1',(row[0],row[0])).fetchone()
            pages.append({**page,'body':page['body'][:160],'compile_status':job['status'] if job else 'done','compile_error':job['last_error'] if job else ''})
        return {'status':'success','pages':pages,'epoch':k.epoch(conn)}


@world_router.get("/api/wiki/page")
def wiki_page(key: str, viewer: str = "gm", timeline_id: int | None = None):
    from encyclopedia_api import page_detail
    from contextlib import closing
    with closing(connect_db(_db_file)) as conn, atomic(conn):
        refresh_wiki(conn)
        return {'status':'success',**page_detail(conn,key,viewer,timeline_id)}


class WikiSynthesis(BaseModel):
    page_key: str


@world_router.post("/api/wiki/synthesize")
def wiki_synthesis(req: WikiSynthesis):
    return synthesize_wiki(_db_file, req.page_key)


@world_router.get("/api/wiki/lint")
def wiki_lint():
    conn = connect_db(_db_file)
    try:
        issues = []
        for row in conn.execute("SELECT s.* FROM entity_states s LEFT JOIN world_entities e ON e.uid=s.entity_uid "
                                "LEFT JOIN state_definitions d ON d.uid=s.state_uid WHERE e.uid IS NULL OR d.uid IS NULL"):
            issues.append({"kind": "broken_state_reference", "entity_uid": row["entity_uid"]})
        for row in conn.execute("SELECT c.id FROM characters c LEFT JOIN world_entities e ON e.uid=c.entity_uid WHERE e.uid IS NULL"):
            issues.append({"kind": "missing_character_identity", "character_id": row[0]})
        return {"status": "success", "issues": issues}
    finally:
        conn.close()


class TickRequest(BaseModel):
    tick: int = Field(ge=0)
    expected_token: str


@world_router.post("/api/world/tick")
def advance_tick(req: TickRequest):
    conn = connect_db(_db_file)
    try:
        with atomic(conn):
            if state_token(conn) != req.expected_token:
                raise HTTPException(409, "世界已更新")
            current = int(conn.execute("SELECT value FROM system_state WHERE key='game_tick'").fetchone()[0])
            if req.tick < current:
                raise HTTPException(422, "统一世界回合不能倒退，请使用回滚")
            conn.execute("UPDATE system_state SET value=? WHERE key='game_tick'", (str(req.tick),))
            conn.execute("UPDATE timelines SET game_tick=? WHERE status='active'", (req.tick,))
            record_event(conn, "tick", f"世界回合推进至 {req.tick}")
            refresh_wiki(conn)
        return {"status": "success", "state_token": state_token(conn)}
    finally:
        conn.close()
