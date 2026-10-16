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
        raise ValueError("实体名称不能为空")
    if not isinstance(aliases or [],list) or any(not isinstance(a,str) or not a.strip() for a in aliases or []):
        raise ValueError('别名必须是非空字符串列表')
    if uid and not re.fullmatch(r'ent_[a-f0-9]{32}',uid): raise ValueError('实体ID格式无效')
    conn.execute("INSERT INTO world_entities(uid,entity_type,name,state_desc,location,status,room_id,aliases,visibility) "
                 "VALUES(?,?,?,?,?,?,?,?,?)", (uid, entity_type, str(name).strip()[:100], state_desc, location,
                    status, room_id, encode(aliases or []), visibility))
    return uid


def resolve_entity(conn, *, uid=None, name=None, entity_type=None):
    if uid:
        row = conn.execute("SELECT * FROM world_entities WHERE uid=?", (uid,)).fetchone()
        if row is None:
            raise ValueError(f"实体引用不存在：{uid}")
        return row
    if not name:
        raise ValueError("需要实体 ID 或名称")
    rows = conn.execute("SELECT * FROM world_entities WHERE name=?", (name,)).fetchall()
    if entity_type:
        rows = [r for r in rows if r["entity_type"] == entity_type]
    if len(rows) != 1:
        raise ValueError(f"名称「{name}」{'有歧义，请使用实体 ID' if rows else '不存在'}")
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
        raise ValueError("visibility 必须为 public、gm、team:队伍ID 或 entity:实体ID")


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
        raise ValueError("状态键不能为空，且不能超过100字符")
    name = str(definition.get("display_name", key)).strip()
    description = str(definition.get("description", "")).strip()
    kind = definition.get("value_type", "text")
    if kind not in {"text", "integer", "number", "boolean", "enum", "json"} or not description:
        raise ValueError("新状态必须提供含义和受支持的值类型")
    enum = definition.get("enum_values", [])
    lower, upper = definition.get("min_value"), definition.get("max_value")
    import math
    if any(v is not None and (type(v) not in {int,float} or not math.isfinite(v)) for v in (lower,upper)):
        raise ValueError("状态范围必须为有限数值")
    if kind == "enum" and (not isinstance(enum, list) or not enum):
        raise ValueError("枚举状态需要非空候选值")
    if lower is not None and upper is not None and lower > upper:
        raise ValueError("状态范围无效")
    existing = conn.execute("SELECT * FROM state_definitions WHERE key=?", (key,)).fetchone()
    if existing:
        # Definitions are stable; changing their semantics requires an explicit new key.
        for field in ("value_type", "description", "min_value", "max_value"):
            if field in definition and definition[field] != existing[field]:
                raise ValueError(f"状态「{key}」的定义与已登记定义冲突")
        if "enum_values" in definition and encode(enum) != existing["enum_values"]:
            raise ValueError("状态枚举定义冲突")
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
        raise ValueError(f"状态 {definition['key']} 的值类型无效")
    if kind in {"integer", "number"}:
        import math
        if not math.isfinite(value) or (definition["min_value"] is not None and value < definition["min_value"]) or (
                definition["max_value"] is not None and value > definition["max_value"]):
            raise ValueError(f"状态 {definition['key']} 超出定义范围")


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
            raise ValueError("状态变更引用未定义的状态")
        if change.get("op", "set") == "remove":
            conn.execute("DELETE FROM entity_states WHERE entity_uid=? AND state_uid=? AND target_uid=?",
                         (uid, definition["uid"], target))
            continue
        if change.get("op", "set") != "set":
            raise ValueError("状态操作只支持 set/remove")
        _check_value(definition, change.get("value"))
        visibility = change.get("visibility", "public")
        validate_visibility(visibility)
        expires = change.get("expires_at_tick")
        if expires is not None and (type(expires) is not int or expires < 0):
            raise ValueError("过期回合必须为非负整数")
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
    criteria["unknown"] = "证据不足、候选均不是、或无法区分"
    result = evaluate({"mention": mention, "context": context}, {"entity": {
        "type": "choice", "instructions": "根据明确上下文判断玩家所指对象；不得猜测。", "criteria": criteria}})["entity"]
    probabilities = sorted(result.probabilities.values(), reverse=True)
    ambiguous = result.status != "ok" or result.value == "unknown" or (result.confidence is not None and result.confidence < .55) or (len(probabilities) > 1 and probabilities[0] - probabilities[1] < .15)
    return {"status": "clarification_required" if ambiguous else "resolved",
            "entity_uid": None if ambiguous else result.value, "candidates": candidates,
            "question": f"你说的「{mention}」指的是哪一个：" + "、".join(c["name"] for c in candidates) + "？" if ambiguous else "",
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
【实体身份与开放状态协议】
名称可重复；已知角色/实体的副作用优先引用上下文中的 entity_uid；也可使用唯一的姓名或别名，由系统归一化。
stat_changes 使用 entity_uid；emotion_deltas/npc_memories 使用 entity_uid。
新实体使用 new_entities:[{ref:'new_1',name,entity_type,description,aliases,visibility}]，不得自造永久ID。
人物的新建优先使用 npc:{ref:'new_1',name,role:'NPC',hp,san,inventory,backstory}。
对话模式新人物使用 new_entities，entity_type 为 npc；可提供 hp、san、inventory，省略时默认50、60、空背包。
新登场且参与互动的人物建议在本分支声明；其副作用引用 ref，如 new_1，也可使用本分支中一致的自然称呼，不能借用其他角色的ID。
纯背景描写无需登记人物，也无需强行为每个人生成情绪、记忆或数值变化。不确定对象的副作用可省略，保持剧情自然连贯。
同一新人物不要同时登记在npc和new_entities中。npc.ref与new_entities.ref在该分支内不能重复。
已有实体的名称/位置/状态/别名/描述变化使用 entity_changes:[{entity_uid,name?,location?,status?,description?,aliases?}]。
状态按需自动定义，不需要人类预设：state_definitions:[{key,display_name,description,value_type,
enum_values?,min_value?,max_value?,effects?,aliases?}]。value_type 为 text/integer/number/boolean/enum/json。
优先复用已知状态定义；同一键的含义、类型和量纲不得改变。描述性状态无需强制数值化。
状态变更使用 state_changes:[{entity_uid:'已有ID或new_1',state_key,op:'set或remove',value?,target_uid?,
visibility:'public或gm或team:队伍ID或entity:实体ID',expires_at_tick?}]。
new_entities/state_definitions/state_changes 必须在各分支内部；未选择分支是草稿，不能成为事实。
有歧义且影响结果时返回 clarification:{question:'向玩家澄清的问题',candidates:[{entity_uid,name}]}，branches:[]。
不得通过猜测确定要出售、丢弃、转移或攻击的对象。新物品可以登记为 item 实体。
世界为同一世界中的分队行动，所有队伍共享事实，但各有独立位置和知情范围。
GM可见的私密信息不能自动变成玩家或NPC的知识；只有其visibility明确允许或确认获知才可用于台词。
"""
