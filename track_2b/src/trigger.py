from providers import evaluate
from world import entity_state_context, resolve_entity, record_event, refresh_wiki, link_character, encode, apply_state_changes
from database import connect_db, atomic
from providers import generation, parse_object
"""
APERTUS-MUNDUS 引擎 — 触发器系统模块 (trigger.py)

支持功能：
- DAG 条件树：{"op": "and|or|not", "children": [...]}，旧 AND 列表自动迁移
- fire_count / cooldown / prerequisite_trigger_ids / exclude_trigger_ids
- trigger_judgements 表记录 AI 推理过程（含 prompt_hash 去重缓存）
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

trigger_router = APIRouter(tags=["触发器系统"])

# ---------------------------------------------------------
# 依赖注入
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
# Schema 迁移（幂等，启动时自动执行）
# ---------------------------------------------------------
def _ensure_schema():
    with safe_db() as conn:
        # triggers 表新增字段
        for col, typedef in [
            ("fire_count",               "INTEGER NOT NULL DEFAULT 0"),
            ("cooldown",                 "INTEGER NOT NULL DEFAULT 0"),
            ("last_fired_at",            "INTEGER NOT NULL DEFAULT 0"),  # Unix 时间戳，避免时区歧义
            ("prerequisite_trigger_ids", "TEXT NOT NULL DEFAULT '[]'"),
            ("exclude_trigger_ids",      "TEXT NOT NULL DEFAULT '[]'"),
            ("max_fire_count",           "INTEGER NOT NULL DEFAULT 0"),
            ("actions",                  "TEXT NOT NULL DEFAULT '[]'"),
        ]:
            try:
                conn.execute(f"ALTER TABLE triggers ADD COLUMN {col} {typedef}")
            except sqlite3.OperationalError:
                pass  # 列已存在

        if 'timeline_id' not in {r[1] for r in conn.execute('PRAGMA table_info(triggers)')}:
            conn.execute('ALTER TABLE triggers ADD COLUMN timeline_id INTEGER')

        # trigger_judgements 表（AI 判定审计记录，不作缓存读路径）
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trigger_judgements (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                trigger_id    INTEGER,
                scene_id      INTEGER,
                scene_name    TEXT,
                timestamp     TEXT,
                reasoning     TEXT,
                result        INTEGER,
                condition_hash TEXT  -- 仅供去重聚合查询，不参与判定逻辑
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_judge_trigger "
            "ON trigger_judgements(trigger_id)"
        )
        try:
            conn.execute("ALTER TABLE trigger_judgements ADD COLUMN condition_hash TEXT")
        except sqlite3.OperationalError:
            pass  # 列已存在

        # game_flags 表：供 set_flag 动作读写命名状态位
        conn.execute("""
            CREATE TABLE IF NOT EXISTS game_flags (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL DEFAULT ''
            )
        """)
        conn.commit()


# ---------------------------------------------------------
# Pydantic 数据模型
# ---------------------------------------------------------
class ConditionItem(BaseModel):
    type: str
    value: str = ""

class TriggerCreateRequest(BaseModel):
    label: str = "未命名触发器"
    target_node_id: int = 0             # passive 模式可为 0（无跳转目标）
    mode: str = "soft"                  # soft | hard | passive
    conditions: list | dict = []        # 接受列表（旧）或树（新）
    cond_type: str = ""
    cond_value: str = ""
    cooldown: int = 0
    prerequisite_trigger_ids: list[int] = []
    exclude_trigger_ids: list[int] = []
    max_fire_count: int = 0             # 0=无限；>0=触发N次后永久封锁
    timeline_id: int | None = None
    actions: list = []                  # 触发时执行的副作用动作列表

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
# 条件树：解析 & 规范化
# ---------------------------------------------------------
def _parse_condition_tree(raw: str | None) -> dict:
    """
    将数据库 conditions 字段解析为标准树节点。
    旧格式 [...] 自动升级为 {"op": "and", "children": [...]}。
    """
    if not raw:
        return {"op": "and", "children": []}
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {"op": "and", "children": []}

    if isinstance(data, list):
        # 旧 AND 列表
        leaves = [{"type": c.get("type", ""), "value": c.get("value", "")}
                  for c in data if c.get("type")]
        return {"op": "and", "children": leaves}

    if isinstance(data, dict) and "op" in data:
        return data

    return {"op": "and", "children": []}


def _normalize_conditions(req) -> str:
    """将请求中的 conditions 字段统一序列化为 JSON 树。"""
    conds = getattr(req, "conditions", [])
    if isinstance(conds, dict) and "op" in conds:
        return json.dumps(conds, ensure_ascii=False)
    if isinstance(conds, list) and conds:
        leaves = [{"type": c.type if hasattr(c, "type") else c.get("type",""),
                   "value": c.value if hasattr(c, "value") else c.get("value","")}
                  for c in conds]
        return json.dumps({"op": "and", "children": leaves}, ensure_ascii=False)
    # 旧单字段回退
    if getattr(req, "cond_type", ""):
        return json.dumps({"op": "and", "children": [
            {"type": req.cond_type, "value": req.cond_value}
        ]}, ensure_ascii=False)
    return json.dumps({"op": "and", "children": []}, ensure_ascii=False)


def _collect_ai_leaves(node: dict) -> list[str]:
    """递归收集树中所有 type=='ai' 的叶节点 value。"""
    if "op" in node:
        result = []
        for child in node.get("children", []):
            result.extend(_collect_ai_leaves(child))
        return result
    if node.get("type") == "ai":
        return [node.get("value", "").strip()]
    return []


# ---------------------------------------------------------
# 单叶节点本地判定
# ---------------------------------------------------------
def _judge_leaf(node: dict, scene_id: int, chars: list, all_inv: str) -> bool | None:
    """返回 True/False 或 None（需 AI 判断）。"""
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
        return None  # 交给 AI 批判

    return False


# ---------------------------------------------------------
# 递归树求值
# ---------------------------------------------------------
def _eval_tree(node,scene_id,chars,all_inv,ai_cache,_depth=0,conn=None):
    return _eval_tree_3val(node,scene_id,chars,all_inv,ai_cache,_depth,conn) is True



# ---------------------------------------------------------
# 三值预求值（True / False / _MAYBE）— AI 剪枝
# ---------------------------------------------------------
_MAYBE = object()  # 哨兵：AI 条件结果未知，需调用后确定


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
# AI 批量判断（judgements 表仅作审计，不作缓存读路径）
# ---------------------------------------------------------
def _batch_judge_ai(values,scene_name,scene_content,conn,trigger_id=None,scene_id=0,timeline_id=None):
    scope={'timeline_id':timeline_id} if timeline_id is not None else {}
    context=_fn_get_system_context(conn,scene_name,scene_content,**scope) if _fn_get_system_context else ()
    questions={str(i):{"type":"noul","instructions":"仅根据已确认事实判断以下条件是否成立："+value} for i,value in enumerate(values)}
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
# AI 即时生成节点内容辅助
# ---------------------------------------------------------
def _generate_node_ai(prompt: str, scene_name: str, conn) -> dict | None:
    """使用 AI 即兴生成一个剧情节点，返回 {name, content} 或 None（失败时）。"""
    if not _generation_client or not _fn_get_system_context:
        return None
    try:
        worldview, party_status, _, session_memory, \
            _, world_entities_text, _, _ = \
            _fn_get_system_context(conn, scene_name, prompt)

        system_prompt = (
            "你是一个专业的 TRPG 叙事生成器。根据提示即兴创作一个新剧情节点。\n"
            "语言沉浸、简洁有力，禁止出戏或解释性旁白。\n\n"
            "--- 世界观 ---\n"
            f"{worldview}\n\n"
            "--- 当前世界实体状态 ---\n"
            f"{world_entities_text}\n\n"
            "返回严格 JSON（仅此两字段）：\n"
            '{"name": "节点标题（10字以内）", "content": "场景骨架（60-100字）"}'
        )
        user_prompt = (
            "【近期记忆】\n"
            f"{session_memory}\n\n"
            "【队伍状态】\n"
            f"{party_status}\n\n"
            "【当前场景】\n"
            f"{scene_name}\n\n"
            "【节点生成提示】\n"
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
            _log.warning("gen_node AI 截断")
            return None
        parsed = json.loads(resp.choices[0].message.content)
        name    = str(parsed.get("name", "触发生成节点")).strip()[:80]
        content = str(parsed.get("content", "")).strip()
        return {"name": name, "content": content} if content else None
    except Exception as e:
        _log.warning("gen_node AI 调用失败: %s", e)
        return None


# ---------------------------------------------------------
# 触发器动作执行（触发时副作用）
# 返回 side_effects dict 供 check_triggers 聚合后下发给前端
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
    动作类型一览：
      set_flag      — {key, value}：写入 game_flags 表
      mod_stat      — {target, attr, delta}：改角色 hp/san，target="PC" 作用全体玩家角色
      add_memory    — {text}：写入会话记忆
      fire_trigger  — {id}：强制激活另一触发器（不递归执行其动作，防止循环）
      inject_text   — {node_id?, text}：向指定节点追加叙事文字（node_id=0 → 当前场景）
      inject_option — {node_id?, text, next_node_id}：向指定节点注入选项
      gen_node      — {prompt, mode}：AI 即时生成新节点并返回供前端跳转
      mod_inventory — {target, op, item}：op="add"|"remove"，修改角色 inventory 纯文本字段
      mod_npc       — {name, field, value?, delta?}：改实体 status / emotion_* / memory_add
      entity_script — {name, action}：appear | leave | die | attack
    """
    members = party_members(conn, timeline_id)
    side_effects: dict = {
        "text_injections":  [],   # [{node_id, text}]
        "option_injections": [],  # [{node_id, option_id, text, next_node_id}]
        "generated_nodes":  [],   # [{trigger_id, node_id, node_name, node_content, mode}]
        "log":              [],   # [str] 人类可读的动作执行摘要，下发给前端显示
        "reveal_rag_ids":   [],   # [int] reveal_rag 解锁的 doc_id，commit 后刷新向量缓存
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
            # ── 原有四种 ──────────────────────────────────────────────────────
            if atype == "set_flag":
                key = str(act.get("key", "")).strip()
                val = str(act.get("value", "")).strip()
                if key:
                    conn.execute(
                        "INSERT OR REPLACE INTO game_flags (key, value) VALUES (?, ?)",
                        (key, val)
                    )
                    _log.info("触发器 %d set_flag: %s = %s", trigger_id, key, val)
                    side_effects["log"].append(f"🚩 标志位 {key} = {val}")

            elif atype == "mod_stat":
                target = str(act.get("target", "PC")).strip()
                attr   = str(act.get("attr",   "hp")).strip().lower()
                delta  = int(act.get("delta",  0))
                if attr in ("hp", "san") and delta != 0:
                    if target.upper() == "PC":
                        targets=[r['id'] for r in conn.execute("SELECT id FROM characters WHERE role='PC'")
                                 if members is None or r['id'] in members]
                        for char_id in targets:
                            conn.execute(f"UPDATE characters SET {attr}=MAX(0,{attr}+?) WHERE id=?",(delta,char_id))
                    else:
                        conn.execute(
                            f"UPDATE characters SET {attr}=MAX(0,{attr}+?) WHERE entity_uid=?",
                            (delta, target)
                        )
                    _log.info("触发器 %d mod_stat: [%s] %s %+d", trigger_id, target, attr, delta)
                    label = "全体玩家" if target.upper() == "PC" else target
                    side_effects["log"].append(f"💢 {label} {attr.upper()} {delta:+d}")

            elif atype == "add_memory":
                text = str(act.get("text", "")).strip()
                if text and _fn_append_to_memory:
                    append_rule_memory(conn, timeline_id, text)
                    _log.info("触发器 %d add_memory: %s…", trigger_id, text[:40])
                    side_effects["log"].append(f"📝 记忆已写入：{text[:30]}…" if len(text) > 30 else f"📝 记忆已写入：{text}")

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
                        _log.info("触发器 %d fire_trigger: 激活 #%d（第%d次）", trigger_id, tid, new_cnt)
                        side_effects["log"].append(f"⚡ 触发器 #{tid} 已激活（第{new_cnt}次）")

            # ── 叙事层 ────────────────────────────────────────────────────────
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
                            # 同步追加到战报史册最近一条记录的深入调查快照
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
                            # 同步追加到战报史册最近一条记录的场景快照
                            conn.execute(
                                "UPDATE chronicle_log SET scene_content=scene_content||? "
                                "WHERE id=(SELECT id FROM chronicle_log WHERE scene_id=? "
                                "ORDER BY id DESC LIMIT 1)",
                                ("\n\n" + text, target_node)
                            )
                        side_effects["text_injections"].append(
                            {"node_id": target_node, "text": text}
                        )
                        _log.info("触发器 %d inject_text → 节点%d: %s…", trigger_id, target_node, text[:30])
                        side_effects["log"].append(f"📖 文本已注入当前场景")

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
                    _log.info("触发器 %d inject_option → 节点%d: [%s]→%d",
                              trigger_id, target_node, opt_text, next_nid)
                    side_effects["log"].append(f"🔀 新选项已注入：「{opt_text[:20]}」→ 节点#{next_nid}")

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
                        _log.info("触发器 %d gen_node → 新节点%d「%s」", trigger_id, new_nid, result["name"])
                        side_effects["log"].append(f"✨ AI 已生成节点「{result['name']}」(#{new_nid})")

            # ── 实体层 ────────────────────────────────────────────────────────
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
                    _log.info("触发器 %d mod_inventory: [%s] %s %s", trigger_id, target, op, item)
                    label = "全体玩家" if target.upper() == "PC" else target
                    op_str = "获得" if op == "add" else "失去"
                    side_effects["log"].append(f"🎒 {label} {op_str}「{item}」")

            elif atype == "mod_npc":
                name  = str(act.get("name", "")).strip()
                field = str(act.get("field", "status")).strip()
                if not name:
                    continue
                row = conn.execute(
                    "SELECT id, state_desc FROM world_entities WHERE uid=?", (name,)
                ).fetchone()
                if not row:
                    _log.warning("触发器 %d mod_npc: 找不到实体「%s」", trigger_id, name)
                    side_effects["log"].append(f"👤 ⚠ 找不到实体「{name}」")
                    continue
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

                if field == "status":
                    val = str(act.get("value", "active")).strip()
                    conn.execute(
                        "UPDATE world_entities SET status=?, updated_at=? WHERE uid=?",
                        (val, ts, name)
                    )
                    _log.info("触发器 %d mod_npc: %s.status → %s", trigger_id, name, val)
                    side_effects["log"].append(f"👤 {name} 状态 → {val}")

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
                    _log.info("触发器 %d mod_npc: %s.%s %+d", trigger_id, name, emo_key, delta)
                    emo_names = {"trust": "信任", "fear": "恐惧", "irritation": "愤怒"}
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
                        _log.info("触发器 %d mod_npc memory_add: %s ← %s…", trigger_id, name, mem_text[:30])
                        side_effects["log"].append(f"👤 {name} 记忆已写入")

            elif atype == "entity_script":
                name   = str(act.get("name",   "")).strip()
                action = str(act.get("action", "appear")).strip()
                if not name:
                    continue
                row = conn.execute(
                    "SELECT id, state_desc FROM world_entities WHERE uid=?", (name,)
                ).fetchone()
                if not row:
                    _log.warning("触发器 %d entity_script: 找不到实体「%s」", trigger_id, name)
                    side_effects["log"].append(f"⚙️ ⚠ 找不到实体「{name}」")
                    continue
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

                if action in ("appear", "leave", "die"):
                    status_map = {"appear": "active", "leave": "moved", "die": "dead"}
                    conn.execute(
                        "UPDATE world_entities SET status=?, updated_at=? WHERE uid=?",
                        (status_map[action], ts, name)
                    )
                    _log.info("触发器 %d entity_script: %s → %s", trigger_id, name, action)
                    action_names = {"appear": "登场", "leave": "离场", "die": "死亡"}
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
                    mem.append(f"[触发器] 在场景「{scene_name}」对玩家发动攻击")
                    sd["emotion"] = emo
                    sd["memory"]  = mem[-20:]
                    conn.execute(
                        "UPDATE world_entities SET state_desc=?, updated_at=? WHERE uid=?",
                        (json.dumps(sd, ensure_ascii=False), ts, name)
                    )
                    _log.info("触发器 %d entity_script: %s 发动攻击", trigger_id, name)
                    side_effects["log"].append(f"⚙️ {name} 发动攻击（愤怒+30，信任-20）")

            # ── 世界构建层 ───────────────────────────────────────────────────
            elif atype == "set_state":
                for change in act.get('changes',[]):
                    character=conn.execute('SELECT id,role FROM characters WHERE entity_uid=?',(change.get('entity_uid'),)).fetchone()
                    if members is not None and character and character['role']=='PC' and character['id'] not in members:
                        raise ValueError('A squad rule cannot change another squad member')
                event=record_event(conn,'trigger_state','触发状态变更',act,timeline_id)
                apply_state_changes(conn,act.get('definitions',[]),act.get('changes',[]),event)
            elif atype == "spawn_npc":
                name=str(act.get('name','')).strip()
                if not name: raise ValueError('NPC名称不能为空')
                char=conn.execute("INSERT INTO characters(name,role,hp,san,inventory,personality) VALUES(?,'NPC',?,?,?,?)",
                    (name,int(act.get('hp',50)),int(act.get('san',60)),str(act.get('inventory','')),str(act.get('desc','')))).lastrowid
                uid=link_character(conn,char,new=True)
                side_effects['log'].append(f'NPC「{name}」登场')
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
                _log.info("触发器 %d mod_worldview[%s]: %s…", trigger_id, op, text[:40])
                op_str = "已替换" if op == "replace" else "已追加"
                side_effects["log"].append(f"🌍 世界观{op_str}（{len(text)} 字）")

            elif atype == "add_lore":
                keywords = str(act.get("keywords", "")).strip()
                content  = str(act.get("content",  "")).strip()
                if keywords and content:
                    conn.execute(
                        "INSERT INTO lorebook (keywords, content) VALUES (?,?)",
                        (keywords, content)
                    )
                    _log.info("触发器 %d add_lore: [%s] %s…", trigger_id, keywords, content[:30])
                    side_effects["log"].append(f"📚 Lorebook 新增：[{keywords[:20]}]")

            elif atype == "reveal_rag":
                doc_id = int(act.get("doc_id", 0))
                if doc_id:
                    row = conn.execute("SELECT title FROM rag_documents WHERE id=?", (doc_id,)).fetchone()
                    conn.execute(
                        "UPDATE rag_documents SET hidden=0 WHERE id=?",
                        (doc_id,)
                    )
                    side_effects["reveal_rag_ids"].append(doc_id)
                    _log.info("触发器 %d reveal_rag: doc_id=%d 已解锁", trigger_id, doc_id)
                    title = row["title"] if row else f"#{doc_id}"
                    side_effects["log"].append(f"🔓 RAG 文档「{title}」已解锁并载入向量库")

        except Exception as e:
            raise ValueError(f"触发器 {trigger_id} 动作 {atype} 失败：{e}") from e

    return side_effects


def _has_gen_node_action(actions) -> bool:
    """动作列表中含有 gen_node 时，跳转目标由 AI 运行时生成，无需固定 target_node_id。"""
    if isinstance(actions, str):
        try:
            actions = json.loads(actions)
        except (json.JSONDecodeError, TypeError):
            return False
    return any(isinstance(a, dict) and a.get("type") == "gen_node" for a in (actions or []))


# ---------------------------------------------------------
# 触发器完整检查（含 DAG、前驱、互斥、冷却、次数上限）
# ---------------------------------------------------------
def _check_trigger(trigger_row, scene_id: int, scene_name: str,
                   scene_content: str, chars: list, all_inv: str,
                   ai_cache: dict, conn, timeline_id=None) -> bool:
    t = trigger_row

    # ── 前驱：所有前驱触发器必须已 fire ────────────────────────────────────
    try:
        prereq_ids = json.loads(t["prerequisite_trigger_ids"] or "[]")
    except (TypeError, json.JSONDecodeError):
        prereq_ids = []
    if prereq_ids:
        for pid in prereq_ids:
            row = conn.execute("SELECT fire_count, fired FROM triggers WHERE id=?", (pid,)).fetchone()
            if not row or (row["fire_count"] == 0 and row["fired"] == 0):
                return False

    # ── 互斥：所有排斥触发器必须未 fire ────────────────────────────────────
    try:
        excl_ids = json.loads(t["exclude_trigger_ids"] or "[]")
    except (TypeError, json.JSONDecodeError):
        excl_ids = []
    if excl_ids:
        for eid in excl_ids:
            row = conn.execute("SELECT fire_count, fired FROM triggers WHERE id=?", (eid,)).fetchone()
            if row and (row["fire_count"] > 0 or row["fired"] == 1):
                return False

    # ── 冷却检查 ────────────────────────────────────────────────────────────
    cooldown = t["cooldown"] if "cooldown" in t.keys() else 0
    if cooldown and cooldown > 0:
        last_ts = t["last_fired_at"] if "last_fired_at" in t.keys() else 0
        if last_ts and (time.time() - int(last_ts)) < cooldown:
            return False

    # ── 触发次数上限 ─────────────────────────────────────────────────────────
    max_fire = t["max_fire_count"] if "max_fire_count" in t.keys() else 0
    if max_fire > 0:
        cur_count = t["fire_count"] if "fire_count" in t.keys() else 0
        if cur_count >= max_fire:
            return False

    # ── 条件树求值 ──────────────────────────────────────────────────────────
    tree = _parse_condition_tree(t["conditions"] if "conditions" in t.keys() else "[]")
    if not tree.get("children"):
        return False

    # ── 三值预求值：非 AI 条件短路剪枝，结果确定时直接返回，跳过 AI 调用 ──
    pre = _eval_tree_3val(tree, scene_id, chars, all_inv, ai_cache, conn=conn)
    if pre is False:
        return False
    if pre is True:
        return True

    # 只有 _MAYBE（非 AI 条件无法单独确定结果）才收集 AI 条件并批量请求
    ai_leaves = _collect_ai_leaves(tree)
    new_ai = [v for v in ai_leaves if v and v not in ai_cache]
    if new_ai:
        unique = list(dict.fromkeys(new_ai))
        batch = _batch_judge_ai(unique, scene_name, scene_content, conn,
                                trigger_id=t["id"], scene_id=scene_id,timeline_id=timeline_id)
        ai_cache.update(batch)

    return _eval_tree(tree, scene_id, chars, all_inv, ai_cache, conn=conn)


# ---------------------------------------------------------
# REST API 端点
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
                raise fastapi.HTTPException(status_code=400, detail="目标节点不存在")
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
                raise fastapi.HTTPException(status_code=400, detail="目标节点不存在")
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
    """将所有触发器重置为未触发状态（调试用）。"""
    with safe_db() as conn:
        conn.execute("UPDATE triggers SET fired=0, fire_count=0, last_fired_at=0")
        conn.commit()
    return {"status": "success"}


@trigger_router.get("/api/game/trigger-judgements")
def get_trigger_judgements(limit: int = 50):
    """查询最近的 AI 判定记录，供 GM 追溯。"""
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
    # 取未永久封锁的触发器：
    # - fired=0：从未触发或无冷却可用
    # - cooldown>0 AND 未达 max_fire 上限：可重复触发
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

        # ── 执行触发器动作副作用，收集前端需感知的副作用 ───────────────────
        side = _execute_actions(
            t["actions"] if "actions" in t.keys() else "[]",
            conn, t["id"], req.scene_id, req.scene_name, timeline_id=timeline_id
        )
        text_injections.extend(side["text_injections"])
        option_injections.extend(side["option_injections"])
        if side["reveal_rag_ids"]:
            any_reveal_rag = True

        # gen_node 条目携带 actions_log，供前端在生成节点 toast 中展示
        for gn in side["generated_nodes"]:
            gn["actions_log"] = side["log"]
        generated_nodes.extend(side["generated_nodes"])

        record_event(conn,"trigger_fired",t["label"],{"trigger_id":t["id"],"actions":t["actions"],"source_event_id":source_event_id},
                     timeline_id, f'trigger:{source_event_id}:{t["id"]}' if source_event_id is not None else None)

        # ── 更新触发状态 ─────────────────────────────────────────────────
        now_ts    = int(time.time())
        cooldown  = t["cooldown"]       if "cooldown"       in t.keys() else 0
        max_fire  = t["max_fire_count"] if "max_fire_count" in t.keys() else 0
        new_count = (t["fire_count"]    if "fire_count"     in t.keys() else 0) + 1

        if cooldown == 0:
            new_fired = 1                                  # 无冷却：单次触发，永久封锁
        elif max_fire > 0 and new_count >= max_fire:
            new_fired = 1                                  # 达到次数上限，永久封锁
        else:
            new_fired = t["fired"]                         # 有冷却且未达上限，保持状态

        conn.execute(
            "UPDATE triggers SET fired=?, fire_count=?, last_fired_at=? WHERE id=?",
            (new_fired, new_count, now_ts, t["id"])
        )

        # ── 构建结果条目 ─────────────────────────────────────────────────
        # gen_node 触发器：跳转通知走 generated_nodes 通道，避免双重弹窗
        # 且 target_node_id=0，fired_results 里的 hard 模式会错误跳转到节点0
        if not side["generated_nodes"]:
            target_node = conn.execute(
                "SELECT * FROM nodes WHERE id=?", (t["target_node_id"],)
            ).fetchone() if t["target_node_id"] else None

            fired_results.append({
                "trigger_id":       t["id"],
                "label":            t["label"],
                "mode":             t["mode"],
                "target_node_id":   t["target_node_id"],
                "target_node_name": target_node["name"] if target_node else "（无跳转目标）",
                "fire_count":       new_count,
                "actions_log":      side["log"],
            })

        # passive 模式无导航意图，不写记忆；soft/hard 模式写简洁的自然语言场景提示
        # 跳转由当前后端事务完成，记忆同样属于当前提交。
        if t["mode"] != "passive" and _fn_append_to_memory:
            if side["generated_nodes"]:
                gn = side["generated_nodes"][0]
                append_rule_memory(
                    conn, timeline_id,
                    f"触发「{t['label']}」，进入「{gn['node_name']}」。"  #触发+触发器名称
                )
            elif t["target_node_id"]:
                target_node = conn.execute(
                    "SELECT name FROM nodes WHERE id=?", (t["target_node_id"],)
                ).fetchone()
                scene_label = target_node["name"] if target_node else f"场景{t['target_node_id']}"
                append_rule_memory(
                    conn, timeline_id,
                    f"触发「{t['label']}」，场景切换至「{scene_label}」。"
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
