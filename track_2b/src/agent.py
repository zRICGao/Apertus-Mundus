from engine import validate_branches
from model_output import NARRATIVE_RULE, clean_branch_prose, prose_renderer
from world import WORLD_PROMPT, known_definitions, state_token, wiki_context, refresh_wiki, resolve_entity, encode
from providers import route_action
from database import connect_db, atomic
from providers import generation, parse_object
"""
APERTUS-MUNDUS 引擎 — AI Agent 模块 (agent.py)
包含：Apertus 生成、可插拔决策、system prompt 构建、
SSE 流式推演端点、结果后处理（NPC/stat/map/entity）。
由 main.py 通过 app.include_router(agent_router) 挂载。
"""

import json
import re
import sqlite3
from datetime import datetime
from contextlib import contextmanager
from fastapi import APIRouter, BackgroundTasks, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from typing import Optional
from logger import get_logger
from entity import apply_emotion_delta, tick_emotion_decay

_log = get_logger("agent")

agent_router = APIRouter(tags=["AI Agent"])

##########################################################
# 将此请求模型添加到 agent.py 顶部
class NPCChatRequest(BaseModel):
    npc_name: str = ""
    entity_uid: str | None = None
    timeline_id: int | None = None
    player_message: str
    chat_history: str = "" # 前端传来的近期聊天记录
###########################################################

# ---------------------------------------------------------
# state_desc JSON 解析器（兼容旧版纯文本）
# ---------------------------------------------------------
def _parse_state_desc(raw: str) -> dict:
    """
    解析 world_entities.state_desc 字段。
    新版：JSON 格式 {"desc":"...", "emotion":{...}, "breakpoint":{...}, "memory":[...]}
    旧版：纯文本 → 自动升级为 {"desc": "原文本", "emotion": {...}, "memory": []}
    """
    if not raw:
        return {"desc": "", "emotion": {"trust": 0, "fear": 0, "irritation": 0}, "memory": []}
    raw = raw.strip()
    if raw.startswith("{"):
        try:
            sd = json.loads(raw)
            sd.setdefault("desc", "")
            sd.setdefault("emotion", {"trust": 0, "fear": 0, "irritation": 0})
            sd.setdefault("memory", [])
            return sd
        except json.JSONDecodeError:
            pass
    # 旧版纯文本兼容
    return {"desc": raw, "emotion": {"trust": 0, "fear": 0, "irritation": 0}, "memory": []}


def _serialize_state_desc(sd: dict) -> str:
    """将 state_desc dict 序列化为 JSON 字符串。"""
    return json.dumps(sd, ensure_ascii=False)


def _format_emotion_label(value: int) -> str:
    """将情绪数值转为人类可读标签。"""
    if value >= 60: return "极高"
    if value >= 30: return "高"
    if value >= 10: return "中"
    if value >= -10: return "平"
    if value >= -30: return "低"
    return "极低"

# ---------------------------------------------------------
# 依赖注入（由 main.py 启动时设置）
# ---------------------------------------------------------
_db_file: str = ""
_generation_client = None   # OpenAI-compatible (Apertus)
_persona_config: dict = {}

# 外部函数引用（由 main.py 注入，避免循环导入）
_get_map_context = None
_auto_place_room = None
_process_map_actions = None
_get_current_room_id = None
_ai_extract_and_upsert_entities = None
_build_persona_instruction = None
_l1_append = None
_l1_get_working_context = None
_append_to_memory = None
_tl_append_memory = None
_get_world_entities_text = None
_rag_retrieve = None
_get_embeddings = None          # rag.get_embeddings（NPC 记忆→L3 向量化用）
_refresh_vector_cache = None    # rag.refresh_vector_cache

# 当前激活的模型（服务器级默认值，可被请求级参数覆盖）
AVAILABLE_MODELS = {"apertus-70b": {"label": "Apertus 70B", "model_id": generation.settings.model}}
_active_model = "apertus-70b"

def _resolve_model(request_model=None):
    if request_model and request_model != "apertus-70b":
        raise ValueError("生成模型只支持 apertus-70b；小模型在决策层配置")
    return "apertus-70b"


def configure_agent(
    db_file: str,
    generation_client,
    persona_config: dict = None,
    # 外部函数注入
    fn_get_map_context=None,
    fn_auto_place_room=None,
    fn_process_map_actions=None,
    fn_get_current_room_id=None,
    fn_ai_extract_and_upsert_entities=None,
    fn_build_persona_instruction=None,
    fn_l1_append=None,
    fn_l1_get_working_context=None,
    fn_append_to_memory=None,
    fn_tl_append_memory=None,
    fn_get_world_entities_text=None,
    fn_rag_retrieve=None,
    fn_get_embeddings=None,
    fn_refresh_vector_cache=None,
):
    """由 main.py 启动时调用，注入所有依赖。"""
    global _db_file, _generation_client, _persona_config
    global _get_map_context, _auto_place_room, _process_map_actions
    global _get_current_room_id, _ai_extract_and_upsert_entities
    global _build_persona_instruction, _l1_append, _l1_get_working_context
    global _append_to_memory, _tl_append_memory, _get_world_entities_text, _rag_retrieve
    global _get_embeddings, _refresh_vector_cache

    _db_file = db_file
    _generation_client = generation_client
    _persona_config = persona_config or {}

    # 注入外部函数
    _get_map_context = fn_get_map_context
    _auto_place_room = fn_auto_place_room
    _process_map_actions = fn_process_map_actions
    _get_current_room_id = fn_get_current_room_id
    _ai_extract_and_upsert_entities = fn_ai_extract_and_upsert_entities
    _build_persona_instruction = fn_build_persona_instruction
    _l1_append = fn_l1_append
    _l1_get_working_context = fn_l1_get_working_context
    _append_to_memory = fn_append_to_memory
    _tl_append_memory = fn_tl_append_memory
    _get_world_entities_text = fn_get_world_entities_text
    _rag_retrieve = fn_rag_retrieve
    _get_embeddings = fn_get_embeddings
    _refresh_vector_cache = fn_refresh_vector_cache


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
# 模型切换 API
# ---------------------------------------------------------
class ModelSwitchRequest(BaseModel):
    model: str  # apertus-70b

@agent_router.get("/api/ai/models")
def list_models():
    return {"status": "success", "models": [{"key": "apertus-70b", "label": "Apertus 70B",
            "model_id": generation.settings.model, "available": True}], "active": "apertus-70b",
            "decision_provider": generation.settings.decision_provider}


@agent_router.post("/api/ai/models/switch")
def switch_model(req: ModelSwitchRequest):
    _resolve_model(req.model)
    return {"status": "success", "active": "apertus-70b", "label": "Apertus 70B"}


# ---------------------------------------------------------
# 统一 AI 调用接口（Apertus 70B）
# ---------------------------------------------------------
def _call_ai(system_prompt, user_prompt, temperature=0.8, max_tokens=2000, json_mode=True, model_override=None, response_format=None):
    _resolve_model(model_override)
    return generation.generate(system_prompt, user_prompt, temperature=temperature,
                               max_tokens=max_tokens, json_mode=json_mode, response_format=response_format,
                               decision=response_format is not None)


# ---------------------------------------------------------
# SSE 流式调用（用于前端打字机效果）
# ---------------------------------------------------------
def _stream_ai_sse(system_prompt, user_prompt, temperature=0.8, max_tokens=2000, model_override=None, json_mode=False, response_format=None):
    _resolve_model(model_override)
    full_text = ""
    stream = None
    try:
        kwargs = {'response_format': {'type': 'json_object'}} if json_mode else {}
        if response_format is not None:
            kwargs.update(response_format=response_format, _decision=True)
        stream = generation.create(messages=[{"role":"system","content":system_prompt},
            {"role":"user","content":user_prompt}], temperature=temperature, max_tokens=max_tokens, stream=True, **kwargs)
        for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                text = chunk.choices[0].delta.content
                full_text += text
                yield f"data: {json.dumps({'type':'text','content':text}, ensure_ascii=False)}\n\n"
    except Exception as exc:
        yield f"data: {json.dumps({'type':'error','content':str(exc)}, ensure_ascii=False)}\n\n"
        return
    finally:
        close = getattr(stream, 'close', None)
        if close:
            close()
    yield f"data: {json.dumps({'type':'done','full_text':full_text}, ensure_ascii=False)}\n\n"


# ---------------------------------------------------------
# 上下文构建器
# ---------------------------------------------------------
def build_system_context(conn, *search_texts, timeline_id=None, sources=None):
    """
    打包提取全知上下文（世界观、角色、百科、记忆、实体、RAG、地图）。
    返回 8 元组。
    """
    selected=set(sources) if sources is not None else {'lore','history','entities','map'}
    wv_row = conn.execute("SELECT value FROM system_state WHERE key = 'worldview'").fetchone()
    worldview = wv_row["value"] if wv_row else ""

    mem_row = conn.execute("SELECT value FROM system_state WHERE key = 'session_memory'").fetchone()
    session_memory = mem_row["value"] if mem_row else ""

    l1_context = _l1_get_working_context(conn, timeline_id) if _l1_get_working_context and 'history' in selected else ""

    chars = conn.execute("SELECT name, role, hp, san, inventory, personality, status FROM characters").fetchall()
    active_chars = [c for c in chars if (c["status"] or "active") == "active"]
    benched_chars = [c for c in chars if (c["status"] or "") == "benched"]
    def _strip_mbti_desc(p: str) -> str:
        return re.sub(r'\([^)]*\)', '', p).strip()

    party_status = "\n".join([
        f"- {c['name']} (HP:{c['hp']}, SAN:{c['san']}) | 物品: {c['inventory'] or '无'}"
        + (f" | 性格: {_strip_mbti_desc(c['personality'])}" if c['personality'] else "")
        for c in active_chars
    ])
    if benched_chars:
        party_status += "\n【暂离角色（不在当前场景，不可直接互动）】\n" + "\n".join([
            f"- {c['name']}（暂离）" for c in benched_chars
        ])
    if not party_status.strip():
        party_status = "当前场景无活跃角色。"

    if timeline_id is not None:
        tl = conn.execute("SELECT * FROM timelines WHERE id=?", (timeline_id,)).fetchone()
        if tl is None:
            raise ValueError("队伍不存在")
        session_memory = tl["memory"] or ""
        ids = {int(x) for x in tl["char_ids"].split(",") if x.strip().isdigit()}
        party_status = "\n".join(f"- [entity_uid={c['entity_uid']}] {c['name']} HP:{c['hp']} SAN:{c['san']} 物品:{c['inventory']}"
            for c in conn.execute("SELECT * FROM characters") if c["id"] in ids)
    else:
        assigned={int(x) for row in conn.execute("SELECT char_ids FROM timelines WHERE status='active'")
                  for x in row[0].split(',') if x.strip().isdigit()}
        party_status = "\n".join(f"- [entity_uid={c['entity_uid']}] {c['name']} HP:{c['hp']} SAN:{c['san']} 物品:{c['inventory']}"
            for c in conn.execute("SELECT * FROM characters") if c["status"] == "active" and (c['role']!='PC' or c['id'] not in assigned))
    combined_text = " ".join(search_texts).lower()
    relevant_lore = ""

    world_entities_text = _get_world_entities_text(conn, *search_texts, timeline_id=timeline_id) if _get_world_entities_text else ""

    # One evidence pipeline. Location labels are a bounded hint, not a second query body.
    current_room_id = None
    room_row = conn.execute("SELECT value FROM system_state WHERE key='current_room_id'").fetchone()
    if room_row and room_row["value"]:
        current_room_id = int(room_row["value"])
    if timeline_id is not None:
        current_room_id = tl["current_room_id"]
    refresh_wiki(conn)
    conn.commit()
    from knowledge import retrieve
    bundle = retrieve(conn, " ".join(search_texts), timeline_id=timeline_id, sources=selected)
    rag_context = bundle.text

    # 地图空间感知
    map_context = ""
    try:
        if _get_map_context:
            map_context = _get_map_context(conn, current_room_id,query=' '.join(search_texts))
    except Exception as e:
        _log.debug("地图上下文构建失败（已降级）: %s", e)

    relevant_lore += "\n【已登记状态定义】\n" + encode(known_definitions(conn))
    if sources is not None:
        if "history" not in sources:
            session_memory = ""
            l1_context = ""
        # The current place/adjacency is a mandatory safety anchor even when map RAG is skipped.
        if "lore" not in sources:
            relevant_lore = "【已登记状态定义】" + encode(known_definitions(conn))
    return worldview, party_status, relevant_lore, session_memory, l1_context, world_entities_text, rag_context, map_context


# ---------------------------------------------------------
# Prompt 模板
# ---------------------------------------------------------
# ─────────────────────────────────────────────────────────
# JSON Schema（优化版：60 字短叙事 + null 字段省略 + 对话精简）
#
# 设计原则：
#   1. 60 字短叙事：node_content 限制为约 60 字的核心场景骨架，
#      保留因果链和细节锚点，玩家选择后由 expand-branch 润色为完整叙事。
#   2. null 字段省略：无变化的字段直接不输出。
#   3. 对话模式精简：砍掉 stat_changes / map_actions / npc 字段。
# ─────────────────────────────────────────────────────────

_DYNAMIC_OPTIONS_JSON_SCHEMA = """{
  "thought_process": "1. ... 2. ... 3. ... 4. ... 5. ...（每步限一句话，总共不超过100字）",
  "branches": [
    {
      "text": "选项简述（20字内）",
      "node_name": "新场景标题（15字内）",
      "node_content": "场景骨架（约60字，包含关键动作、感官反馈、情绪反应等核心锚点，仅当必要时才补充环境描写）",
      "stat_changes": [{"entity_uid": "上下文中的ID", "hp_delta": 0, "san_delta": 0, "inventory_append": "", "inventory_remove": "", "reason": "原因(10字内)"}],
      "entity_updates": "该分支导致的实体状态变化",
      "emotion_deltas": [{"entity_uid": "上下文中的ID", "trust": 0, "fear": 0, "irritation": 0}],
      "npc_memories": [{"entity_uid": "上下文中的ID", "memory": "从该NPC视角记住的一句话（20字内）"}],
      "map_actions": {
        "movement": {"target_room_id": "已有相邻地点的整数ID，或用target_room_ref引用本分支新区域", "reason": "移动原因"},
        "new_room": {"ref":"new_room", "label": "独立新区域名称", "description": "区域描述", "floor":"整数；默认当前楼层", "edge_type":"normal/stairs/elevator/portal", "aliases":[]},
        "reveal_room": {"room_id":"已有但本次才发现的地点ID"},
        "reveal_edge": {"edge_id":"已有但本次才发现的通道ID"},
        "unlock_edge": {"from_label": "房间A名称", "to_label": "房间B名称", "key_used": "使用的钥匙名"}
      },
      "npc": {"ref": "new_npc", "name": "姓名(10字内)", "role": "NPC", "hp": 50, "san": 60, "inventory": "持有物(30字内)", "backstory": "背景(50字内)"},
      "likelihood": "极高/高/中等/低/极低（一句话理由，基于角色状态和世界观规则）"
    }
  ]
}
【极重要·字段省略规则】：
- 若某分支没有 stat_changes，则省略整个 key
- 若某分支没有 entity_updates / emotion_deltas / npc_memories，则省略
- entity_updates 必须是文本字符串；无变化时省略或使用空字符串，不要使用对象、数组或 null。结构化实体变化使用 entity_changes / state_changes。
- 若无空间变化，省略 map_actions；若无新 NPC，省略 npc
- 剧情场景不等于新的物理地点。换称呼或同一地点内的环境描写不得新建房间。优先使用已登记地点ID。
- new_room 只表示发现区域。实际进入必须同时提供 movement:{target_room_ref:"new_room"}；仅发现入口时不移动。
- floor省略时继承当前楼层；跨楼层必须明确stairs/elevator/portal。不同楼层允许同名，名称有歧义时使用ID。
- unlock_edge负责扣除使用的钥匙，不得同时在stat_changes.inventory_remove中重复扣除。
- likelihood 字段每个分支必须输出，不可省略
- 只保留有实际内容的字段"""

# 【对话模式】JSON Schema — 砍掉 stat_changes / map_actions / npc，强化情绪和记忆
_DIALOGUE_JSON_SCHEMA = """{
  "thought_process": "1. 社交意图识别：... 2. NPC性格反应：... 3. 信息博弈：... 4. 关系位移：...（每步限一句话）",
  "branches": [
    {
      "text": "对话走向简述（20字内）",
      "node_name": "场景标题（15字内）",
      "node_content": "对话场景骨架（约60字，含NPC关键台词用「」包裹、表情/肢体语言）",
      "san_delta": {"entity_uid": "上下文中的ID", "delta": -5, "reason": "心理冲击原因"},
      "entity_updates": "NPC态度变化描述",
      "emotion_deltas": [{"entity_uid": "上下文中的ID", "trust": 10, "fear": 0, "irritation": -5}],
      "npc_memories": [{"entity_uid": "上下文中的ID", "memory": "从该NPC视角记住的一句话"}],
      "new_entities": [{"ref": "new_1", "name": "未登记的交谈对象称呼", "entity_type": "npc", "description": "已知身份描述"}],
      "likelihood": "极高/高/中等/低/极低（一句话理由，基于NPC性格和玩家行为）"
    }
  ]
}
【极重要·字段省略规则】：
- 对话模式下不允许出现 stat_changes / map_actions / npc 字段；未登记的交谈对象可用 new_entities 声明，已登记则省略
- 若无 SAN 变化，省略 san_delta；其余同上
- entity_updates 必须是文本字符串；无变化时省略或使用空字符串，不要使用对象、数组或 null。结构化实体变化使用 entity_changes / state_changes。
- likelihood 字段每个分支必须输出，不可省略
- 只保留有实际内容的字段"""

# 【行动模式】JSON Schema — 与混合模式相同
_ACTION_JSON_SCHEMA = _DYNAMIC_OPTIONS_JSON_SCHEMA

# ---------------------------------------------------------
# 三套推演指令模板
# ---------------------------------------------------------
_INSTRUCTION_DIALOGUE = """
=== 推演指令（社交对话模式）===
这是一次社交互动。玩家正在通过言语与场景中的角色交涉。

【核心约束】
- 不得产生任何物理伤害（hp_delta 必须为 0）
- 不得改变物品持有状态（inventory_append / inventory_remove 必须为空）
- 不得触发空间移动（map_actions 必须为 null）
- SAN 值可以因心理冲击而变化（例如得知可怕真相）
- entity_updates 应聚焦于 NPC 态度、情绪、信任度的变化

【情绪状态机】
有明确的情绪变化时输出 emotion_deltas 数组，记录该分支对在场 NPC 情绪的影响；无变化可省略：
- trust（信任）：正值=更信任玩家，负值=更不信任
- fear（恐惧）：正值=更害怕，负值=放松
- irritation（烦躁）：正值=更恼火，负值=缓和
注意：delta 分四档——日常闲聊(1-5)、明显影响(6-15)、重大转折(16-20)、超过20仅限救命/深层创伤等极端事件。系统会对小 delta 施加阻尼，对 delta≥20 的重大事件允许穿透阻尼直接入账。

【NPC 记忆】
需要持续记忆的互动可输出 npc_memories 数组，用一句话从 NPC 的第一人称视角记录；背景人物无需强行记录：
- 只记录该 NPC 亲眼所见/亲耳所闻的事，不能记录 NPC 不可能知道的信息
- 20字以内，例如："有人来打听地下室的事"、"那个法师送了我一块怀表"

【thought_process 必须包含以下 4 步推理】
1. 社交意图识别：玩家这句话的真实目的是什么？（威吓/套话/说服/欺骗/安慰）
2. NPC 性格反应：根据该 NPC 的性格特质，ta 会如何接招？（抗拒/动摇/配合/反击）
3. 信息博弈：这次对话是否会泄露/获取关键信息？哪些信息是 NPC 会守住的底线？
4. 关系位移：对话结束后，NPC 对玩家的态度会发生什么变化？

推演 2-4 个对话走向分支。每个分支的 node_content（约60字）必须包含 NPC 的关键台词（用「」包裹）和核心肢体语言。
【极其重要】每个分支的 entity_updates / emotion_deltas / npc_memories 必须写在该分支对象内部。不同对话走向可能导致不同的情绪后果。
【对话模式禁止字段】不要输出 stat_changes / map_actions / npc 字段。SAN 变化使用 san_delta 字段。
【概率判定】每个分支必须输出 likelihood 字段，格式为"极高/高/中等/低/极低（理由）"。理由基于 NPC 性格倾向、玩家社交行为的说服力和当前情境压力，不超过15字。不要输出数字概率。"""

_INSTRUCTION_ACTION = """
=== 推演指令（物理行动模式）===
这是一次物理行动。玩家正在执行涉及身体、环境或物品的操作。

【核心约束】
- HP/SAN 变化、物品得失、空间移动均可正常发生
- NPC 的言语反应应简短，重点描述其物理行为（拔剑、逃跑、倒地）
- 移动到已有区域须引用【地图空间感知】的相邻地点ID；进入本分支新区域时使用 movement.target_room_ref 引用 new_room.ref
- 仅发现新区域填 new_room，实际进入须另填 movement；揭示已有地点/通道使用 reveal_room/reveal_edge，解锁填 unlock_edge

【thought_process 必须包含以下 5 步推理】
1. 可行性检查：角色当前 HP/SAN/物品是否支持此行动？
2. 世界观合规：此行动是否违反物理法则或世界规则？
3. 空间合规：行动是否发生在当前位置可到达的范围内？
4. 物理后果：环境和在场角色会受到什么直接物理影响？
5. 连锁反应：此行动会触发哪些后续事件？

推演 2-4 个结果分支。每个分支的 node_content（约60字）必须包含关键动作描写、伤害程度和环境变化等核心锚点。
【极其重要】每个分支的 stat_changes / entity_updates / map_actions / npc 必须写在该分支对象内部。不同结果可能导致完全不同的HP变化和空间移动。
【字段省略】没有变化的字段直接省略，不要写空数组或 null。
【概率判定】每个分支必须输出 likelihood 字段，格式为"极高/高/中等/低/极低（理由）"。理由基于角色属性、装备支撑度和物理可行性，不超过15字。不要输出数字概率。"""

_INSTRUCTION_MIXED = """
=== 推演指令 ===
玩家做出了意外举动。你必须先在 thought_process 字段中进行严格的逻辑自检（内省），然后再输出推演结果。

【thought_process 必须包含以下 5 步推理（每步一句话）】
1. 角色状态检查：当前行动者的 HP/SAN/负面状态是否限制了此行动？
2. 世界观合规：此行动是否违反世界观中的物理法则、禁忌或社会规则？
3. 空间合规：若地图已启用，行动是否发生在当前位置可到达的范围内？
4. NPC 动机分析：场景中的 NPC 会如何反应？他们的动机是什么？
5. 后果推演：综合以上分析，合理的结局应当是什么？

完成以上内省后，推演 2-4 个合理分支。每个分支的 node_content（约60字）应包含核心因果描写。

【极其重要】每个分支的副作用字段必须写在该分支对象内部，而不是全局字段。
不同分支可能导致完全不同的后果（例如分支A扣HP，分支B不扣），系统会在玩家选择后只执行该分支的副作用。
移动到已有区域须引用【地图空间感知】的相邻地点ID；进入本分支新区域时使用 movement.target_room_ref 引用 new_room.ref。
仅发现区域不等于进入。已有但未发现的地点和通道使用 reveal_room/reveal_edge；解锁使用 unlock_edge。
【字段省略】没有变化的字段直接省略，不要写空数组或 null。
【概率判定】每个分支必须输出 likelihood 字段，格式为"极高/高/中等/低/极低（理由）"。综合 thought_process 的五步推理评估，理由不超过15字。不要输出数字概率。"""


def _build_dynamic_system_prompt(worldview, party_status, relevant_lore,
                                  session_memory, l1_context,
                                  world_entities_text, rag_context, map_context,
                                  is_timeline=False, tl_label="",
                                  action_type="mixed",
                                  gm_correction="",
                                  mood="",
                                  force_thrust=False):
    """构建推演用的 system prompt。根据 action_type 切换上下文策略和指令集。"""
    prefix = f"你是跑团GM，正在处理同一世界的分队「{tl_label}」中的剧情推演。" if is_timeline else "你是一个跑团GM。"
    persona = ""
    if _build_persona_instruction:
        persona = _build_persona_instruction()

    # GM 纠正指令块（非空时注入到 prompt 末尾）
    gm_correction_block = ""
    if gm_correction and gm_correction.strip():
        gm_correction_block = f"""

=== 【GM 纠正指令——最高优先级，必须遵守】===
GM 对上一次推演结果不满意，明确要求你在本次推演中：
{gm_correction.strip()}
你必须严格执行此纠正指令。如果 GM 的纠正与世界观冲突，以 GM 指令为准。"""

    # 氛围预设块
    mood_block = ""
    if mood and mood.strip():
        mood_block = f"""

=== 【氛围指令】===
本次推演的叙事基调为「{mood.strip()}」，分支描写、NPC语气和环境细节都应契合此氛围。"""

    # 叙事推力块（一次性）
    thrust_block = ""
    if force_thrust:
        thrust_block = """

=== 【系统指令：推动剧情】===
GM 认为当前剧情节奏需要加速。在本次推演中：
- 至少一个分支必须包含 NPC 主动抛出新线索、突发事件或尖锐质问
- 至少一个分支保持当前节奏供玩家选择"""

    # ── 根据 action_type 选择上下文策略 ──
    if action_type == "dialogue":
        # 对话模式：砍地图、强化 persona
        context_block = f"""{prefix}
你必须严格遵守以下信息层级，绝不能编造或忽视任何已设定的内容。

=== 第一优先级：硬性约束（违反即为错误）===
【全局世界观——所有推演必须在此框架内，不得自行发明世界观外的设定】
{worldview}

【角色当前状态——推演时必须参考，尤其关注 SAN 值和性格特征】
{party_status}

{f"【已知世界实体（L2 实体档案）——必须遵守其当前状态，尤其注意 NPC 的性格和态度】{chr(10)}{world_entities_text}" if world_entities_text else ""}

=== 第二优先级：上下文参考 ===
{f"【L1 近期推演快照】{chr(10)}{l1_context}" if l1_context else ""}

【剧情记忆流】
{session_memory}

【相关设定词条】
{relevant_lore}
{f"【知识库检索结果（L3 长期记忆 + 背景设定）】{chr(10)}{rag_context}" if rag_context else ""}

=== 性格交互协议（对话模式强化）===
{persona if persona else "根据 NPC 的已有设定推演其社交反应，注意其说话方式、肢体语言和心理状态的一致性。"}

{_INSTRUCTION_DIALOGUE}

必须返回严格 JSON（thought_process 为第一个字段）：
{_DIALOGUE_JSON_SCHEMA}{mood_block}{thrust_block}{gm_correction_block}"""

    elif action_type == "action":
        # 行动模式：强化地图、砍 persona
        context_block = f"""{prefix}
你必须严格遵守以下信息层级，绝不能编造或忽视任何已设定的内容。

=== 第一优先级：硬性约束（违反即为错误）===
【全局世界观——所有推演必须在此框架内，不得自行发明世界观外的设定】
{worldview}

【角色当前状态——推演时必须参考，HP/SAN/物品必须与此一致】
{party_status}

{f"【已知世界实体（L2 实体档案）——必须遵守其当前状态，不得矛盾】{chr(10)}{world_entities_text}" if world_entities_text else ""}
{f"【地图空间感知——AI必须遵守此空间结构推演，不可凭空创造不存在的房间或通道】{chr(10)}{map_context}" if map_context else ""}

=== 第二优先级：上下文参考 ===
{f"【L1 近期推演快照】{chr(10)}{l1_context}" if l1_context else ""}

【剧情记忆流】
{session_memory}

【相关设定词条】
{relevant_lore}
{f"【知识库检索结果（L3 长期记忆 + 背景设定）】{chr(10)}{rag_context}" if rag_context else ""}

{_INSTRUCTION_ACTION}

必须返回严格 JSON（thought_process 为第一个字段）：
{_ACTION_JSON_SCHEMA}{mood_block}{thrust_block}{gm_correction_block}"""

    else:
        # 混合模式：沿用原逻辑
        context_block = f"""{prefix}
你必须严格遵守以下信息层级，绝不能编造或忽视任何已设定的内容。

=== 第一优先级：硬性约束（违反即为错误）===
【全局世界观——所有推演必须在此框架内，不得自行发明世界观外的设定】
{worldview}

【角色当前状态——推演时必须参考，HP/SAN/物品必须与此一致】
{party_status}

{f"【已知世界实体（L2 实体档案）——必须遵守其当前状态，不得矛盾】{chr(10)}{world_entities_text}" if world_entities_text else ""}
{f"【地图空间感知——AI必须遵守此空间结构推演，不可凭空创造不存在的房间或通道】{chr(10)}{map_context}" if map_context else ""}

=== 第二优先级：上下文参考 ===
{f"【L1 近期推演快照】{chr(10)}{l1_context}" if l1_context else ""}

【剧情记忆流】
{session_memory}

【相关设定词条】
{relevant_lore}
{f"【知识库检索结果（L3 长期记忆 + 背景设定）】{chr(10)}{rag_context}" if rag_context else ""}
{persona}

{_INSTRUCTION_MIXED}

必须返回严格 JSON（thought_process 为第一个字段）：
{_DYNAMIC_OPTIONS_JSON_SCHEMA}{mood_block}{thrust_block}{gm_correction_block}"""

    return context_block + WORLD_PROMPT + '\n' + NARRATIVE_RULE


# ---------------------------------------------------------
# 推演结果后处理（NPC / stat / map / entity / memory）
# ---------------------------------------------------------
def _post_process_dynamic_result(conn, parsed: dict, scene_name: str,
                                  player_action: str, current_node_id: int,
                                  timeline_id: int | None = None,
                                  tl_id_for_memory: int | None = None,
                                  action_type: str = "mixed"):
    """
    Phase 1：推演完成后仅创建草稿分支。
    AI 返回 60 字短叙事 node_content + 副作用，创建节点和选项。
    副作用序列化存储到 pending_effects 表，等玩家选择后执行。

    前端调用链：
      Phase 1: 推演 → _post_process（创建节点 + pending_effects）
      Phase 2: expand-branch（扩写候选，等待确认）
      Phase 3: commit-scene（确认正文与世界变化，执行规则）
    """
    thought_process = prose_renderer(conn, timeline_id)(str(parsed.get("thought_process", ""))) if isinstance(parsed, dict) else ""

    branches = validate_branches(parsed)
    if not branches:
        return [], None, [], {}, thought_process, ""

    if not conn.execute("SELECT id FROM nodes WHERE id = ?", (current_node_id,)).fetchone():
        raise ValueError("节点不存在")

    new_options = []
    token = parsed.get("_state_token") or state_token(conn)
    if token != state_token(conn):
        raise ValueError("世界已更新，请重新推演")
    for branch_index, branch in enumerate(branches):
        clean_branch_prose(branch, prose_renderer(conn, timeline_id, branch))
        if parsed.get('_summary_contract') and len(branch['node_content']) > 100:
            raise ValueError('归一化后的分支摘要不得超过100字符')
        n_name = branch.get("node_name", "未知节点")[:50]
        n_content = branch.get("node_content", "")[:1000]
        o_text = branch.get("text", n_name)[:100]

        # ── 收集副作用（优先 per-branch，fallback 到全局；省略的字段默认为空） ──
        b_stat_changes = branch.get("stat_changes", [])
        b_entity_updates = branch.get("entity_updates", "")
        b_map_actions = branch.get("map_actions")
        b_npc = branch.get("npc")
        b_emotion_deltas = branch.get("emotion_deltas") or []
        b_npc_memories = branch.get("npc_memories") or []

        # ── 对话模式守卫 ──
        if action_type == "dialogue":
            san_delta_obj = branch.get("san_delta")
            if san_delta_obj and isinstance(san_delta_obj, dict):
                b_stat_changes = [{
                    "entity_uid": san_delta_obj.get("entity_uid"),
                    "char_name": san_delta_obj.get("char_name", ""),
                    "hp_delta": 0,
                    "san_delta": int(san_delta_obj.get("delta", 0)),
                    "inventory_append": "", "inventory_remove": "",
                    "reason": san_delta_obj.get("reason", ""),
                }]
            elif b_stat_changes and isinstance(b_stat_changes, list):
                for c in b_stat_changes:
                    c["hp_delta"] = 0
                    c["inventory_append"] = ""
                    c["inventory_remove"] = ""
            if not parsed.get('_summary_contract'):
                b_map_actions = None
            b_npc = None

        fx_payload = {
            "state_token": token, "parent_node_id": current_node_id, "phase": "DRAFT_SUMMARY",
            "new_entities": branch.get("new_entities", []), "entity_changes": branch.get("entity_changes", []),
            "state_definitions": branch.get("state_definitions", []), "state_changes": branch.get("state_changes", []),
            "stat_changes": b_stat_changes or [],
            "reference_warnings": branch.get('reference_warnings', []),
            "deferred_effects": branch.get('deferred_effects', []),
            "entity_updates": b_entity_updates or "",
            "emotion_deltas": b_emotion_deltas or [],
            "npc_memories": b_npc_memories or [],
            "map_actions": b_map_actions,
            "npc": b_npc,
            "action_type": action_type,
            "timeline_id": timeline_id,
            "tl_id_for_memory": tl_id_for_memory,
            "scene_name": scene_name,
            "player_action": player_action,
            "thought_process": thought_process,
            "skeleton": n_content,  # 60字骨架原文，冻结于此，不受 expand-branch 扩写影响
        }
        if parsed.get('_summary_contract'):
            fx_payload.update(summary_contract=parsed['_summary_contract'],
                              spatial_context=parsed['_spatial_context'], request_input=parsed['_request_input'])
        if parsed.get('_npc_review_id'):
            from npc_review import seal
            seal(conn, fx_payload, parsed['_npc_review_id'], branch_index)

        summary_text = o_text[:100]

        # 60字短叙事：content 已含核心骨架，玩家选择后由 expand-branch 润色扩写
        cursor = conn.execute("INSERT INTO nodes (name, summary, content, is_draft) VALUES (?, ?, ?, 1)",
                              (n_name, summary_text, n_content))
        n_id = cursor.lastrowid

        conn.execute(
            "INSERT OR REPLACE INTO pending_effects (node_id, payload) VALUES (?, ?)",
            (n_id, json.dumps(fx_payload, ensure_ascii=False))
        )

        conn.execute("INSERT INTO options (node_id, text, next_node_id) VALUES (?, ?, ?)",
                     (current_node_id, o_text, n_id))
        new_options.append({
            "text": o_text, "next_node_id": n_id,
            "node_name": n_name, "node_content": n_content,
            "pending_stat_changes": b_stat_changes or [],
            "pending_entity_updates": b_entity_updates or "",
            "pending_map_actions": b_map_actions,
            "pending_npc": b_npc,
            "npc_additions": fx_payload.get('npc_additions', []),
            "reference_warnings": fx_payload['reference_warnings'],
            "deferred_effects": fx_payload['deferred_effects'],
            "likelihood": branch.get("likelihood", ""),
        })

    conn.commit()
    return new_options, None, [], {"moved_to": None, "new_room": None, "unlocked_edge": None, "errors": []}, thought_process, ""


# ---------------------------------------------------------
# Phase 3：用户确认扩写正文后，事务提交副作用和规则
# ---------------------------------------------------------
class ApplyBranchEffectsRequest(BaseModel):
    node_id: int
    expected_token: str | None = None
    timeline_id: int | None = None
    confirm_npc_additions: bool = False


@agent_router.post("/api/ai/apply-branch-effects")
def apply_branch_effects(req: ApplyBranchEffectsRequest, background_tasks: BackgroundTasks):
    from engine import apply_branch
    from memory_service import maintain_memory
    conn = get_db_connection()
    try:
        scope = req.timeline_id if 'timeline_id' in req.model_fields_set else ...
        result = apply_branch(conn, req.node_id, req.expected_token, expected_timeline=scope,
                              confirm_npc_additions=req.confirm_npc_additions)
        if result["status"] == "success":
            background_tasks.add_task(maintain_memory,result.get("timeline_id"))
        return result
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    finally:
        conn.close()


# ---------------------------------------------------------
# Phase 2：玩家选择摘要后，扩写候选正文并等待确认
# （60字短叙事的核心配套端点——润色扩写）
#
# 【调用顺序】摘要 → expand-branch（服务器候选效果）→ 用户确认 → commit-scene。
#   扩写仍是草稿，不产生世界事件、记忆或触发器效果。
# ---------------------------------------------------------
class ExpandBranchRequest(BaseModel):
    timeline_id: int | None = None
    node_id: int              # 玩家选择的目标节点 ID
    scene_name: str = ""      # 父场景名
    scene_content: str = ""   # 父场景正文（关键上下文）
    player_action: str = ""   # 玩家动作
    model: str | None = None  # 请求级模型选择（可选）
    fx_context: str = ""      # 兼容旧客户端，实际采用服务器已确认事件
    action_type: str = ""     # 兼容旧客户端，实际采用服务器已确认事件

class ExpandBranchStreamRequest(ExpandBranchRequest):
    pass


class PrepareBranchRequest(BaseModel):
    node_id: int
    timeline_id: int | None = None
    operation_key: str = Field(min_length=1, max_length=200)


@agent_router.post('/api/ai/prepare-branch')
def prepare_branch(request: PrepareBranchRequest):
    from selected_branches import prepare
    return prepare(request)


def _build_fx_context(fx_row) -> tuple[str, str]:
    """
    从 pending_effects 行中提取副作用上下文文本和 action_type。
    返回 (fx_context_str, action_type)。
    供 expand-branch 的非流式和流式端点共享。
    """
    if not fx_row:
        return "", "mixed"
    try:
        fx = json.loads(fx_row["payload"])
    except (json.JSONDecodeError, TypeError):
        return "", "mixed"

    action_type = fx.get("action_type", "mixed")
    fx_parts = []

    for sc in (fx.get("stat_changes") or []):
        parts = []
        if sc.get("hp_delta"):  parts.append(f"HP{sc['hp_delta']:+d}")
        if sc.get("san_delta"): parts.append(f"SAN{sc['san_delta']:+d}")
        if sc.get("inventory_append"): parts.append(f"获得[{sc['inventory_append']}]")
        if sc.get("inventory_remove"): parts.append(f"失去[{sc['inventory_remove']}]")
        if parts:
            fx_parts.append(f"{sc.get('char_name', '')}: {', '.join(parts)}")

    if fx.get("entity_updates"):
        fx_parts.append(f"实体变化：{fx['entity_updates']}")

    ma = fx.get("map_actions")
    if ma and isinstance(ma, dict):
        fx_parts.append('候选地图动作（发现与进入分别执行）：'+encode(ma))
        if ma.get("movement"):
            fx_parts.append(f"移动至：{ma['movement'].get('target_room_label', '')}")
        if ma.get("new_room"):
            fx_parts.append(f"发现新区域：{ma['new_room'].get('label', '')}")

    npc = fx.get("npc")
    if npc and isinstance(npc, dict):
        fx_parts.append(f"NPC登场：{npc.get('name', '')} — {npc.get('backstory', '')}")

    if fx.get("new_entities"):
        fx_parts.append("新实体候选档案：" + encode(fx["new_entities"]))
    for field, label in (('emotion_deltas','候选情绪变化'),('npc_memories','候选人物记忆'),
                         ('entity_changes','候选实体变化'),('state_definitions','候选状态定义'),('state_changes','候选动态状态')):
        if fx.get(field): fx_parts.append(label + '：' + encode(fx[field]))
    if fx.get('deferred_effects'):
        fx_parts.append('以下变化的对象未确定，暂缓执行；不要据此编造具体数值、物品或关系结果：' + encode(fx['deferred_effects']))

    fx_context = ("【该分支的候选结果】\n" + "\n".join(fx_parts)) if fx_parts else "【该分支未提议额外副作用】"
    return fx_context, action_type


def _build_expand_prompts(conn, req, node_name: str, current_content: str,
                           fx_context: str, action_type: str) -> tuple[str, str]:
    """
    构建 expand-branch 的 system/user prompt。
    注入完整上下文：世界观、角色、记忆流、设定词条、实体、地图、父场景正文、副作用。
    """
    ctx = build_system_context(conn, req.scene_name, node_name, req.player_action, timeline_id=req.timeline_id)
    worldview, party_status, relevant_lore, session_memory, \
        l1_context, world_entities_text, rag_context, map_context = ctx

    if action_type == "dialogue":
        style_hint = "【沉浸式对话】：除了台词本身（用「」包裹），补充说话时的神态变化、语气停顿或习惯性的小动作。环境描写仅作为自然的背景板，需绝对契合当前场景的氛围（如日常、严肃或轻松）。"
    elif action_type == "action":
        style_hint = "【动作与感知】：丰富动作的过程细节与直接的感官体验（如物品的触感、周遭的白噪音、温度等）。环境描写应与角色的动作产生实际交互（例如：拉开椅子发出摩擦声、端起冒热气的餐盘），拒绝流水账式的风景速写。"
    else:
        style_hint = "【综合推演】：自然流畅地交织动作、对话与周遭环境。保持符合当前场景基调的沉浸感，日常则温馨平实，危机则节奏紧凑。"

    system_prompt = f"""你是跑团GM。请为玩家选择的分支撰写一段完整的、引人入胜的场景描写。

【全局世界观】
{worldview}

【角色当前状态】
{party_status}

{f"【已知世界实体】{chr(10)}{world_entities_text}" if world_entities_text else ""}
{f"【地图空间感知】{chr(10)}{map_context}" if map_context else ""}
{f"【相关设定词条】{chr(10)}{relevant_lore}" if relevant_lore and relevant_lore != "无" else ""}
{f"【近期剧情记忆】{chr(10)}{session_memory}" if session_memory else ""}

{f"【L1 近期推演快照】{chr(10)}{l1_context}" if l1_context else ""}
{f"【百科证据】{chr(10)}{rag_context}" if rag_context else ""}

{fx_context}

【写作要求】
- 基于下方【分支骨架】进行润色扩写，将其转化为富有代入感的文字描述。
- 弹性字数：字数由事件的内容量自然决定（短至三五十字、长至百余字均可），绝对不要刻意凑字数。
- {style_hint}
- 当前结构化状态和已确认事件是权威；若骨架与其冲突，明确指出冲突，不通过编造消解。
- 保留骨架中的所有关键细节（动作、伤害程度、环境元素），不得矛盾
- 必须与【候选结果】中的数值变化一致——不得编造额外的 HP/物品/空间变化
- 新登场角色以候选档案为准，不得在扩写时额外新增未声明角色。
- 承接父场景的内容和氛围，保持叙事连贯,给出自然的动作收尾或留白
- 不要输出 JSON，直接输出纯文本场景描写
- 不要加任何前缀、标题或解释"""
    system_prompt += '\n' + NARRATIVE_RULE

    # user prompt 包含完整的因果链：父场景 → 玩家行动 → 骨架 → 请求扩写
    user_parts = []
    if req.scene_name:
        user_parts.append(f"父场景名称：{req.scene_name}")
    if req.scene_content:
        user_parts.append(f"父场景正文：{req.scene_content[:300]}")
    if req.player_action:
        user_parts.append(f"玩家动作：{req.player_action}")
    user_parts.append(f"分支标题：{node_name}")
    user_parts.append(f"分支骨架（请基于此润色扩写）：{current_content}")
    user_parts.append("请将上述骨架润色为一段完整的、有代入感且契合当前氛围的场景描写。")

    user_prompt = "\n".join(user_parts)
    return system_prompt, user_prompt


@agent_router.post("/api/ai/expand-branch")
def expand_branch_content(request: ExpandBranchRequest):
    from agent_operations import expand
    return expand(request,stream=False)


@agent_router.post("/api/ai/expand-branch/stream")
def expand_branch_stream(request: ExpandBranchStreamRequest):
    from agent_operations import expand
    return expand(request,stream=True)

class DynamicActionRequest(BaseModel):
    current_node_id: int
    operation_key: str | None = Field(default=None, min_length=1, max_length=200)
    timeline_id: int | None = None
    resolved_entities: dict[str, str] = {}
    mentioned_entities: list[str] = []
    scene_name: str = ""
    content: str = ""
    player_action: str = ""
    gm_correction: str = ""  # GM 纠正指令（重新推演时填写）
    action_type: str = "auto"  # "dialogue" | "action" | "mixed" — 动作经济分离
    model: str | None = None   # 请求级模型选择（可选，覆盖服务器默认值）
    mood: str = ""             # 叙事氛围预设（如"恐怖"/"史诗"，空字符串=不干预）
    force_thrust: bool = False  # GM叙事推力：强制注入推动剧情指令（一次性）

class ExpandTextRequest(BaseModel):
    node_id: int | None = None
    scene_name: str = ""
    content: str = ""
    model: str | None = None   # 请求级模型选择（可选）
    timeline_id: int | None = None


# ---------------------------------------------------------
# SSE 流式推演端点（核心）
# ---------------------------------------------------------
@agent_router.post("/api/ai/dynamic-options")
def dynamic_options_handler(request: DynamicActionRequest):
    from agent_operations import dynamic
    return dynamic(request,stream=False)


@agent_router.post("/api/ai/dynamic-options/stream")
def dynamic_options_stream(request: DynamicActionRequest):
    from agent_operations import dynamic
    return dynamic(request,stream=True)


# ---------------------------------------------------------
# 扩写端点
# ---------------------------------------------------------
@agent_router.post("/api/ai/expand-text")
def expand_scene_text(request:ExpandTextRequest):
    from agent_operations import preview
    return preview(request,stream=False)


# ---------------------------------------------------------
# 扩写流式端点
# ---------------------------------------------------------
@agent_router.post("/api/ai/expand-text/stream")
def expand_scene_text_stream(request:ExpandTextRequest):
    from agent_operations import preview
    return preview(request,stream=True)


##########################################################################
# 将此路由追加到 agent.py 末尾
@agent_router.post("/api/ai/npc-chat/stream")
def npc_chat_stream(request:NPCChatRequest):
    from agent_operations import npc_chat
    return npc_chat(request)


# ── 请求模型 ──────────────────────────────────────────────────────────────────
class NPCChatCommitRequest(BaseModel):
    operation_key: str = Field(default_factory=lambda: __import__("uuid").uuid4().hex)
    npc_name: str = ""
    entity_uid: str | None = None
    timeline_id: int | None = None
    player_message: str      # 玩家最后一条消息
    npc_reply: str           # NPC 完整回复文本
    chat_summary: str = ""   # 可选：前端传来的对话摘要（不传时由后端推断）


@agent_router.post("/api/ai/npc-chat/commit")
def npc_chat_commit(request:NPCChatCommitRequest):
    from agent_operations import npc_commit
    return npc_commit(request)


@agent_router.get("/api/ai/npc-chat/history")
def npc_chat_history(npc_name:str="",limit:int=200,entity_uid:str|None=None,timeline_id:int|None=None):
    from agent_operations import chat_history
    return chat_history(npc_name,limit,entity_uid,timeline_id)

