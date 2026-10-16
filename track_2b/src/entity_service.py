"""Entity CRUD and extraction through stable identities, retaining the legacy API."""
import json
from contextlib import contextmanager
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from database import atomic, connect_db
from providers import generation, parse_object
from world import (create_entity, encode, entity_candidates, entity_state_context, link_character,
                   record_event, refresh_wiki, resolve_entity, visible, now)

entity_router = APIRouter(tags=["World Entities"])
_db_file = ""


def configure_entity(db_file, generation_client=None):
    global _db_file
    _db_file = db_file


def get_db_connection():
    return connect_db(_db_file)


@contextmanager
def safe_db():
    conn = get_db_connection()
    try:
        yield conn
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


class WorldEntityUpsertRequest(BaseModel):
    uid: str | None = None
    entity_type: str = "npc"
    name: str
    location: str = ""
    status: str = "active"
    last_seen_by: str = ""
    state_desc: str = ""
    room_id: int | None = None
    aliases: list[str] = Field(default_factory=list)
    visibility: str = "public"


class UpdatePersonaRequest(BaseModel):
    desc: str | None = None
    emotion: dict | None = None
    breakpoint: dict | None = None
    memory: list | None = None


def get_world_entities_text(conn, *search_texts, viewer="gm", timeline_id=None):
    from knowledge import allowed
    text = " ".join(search_texts)
    candidates = entity_candidates(conn, text, viewer=viewer, timeline_id=timeline_id) if text else []
    ids = {c["entity_uid"] for c in candidates}
    rows = [r for r in conn.execute("SELECT * FROM world_entities ORDER BY updated_at DESC")
            if allowed(r["visibility"], None, viewer, timeline_id)]
    rows.sort(key=lambda r: r["uid"] not in ids)
    lines = []
    for row in rows[:24]:
        try:
            sd = json.loads(row["state_desc"] or "{}")
        except ValueError:
            sd = {"desc": row["state_desc"]}
        if not isinstance(sd, dict):
            sd = {"desc": row["state_desc"]}
        desc = "Not yet introduced" if row["status"] == "pending" and viewer != "gm" else sd.get("desc", "")
        line = f"- [entity_uid={row['uid']}] {row['name']} ({row['entity_type']}) | {row['location']} | {row['status']} | {desc}"
        line += f" | Aliases:{row['aliases']}"
        if sd.get("emotion"):
            line += " | Emotions:" + encode(sd["emotion"])
        states = entity_state_context(conn, row["uid"], viewer, timeline_id)
        if states:
            line += "\n  Dynamic states:" + encode(states)
        memories = [r["content"] for r in conn.execute("SELECT * FROM entity_memories WHERE entity_uid=? ORDER BY id DESC LIMIT 30", (row["uid"],))
                    if allowed(r["visibility"], r['timeline_id'], viewer, timeline_id)]
        # Legacy memories have no visibility metadata; expose only to GM.
        if viewer == "gm" and timeline_id is None:
            memories += sd.get("memory", [])[-3:]
        if memories:
            line += "\n  Known memories:" + " / ".join(memories[:6])
        lines.append(line)
    return "\n".join(lines)


def extract_entity_proposals(conn, scene_name, content, player_action, result_text, timeline_label="Main timeline"):
    context = get_world_entities_text(conn, scene_name, content, player_action)
    raw = generation.generate(
        "You are an entity-state tracker. Extract only outcomes that have been confirmed. Existing entities must use their existing entity_uid; "
        "do not merge people based only on the same name or appearance. For newly named entities, output new:true and do not invent permanent IDs."
        "Return JSON only: {\"entities\":[{\"entity_uid\":\"Existing ID (omit for a new entity)\",\"new\":false,"
        "\"name\":\"Name\",\"entity_type\":\"npc/location/event/item\",\"location\":\"Location\","
        "\"status\":\"active/dead/moved/resolved/pending\",\"description\":\"State description\",\"aliases\":[]}]}\n" + context,
        encode({"scene": scene_name, "content": content, "action": player_action, "result": result_text}),
        json_mode=True, temperature=.1, max_tokens=1800)
    entities = parse_object(raw).get("entities", [])
    if not isinstance(entities, list) or any(not isinstance(e, dict) for e in entities):
        raise ValueError("Invalid entity extraction result")
    return entities


def apply_entity_proposals(conn, entities, event_id=None):
    updated = []
    for proposal in entities:
        if proposal.get("entity_uid"):
            row = resolve_entity(conn, uid=proposal["entity_uid"])
            uid = row["uid"]
            try:
                sd = json.loads(row["state_desc"] or "{}")
            except ValueError:
                sd = {"desc": row["state_desc"]}
            if not isinstance(sd, dict):
                sd = {}
            if "description" in proposal or "state_desc" in proposal:
                sd["desc"] = proposal.get("description", proposal.get("state_desc", ""))
            status = proposal.get("status", row["status"])
            if status not in {"active", "dead", "moved", "resolved", "pending"}:
                raise ValueError("Invalid entity status")
            aliases = list(dict.fromkeys(json.loads(row["aliases"] or "[]") + proposal.get("aliases", [])))
            name = proposal.get('name',row['name'])
            if name != row['name'] and row['name'] not in aliases: aliases.append(row['name'])
            conn.execute("UPDATE world_entities SET state_desc=?,location=?,status=?,aliases=?,updated_at=? WHERE uid=?",
                (encode(sd), proposal.get("location", row["location"]), status, encode(aliases), now(), uid))
            conn.execute('UPDATE world_entities SET name=? WHERE uid=?',(name,uid))
            conn.execute('UPDATE characters SET name=? WHERE entity_uid=?',(name,uid))
        elif proposal.get("new") is True:
            if proposal.get('entity_type', 'npc') == 'npc':
                raise ValueError('Automatically created NPCs must go through character review and use a new-character declaration')
            uid = create_entity(conn, name=proposal["name"], entity_type=proposal.get("entity_type", "npc"),
                state_desc=encode({"desc": proposal.get("description", proposal.get("state_desc", ""))}),
                aliases=proposal.get("aliases", []), location=proposal.get("location", ""),
                status=proposal.get("status", "active"), visibility=proposal.get("visibility", "public"))
        else:
            raise ValueError("Existing entity updates must provide entity_uid; new entities must explicitly specify new:true")
        updated.append(uid)
    return updated


def ai_extract_and_upsert_entities(conn, scene_name, content, player_action, ai_branches_text, timeline_label):
    proposals = extract_entity_proposals(conn, scene_name, content, player_action, ai_branches_text, timeline_label)
    with atomic(conn):
        event = record_event(conn, "entity_update", ai_branches_text, {"entities": proposals})
        apply_entity_proposals(conn, proposals, event)
        refresh_wiki(conn)


@entity_router.get("/api/world-entities")
def list_world_entities():
    with safe_db() as conn:
        return {"status": "success", "entities": [dict(r) for r in conn.execute("SELECT * FROM world_entities ORDER BY id")]}


@entity_router.post("/api/world-entities")
def upsert_world_entity(req: WorldEntityUpsertRequest):
    from world import validate_visibility
    validate_visibility(req.visibility)
    with safe_db() as conn, atomic(conn):
        if req.uid:
            previous=resolve_entity(conn, uid=req.uid)
            aliases=list(req.aliases)
            if previous['name']!=req.name and previous['name'] not in aliases: aliases.append(previous['name'])
            conn.execute("UPDATE world_entities SET name=?,entity_type=?,location=?,status=?,last_seen_by=?,state_desc=?,"
                         "room_id=?,aliases=?,visibility=?,updated_at=? WHERE uid=?", (req.name, req.entity_type, req.location,
                         req.status, req.last_seen_by, req.state_desc, req.room_id, encode(aliases), req.visibility, now(), req.uid))
            conn.execute("UPDATE characters SET name=? WHERE entity_uid=?", (req.name, req.uid))
            uid = req.uid
        else:
            uid = create_entity(conn, **{k: v for k, v in req.model_dump().items() if k not in {"uid", "last_seen_by"}})
        record_event(conn, "entity_edit", f"Register entity {req.name}", {"entity_uid": uid})
        refresh_wiki(conn)
        return {"status": "success", "entity_uid": uid}


@entity_router.delete("/api/world-entities/{entity_id}")
def delete_world_entity(entity_id: int):
    with safe_db() as conn, atomic(conn):
        row = conn.execute("SELECT uid FROM world_entities WHERE id=?", (entity_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Entity does not exist")
        if conn.execute("SELECT id FROM characters WHERE entity_uid=?", (row[0],)).fetchone():
            raise HTTPException(409, "This entity remains linked to a character; you may set its status to resolved")
        conn.execute("DELETE FROM entity_states WHERE entity_uid=? OR target_uid=?", (row[0], row[0]))
        conn.execute("DELETE FROM entity_memories WHERE entity_uid=?", (row[0],))
        conn.execute('DELETE FROM entity_relations WHERE subject_uid=? OR object_uid=?',(row[0],row[0]))
        conn.execute('DELETE FROM npc_chat_logs WHERE entity_uid=?',(row[0],))
        conn.execute("DELETE FROM world_entities WHERE id=?", (entity_id,))
        conn.execute("DELETE FROM wiki_pages WHERE entity_uid=?", (row[0],))
        record_event(conn, "entity_remove", "Remove entity", {"entity_uid": row[0]})
        refresh_wiki(conn)
    return {"status": "success"}


@entity_router.put("/api/world-entities/{entity_id}/room")
def set_entity_room(entity_id: int, room_id: int | None = None):
    with safe_db() as conn, atomic(conn):
        conn.execute("UPDATE world_entities SET room_id=? WHERE id=?", (room_id, entity_id))
        record_event(conn, "entity_move", "Update entity location", {"id": entity_id, "room_id": room_id})
        refresh_wiki(conn)
    return {"status": "success"}


@entity_router.get("/api/world-entities/{entity_id}/persona")
def get_entity_persona(entity_id: int):
    with safe_db() as conn:
        row = conn.execute("SELECT * FROM world_entities WHERE id=?", (entity_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Entity does not exist")
        try:
            sd = json.loads(row["state_desc"] or "{}")
        except ValueError:
            sd = {"desc": row["state_desc"]}
        return {"status": "success", "name": row["name"], "entity_id": entity_id, "entity_uid": row["uid"], "persona": sd}


@entity_router.put("/api/world-entities/{entity_id}/persona")
def update_entity_persona(entity_id: int, req: UpdatePersonaRequest):
    with safe_db() as conn, atomic(conn):
        row = conn.execute("SELECT * FROM world_entities WHERE id=?", (entity_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Entity does not exist")
        try:
            sd = json.loads(row["state_desc"] or "{}")
        except ValueError:
            sd = {"desc": row["state_desc"]}
        for key, value in req.model_dump(exclude_none=True).items():
            if key == "emotion":
                old = sd.get(key, {})
                old.update({k: max(-100, min(100, int(v))) for k, v in value.items() if k in {"trust", "fear", "irritation"}})
                value = old
            sd[key] = value
        conn.execute("UPDATE world_entities SET state_desc=?,updated_at=? WHERE id=?", (encode(sd), now(), entity_id))
        record_event(conn, "persona_edit", "Update character profile", {"entity_uid": row["uid"]})
        refresh_wiki(conn)
    return {"status": "success"}
