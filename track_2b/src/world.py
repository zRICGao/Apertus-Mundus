"""Stable identities, extensible state, committed events and a rebuildable LLM wiki."""
import hashlib
import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone

from database import atomic, connect_db
from knowledge import TABLES as KNOWLEDGE_TABLES
from providers import parse_object


def new_uid(prefix="ent"):
    return f"{prefix}_{uuid.uuid4().hex}"


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def now():
    return datetime.now(timezone.utc).isoformat()


def columns(conn, table):
    return {r["name"] for r in conn.execute(f'PRAGMA table_info("{table}")')}


WORLD_TABLES = ("state_definitions", "entity_states", "entity_memories", "entity_relations",
                "world_events", "wiki_pages", "wiki_sources", "wiki_jobs", "npc_generation_reviews", "scene_locations")
SNAPSHOT_TABLES = ("characters", "world_entities", "timelines", "nodes", "options",
                  "map_rooms", "map_edges", "memory_l1", "pending_effects", "npc_chat_logs",
                  "triggers", "game_flags", "rag_documents", "rag_chunks", "chronicle_log",
                  "system_state", "lorebook", "trigger_judgements", *WORLD_TABLES, *KNOWLEDGE_TABLES)


def init_world(conn):
    """Migrate legacy identity columns while preserving IDs and user data."""
    conn.execute('CREATE TABLE IF NOT EXISTS scene_locations (node_id INTEGER PRIMARY KEY,room_id INTEGER NOT NULL)')
    conn.execute('INSERT OR IGNORE INTO scene_locations(node_id,room_id) '
                 'SELECT node_id,MIN(id) FROM map_rooms WHERE node_id>0 '
                 'GROUP BY node_id HAVING COUNT(*)=1')
    conn.execute('CREATE TABLE IF NOT EXISTS npc_generation_reviews ('
                 'id TEXT PRIMARY KEY, operation_key TEXT UNIQUE, epoch TEXT NOT NULL, '
                 'state_token TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL, '
                 'status TEXT NOT NULL DEFAULT \'pending\', result TEXT, resolution TEXT, '
                 'lease_until REAL NOT NULL DEFAULT 0, created_at TEXT NOT NULL)')
    schema = conn.execute("SELECT sql FROM sqlite_master WHERE name='world_entities'").fetchone()[0]
    if re.search(r'name\s+TEXT\s+NOT\s+NULL\s+UNIQUE', schema, re.I):
        updated = re.sub(r'name\s+TEXT\s+NOT\s+NULL\s+UNIQUE', 'name TEXT NOT NULL', schema, flags=re.I)
        conn.execute("ALTER TABLE world_entities RENAME TO world_entities_legacy")
        conn.execute(updated)
        names = ','.join(f'"{c}"' for c in columns(conn, "world_entities"))
        conn.execute(f"INSERT INTO world_entities ({names}) SELECT {names} FROM world_entities_legacy")
        conn.execute("DROP TABLE world_entities_legacy")
    for table, name, definition in [
        ("world_entities", "uid", "TEXT"), ("world_entities", "visibility", "TEXT NOT NULL DEFAULT 'public'"),
        ("characters", "entity_uid", "TEXT"), ("npc_chat_logs", "entity_uid", "TEXT"),
        ("npc_chat_logs", "timeline_id", "INTEGER"), ("timelines", "game_tick", "INTEGER NOT NULL DEFAULT 0"),
        ("nodes", "is_draft", "INTEGER NOT NULL DEFAULT 0"),
    ]:
        if name not in columns(conn, table):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
    conn.execute('UPDATE nodes SET is_draft=1 WHERE id IN (SELECT node_id FROM pending_effects)')
    for row in conn.execute("SELECT id FROM world_entities WHERE uid IS NULL OR uid=''").fetchall():
        conn.execute("UPDATE world_entities SET uid=? WHERE id=?", (new_uid(), row["id"]))
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_entity_uid ON world_entities(uid)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_character_entity ON characters(entity_uid)")
    for char in conn.execute("SELECT * FROM characters WHERE entity_uid IS NULL OR entity_uid=''").fetchall():
        matches = conn.execute("SELECT uid FROM world_entities WHERE name=?", (char["name"],)).fetchall()
        linked = {r[0] for r in conn.execute("SELECT entity_uid FROM characters WHERE entity_uid IS NOT NULL")}
        uid = matches[0][0] if len(matches) == 1 and matches[0][0] not in linked else create_entity(
            conn, name=char["name"], entity_type="npc" if char["role"] == "NPC" else "pc",
            state_desc=encode({"desc": char["personality"] or "", "emotion": {}, "memory": []}))
        conn.execute("UPDATE characters SET entity_uid=? WHERE id=?", (uid, char["id"]))
    conn.execute("UPDATE npc_chat_logs SET entity_uid=(SELECT uid FROM world_entities e "
                 "WHERE e.name=npc_chat_logs.npc_name) WHERE entity_uid IS NULL AND "
                 "(SELECT COUNT(*) FROM world_entities e WHERE e.name=npc_chat_logs.npc_name)=1")
    statements = [
        "CREATE TABLE IF NOT EXISTS wiki_jobs (event_uid TEXT PRIMARY KEY, event_id INTEGER NOT NULL, "
        "status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0, "
        "next_attempt REAL NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '')",
        "CREATE TABLE IF NOT EXISTS game_flags (key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT '')",
        "CREATE TABLE IF NOT EXISTS state_definitions (uid TEXT PRIMARY KEY, key TEXT UNIQUE NOT NULL, "
        "display_name TEXT NOT NULL, description TEXT NOT NULL, value_type TEXT NOT NULL, "
        "enum_values TEXT NOT NULL DEFAULT '[]', min_value REAL, max_value REAL, effects TEXT NOT NULL DEFAULT '', "
        "aliases TEXT NOT NULL DEFAULT '[]', source_event_id INTEGER)",
        "CREATE TABLE IF NOT EXISTS entity_states (entity_uid TEXT NOT NULL, state_uid TEXT NOT NULL, "
        "target_uid TEXT NOT NULL DEFAULT '', value TEXT NOT NULL, visibility TEXT NOT NULL DEFAULT 'public', "
        "expires_at_tick INTEGER, source_event_id INTEGER, version INTEGER NOT NULL DEFAULT 1, "
        "PRIMARY KEY(entity_uid,state_uid,target_uid))",
        "CREATE TABLE IF NOT EXISTS entity_relations (uid TEXT PRIMARY KEY, subject_uid TEXT NOT NULL, "
        "predicate TEXT NOT NULL, object_uid TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', "
        "visibility TEXT NOT NULL DEFAULT 'public', source_event_id INTEGER)",
        "CREATE TABLE IF NOT EXISTS entity_memories (id INTEGER PRIMARY KEY AUTOINCREMENT, entity_uid TEXT NOT NULL, "
        "content TEXT NOT NULL, timeline_id INTEGER, visibility TEXT NOT NULL DEFAULT 'public', source_event_id INTEGER)",
        "CREATE TABLE IF NOT EXISTS world_events (id INTEGER PRIMARY KEY AUTOINCREMENT, event_uid TEXT UNIQUE NOT NULL, "
        "operation_key TEXT UNIQUE, kind TEXT NOT NULL, timeline_id INTEGER, game_tick INTEGER NOT NULL DEFAULT 0, "
        "content TEXT NOT NULL, payload TEXT NOT NULL, valid INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS wiki_pages (page_key TEXT PRIMARY KEY, title TEXT NOT NULL, body TEXT NOT NULL, "
        "entity_uid TEXT, visibility TEXT NOT NULL DEFAULT 'public', source_event_ids TEXT NOT NULL DEFAULT '[]', "
        "source_token TEXT NOT NULL, generated INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS wiki_sources (source_key TEXT PRIMARY KEY, title TEXT NOT NULL, content TEXT NOT NULL, "
        "visibility TEXT NOT NULL DEFAULT 'public', created_at TEXT NOT NULL)",
    ]
    for statement in statements:
        conn.execute(statement)
    from knowledge import init
    init(conn)
    conn.execute("INSERT OR IGNORE INTO system_state(key,value) VALUES('world_epoch',?)", (new_uid("world"),))
    conn.execute("INSERT OR IGNORE INTO system_state(key,value) VALUES('game_tick','0')")


def create_entity(conn, *, name, entity_type="npc", state_desc="", location="", status="active",
                  room_id=None, aliases=None, visibility="public", uid=None):
    uid = uid or new_uid()
    validate_visibility(visibility)
    if not str(name).strip():
        raise ValueError("Entity name cannot be empty")
    if not isinstance(aliases or [],list) or any(not isinstance(a,str) or not a.strip() for a in aliases or []):
        raise ValueError('Aliases must be a list of nonempty strings')
    if uid and not re.fullmatch(r'ent_[a-f0-9]{32}',uid): raise ValueError('Invalid entity ID format')
    conn.execute("INSERT INTO world_entities(uid,entity_type,name,state_desc,location,status,room_id,aliases,visibility) "
                 "VALUES(?,?,?,?,?,?,?,?,?)", (uid, entity_type, str(name).strip()[:100], state_desc, location,
                    status, room_id, encode(aliases or []), visibility))
    return uid


def resolve_entity(conn, *, uid=None, name=None, entity_type=None):
    if uid:
        row = conn.execute("SELECT * FROM world_entities WHERE uid=?", (uid,)).fetchone()
        if row is None:
            raise ValueError(f"Entity reference does not exist: {uid}")
        return row
    if not name:
        raise ValueError("Entity ID or name required")
    rows = conn.execute("SELECT * FROM world_entities WHERE name=?", (name,)).fetchall()
    if entity_type:
        rows = [r for r in rows if r["entity_type"] == entity_type]
    if len(rows) != 1:
        raise ValueError(f"Name [{name}]{'is ambiguous; use an entity ID' if rows else 'does not exist'}")
    return rows[0]


def link_character(conn, char_id, uid=None, *, new=False, desc=""):
    char = conn.execute("SELECT * FROM characters WHERE id=?", (char_id,)).fetchone()
    if uid:
        resolve_entity(conn, uid=uid)
    elif char["entity_uid"]:
        return char["entity_uid"]
    else:
        uid = create_entity(conn, name=char["name"], entity_type="npc" if char["role"] == "NPC" else "pc",
                            state_desc=encode({"desc": desc or char["personality"] or "", "emotion": {}, "memory": []}))
    conn.execute("UPDATE characters SET entity_uid=? WHERE id=?", (uid, char_id))
    return uid


def validate_visibility(value):
    if value not in {"public", "gm"} and not re.fullmatch(r"(?:team:\d+|entity:ent_[a-f0-9]{32})", value):
        raise ValueError("visibility must be public, gm, team:<party ID>, or entity:<entity ID>")


def visible(scope, viewer="gm", timeline_id=None):
    return viewer == "gm" or scope == "public" or scope == viewer or (timeline_id is not None and scope == f"team:{timeline_id}")


def state_token(conn):
    """Only authoritative state participates; background wiki writes never stale a draft."""
    digest = hashlib.sha256()
    for table in ("characters", "world_entities", "timelines", "map_rooms", "map_edges", "game_flags",
                  "entity_states", "state_definitions", "entity_relations", "entity_memories", "lorebook",
                  "rag_documents", "rag_chunks", "wiki_sources", "triggers", "scene_locations"):
        rows = [dict(r) for r in conn.execute(f'SELECT * FROM "{table}"').fetchall()]
        digest.update(encode(sorted(rows, key=lambda r: encode(r))).encode())
    events = conn.execute('SELECT id,kind,content,timeline_id,game_tick,valid FROM world_events ORDER BY id').fetchall()
    digest.update(encode([dict(r) for r in events]).encode())
    nodes=conn.execute('SELECT id,name,content FROM nodes WHERE is_draft=0 ORDER BY id').fetchall()
    digest.update(encode([dict(r) for r in nodes]).encode())
    rows = conn.execute("SELECT key,value FROM system_state WHERE key IN "
                        "('world_epoch','game_tick','current_room_id','player_current_scene_id','worldview') ORDER BY key").fetchall()
    digest.update(encode([dict(r) for r in rows]).encode())
    return digest.hexdigest()


def record_event(conn, kind, content, payload=None, timeline_id=None, operation_key=None):
    row = conn.execute("SELECT value FROM system_state WHERE key='game_tick'").fetchone()
    tick = int(row[0]) if row else 0
    cur = conn.execute("INSERT INTO world_events(event_uid,operation_key,kind,timeline_id,game_tick,content,payload,created_at) "
                       "VALUES(?,?,?,?,?,?,?,?)", (new_uid("evt"), operation_key, kind, timeline_id, tick,
                       content, encode(payload or {}), now()))
    return cur.lastrowid


def known_definitions(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM state_definitions ORDER BY key")]


def define_state(conn, definition, event_id):
    key = str(definition.get("key", "")).strip()
    if not key or len(key) > 100:
        raise ValueError("State key must be nonempty and at most 100 characters")
    name = str(definition.get("display_name", key)).strip()
    description = str(definition.get("description", "")).strip()
    kind = definition.get("value_type", "text")
    if kind not in {"text", "integer", "number", "boolean", "enum", "json"} or not description:
        raise ValueError("A new state must provide its meaning and a supported value type")
    enum = definition.get("enum_values", [])
    lower, upper = definition.get("min_value"), definition.get("max_value")
    import math
    if any(v is not None and (type(v) not in {int,float} or not math.isfinite(v)) for v in (lower,upper)):
        raise ValueError("State range must contain finite numbers")
    if kind == "enum" and (not isinstance(enum, list) or not enum):
        raise ValueError("Enum state requires nonempty allowed values")
    if lower is not None and upper is not None and lower > upper:
        raise ValueError("Invalid state range")
    existing = conn.execute("SELECT * FROM state_definitions WHERE key=?", (key,)).fetchone()
    if existing:
        # Definitions are stable; changing their semantics requires an explicit new key.
        for field in ("value_type", "description", "min_value", "max_value"):
            if field in definition and definition[field] != existing[field]:
                raise ValueError(f"State [{key}] definition conflicts with the registered definition")
        if "enum_values" in definition and encode(enum) != existing["enum_values"]:
            raise ValueError("State enum definition conflict")
        return existing["uid"]
    uid = new_uid("state")
    conn.execute("INSERT INTO state_definitions VALUES(?,?,?,?,?,?,?,?,?,?,?)", (uid, key, name, description,
        kind, encode(enum), lower, upper, str(definition.get("effects", "")),
        encode(definition.get("aliases", [])), event_id))
    return uid


def _check_value(definition, value):
    kind = definition["value_type"]
    valid = {"text": isinstance(value, str), "integer": type(value) is int,
             "number": type(value) in {int, float}, "boolean": type(value) is bool,
             "enum": value in json.loads(definition["enum_values"]), "json": True}[kind]
    if not valid:
        raise ValueError(f"State  {definition['key']} has an invalid value type")
    if kind in {"integer", "number"}:
        import math
        if not math.isfinite(value) or (definition["min_value"] is not None and value < definition["min_value"]) or (
                definition["max_value"] is not None and value > definition["max_value"]):
            raise ValueError(f"State  {definition['key']} is outside the defined range")


def apply_state_changes(conn, definitions, changes, event_id, references=None):
    references = references or {}
    for definition in definitions:
        define_state(conn, definition, event_id)
    for change in changes:
        uid = references.get(change.get("entity_uid"), change.get("entity_uid"))
        resolve_entity(conn, uid=uid)
        target = references.get(change.get("target_uid"), change.get("target_uid")) or ""
        if target:
            resolve_entity(conn, uid=target)
        definition = conn.execute("SELECT * FROM state_definitions WHERE key=? OR uid=?",
                                  (change.get("state_key"), change.get("state_uid"))).fetchone()
        if definition is None:
            raise ValueError("State change references an undefined state")
        if change.get("op", "set") == "remove":
            conn.execute("DELETE FROM entity_states WHERE entity_uid=? AND state_uid=? AND target_uid=?",
                         (uid, definition["uid"], target))
            continue
        if change.get("op", "set") != "set":
            raise ValueError("State operations only support set/remove")
        _check_value(definition, change.get("value"))
        visibility = change.get("visibility", "public")
        validate_visibility(visibility)
        expires = change.get("expires_at_tick")
        if expires is not None and (type(expires) is not int or expires < 0):
            raise ValueError("Expiration tick must be a nonnegative integer")
        conn.execute("INSERT INTO entity_states(entity_uid,state_uid,target_uid,value,visibility,expires_at_tick,source_event_id) "
                     "VALUES(?,?,?,?,?,?,?) ON CONFLICT(entity_uid,state_uid,target_uid) DO UPDATE SET "
                     "value=excluded.value,visibility=excluded.visibility,expires_at_tick=excluded.expires_at_tick,"
                     "source_event_id=excluded.source_event_id,version=entity_states.version+1",
                     (uid, definition["uid"], target, encode(change.get("value")), visibility, expires, event_id))


def entity_state_context(conn, uid, viewer="gm", timeline_id=None):
    from knowledge import allowed
    tick = int(conn.execute("SELECT value FROM system_state WHERE key='game_tick'").fetchone()[0])
    result = []
    rows = conn.execute("SELECT s.*,d.key,d.display_name,d.description,d.value_type,d.effects FROM entity_states s "
                        "JOIN state_definitions d ON d.uid=s.state_uid WHERE s.entity_uid=?", (uid,))
    for row in rows:
        if allowed(row["visibility"], None, viewer, timeline_id) and (row["expires_at_tick"] is None or row["expires_at_tick"] > tick):
            result.append({"key": row["key"], "name": row["display_name"], "value": json.loads(row["value"]),
                           "meaning": row["description"], "effects": row["effects"], "target_uid": row["target_uid"],
                           "visibility":row['visibility'],"expires_at_tick":row['expires_at_tick'],"source_event_id":row['source_event_id']})
    return result


def entity_candidates(conn, mention, *, viewer="gm", timeline_id=None):
    rows = conn.execute("SELECT * FROM world_entities ORDER BY updated_at DESC").fetchall()
    scored = []
    text = mention.lower().strip()
    for row in rows:
        if not visible(row["visibility"], viewer, timeline_id):
            continue
        labels = [row["name"], *json.loads(row["aliases"] or "[]")]
        score = 10 if any(text == x.lower() for x in labels) else 4 if any(text in x.lower() or x.lower() in text for x in labels) else 0
        if score:
            scored.append((score, {"entity_uid": row["uid"], "name": row["name"], "aliases": labels[1:],
                                  "location": row["location"], "type": row["entity_type"], "status": row["status"]}))
    return [r for _, r in sorted(scored, key=lambda pair: -pair[0])[:20]]


def resolve_mention(conn, mention, context="", *, viewer="gm", timeline_id=None):
    from providers import evaluate
    candidates = entity_candidates(conn, mention, viewer=viewer, timeline_id=timeline_id)
    exact = [c for c in candidates if mention in [c['name'],*c['aliases']]]
    if len(exact) == 1 and len(candidates) == 1:
        return {"status": "resolved", "entity_uid": exact[0]["entity_uid"], "candidates": candidates}
    if not candidates:
        return {"status": "unknown", "entity_uid": None, "candidates": []}
    criteria = {c["entity_uid"]: encode(c) for c in candidates}
    criteria["unknown"] = "Insufficient evidence, no candidate matches, or cannot distinguish"
    result = evaluate({"mention": mention, "context": context}, {"entity": {
        "type": "choice", "instructions": "Resolve the object referenced by the player using explicit context; do not guess.", "criteria": criteria}})["entity"]
    probabilities = sorted(result.probabilities.values(), reverse=True)
    ambiguous = result.status != "ok" or result.value == "unknown" or (result.confidence is not None and result.confidence < .55) or (len(probabilities) > 1 and probabilities[0] - probabilities[1] < .15)
    return {"status": "clarification_required" if ambiguous else "resolved",
            "entity_uid": None if ambiguous else result.value, "candidates": candidates,
            "question": f"When you said [{mention}], which one did you mean: " + ", ".join(c["name"] for c in candidates) + "?" if ambiguous else "",
            "provider": result.provider, "confidence": result.confidence}


def wiki_context(conn, query, *, viewer="gm", timeline_id=None, limit=6):
    from knowledge import retrieve
    return retrieve(conn, query, viewer=viewer, timeline_id=timeline_id, limit=limit).text


def refresh_wiki(conn):
    from knowledge import sync
    sync(conn)


def synthesize_wiki(db_file, page_key):
    from wiki_compiler import compile_page
    return compile_page(db_file, page_key)


def snapshot(conn):
    refresh_wiki(conn)
    return {table: [dict(r) for r in conn.execute(f'SELECT * FROM "{table}"')]
            for table in SNAPSHOT_TABLES if columns(conn, table)}


def restore(conn, data):
    # Old saves have no knowledge tables; never inherit another campaign's index/history.
    for table in KNOWLEDGE_TABLES:
        if table not in data:
            conn.execute(f'DELETE FROM "{table}"')
    if 'npc_generation_reviews' not in data:
        conn.execute('DELETE FROM npc_generation_reviews')
    if 'scene_locations' not in data:
        conn.execute('DELETE FROM scene_locations')
    for table in SNAPSHOT_TABLES:
        if table not in data or not columns(conn, table):
            continue
        conn.execute(f'DELETE FROM "{table}"')
        allowed = columns(conn, table)
        for row in data[table]:
            row = {k: v for k, v in row.items() if k in allowed}
            if not row:
                continue
            names = ','.join(f'"{k}"' for k in row)
            conn.execute(f'INSERT INTO "{table}" ({names}) VALUES ({",".join("?" for _ in row)})', list(row.values()))
    # Invalidates work from older campaigns/checkpoints, including background memory jobs.
    conn.execute("INSERT OR REPLACE INTO system_state VALUES('world_epoch',?)", (new_uid("world"),))
    init_world(conn)
    conn.execute("UPDATE wiki_jobs SET status='pending' WHERE status='running'")
    refresh_wiki(conn)
    conn.execute("UPDATE knowledge_jobs SET epoch=?,status=CASE WHEN status='running' THEN 'pending' ELSE status END,lease_until=0", (conn.execute("SELECT value FROM system_state WHERE key='world_epoch'").fetchone()[0],))
    token = state_token(conn)
    for row in conn.execute("SELECT id,payload FROM pending_effects").fetchall():
        payload = json.loads(row["payload"])
        payload["state_token"] = token
        conn.execute("UPDATE pending_effects SET payload=? WHERE id=?", (encode(payload), row["id"]))
    refresh_wiki(conn)


WORLD_PROMPT = """
[Entity Identity and Open State Protocol]
Names may repeat. Side effects targeting known characters/entities should preferably use entity_uid from context; unique names or aliases may also be used and will be normalized by the system.
stat_changes uses entity_uid; emotion_deltas/npc_memories use entity_uid.
For new entities use new_entities:[{ref:'new_1',name,entity_type,description,aliases,visibility}]; do not invent permanent IDs.
Prefer npc:{ref:'new_1',name,role:'NPC',hp,san,inventory,backstory} to create a character.
New characters in dialogue mode use new_entities with entity_type npc; hp, san, and inventory are optional and default to 50, 60, and an empty inventory.
Declare newly introduced interactive characters within the current branch. Their side effects reference ref, such as new_1, or a consistent unambiguous in-branch name; do not borrow the ID of another character.
Pure background descriptions do not require character registration or forced emotion, memory, or numerical changes for everyone. Omit effects with uncertain targets, keeping the story natural and coherent.
Do not register the same new character in both npc and new_entities. npc.ref and new_entities.ref must be unique within the branch.
Changes to the name/location/status/aliases/description of an existing entity use entity_changes:[{entity_uid,name?,location?,status?,description?,aliases?}].
Define states automatically as needed, without requiring human presets: state_definitions:[{key,display_name,description,value_type,
enum_values?,min_value?,max_value?,effects?,aliases?}]. value_type can be text/integer/number/boolean/enum/json.
Prefer reusing known state definitions. Do not change the meaning, type, or units of an existing key. Descriptive states do not need to be numerical.
State changes use state_changes:[{entity_uid:'existing ID or new_1',state_key,op:'set or remove',value?,target_uid?,
visibility:'public or gm or team:<party ID> or entity:<entity ID>',expires_at_tick?}].
new_entities/state_definitions/state_changes must occur inside individual branches; an unselected branch remains a draft and must not become a fact.
If ambiguity affects the outcome, return clarification:{question:'question to ask the player',candidates:[{entity_uid,name}]},branches:[].
Do not guess which object is being sold, discarded, transferred, or attacked. A new item can be registered as an item entity.
Parties act separately within one shared world. All parties share world facts but have separate locations and scopes of knowledge.
Private information visible to the GM does not automatically become player or NPC knowledge; it may be used in dialogue only when visibility explicitly permits or knowledge acquisition is confirmed.
"""
