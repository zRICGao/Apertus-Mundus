from providers import evaluate
from world import entity_state_context, resolve_entity, record_event, refresh_wiki, link_character, encode, apply_state_changes
from database import connect_db, atomic
from providers import generation, parse_object
"""
APERTUS-MUNDUS Engine — Trigger System Module (trigger.py)

Supported features:
- DAG condition tree:{"op": "and|or|not", "children": [...]}, with automatic migration of legacy AND lists
- fire_count / cooldown / prerequisite_trigger_ids / exclude_trigger_ids
- The trigger_judgements table records AI decision reasoning (including prompt_hash deduplication cache)
"""

import hashlib
import json
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime

import fastapi
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from logger import get_logger
from rag import refresh_vector_cache

_log = get_logger("trigger")

trigger_router = APIRouter(tags=["Trigger System"])

# ---------------------------------------------------------
# Dependency injection
# ---------------------------------------------------------
_db_file: str = ""
_generation_client = None
_fn_get_system_context = None
_fn_append_to_memory = None


def configure_trigger(db_file: str, generation_client,
                      fn_get_system_context=None,
                      fn_append_to_memory=None):
    global _db_file, _generation_client, _fn_get_system_context, _fn_append_to_memory
    _db_file = db_file
    _generation_client = generation_client
    _fn_get_system_context = fn_get_system_context
    _fn_append_to_memory = fn_append_to_memory
    _ensure_schema()


def get_db_connection():
    return connect_db(_db_file)



@contextmanager
def safe_db():
    conn = get_db_connection()
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------
# Schema migration (idempotent, runs automatically on startup)
# ---------------------------------------------------------
def _ensure_schema():
    with safe_db() as conn:
        # Additional triggers table columns
        for col, typedef in [
            ("fire_count",               "INTEGER NOT NULL DEFAULT 0"),
            ("cooldown",                 "INTEGER NOT NULL DEFAULT 0"),
            ("last_fired_at",            "INTEGER NOT NULL DEFAULT 0"),  # Unix timestamp to avoid timezone ambiguity
            ("prerequisite_trigger_ids", "TEXT NOT NULL DEFAULT '[]'"),
            ("exclude_trigger_ids",      "TEXT NOT NULL DEFAULT '[]'"),
            ("max_fire_count",           "INTEGER NOT NULL DEFAULT 0"),
            ("actions",                  "TEXT NOT NULL DEFAULT '[]'"),
        ]:
            try:
                conn.execute(f"ALTER TABLE triggers ADD COLUMN {col} {typedef}")
            except sqlite3.OperationalError:
                pass  # Column already exists

        if 'timeline_id' not in {r[1] for r in conn.execute('PRAGMA table_info(triggers)')}:
            conn.execute('ALTER TABLE triggers ADD COLUMN timeline_id INTEGER')

        # trigger_judgements table (AI decision audit trail, not a cache read path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trigger_judgements (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                trigger_id    INTEGER,
                scene_id      INTEGER,
                scene_name    TEXT,
                timestamp     TEXT,
                reasoning     TEXT,
                result        INTEGER,
                condition_hash TEXT  -- For deduplicated aggregate queries only; not used in decision logic
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_judge_trigger "
            "ON trigger_judgements(trigger_id)"
        )
        try:
            conn.execute("ALTER TABLE trigger_judgements ADD COLUMN condition_hash TEXT")
        except sqlite3.OperationalError:
            pass  # Column already exists

        # game_flags table: named state flags read and written by set_flag
        conn.execute("""
            CREATE TABLE IF NOT EXISTS game_flags (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL DEFAULT ''
            )
        """)
        conn.commit()


# ---------------------------------------------------------
# Pydantic data models
# ---------------------------------------------------------
class ConditionItem(BaseModel):
    type: str
    value: str = ""

class TriggerCreateRequest(BaseModel):
    label: str = "Unnamed Trigger"
    target_node_id: int = 0             # passive mode can use 0 (no navigation target)
    mode: str = "soft"                  # soft | hard | passive
    conditions: list | dict = []        # Accept legacy lists or new tree structures
    cond_type: str = ""
    cond_value: str = ""
    cooldown: int = 0
    prerequisite_trigger_ids: list[int] = []
    exclude_trigger_ids: list[int] = []
    max_fire_count: int = 0             # 0=unlimited; >0=permanently block after N activations
    timeline_id: int | None = None
    actions: list = []                  # List of side-effect actions executed on trigger activation

class TriggerUpdateRequest(BaseModel):
    label: str
    target_node_id: int = 0
    mode: str
    conditions: list | dict = []
    cond_type: str = ""
    cond_value: str = ""
    cooldown: int = 0
    prerequisite_trigger_ids: list[int] = []
    exclude_trigger_ids: list[int] = []
    max_fire_count: int = 0
    timeline_id: int | None = None
    actions: list = []

class CheckTriggersRequest(BaseModel):
    scene_id: int
    scene_name: str
    scene_content: str
    timeline_id: int | None = None


# ---------------------------------------------------------
# Condition tree: parsing and normalization
# ---------------------------------------------------------
def _parse_condition_tree(raw: str | None) -> dict:
    """
    Parse the database conditions field into canonical tree nodes.
    Legacy [...] is automatically upgraded to  {"op": "and", "children": [...]}.
    """
    if not raw:
        return {"op": "and", "children": []}
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {"op": "and", "children": []}

    if isinstance(data, list):
        # Legacy AND list
        leaves = [{"type": c.get("type", ""), "value": c.get("value", "")}
                  for c in data if c.get("type")]
        return {"op": "and", "children": leaves}

    if isinstance(data, dict) and "op" in data:
        return data

    return {"op": "and", "children": []}


def _normalize_conditions(req) -> str:
    """Serialize the request conditions field consistently as a JSON tree."""
    conds = getattr(req, "conditions", [])
    if isinstance(conds, dict) and "op" in conds:
        return json.dumps(conds, ensure_ascii=False)
    if isinstance(conds, list) and conds:
        leaves = [{"type": c.type if hasattr(c, "type") else c.get("type",""),
                   "value": c.value if hasattr(c, "value") else c.get("value","")}
                  for c in conds]
        return json.dumps({"op": "and", "children": leaves}, ensure_ascii=False)
    # Legacy single-field fallback
    if getattr(req, "cond_type", ""):
        return json.dumps({"op": "and", "children": [
            {"type": req.cond_type, "value": req.cond_value}
        ]}, ensure_ascii=False)
    return json.dumps({"op": "and", "children": []}, ensure_ascii=False)


def _collect_ai_leaves(node: dict) -> list[str]:
    """Recursively collect value from all tree leaves with type=='ai'."""
    if "op" in node:
        result = []
        for child in node.get("children", []):
            result.extend(_collect_ai_leaves(child))
        return result
    if node.get("type") == "ai":
        return [node.get("value", "").strip()]
    return []


# ---------------------------------------------------------
# Local evaluation of individual leaves
# ---------------------------------------------------------
def _judge_leaf(node: dict, scene_id: int, chars: list, all_inv: str) -> bool | None:
    """Return True/False or None (requires AI evaluation)."""
    ctype = node.get("type", "")
    cval  = node.get("value", "").strip()

    if ctype == "scene":
        try:
            return scene_id == int(cval)
        except ValueError:
            return False

    if ctype == "item":
        kws = [k.strip().lower() for k in cval.split(",") if k.strip()]
        return any(k in all_inv for k in kws)

    if ctype == "stat":
        _OPS = [("<=", lambda a, b: a <= b), (">=", lambda a, b: a >= b),
                ("<",  lambda a, b: a < b),  (">",  lambda a, b: a > b),
                ("==", lambda a, b: a == b)]
        for part in cval.split(","):
            part = part.strip().lower()
            for op_str, op_fn in _OPS:
                if op_str in part:
                    attr, _, thr_str = part.partition(op_str)
                    attr = attr.strip()
                    try:
                        thr = int(thr_str.strip())
                        if any(c["role"] == "PC" and op_fn(int(c.get(attr) or 0), thr)
                               for c in chars):
                            return True
                    except (ValueError, TypeError):
                        pass
                    break
        return False

    if ctype == "ai":
        return None  # Delegate evaluation to AI

    return False


# ---------------------------------------------------------
# Recursive tree evaluation
# ---------------------------------------------------------
def _eval_tree(node,scene_id,chars,all_inv,ai_cache,_depth=0,conn=None):
    return _eval_tree_3val(node,scene_id,chars,all_inv,ai_cache,_depth,conn) is True



# ---------------------------------------------------------
# Three-valued preliminary evaluation (True / False / _MAYBE) — AI pruning
# ---------------------------------------------------------
_MAYBE = object()  # Sentinel: AI condition outcome unknown until model call


def _eval_tree_3val(node,scene_id,chars,all_inv,ai_cache,_depth=0,conn=None):
    if _depth>20: return _MAYBE
    if 'op' in node:
        children=node.get('children',[])
        if not children: return _MAYBE
        values=[_eval_tree_3val(c,scene_id,chars,all_inv,ai_cache,_depth+1,conn) for c in children]
        op=node['op']
        if op=='and': return False if any(v is False for v in values) else _MAYBE if any(v is _MAYBE for v in values) else True
        if op=='or': return True if any(v is True for v in values) else _MAYBE if any(v is _MAYBE for v in values) else False
        if op=='not' and len(values)==1: return _MAYBE if values[0] is _MAYBE else not values[0]
        return _MAYBE
    if node.get('type')=='entity_state':
        if conn is None: return _MAYBE
        spec=node.get('value',{})
        try:
            if isinstance(spec,str): spec=json.loads(spec)
            entity=resolve_entity(conn,uid=spec['entity_uid'])
            states=entity_state_context(conn,entity['uid'])
            values=[s['value'] for s in states if s['key']==spec['state_key'] and s['target_uid']==spec.get('target_uid','')]
            if not values: return _MAYBE
            a,b=values[0],spec['value']
            import operator
            compare={'eq':operator.eq,'ne':operator.ne,'gt':operator.gt,'ge':operator.ge,'lt':operator.lt,'le':operator.le}[spec.get('operator','eq')]
            return compare(a,b)
        except (ValueError,TypeError,KeyError): return _MAYBE
    if node.get('type') not in {'ai','scene','item','stat'}: return _MAYBE
    r=_judge_leaf(node,scene_id,chars,all_inv)
    if r is None:
        cached=ai_cache.get(node.get('value','').strip())
        return _MAYBE if cached is None else cached
    return r



# ---------------------------------------------------------
# Batched AI decisions (judgements table is audit-only, not a cache read path)
# ---------------------------------------------------------
def _batch_judge_ai(values,scene_name,scene_content,conn,trigger_id=None,scene_id=0,timeline_id=None):
    scope={'timeline_id':timeline_id} if timeline_id is not None else {}
    context=_fn_get_system_context(conn,scene_name,scene_content,**scope) if _fn_get_system_context else ()
    questions={str(i):{"type":"noul","instructions":"Determine whether the following condition holds using confirmed facts only: "+value} for i,value in enumerate(values)}
    decisions=evaluate({"scene":scene_name,"content":scene_content,"confirmed_context":context},questions)
    results={}
    for i,value in enumerate(values):
        decision=decisions[str(i)]
        answer=None if decision.status!='ok' or .1<decision.value<.9 else decision.value>=.9
        results[value]=answer
        if answer is not None:
            conn.execute("INSERT INTO trigger_judgements(trigger_id,scene_id,scene_name,timestamp,reasoning,result,condition_hash) VALUES(?,?,?,?,?,?,?)",
                (trigger_id,scene_id,scene_name,datetime.now().isoformat(),decision.provider+': '+decision.evidence,int(answer),hashlib.md5(value.encode()).hexdigest()))
    return results



# ---------------------------------------------------------
# AI-assisted on-demand node generation
# ---------------------------------------------------------
def _generate_node_ai(prompt: str, scene_name: str, conn) -> dict | None:
    """Improvise a story node using AI; return {name, content} or None on failure."""
    if not _generation_client or not _fn_get_system_context:
        return None
    try:
        worldview, party_status, _, session_memory, \
            _, world_entities_text, _, _ = \
            _fn_get_system_context(conn, scene_name, prompt)

        system_prompt = (
            "You are a professional TRPG narrative generator. Improvise a new story node from the prompt.\n"
            "Write immersive, concise, evocative prose. Do not break immersion or add explanatory narration.\n\n"
            "--- World Lore ---\n"
            f"{worldview}\n\n"
            "--- Current World Entity States ---\n"
            f"{world_entities_text}\n\n"
            "Return strict JSON (these two fields only):\n"
            '{"name": "Node title (at most 10 characters)", "content": "Scene outline (60–100 characters)"}'
        )
        user_prompt = (
            "[Recent Memory]\n"
            f"{session_memory}\n\n"
            "[Party Status]\n"
            f"{party_status}\n\n"
            "[Current Scene]\n"
            f"{scene_name}\n\n"
            "[Node Generation Prompt]\n"
            f"{prompt}"
        )
        resp = _generation_client.chat.completions.create(
            model=generation.settings.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_prompt},
            ],
            temperature=0.75,
            max_tokens=400,
            response_format={"type": "json_object"},
        )
        if resp.choices[0].finish_reason == "length":
            _log.warning("gen_node AI output truncated")
            return None
        parsed = json.loads(resp.choices[0].message.content)
        name    = str(parsed.get("name", "Trigger-generated node")).strip()[:80]
        content = str(parsed.get("content", "")).strip()
        return {"name": name, "content": content} if content else None
    except Exception as e:
        _log.warning("gen_node AI call failed: %s", e)
        return None


# ---------------------------------------------------------
# Execute trigger actions (side effects on activation)
# Return side_effects dict for check_triggers to aggregate and send to the frontend
# ---------------------------------------------------------

def party_members(conn, timeline_id):
    if timeline_id is None: return None
    team=conn.execute('SELECT char_ids,status FROM timelines WHERE id=?',(timeline_id,)).fetchone()
    if not team or team['status']!='active': raise ValueError('The squad is not active')
    return {int(x) for x in team['char_ids'].split(',') if x.isdigit()}


def append_rule_memory(conn, timeline_id, text):
    if timeline_id is None:
        if _fn_append_to_memory: _fn_append_to_memory(conn,text)
    else:
        from memory_service import _tl_append_memory
        _tl_append_memory(conn,timeline_id,text)


def _execute_actions(actions_raw: str, conn, trigger_id: int,
                     scene_id: int, scene_name: str, timeline_id=None) -> dict:
    """
    Action types:
      set_flag      — {key, value}: write to the game_flags table
      mod_stat      — {target, attr, delta, ceiling?}: modify hp/san; an optional ceiling caps recovery
      add_memory    — {text}: append to session memory
      fire_trigger  — {id}: activate another trigger without recursively executing its actions to prevent cycles
      inject_text   — {node_id?, text}：append narrative text to a node (node_id=0 → current scene)
      inject_option — {node_id?, text, next_node_id}: inject an option into a node
      gen_node      — {prompt, mode}: generate a new node on demand and return it for frontend navigation
      mod_inventory — {target, op, item}: op="add"|"remove", modify the plain-text character inventory field
      mod_npc       — {name, field, value?, delta?}: modify entity status / emotion_* / memory_add
      entity_script — {name, action}：appear | leave | die | attack
    """
    members = party_members(conn, timeline_id)
    side_effects: dict = {
        "text_injections":  [],   # [{node_id, text}]
        "option_injections": [],  # [{node_id, option_id, text, next_node_id}]
        "generated_nodes":  [],   # [{trigger_id, node_id, node_name, node_content, mode}]
        "log":              [],   # [str] Human-readable action summary sent to the frontend
        "reveal_rag_ids":   [],   # [int] doc_id values unlocked by reveal_rag; refresh vector cache after commit
    }

    try:
        actions = json.loads(actions_raw or "[]")
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError('Invalid trigger action JSON') from exc

    if not isinstance(actions,list) or any(not isinstance(a,dict) for a in actions):
        raise ValueError('Trigger actions must be objects')
    for act in actions:
        # Resolve once; every targeted SQL mutation below uses the immutable UID.
        if act.get('type') in {'mod_npc','entity_script','mod_stat','mod_inventory'} and (act.get('entity_uid') or act.get('target','').upper()!='PC' and act.get('type') in {'mod_stat','mod_inventory'} or act.get('type') in {'mod_npc','entity_script'}):
            row=resolve_entity(conn,uid=act.get('entity_uid'),name=act.get('name') or act.get('target'))
            character=conn.execute('SELECT id,role FROM characters WHERE entity_uid=?',(row['uid'],)).fetchone()
            if members is not None and character and character['role']=='PC' and character['id'] not in members:
                raise ValueError('A squad rule cannot change another squad member')
            act=dict(act); act['name']=row['uid']; act['target']=row['uid']
        atype = act.get("type", "")
        if atype not in {'set_flag','mod_stat','add_memory','fire_trigger','inject_text','inject_option',
                         'gen_node','mod_inventory','mod_npc','entity_script','set_state','spawn_npc','mod_worldview','add_lore','reveal_rag'}:
            raise ValueError(f'Unknown trigger action: {atype}')
        try:
            # ── Original four types ──────────────────────────────────────────────────────
            if atype == "set_flag":
                key = str(act.get("key", "")).strip()
                val = str(act.get("value", "")).strip()
                if key:
                    conn.execute(
                        "INSERT OR REPLACE INTO game_flags (key, value) VALUES (?, ?)",
                        (key, val)
                    )
                    _log.info("Trigger %d set_flag: %s = %s", trigger_id, key, val)
                    side_effects["log"].append(f"🚩 Flag {key} = {val}")

            elif atype == "mod_stat":
                target = str(act.get("target", "PC")).strip()
                attr   = str(act.get("attr",   "hp")).strip().lower()
                delta  = int(act.get("delta",  0))
                if attr in ("hp", "san") and delta != 0:
                    expression = f"MAX(0,{attr}+?)"
                    parameters = [delta]
                    if act.get('ceiling') is not None:
                        ceiling = act['ceiling']
                        if type(ceiling) is not int or ceiling < 0:
                            raise ValueError('Stat ceiling must be a nonnegative integer')
                        expression = f"MIN(?,{expression})"
                        parameters.insert(0, ceiling)
                    if target.upper() == "PC":
                        targets=[r['id'] for r in conn.execute("SELECT id FROM characters WHERE role='PC'")
                                 if members is None or r['id'] in members]
                        for char_id in targets:
                            conn.execute(f"UPDATE characters SET {attr}={expression} WHERE id=?",(*parameters,char_id))
                    else:
                        conn.execute(
                            f"UPDATE characters SET {attr}={expression} WHERE entity_uid=?",
                            (*parameters, target)
                        )
                    _log.info("Trigger %d mod_stat: [%s] %s %+d", trigger_id, target, attr, delta)
                    label = "All player characters" if target.upper() == "PC" else target
                    side_effects["log"].append(f"💢 {label} {attr.upper()} {delta:+d}")

            elif atype == "add_memory":
                text = str(act.get("text", "")).strip()
                if text and _fn_append_to_memory:
                    append_rule_memory(conn, timeline_id, text)
                    _log.info("Trigger %d add_memory: %s…", trigger_id, text[:40])
                    side_effects["log"].append(f"📝 Memory saved: {text[:30]}…" if len(text) > 30 else f"📝 Memory saved: {text}")

            elif atype == "fire_trigger":
                tid = int(act.get("id", 0))
                if tid and tid != trigger_id:
                    row = conn.execute(
                        "SELECT fire_count, cooldown, max_fire_count FROM triggers WHERE id=?",
                        (tid,)
                    ).fetchone()
                    if row:
                        new_cnt   = (row["fire_count"] or 0) + 1
                        max_fc    = row["max_fire_count"] or 0
                        new_fired = 1 if (row["cooldown"] or 0) == 0 or (max_fc > 0 and new_cnt >= max_fc) else 0
                        conn.execute(
                            "UPDATE triggers SET fired=?, fire_count=?, last_fired_at=? WHERE id=?",
                            (new_fired, new_cnt, int(time.time()), tid)
                        )
                        _log.info("Trigger %d fire_trigger: activated #%d (time %d)", trigger_id, tid, new_cnt)
                        side_effects["log"].append(f"⚡ Trigger #{tid} activated (time {new_cnt})")

            # ── Narrative layer ────────────────────────────────────────────────────────
            elif atype == "inject_text":
                target_node = int(act.get("node_id", 0) or 0) or scene_id
                text = str(act.get("text", "")).strip()
                if text and target_node:
                    row = conn.execute(
                        "SELECT content, expanded_content FROM nodes WHERE id=?",
                        (target_node,)
                    ).fetchone()
                    if row:
                        if row["expanded_content"]:
                            conn.execute(
                                "UPDATE nodes SET expanded_content=expanded_content||? WHERE id=?",
                                ("\n\n" + text, target_node)
                            )
                            # Also append to the investigation snapshot of the latest battle chronicle entry
                            conn.execute(
                                "UPDATE chronicle_log SET expanded_content=expanded_content||? "
                                "WHERE id=(SELECT id FROM chronicle_log WHERE scene_id=? "
                                "ORDER BY id DESC LIMIT 1)",
                                ("\n\n" + text, target_node)
                            )
                        else:
                            conn.execute(
                                "UPDATE nodes SET content=content||? WHERE id=?",
                                ("\n\n" + text, target_node)
                            )
                            # Also append to the scene snapshot of the latest battle chronicle entry
                            conn.execute(
                                "UPDATE chronicle_log SET scene_content=scene_content||? "
                                "WHERE id=(SELECT id FROM chronicle_log WHERE scene_id=? "
                                "ORDER BY id DESC LIMIT 1)",
                                ("\n\n" + text, target_node)
                            )
                        side_effects["text_injections"].append(
                            {"node_id": target_node, "text": text}
                        )
                        _log.info("Trigger %d inject_text → node %d: %s…", trigger_id, target_node, text[:30])
                        side_effects["log"].append(f"📖 Text injected into the current scene")

            elif atype == "inject_option":
                target_node = int(act.get("node_id", 0) or 0) or scene_id
                opt_text    = str(act.get("text", "")).strip()
                next_nid    = int(act.get("next_node_id", 0) or 0)
                if opt_text and next_nid and target_node:
                    cur = conn.execute(
                        "INSERT INTO options (node_id, text, next_node_id) VALUES (?,?,?)",
                        (target_node, opt_text, next_nid)
                    )
                    side_effects["option_injections"].append({
                        "node_id":     target_node,
                        "option_id":   cur.lastrowid,
                        "text":        opt_text,
                        "next_node_id": next_nid,
                    })
                    _log.info("Trigger %d inject_option → node %d: [%s]→%d",
                              trigger_id, target_node, opt_text, next_nid)
                    side_effects["log"].append(f"🔀 New option injected: [{opt_text[:20]}] → node #{next_nid}")

            elif atype == "gen_node":
                prompt   = str(act.get("prompt", "")).strip()
                nav_mode = str(act.get("mode", "soft")).strip()
                if prompt:
                    result = _generate_node_ai(prompt, scene_name, conn)
                    if not result: raise ValueError('Trigger scene generation failed')
                    if result:
                        cur = conn.execute(
                            "INSERT INTO nodes (name, summary, content) VALUES (?,?,?)",
                            (result["name"], "", result["content"])
                        )
                        new_nid = cur.lastrowid
                        side_effects["generated_nodes"].append({
                            "trigger_id":   trigger_id,
                            "node_id":      new_nid,
                            "node_name":    result["name"],
                            "node_content": result["content"],
                            "mode":         nav_mode,
                        })
                        _log.info("Trigger %d gen_node → new node %d [%s]", trigger_id, new_nid, result["name"])
                        side_effects["log"].append(f"✨ AI generated node [{result['name']}] (#{new_nid})")

            # ── Entity layer ────────────────────────────────────────────────────────
            elif atype == "mod_inventory":
                target = str(act.get("target", "PC")).strip()
                op     = str(act.get("op",     "add")).strip()
                item   = str(act.get("item",   "")).strip()
                if item:
                    if target.upper() == "PC":
                        chars = [dict(r) for r in conn.execute(
                            "SELECT id, inventory FROM characters WHERE role='PC'"
                        ).fetchall()]
                    else:
                        chars = [dict(r) for r in conn.execute(
                            "SELECT id, inventory FROM characters WHERE entity_uid=?", (target,)
                        ).fetchall()]
                    for c in chars:
                        if target.upper() == "PC" and members is not None and c["id"] not in members: continue
                        inv = (c.get("inventory") or "").strip()
                        if op == "add":
                            new_inv = (inv + ", " + item).lstrip(", ")
                        else:
                            parts   = [p.strip() for p in inv.split(",")]
                            parts   = [p for p in parts if item.lower() != p.lower()]
                            new_inv = ", ".join(p for p in parts if p)
                        conn.execute(
                            "UPDATE characters SET inventory=? WHERE id=?",
                            (new_inv, c["id"])
                        )
                    _log.info("Trigger %d mod_inventory: [%s] %s %s", trigger_id, target, op, item)
                    label = "All player characters" if target.upper() == "PC" else target
                    op_str = "gained" if op == "add" else "lost"
                    side_effects["log"].append(f"🎒 {label} {op_str} [{item}]")

            elif atype == "mod_npc":
                name  = str(act.get("name", "")).strip()
                field = str(act.get("field", "status")).strip()
                if not name:
                    continue
                row = conn.execute(
                    "SELECT id, state_desc FROM world_entities WHERE uid=?", (name,)
                ).fetchone()
                if not row:
                    _log.warning("Trigger %d mod_npc: Entity not found: [%s]", trigger_id, name)
                    side_effects["log"].append(f"👤 ⚠ Entity not found: [{name}]")
                    continue
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

                if field == "status":
                    val = str(act.get("value", "active")).strip()
                    conn.execute(
                        "UPDATE world_entities SET status=?, updated_at=? WHERE uid=?",
                        (val, ts, name)
                    )
                    _log.info("Trigger %d mod_npc: %s.status → %s", trigger_id, name, val)
                    side_effects["log"].append(f"👤 {name} status → {val}")

                elif field.startswith("emotion_"):
                    emo_key = field[len("emotion_"):]
                    delta   = int(act.get("delta", 0))
                    try:
                        sd = json.loads(row["state_desc"] or "{}")
                    except (json.JSONDecodeError, TypeError):
                        sd = {}
                    emo = sd.get("emotion", {"trust": 0, "fear": 0, "irritation": 0})
                    emo[emo_key] = max(-100, min(100, emo.get(emo_key, 0) + delta))
                    sd["emotion"] = emo
                    conn.execute(
                        "UPDATE world_entities SET state_desc=?, updated_at=? WHERE uid=?",
                        (json.dumps(sd, ensure_ascii=False), ts, name)
                    )
                    _log.info("Trigger %d mod_npc: %s.%s %+d", trigger_id, name, emo_key, delta)
                    emo_names = {"trust": "Trust", "fear": "Fear", "irritation": "Irritation"}
                    side_effects["log"].append(f"👤 {name} {emo_names.get(emo_key, emo_key)} {delta:+d} → {emo[emo_key]}")

                elif field == "memory_add":
                    mem_text = str(act.get("value", "")).strip()
                    if mem_text:
                        try:
                            sd = json.loads(row["state_desc"] or "{}")
                        except (json.JSONDecodeError, TypeError):
                            sd = {}
                        mem = sd.get("memory", [])
                        mem.append(mem_text)
                        sd["memory"] = mem[-20:]
                        conn.execute(
                            "UPDATE world_entities SET state_desc=?, updated_at=? WHERE uid=?",
                            (json.dumps(sd, ensure_ascii=False), ts, name)
                        )
                        _log.info("Trigger %d mod_npc memory_add: %s ← %s…", trigger_id, name, mem_text[:30])
                        side_effects["log"].append(f"👤 {name} memory saved")

            elif atype == "entity_script":
                name   = str(act.get("name",   "")).strip()
                action = str(act.get("action", "appear")).strip()
                if not name:
                    continue
                row = conn.execute(
                    "SELECT id, state_desc FROM world_entities WHERE uid=?", (name,)
                ).fetchone()
                if not row:
                    _log.warning("Trigger %d entity_script: Entity not found: [%s]", trigger_id, name)
                    side_effects["log"].append(f"⚙️ ⚠ Entity not found: [{name}]")
                    continue
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

                if action in ("appear", "leave", "die"):
                    status_map = {"appear": "active", "leave": "moved", "die": "dead"}
                    conn.execute(
                        "UPDATE world_entities SET status=?, updated_at=? WHERE uid=?",
                        (status_map[action], ts, name)
                    )
                    _log.info("Trigger %d entity_script: %s → %s", trigger_id, name, action)
                    action_names = {"appear": "appeared", "leave": "left", "die": "died"}
                    side_effects["log"].append(f"⚙️ {name} {action_names[action]}")

                elif action == "attack":
                    try:
                        sd = json.loads(row["state_desc"] or "{}")
                    except (json.JSONDecodeError, TypeError):
                        sd = {}
                    emo = sd.get("emotion", {"trust": 0, "fear": 0, "irritation": 0})
                    emo["irritation"] = min(100, emo.get("irritation", 0) + 30)
                    emo["trust"]      = max(-100, emo.get("trust", 0) - 20)
                    mem = sd.get("memory", [])
                    mem.append(f"[Trigger] Attacked players in scene [{scene_name}]")
                    sd["emotion"] = emo
                    sd["memory"]  = mem[-20:]
                    conn.execute(
                        "UPDATE world_entities SET state_desc=?, updated_at=? WHERE uid=?",
                        (json.dumps(sd, ensure_ascii=False), ts, name)
                    )
                    _log.info("Trigger %d entity_script: %s attacked", trigger_id, name)
                    side_effects["log"].append(f"⚙️ {name} attacked (irritation +30, trust -20)")

            # ── World-building layer ───────────────────────────────────────────────────
            elif atype == "set_state":
                for change in act.get('changes',[]):
                    character=conn.execute('SELECT id,role FROM characters WHERE entity_uid=?',(change.get('entity_uid'),)).fetchone()
                    if members is not None and character and character['role']=='PC' and character['id'] not in members:
                        raise ValueError('A squad rule cannot change another squad member')
                event=record_event(conn,'trigger_state','Trigger state change',act,timeline_id)
                apply_state_changes(conn,act.get('definitions',[]),act.get('changes',[]),event)
            elif atype == "spawn_npc":
                name=str(act.get('name','')).strip()
                if not name: raise ValueError('NPC name cannot be empty')
                char=conn.execute("INSERT INTO characters(name,role,hp,san,inventory,personality) VALUES(?,'NPC',?,?,?,?)",
                    (name,int(act.get('hp',50)),int(act.get('san',60)),str(act.get('inventory','')),str(act.get('desc','')))).lastrowid
                uid=link_character(conn,char,new=True)
                side_effects['log'].append(f'NPC [{name}] appeared')
            elif atype == "mod_worldview":
                op   = str(act.get("op",   "append")).strip()
                text = str(act.get("text", "")).strip()
                if not text:
                    continue
                if op == "replace":
                    conn.execute(
                        "UPDATE system_state SET value=? WHERE key='worldview'", (text,)
                    )
                else:
                    conn.execute(
                        "UPDATE system_state SET value=value||? WHERE key='worldview'",
                        ("\n\n" + text,)
                    )
                _log.info("Trigger %d mod_worldview[%s]: %s…", trigger_id, op, text[:40])
                op_str = "replaced" if op == "replace" else "appended"
                side_effects["log"].append(f"🌍 World lore {op_str} ({len(text)} characters)")

            elif atype == "add_lore":
                keywords = str(act.get("keywords", "")).strip()
                content  = str(act.get("content",  "")).strip()
                if keywords and content:
                    conn.execute(
                        "INSERT INTO lorebook (keywords, content) VALUES (?,?)",
                        (keywords, content)
                    )
                    _log.info("Trigger %d add_lore: [%s] %s…", trigger_id, keywords, content[:30])
                    side_effects["log"].append(f"📚 Lorebook added: [{keywords[:20]}]")

            elif atype == "reveal_rag":
                doc_id = int(act.get("doc_id", 0))
                if doc_id:
                    row = conn.execute("SELECT title FROM rag_documents WHERE id=?", (doc_id,)).fetchone()
                    conn.execute(
                        "UPDATE rag_documents SET hidden=0 WHERE id=?",
                        (doc_id,)
                    )
                    side_effects["reveal_rag_ids"].append(doc_id)
                    _log.info("Trigger %d reveal_rag: doc_id=%d unlocked", trigger_id, doc_id)
                    title = row["title"] if row else f"#{doc_id}"
                    side_effects["log"].append(f"🔓 RAG document [{title}] unlocked and loaded into the vector index")

        except Exception as e:
            raise ValueError(f"Trigger {trigger_id} action {atype} failed: {e}") from e

    return side_effects


def _has_gen_node_action(actions) -> bool:
    """If the action list includes gen_node, the navigation target is created by AI at runtime, so a fixed target_node_id is unnecessary."""
    if isinstance(actions, str):
        try:
            actions = json.loads(actions)
        except (json.JSONDecodeError, TypeError):
            return False
    return any(isinstance(a, dict) and a.get("type") == "gen_node" for a in (actions or []))


# ---------------------------------------------------------
# Full trigger evaluation (DAG, prerequisites, exclusions, cooldown, and activation limit)
# ---------------------------------------------------------
def _check_trigger(trigger_row, scene_id: int, scene_name: str,
                   scene_content: str, chars: list, all_inv: str,
                   ai_cache: dict, conn, timeline_id=None) -> bool:
    t = trigger_row

    # ── Prerequisites: all prerequisite triggers must have fired ────────────────────────────────────
    try:
        prereq_ids = json.loads(t["prerequisite_trigger_ids"] or "[]")
    except (TypeError, json.JSONDecodeError):
        prereq_ids = []
    if prereq_ids:
        for pid in prereq_ids:
            row = conn.execute("SELECT fire_count, fired FROM triggers WHERE id=?", (pid,)).fetchone()
            if not row or (row["fire_count"] == 0 and row["fired"] == 0):
                return False

    # ── Exclusions: all excluded triggers must not have fired ────────────────────────────────────
    try:
        excl_ids = json.loads(t["exclude_trigger_ids"] or "[]")
    except (TypeError, json.JSONDecodeError):
        excl_ids = []
    if excl_ids:
        for eid in excl_ids:
            row = conn.execute("SELECT fire_count, fired FROM triggers WHERE id=?", (eid,)).fetchone()
            if row and (row["fire_count"] > 0 or row["fired"] == 1):
                return False

    # ── Cooldown check ────────────────────────────────────────────────────────────
    cooldown = t["cooldown"] if "cooldown" in t.keys() else 0
    if cooldown and cooldown > 0:
        last_ts = t["last_fired_at"] if "last_fired_at" in t.keys() else 0
        if last_ts and (time.time() - int(last_ts)) < cooldown:
            return False

    # ── Activation limit ─────────────────────────────────────────────────────────
    max_fire = t["max_fire_count"] if "max_fire_count" in t.keys() else 0
    if max_fire > 0:
        cur_count = t["fire_count"] if "fire_count" in t.keys() else 0
        if cur_count >= max_fire:
            return False

    # ── Condition tree evaluation ──────────────────────────────────────────────────────────
    tree = _parse_condition_tree(t["conditions"] if "conditions" in t.keys() else "[]")
    if not tree.get("children"):
        return False

    # ── Three-valued pre-evaluation: short-circuit non-AI conditions and skip AI calls for known outcomes ──
    pre = _eval_tree_3val(tree, scene_id, chars, all_inv, ai_cache, conn=conn)
    if pre is False:
        return False
    if pre is True:
        return True

    # Collect and batch AI conditions only for _MAYBE (non-AI conditions alone cannot decide)
    ai_leaves = _collect_ai_leaves(tree)
    new_ai = [v for v in ai_leaves if v and v not in ai_cache]
    if new_ai:
        unique = list(dict.fromkeys(new_ai))
        batch = _batch_judge_ai(unique, scene_name, scene_content, conn,
                                trigger_id=t["id"], scene_id=scene_id,timeline_id=timeline_id)
        ai_cache.update(batch)

    return _eval_tree(tree, scene_id, chars, all_inv, ai_cache, conn=conn)


# ---------------------------------------------------------
# REST API endpoints
# ---------------------------------------------------------
@trigger_router.get("/api/game/triggers")
def get_triggers():
    with safe_db() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM triggers ORDER BY id").fetchall()]
    for r in rows:
        tree = _parse_condition_tree(r.get("conditions", "[]"))
        r["conditions"] = tree
        r.setdefault("fire_count", 0)
        r.setdefault("cooldown", 0)
        r.setdefault("last_fired_at", "")
        r.setdefault("prerequisite_trigger_ids", [])
        r.setdefault("exclude_trigger_ids", [])
        r.setdefault("max_fire_count", 0)
        r.setdefault("actions", [])
        for fld in ("prerequisite_trigger_ids", "exclude_trigger_ids", "actions"):
            if isinstance(r[fld], str):
                try:
                    r[fld] = json.loads(r[fld])
                except (json.JSONDecodeError, TypeError):
                    r[fld] = []
    return {"status": "success", "triggers": rows}


@trigger_router.post("/api/game/trigger")
def create_trigger(req: TriggerCreateRequest):
    with safe_db() as conn:
        if req.target_node_id and req.mode != "passive" and not _has_gen_node_action(req.actions):
            if not conn.execute("SELECT id FROM nodes WHERE id=?", (req.target_node_id,)).fetchone():
                raise fastapi.HTTPException(status_code=400, detail="Target node does not exist")
        conditions_json = _normalize_conditions(req)
        c = conn.execute(
            "INSERT INTO triggers "
            "(label, target_node_id, mode, cond_type, cond_value, conditions, "
            " cooldown, prerequisite_trigger_ids, exclude_trigger_ids, max_fire_count, actions) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (req.label[:80], req.target_node_id or 0, req.mode,
             req.cond_type, req.cond_value[:200],
             conditions_json, req.cooldown,
             json.dumps(req.prerequisite_trigger_ids),
             json.dumps(req.exclude_trigger_ids),
             req.max_fire_count,
             json.dumps(req.actions, ensure_ascii=False))
        )
        conn.execute("UPDATE triggers SET timeline_id=? WHERE id=?", (req.timeline_id, c.lastrowid))
        conn.commit()
    return {"status": "success", "id": c.lastrowid}


@trigger_router.put("/api/game/trigger/{tid}")
def update_trigger(tid: int, req: TriggerUpdateRequest):
    with safe_db() as conn:
        if req.target_node_id and req.mode != "passive" and not _has_gen_node_action(req.actions):
            if not conn.execute("SELECT id FROM nodes WHERE id=?", (req.target_node_id,)).fetchone():
                raise fastapi.HTTPException(status_code=400, detail="Target node does not exist")
        conditions_json = _normalize_conditions(req)
        conn.execute("UPDATE triggers SET timeline_id=? WHERE id=?", (req.timeline_id, tid))
        conn.execute(
            "UPDATE triggers SET label=?, target_node_id=?, mode=?, "
            "cond_type=?, cond_value=?, conditions=?, "
            "cooldown=?, prerequisite_trigger_ids=?, exclude_trigger_ids=?, "
            "max_fire_count=?, actions=? "
            "WHERE id=?",
            (req.label[:80], req.target_node_id or 0, req.mode,
             req.cond_type, req.cond_value[:200],
             conditions_json, req.cooldown,
             json.dumps(req.prerequisite_trigger_ids),
             json.dumps(req.exclude_trigger_ids),
             req.max_fire_count,
             json.dumps(req.actions, ensure_ascii=False),
             tid)
        )
        conn.commit()
    return {"status": "success"}


@trigger_router.delete("/api/game/trigger/{tid}")
def delete_trigger(tid: int):
    with safe_db() as conn:
        conn.execute("DELETE FROM triggers WHERE id=?", (tid,))
        conn.commit()
    return {"status": "success"}


@trigger_router.post("/api/game/trigger/{tid}/reset")
def reset_trigger(tid: int):
    with safe_db() as conn:
        conn.execute(
            "UPDATE triggers SET fired=0, fire_count=0, last_fired_at=0 WHERE id=?",
            (tid,)
        )
        conn.commit()
    return {"status": "success"}


@trigger_router.post("/api/game/triggers/reset-all")
def reset_all_triggers():
    """Reset all triggers to their unfired state (debugging)."""
    with safe_db() as conn:
        conn.execute("UPDATE triggers SET fired=0, fire_count=0, last_fired_at=0")
        conn.commit()
    return {"status": "success"}


@trigger_router.get("/api/game/trigger-judgements")
def get_trigger_judgements(limit: int = 50):
    """Query recent AI decision records for GM audit."""
    with safe_db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM trigger_judgements ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()]
    return {"status": "success", "judgements": rows}


def evaluate_triggers(conn, scene_id, scene_name, scene_content, timeline_id=None, source_event_id=None):
    """Apply rules using the caller's transaction; never commit or close its connection."""
    from types import SimpleNamespace
    if not conn.atomic_depth:
        raise ValueError('Trigger evaluation requires a world transaction')
    req=SimpleNamespace(scene_id=scene_id,scene_name=scene_name,scene_content=scene_content)
    # Get triggers that are not permanently blocked:
    # - fired=0: never fired or available without cooldown
    # - cooldown>0 AND below max_fire limit: can fire repeatedly
    pending = conn.execute(
        "SELECT * FROM triggers WHERE fired=0 "
        "OR (cooldown>0 AND (max_fire_count=0 OR fire_count < max_fire_count)) ORDER BY id"
    ).fetchall()
    if not pending:
        return {"status": "success", "fired": []}

    members = party_members(conn, timeline_id)
    chars = [dict(r) for r in conn.execute("SELECT * FROM characters").fetchall()
             if members is None or r['id'] in members]
    all_inv = " ".join([(c.get("inventory") or "").lower() for c in chars])
    ai_cache: dict[str, bool] = {}

    fired_results    = []
    text_injections  = []
    option_injections = []
    generated_nodes  = []
    any_reveal_rag   = False

    for original in pending:
        # Earlier rules may change prerequisites, cooldowns, inventories and character state.
        t=conn.execute('SELECT * FROM triggers WHERE id=?',(original['id'],)).fetchone()
        if t['fired'] and (not t['cooldown'] or (t['max_fire_count'] and t['fire_count']>=t['max_fire_count'])):
            continue
        chars=[dict(r) for r in conn.execute('SELECT * FROM characters') if members is None or r['id'] in members]
        all_inv=' '.join((c.get('inventory') or '').lower() for c in chars)
        if t['timeline_id'] is not None and t['timeline_id'] != timeline_id:
            continue
        if source_event_id is not None and conn.execute('SELECT id FROM world_events WHERE operation_key=?',
                (f'trigger:{source_event_id}:{t["id"]}',)).fetchone():
            continue
        if not _check_trigger(t, req.scene_id, req.scene_name,
                               req.scene_content, chars, all_inv,
                               ai_cache, conn,timeline_id=timeline_id):
            continue

        # ── Execute trigger action side effects and collect frontend-visible changes ───────────────────
        side = _execute_actions(
            t["actions"] if "actions" in t.keys() else "[]",
            conn, t["id"], req.scene_id, req.scene_name, timeline_id=timeline_id
        )
        text_injections.extend(side["text_injections"])
        option_injections.extend(side["option_injections"])
        if side["reveal_rag_ids"]:
            any_reveal_rag = True

        # gen_node entries carry actions_log for the generated-node toast
        for gn in side["generated_nodes"]:
            gn["actions_log"] = side["log"]
        generated_nodes.extend(side["generated_nodes"])

        record_event(conn,"trigger_fired",t["label"],{"trigger_id":t["id"],"actions":t["actions"],"source_event_id":source_event_id},
                     timeline_id, f'trigger:{source_event_id}:{t["id"]}' if source_event_id is not None else None)

        # ── Update trigger state ─────────────────────────────────────────────────
        now_ts    = int(time.time())
        cooldown  = t["cooldown"]       if "cooldown"       in t.keys() else 0
        max_fire  = t["max_fire_count"] if "max_fire_count" in t.keys() else 0
        new_count = (t["fire_count"]    if "fire_count"     in t.keys() else 0) + 1

        if cooldown == 0:
            new_fired = 1                                  # No cooldown: one activation, permanently blocked
        elif max_fire > 0 and new_count >= max_fire:
            new_fired = 1                                  # Activation limit reached; permanently blocked
        else:
            new_fired = t["fired"]                         # Cooldown present and limit not reached; retain state

        conn.execute(
            "UPDATE triggers SET fired=?, fire_count=?, last_fired_at=? WHERE id=?",
            (new_fired, new_count, now_ts, t["id"])
        )

        # ── Build result entries ─────────────────────────────────────────────────
        # gen_node triggers use the generated_nodes navigation channel to avoid duplicate dialogs
        # With target_node_id=0, hard mode in fired_results would incorrectly navigate to node 0
        if not side["generated_nodes"]:
            target_node = conn.execute(
                "SELECT * FROM nodes WHERE id=?", (t["target_node_id"],)
            ).fetchone() if t["target_node_id"] else None

            fired_results.append({
                "trigger_id":       t["id"],
                "label":            t["label"],
                "mode":             t["mode"],
                "target_node_id":   t["target_node_id"],
                "target_node_name": target_node["name"] if target_node else "(no navigation target)",
                "fire_count":       new_count,
                "actions_log":      side["log"],
            })

        # passive mode has no navigation intent and writes no memory; soft/hard write a concise natural-language scene hint
        # Navigation occurs in the current backend transaction, and memory belongs to the same commit.
        if t["mode"] != "passive" and _fn_append_to_memory:
            if side["generated_nodes"]:
                gn = side["generated_nodes"][0]
                append_rule_memory(
                    conn, timeline_id,
                    f"Triggered [{t['label']}], entered [{gn['node_name']}]."  #trigger + trigger name
                )
            elif t["target_node_id"]:
                target_node = conn.execute(
                    "SELECT name FROM nodes WHERE id=?", (t["target_node_id"],)
                ).fetchone()
                scene_label = target_node["name"] if target_node else f"Scene {t['target_node_id']}"
                append_rule_memory(
                    conn, timeline_id,
                    f"Triggered [{t['label']}], scene switched to [{scene_label}]."
                )

    hard_targets={item['target_node_id'] for item in fired_results if item['mode']=='hard' and item['target_node_id']}
    hard_targets.update(item['node_id'] for item in generated_nodes if item['mode']=='hard')
    if len(hard_targets)>1: raise ValueError('Conflicting forced destinations; scene commit was rolled back')
    navigation=next(iter(hard_targets),None)
    if navigation:
        destination=conn.execute('SELECT is_draft FROM nodes WHERE id=?',(navigation,)).fetchone()
        if not destination or destination['is_draft']: raise ValueError('Invalid forced destination')
        if timeline_id is None:
            conn.execute("INSERT OR REPLACE INTO system_state VALUES('player_current_scene_id',?)",(str(navigation),))
        else:
            conn.execute('UPDATE timelines SET current_node_id=? WHERE id=?',(navigation,timeline_id))
        from spatial import scene_room,set_position
        room=scene_room(conn,navigation)
        if room: set_position(conn,room['id'],timeline_id)
        record_event(conn,'trigger_navigation','Forced scene transition',{'node_id':navigation,'source_event_id':source_event_id},timeline_id)
    return {"status":"success", "fired":fired_results, "text_injections":text_injections,
            "option_injections":option_injections, "generated_nodes":generated_nodes,'navigation':navigation}


@trigger_router.post("/api/game/check-triggers")
def check_triggers(req: CheckTriggersRequest):
    # Compatibility/manual GM check. Scene commits call evaluate_triggers directly.
    with safe_db() as conn, atomic(conn):
        node=conn.execute('SELECT * FROM nodes WHERE id=?',(req.scene_id,)).fetchone()
        if not node or node['is_draft']:
            raise HTTPException(409,'Only confirmed scenes can run world rules')
        result=evaluate_triggers(conn,req.scene_id,node['name'],node['content'],req.timeline_id)
        refresh_wiki(conn)
    return result
