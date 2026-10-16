from engine import validate_branches
from model_output import NARRATIVE_RULE, clean_branch_prose, prose_renderer
from world import WORLD_PROMPT, known_definitions, state_token, wiki_context, refresh_wiki, resolve_entity, encode
from providers import route_action
from database import connect_db, atomic
from providers import generation, parse_object
"""
APERTUS-MUNDUS engine — AI Agent module (agent.py)
Includes Apertus generation, pluggable decision-making, and system prompt construction,
SSE streaming simulation endpoints, and result post-processing (NPC/stat/map/entity).
Mounted by main.py through app.include_router(agent_router).
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
# Add this request model at the top of agent.py
class NPCChatRequest(BaseModel):
    npc_name: str = ""
    entity_uid: str | None = None
    timeline_id: int | None = None
    player_message: str
    chat_history: str = "" # Recent chat history received from the frontend
###########################################################

# ---------------------------------------------------------
# state_desc JSON parser (compatible with legacy plain text)
# ---------------------------------------------------------
def _parse_state_desc(raw: str) -> dict:
    """
    Parse the world_entities.state_desc field.
    New format: JSON  {"desc":"...", "emotion":{...}, "breakpoint":{...}, "memory":[...]}
    Legacy format: plain text -> automatically upgraded to {"desc": "Original text", "emotion": {...}, "memory": []}
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
    # Legacy plain-text compatibility
    return {"desc": raw, "emotion": {"trust": 0, "fear": 0, "irritation": 0}, "memory": []}


def _serialize_state_desc(sd: dict) -> str:
    """Serialize the state_desc dictionary as a JSON string."""
    return json.dumps(sd, ensure_ascii=False)


def _format_emotion_label(value: int) -> str:
    """Convert numerical emotion values into readable labels."""
    if value >= 60: return "Very high"
    if value >= 30: return "High"
    if value >= 10: return "Medium"
    if value >= -10: return "Neutral"
    if value >= -30: return "Low"
    return "Very low"

# ---------------------------------------------------------
# Dependency injection (configured by main.py at startup)
# ---------------------------------------------------------
_db_file: str = ""
_generation_client = None   # OpenAI-compatible (Apertus)
_persona_config: dict = {}

# External function references (injected by main.py to avoid circular imports)
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
_get_embeddings = None          # rag.get_embeddings (used to vectorize NPC memories into L3)
_refresh_vector_cache = None    # rag.refresh_vector_cache

# Currently active model (server default, overridable per request)
AVAILABLE_MODELS = {"apertus-70b": {"label": "Apertus 70B", "model_id": generation.settings.model}}
_active_model = "apertus-70b"

def _resolve_model(request_model=None):
    if request_model and request_model != "apertus-70b":
        raise ValueError("Generation supports only apertus-70b; smaller models are configured in the decision layer")
    return "apertus-70b"


def configure_agent(
    db_file: str,
    generation_client,
    persona_config: dict = None,
    # External function injection
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
    """Called by main.py at startup to inject all dependencies."""
    global _db_file, _generation_client, _persona_config
    global _get_map_context, _auto_place_room, _process_map_actions
    global _get_current_room_id, _ai_extract_and_upsert_entities
    global _build_persona_instruction, _l1_append, _l1_get_working_context
    global _append_to_memory, _tl_append_memory, _get_world_entities_text, _rag_retrieve
    global _get_embeddings, _refresh_vector_cache

    _db_file = db_file
    _generation_client = generation_client
    _persona_config = persona_config or {}

    # Inject external functions
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
# Model switching API
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
# Unified AI invocation interface (Apertus 70B)
# ---------------------------------------------------------
def _call_ai(system_prompt, user_prompt, temperature=0.8, max_tokens=2000, json_mode=True, model_override=None, response_format=None):
    _resolve_model(model_override)
    return generation.generate(system_prompt, user_prompt, temperature=temperature,
                               max_tokens=max_tokens, json_mode=json_mode, response_format=response_format,
                               decision=response_format is not None)


# ---------------------------------------------------------
# SSE streaming call (for the frontend typewriter effect)
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
# Context builder
# ---------------------------------------------------------
def build_system_context(conn, *search_texts, timeline_id=None, sources=None):
    """
    Gather omniscient context (worldview, characters, encyclopedia, memory, entities, RAG and map).
    Return an 8-tuple.
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
        f"- {c['name']} (HP:{c['hp']}, SAN:{c['san']}) | Items: {c['inventory'] or 'None'}"
        + (f" | Personality: {_strip_mbti_desc(c['personality'])}" if c['personality'] else "")
        for c in active_chars
    ])
    if benched_chars:
        party_status += "\n[Inactive characters (not in the current scene; cannot interact directly)]\n" + "\n".join([
            f"- {c['name']}(away)" for c in benched_chars
        ])
    if not party_status.strip():
        party_status = "There are no active characters in the current scene."

    if timeline_id is not None:
        tl = conn.execute("SELECT * FROM timelines WHERE id=?", (timeline_id,)).fetchone()
        if tl is None:
            raise ValueError("Team does not exist")
        session_memory = tl["memory"] or ""
        ids = {int(x) for x in tl["char_ids"].split(",") if x.strip().isdigit()}
        party_status = "\n".join(f"- [entity_uid={c['entity_uid']}] {c['name']} HP:{c['hp']} SAN:{c['san']} Items:{c['inventory']}"
            for c in conn.execute("SELECT * FROM characters") if c["id"] in ids)
    else:
        assigned={int(x) for row in conn.execute("SELECT char_ids FROM timelines WHERE status='active'")
                  for x in row[0].split(',') if x.strip().isdigit()}
        party_status = "\n".join(f"- [entity_uid={c['entity_uid']}] {c['name']} HP:{c['hp']} SAN:{c['san']} Items:{c['inventory']}"
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

    # Spatial map awareness
    map_context = ""
    try:
        if _get_map_context:
            map_context = _get_map_context(conn, current_room_id,query=' '.join(search_texts))
    except Exception as e:
        _log.debug("Failed to build map context (degraded gracefully): %s", e)

    relevant_lore += "\n[Registered state definitions]\n" + encode(known_definitions(conn))
    if sources is not None:
        if "history" not in sources:
            session_memory = ""
            l1_context = ""
        # The current place/adjacency is a mandatory safety anchor even when map RAG is skipped.
        if "lore" not in sources:
            relevant_lore = "[Registered state definitions]" + encode(known_definitions(conn))
    return worldview, party_status, relevant_lore, session_memory, l1_context, world_entities_text, rag_context, map_context


# ---------------------------------------------------------
# Prompt templates
# ---------------------------------------------------------
# ─────────────────────────────────────────────────────────
# JSON Schema (optimized for short 60-character narrative, omission of null fields, and concise dialogue)
#
# Design principles:
#   1. Short narrative: limit node_content to a core outline of roughly 60 characters,
#      preserving causality and details; expand-branch turns the selected option into full prose.
#   2. Omit null fields: do not output fields with no changes.
#   3. Simplify dialogue mode: remove stat_changes / map_actions / npc fields.
# ─────────────────────────────────────────────────────────

_DYNAMIC_OPTIONS_JSON_SCHEMA = """{
  "thought_process": "1. ... 2. ... 3. ... 4. ... 5. ...(one sentence per step; no more than 100 characters total)",
  "branches": [
    {
      "text": "Brief option description (within 20 characters)",
      "node_name": "New scene title (within 15 characters)",
      "node_content": "Scene outline (about 60 characters with key actions, sensory cues, emotional reactions and essential anchors; add setting details only if necessary)",
      "stat_changes": [{"entity_uid": "ID from context", "hp_delta": 0, "san_delta": 0, "inventory_append": "", "inventory_remove": "", "reason": "Reason (within 10 characters)"}],
      "entity_updates": "Entity-state changes caused by this branch",
      "emotion_deltas": [{"entity_uid": "ID from context", "trust": 0, "fear": 0, "irritation": 0}],
      "npc_memories": [{"entity_uid": "ID from context", "memory": "One sentence remembered from this NPC's perspective (within 20 characters)"}],
      "map_actions": {
        "movement": {"target_room_id": "Integer ID of an existing adjacent location, or target_room_ref referring to this branch's new area", "reason": "Reason for movement"},
        "new_room": {"ref":"new_room", "label": "Distinct new area name", "description": "Area description", "floor":"Integer; defaults to current floor", "edge_type":"normal/stairs/elevator/portal", "aliases":[]},
        "reveal_room": {"room_id":"ID of an existing location newly discovered in this branch"},
        "reveal_edge": {"edge_id":"Existing passage ID discovered for the first time in this branch"},
        "unlock_edge": {"from_label": "Room AName", "to_label": "Room BName", "key_used": "Name of the key used"}
      },
      "npc": {"ref": "new_npc", "name": "Name (within 10 characters)", "role": "NPC", "hp": 50, "san": 60, "inventory": "Carried items (within 30 characters)", "backstory": "Backstory (within 50 characters)"},
      "likelihood": "Very high/high/medium/low/very low (one-sentence reason based on character state and worldview rules)"
    }
  ]
}
[Critical field omission rules]:
- If a branch has no stat_changes, omit the key entirely
- Omit entity_updates / emotion_deltas / npc_memories when absent from a branch
- entity_updates must be a text string; omit if unchanged or use an empty string, never an object, array or null. Use entity_changes / state_changes for structured entity changes.
- Omit map_actions if there is no spatial change, and omit npc if there is no new NPC
- A narrative scene is not a new physical location. Alternate names or environmental descriptions within one place do not justify a new room; use registered location IDs.
- new_room only denotes discovery of an area. Actual entry also requires movement:{target_room_ref:"new_room"}; discovering an entrance alone does not move the team.
- Omitted floor inherits the current floor; crossing floors requires stairs/elevator/portal. Different floors may share names; use IDs for ambiguous names.
- unlock_edge consumes the key used; do not also remove it in stat_changes.inventory_remove.
- Every branch must include the likelihood field
- Include only fields with actual content"""

# [Dialogue mode] JSON Schema — omit stat_changes / map_actions / npc and emphasize emotions and memories
_DIALOGUE_JSON_SCHEMA = """{
  "thought_process": "1. Social intent analysis: ... 2. NPC personality response: ... 3. Information exchange: ... 4. Relationship shift: ...(one sentence per step)",
  "branches": [
    {
      "text": "Brief dialogue outcome (within 20 characters)",
      "node_name": "Scene title (within 15 characters)",
      "node_content": "Dialogue scene outline (about 60 characters; enclose key NPC lines in quotation marks and describe expressions and gestures)",
      "san_delta": {"entity_uid": "ID from context", "delta": -5, "reason": "Reason for psychological shock"},
      "entity_updates": "NPC attitude changeDescription",
      "emotion_deltas": [{"entity_uid": "ID from context", "trust": 10, "fear": 0, "irritation": -5}],
      "npc_memories": [{"entity_uid": "ID from context", "memory": "One sentence recalled from the NPC's perspective"}],
      "new_entities": [{"ref": "new_1", "name": "Name used for an unregistered conversation partner", "entity_type": "npc", "description": "Known identityDescription"}],
      "likelihood": "Very high/high/medium/low/very low (one-sentence reason based on NPC personality and player behavior)"
    }
  ]
}
[Critical field omission rules]:
- Dialogue mode must not include stat_changes / map_actions / npc fields; declare unregistered conversation partners with new_entities and omit them if already registered
- Omit san_delta if SAN is unchanged; otherwise follow the rules above
- entity_updates must be a text string; omit if unchanged or use an empty string, never an object, array or null. Use entity_changes / state_changes for structured entity changes.
- Every branch must include the likelihood field
- Include only fields with actual content"""

# [Action mode] JSON Schema — same as mixed mode
_ACTION_JSON_SCHEMA = _DYNAMIC_OPTIONS_JSON_SCHEMA

# ---------------------------------------------------------
# Three simulation instruction templates
# ---------------------------------------------------------
_INSTRUCTION_DIALOGUE = """
=== Simulation instructions (social dialogue mode)===
This is a social interaction. The player is negotiating through words with a character in the scene.

【Core constraints】
- No physical damage may occur (hp_delta must be 0)
- Item ownership must not change (inventory_append / inventory_remove must be empty)
- No spatial movement may occur (map_actions must be null)
- SAN may change due to psychological shock (e.g., learning a terrifying truth)
- entity_updates should focus on NPC attitude, emotions, and changes in trust

[Emotion state machine]
When emotions clearly change, output emotion_deltas describing their effect on present NPCs; omit if nothing changes:
- trust: positive = more trust in the player; negative = less trust
- fear(fear): positive = more afraid; negative = more relaxed
- irritation(irritation): positive = more irritated; negative = calmer
Note: four delta levels: casual conversation (1-5), noticeable impact (6-15), major turning point (16-20), and over 20 only for extreme events such as lifesaving acts or deep trauma. Small deltas are damped; major events with delta >=20 can bypass damping.

【NPC Memory】
For interactions worth remembering, output npc_memories with one sentence in the NPC's first-person perspective; background characters need not record memories:
- Record only what the NPC personally saw or heard, never information they could not know
- Within 20 characters, for example: "Someone asked about the basement"、"That mage gave me a pocket watch"

【thought_process must contain these four reasoning steps】
1. Social intent: what does the player really intend? (intimidation/probing/persuasion/deception/comfort)
2. NPC personality reaction: How will the NPC respond based on personality? (resist / waver / cooperate / retaliate)
3. Information exchange: Will this conversation reveal or obtain important information? What limits will the NPC maintain?
4. Relationship shift: how will the NPC's attitude toward the player change after the dialogue?

Simulate 2-4 dialogue branches. Each branch's node_content (around 60 characters) must include key NPC dialogue (in quotation marks) and essential body language.
[Extremely important] Each branch must contain its own entity_updates / emotion_deltas / npc_memories. Different dialogue paths may have different emotional outcomes.
【Forbidden dialogue-mode fields】Do not output stat_changes / map_actions / npc fields. Use san_delta for SAN changes.
[Likelihood assessment] Each branch must output likelihood as "very high/high/medium/low/very low (reason)". Base the reason on NPC personality, the persuasiveness of the player's social action and situational pressure, within 15 characters. Do not output numeric probabilities."""

_INSTRUCTION_ACTION = """
=== Simulation instructions (physical action mode) ===
This is a physical action. The player is acting on the body, environment, or items.

【Core constraints】
- HP/SAN changes, item gains/losses, and spatial movement are allowed
- NPC dialogue should be brief, focusing on physical actions (drawing swords, fleeing, falling)
- To move to an existing area, reference an adjacent location ID in [Spatial map awareness]; entering this branch's new area requires movement.target_room_ref referring to new_room.ref
- For discovery only, use new_room; for actual entry also provide movement. Reveal existing locations/passages with reveal_room/reveal_edge and unlock using unlock_edge

【thought_process must include these five reasoning steps】
1. Feasibility: do the character's current HP/SAN/items support this action?
2. Worldview compliance: does this action violate physical laws or world rules?
3. Spatial compliance: is the action reachable from the current position?
4. Physical consequences: what direct physical effects will the environment and present characters experience?
5. Chain reactions: what subsequent events will this action trigger?

Simulate 2-4 outcome branches. Each branch's node_content (about 60 characters) must include key actions, damage severity, environmental changes, and other essential anchors.
[Extremely important] Each branch must contain its own stat_changes / entity_updates / map_actions / npc. Different outcomes can cause entirely different HP changes and spatial movements.
【Field omission】Omit unchanged fields rather than outputting empty arrays or null.
【Likelihood assessment】Each branch must output likelihood as "very high/high/medium/low/very low (reason)". Base the reason on character attributes, equipment, and physical feasibility, within 15 characters. Do not output numeric probabilities."""

_INSTRUCTION_MIXED = """
=== Simulation instructions ===
The player has taken an unexpected action. First conduct rigorous logical self-checks in thought_process, then output the simulation results.

【thought_process must include these five reasoning steps（one sentence per step）】
1. Character state: do the actor's HP/SAN/negative states limit the action?
2. Worldview compliance: does the action violate the worldview's physics, taboos or social rules?
3. Spatial compliance: if the map is enabled, is the action reachable from the current location?
4. NPC motivation: how will NPCs in the scene react and what are their motivations?
5. Outcome simulation: based on the analysis, what is a plausible outcome?

After these self-checks, simulate 2-4 plausible branches. Each branch's node_content (about 60 characters) should present core causal details.

【Extremely important】Each branch's side-effect fields must be inside that branch object, not global fields.
Different branches may yield completely different results (e.g. losing HP in branch A but not B). The system applies only the selected branch's effects.
Moving to an existing area requires an adjacent location ID from [Spatial map awareness]; to enter a new area in this branch, use movement.target_room_ref referring to new_room.ref.
Discovery is not entry. Use reveal_room/reveal_edge for previously undiscovered registered locations and passages, and unlock_edge for unlocking.
【Field omission】Omit unchanged fields rather than outputting empty arrays or null.
【Likelihood assessment】Each branch must output likelihood as "very high/high/medium/low/very low (reason)". Assess it using the five reasoning steps in thought_process, with a reason under 15 characters. Do not give numeric probabilities."""


def _build_dynamic_system_prompt(worldview, party_status, relevant_lore,
                                  session_memory, l1_context,
                                  world_entities_text, rag_context, map_context,
                                  is_timeline=False, tl_label="",
                                  action_type="mixed",
                                  gm_correction="",
                                  mood="",
                                  force_thrust=False):
    """Build the simulation system prompt. Switch context strategy and instructions according to action_type."""
    prefix = f'You are the tabletop RPG GM, simulating a team in the same world: "{tl_label}".' if is_timeline else "You are a tabletop RPG GM."
    persona = ""
    if _build_persona_instruction:
        persona = _build_persona_instruction()

    # GM correction block (appended to the prompt when nonempty)
    gm_correction_block = ""
    if gm_correction and gm_correction.strip():
        gm_correction_block = f"""

=== 【GM correction instruction — highest priority, must be followed】===
The GM was dissatisfied with the previous simulation and explicitly requires the following this time:
{gm_correction.strip()}
You must follow the correction exactly. If it conflicts with the worldview, the GM's instruction takes precedence."""

    # Mood preset block
    mood_block = ""
    if mood and mood.strip():
        mood_block = f"""

=== 【Mood instructions】===
The narrative mood of this simulation is "{mood.strip()}"; branch descriptions, NPC tone, and environmental details must match this mood."""

    # Narrative momentum block (one-time)
    thrust_block = ""
    if force_thrust:
        thrust_block = """

=== 【System instruction: advance the story】===
The GM wants faster pacing. In this simulation:
- At least one branch must include an NPC proactively introducing a new clue, sudden event, or pointed question
- At least one branch must retain the existing pace as an option for the player"""

    # ── Choose the context strategy based on action_type ──
    if action_type == "dialogue":
        # Dialogue mode: omit map context and emphasize persona
        context_block = f"""{prefix}
Strictly respect the following information hierarchy; never invent or ignore established facts.

=== First priority: hard constraints (violations are errors)===
[Global worldview — all simulations must remain within this framework and never invent settings outside it]
{worldview}

[Current character states — must be considered, especially SAN and personality traits]
{party_status}

{f"[Known world entities (L2 entity profiles) — follow their current states, especially NPC personality and attitude]{chr(10)}{world_entities_text}" if world_entities_text else ""}

=== Second priority: contextual references ===
{f"【L1 recent simulation snapshots】{chr(10)}{l1_context}" if l1_context else ""}

[Story memory stream]
{session_memory}

【Relevant lore entries】
{relevant_lore}
{f"[Knowledge-base retrieval (L3 long-term memory and background lore)]{chr(10)}{rag_context}" if rag_context else ""}

=== Personality interaction protocol (enhanced dialogue mode) ===
{persona if persona else "Simulate social responses according to the NPC's existing persona, keeping speech patterns, body language and psychological state consistent."}

{_INSTRUCTION_DIALOGUE}

Return strictly valid JSON (thought_process first):
{_DIALOGUE_JSON_SCHEMA}{mood_block}{thrust_block}{gm_correction_block}"""

    elif action_type == "action":
        # Action mode: emphasize map context, omit persona
        context_block = f"""{prefix}
Strictly respect the following information hierarchy; never invent or ignore established facts.

=== First priority: hard constraints (violations are errors)===
[Global worldview — all simulations must remain within this framework and never invent settings outside it]
{worldview}

[Current character states — HP/SAN/items must stay consistent]
{party_status}

{f"[Known world entities (L2 entity profiles) — their current states are authoritative; do not contradict them]{chr(10)}{world_entities_text}" if world_entities_text else ""}
{f"[Spatial map awareness — follow this spatial structure; never invent nonexistent rooms or passages]{chr(10)}{map_context}" if map_context else ""}

=== Second priority: contextual references ===
{f"【L1 recent simulation snapshots】{chr(10)}{l1_context}" if l1_context else ""}

[Story memory stream]
{session_memory}

【Relevant lore entries】
{relevant_lore}
{f"[Knowledge-base retrieval (L3 long-term memory and background lore)]{chr(10)}{rag_context}" if rag_context else ""}

{_INSTRUCTION_ACTION}

Return strictly valid JSON (thought_process first):
{_ACTION_JSON_SCHEMA}{mood_block}{thrust_block}{gm_correction_block}"""

    else:
        # Mixed mode: retain the existing behavior
        context_block = f"""{prefix}
Strictly respect the following information hierarchy; never invent or ignore established facts.

=== First priority: hard constraints (violations are errors)===
[Global worldview — all simulations must remain within this framework and never invent settings outside it]
{worldview}

[Current character states — HP/SAN/items must stay consistent]
{party_status}

{f"[Known world entities (L2 entity profiles) — their current states are authoritative; do not contradict them]{chr(10)}{world_entities_text}" if world_entities_text else ""}
{f"[Spatial map awareness — follow this spatial structure; never invent nonexistent rooms or passages]{chr(10)}{map_context}" if map_context else ""}

=== Second priority: contextual references ===
{f"【L1 recent simulation snapshots】{chr(10)}{l1_context}" if l1_context else ""}

[Story memory stream]
{session_memory}

【Relevant lore entries】
{relevant_lore}
{f"[Knowledge-base retrieval (L3 long-term memory and background lore)]{chr(10)}{rag_context}" if rag_context else ""}
{persona}

{_INSTRUCTION_MIXED}

Return strictly valid JSON (thought_process first):
{_DYNAMIC_OPTIONS_JSON_SCHEMA}{mood_block}{thrust_block}{gm_correction_block}"""

    return context_block + WORLD_PROMPT + '\n' + NARRATIVE_RULE


# ---------------------------------------------------------
# Simulation result postprocessing (NPC / stat / map / entity / memory)
# ---------------------------------------------------------
def _post_process_dynamic_result(conn, parsed: dict, scene_name: str,
                                  player_action: str, current_node_id: int,
                                  timeline_id: int | None = None,
                                  tl_id_for_memory: int | None = None,
                                  action_type: str = "mixed"):
    """
    Phase 1：Only create draft branches after simulation.
    AI returns a 60-character node_content outline and effects, then creates nodes and options.
    Serialize effects to pending_effects and apply them only after the player chooses.

    Frontend call chain:
      Phase 1: Simulation -> _post_process (create nodes + pending_effects)
      Phase 2: expand-branch（Expand candidate, await confirmation）
      Phase 3: commit-scene (confirm content and world changes, then apply rules)
    """
    thought_process = prose_renderer(conn, timeline_id)(str(parsed.get("thought_process", ""))) if isinstance(parsed, dict) else ""

    branches = validate_branches(parsed)
    if not branches:
        return [], None, [], {}, thought_process, ""

    if not conn.execute("SELECT id FROM nodes WHERE id = ?", (current_node_id,)).fetchone():
        raise ValueError("Node does not exist")

    new_options = []
    token = parsed.get("_state_token") or state_token(conn)
    if token != state_token(conn):
        raise ValueError("World updated; regenerate")
    for branch_index, branch in enumerate(branches):
        clean_branch_prose(branch, prose_renderer(conn, timeline_id, branch))
        if parsed.get('_summary_contract') and len(branch['node_content']) > 100:
            raise ValueError('Normalized branch summary must not exceed 100 characters')
        n_name = branch.get("node_name", "Unknown node")[:50]
        n_content = branch.get("node_content", "")[:1000]
        o_text = branch.get("text", n_name)[:100]

        # ── Collect side effects (prefer per-branch, fall back to global; missing fields default to empty) ──
        b_stat_changes = branch.get("stat_changes", [])
        b_entity_updates = branch.get("entity_updates", "")
        b_map_actions = branch.get("map_actions")
        b_npc = branch.get("npc")
        b_emotion_deltas = branch.get("emotion_deltas") or []
        b_npc_memories = branch.get("npc_memories") or []

        # ── Dialogue-mode guard ──
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
            "skeleton": n_content,  # Original 60-character outline; frozen here, unaffected by expand-branch
        }
        if parsed.get('_summary_contract'):
            fx_payload.update(summary_contract=parsed['_summary_contract'],
                              spatial_context=parsed['_spatial_context'], request_input=parsed['_request_input'])
        if parsed.get('_npc_review_id'):
            from npc_review import seal
            seal(conn, fx_payload, parsed['_npc_review_id'], branch_index)

        summary_text = o_text[:100]

        # 60-character short narrative: content contains the essential outline; expand-branch polishes it after selection
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
# Phase 3: after user approval of expanded content, commit effects and rules atomically
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
# Phase 2: after selecting a summary, expand candidate content and await approval
# （Core endpoint for short narrative outlines: prose expansion）
#
# [Call order] summary -> expand-branch (server-side candidate effects) -> user confirmation -> commit-scene.
#   Expansion is still a draft: no world events, memory, or trigger effects are created.
# ---------------------------------------------------------
class ExpandBranchRequest(BaseModel):
    timeline_id: int | None = None
    node_id: int              # Node ID selected by player
    scene_name: str = ""      # Parent scene name
    scene_content: str = ""   # Parent scene content (essential context)
    player_action: str = ""   # Player action
    model: str | None = None  # Per-request model selection (optional)
    fx_context: str = ""      # Legacy-client compatibility; actual data comes from server-confirmed events
    action_type: str = ""     # Legacy-client compatibility; actual data comes from server-confirmed events

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
    Extract effect context and action_type from the pending_effects record.
    Return (fx_context_str, action_type).
    Shared by non-streaming and streaming expand-branch endpoints.
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
        if sc.get("inventory_append"): parts.append(f"Gained [{sc['inventory_append']}]")
        if sc.get("inventory_remove"): parts.append(f"Lost [{sc['inventory_remove']}]")
        if parts:
            fx_parts.append(f"{sc.get('char_name', '')}: {', '.join(parts)}")

    if fx.get("entity_updates"):
        fx_parts.append(f"Entity changes: {fx['entity_updates']}")

    ma = fx.get("map_actions")
    if ma and isinstance(ma, dict):
        fx_parts.append('Candidate map actions (discovery and entry are separate): '+encode(ma))
        if ma.get("movement"):
            fx_parts.append(f"Move to: {ma['movement'].get('target_room_label', '')}")
        if ma.get("new_room"):
            fx_parts.append(f"Discover new area: {ma['new_room'].get('label', '')}")

    npc = fx.get("npc")
    if npc and isinstance(npc, dict):
        fx_parts.append(f"NPC introduction: {npc.get('name', '')} — {npc.get('backstory', '')}")

    if fx.get("new_entities"):
        fx_parts.append("Candidate profiles for new entities: " + encode(fx["new_entities"]))
    for field, label in (('emotion_deltas','Candidate emotion changes'),('npc_memories','Candidate character memories'),
                         ('entity_changes','Candidate entity changes'),('state_definitions','Candidate state definitions'),('state_changes','Candidate dynamic states')):
        if fx.get(field): fx_parts.append(label + '：' + encode(fx[field]))
    if fx.get('deferred_effects'):
        fx_parts.append('The targets of the following changes are unresolved and execution is deferred; do not invent specific stat, item or relationship outcomes from them: ' + encode(fx['deferred_effects']))

    fx_context = ("【Candidate outcomes for this branch】\n" + "\n".join(fx_parts)) if fx_parts else "【This branch proposes no additional side effects】"
    return fx_context, action_type


def _build_expand_prompts(conn, req, node_name: str, current_content: str,
                           fx_context: str, action_type: str) -> tuple[str, str]:
    """
    Build the expand-branch system/user prompts.
    Inject full context: worldview, characters, memory stream, lore entries, entities, map, parent scene content, and effects.
    """
    ctx = build_system_context(conn, req.scene_name, node_name, req.player_action, timeline_id=req.timeline_id)
    worldview, party_status, relevant_lore, session_memory, \
        l1_context, world_entities_text, rag_context, map_context = ctx

    if action_type == "dialogue":
        style_hint = "[Immersive dialogue]: Besides dialogue (enclosed in quotation marks), add changes in expression, pauses, and habitual gestures. Setting details should form a natural backdrop appropriate to the current scene's mood (everyday, serious, or lighthearted)."
    elif action_type == "action":
        style_hint = "【Action and perception】：Enrich actions with process details and direct sensory experience (texture, ambient noise, temperature). Setting descriptions should interact with character actions (e.g. scraping a chair or lifting a steaming plate), not become a list of scenery."
    else:
        style_hint = "[Integrated simulation]: Weave together action, dialogue and surroundings naturally and fluidly. Maintain immersion appropriate to the scene: grounded and warm in everyday moments, fast-paced in crises."

    system_prompt = f"""You are the tabletop RPG GM. Write a complete, engaging scene description for the branch selected by the player.

[Global worldview]
{worldview}

[Current character states]
{party_status}

{f"[Known world entities]{chr(10)}{world_entities_text}" if world_entities_text else ""}
{f"【Spatial map awareness】{chr(10)}{map_context}" if map_context else ""}
{f"[Relevant lore entries]{chr(10)}{relevant_lore}" if relevant_lore and relevant_lore != "None" else ""}
{f"【Recent story memory】{chr(10)}{session_memory}" if session_memory else ""}

{f"【L1 recent simulation snapshots】{chr(10)}{l1_context}" if l1_context else ""}
{f"[Encyclopedia evidence]{chr(10)}{rag_context}" if rag_context else ""}

{fx_context}

【Writing requirements】
- Polish and expand the [Branch outline] below into immersive prose.
- Flexible length: let the events determine length naturally, whether 30-50 words or over a hundred; never pad the writing.
- {style_hint}
- Current structured states and confirmed events are authoritative; explicitly flag any contradiction instead of inventing a resolution.
- Preserve all important details from the outline (actions, damage severity, environmental elements) without contradiction
- Remain consistent with numeric effects in [Candidate outcomes]; do not invent additional HP, item, or spatial changes
- Follow approved candidate profiles for new characters; do not introduce unregistered characters during expansion.
- Continue the parent scene's content and mood; maintain narrative flow with a natural conclusion or an open ending
- Output plain scene prose, not JSON
- Do not add any prefix, title or explanation"""
    system_prompt += '\n' + NARRATIVE_RULE

    # User prompt contains the full causal chain: parent scene -> player action -> outline -> expansion request
    user_parts = []
    if req.scene_name:
        user_parts.append(f"Parent scene name: {req.scene_name}")
    if req.scene_content:
        user_parts.append(f"Parent scene content: {req.scene_content[:300]}")
    if req.player_action:
        user_parts.append(f"Player action: {req.player_action}")
    user_parts.append(f"Branch title: {node_name}")
    user_parts.append(f"Branch outline (polish and expand this): {current_content}")
    user_parts.append("Polish the outline above into a complete, immersive scene description consistent with the current mood.")

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
    gm_correction: str = ""  # GM correction instructions (when regenerating)
    action_type: str = "auto"  # "dialogue" | "action" | "mixed" — separate action economy
    model: str | None = None   # Per-request model choice (optional; overrides server default)
    mood: str = ""             # Narrative mood preset (e.g. "horror"/"epic"; empty means no intervention)
    force_thrust: bool = False  # GM narrative push: insert story-advancing instructions (one-time)

class ExpandTextRequest(BaseModel):
    node_id: int | None = None
    scene_name: str = ""
    content: str = ""
    model: str | None = None   # Per-request model selection (optional)
    timeline_id: int | None = None


# ---------------------------------------------------------
# SSE simulation streaming endpoint (core)
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
# Expansion endpoint
# ---------------------------------------------------------
@agent_router.post("/api/ai/expand-text")
def expand_scene_text(request:ExpandTextRequest):
    from agent_operations import preview
    return preview(request,stream=False)


# ---------------------------------------------------------
# Streaming expansion endpoint
# ---------------------------------------------------------
@agent_router.post("/api/ai/expand-text/stream")
def expand_scene_text_stream(request:ExpandTextRequest):
    from agent_operations import preview
    return preview(request,stream=True)


##########################################################################
# Append this route to the end of agent.py
@agent_router.post("/api/ai/npc-chat/stream")
def npc_chat_stream(request:NPCChatRequest):
    from agent_operations import npc_chat
    return npc_chat(request)


# ── Request model ──────────────────────────────────────────────────────────────────
class NPCChatCommitRequest(BaseModel):
    operation_key: str = Field(default_factory=lambda: __import__("uuid").uuid4().hex)
    npc_name: str = ""
    entity_uid: str | None = None
    timeline_id: int | None = None
    player_message: str      # Player's last message
    npc_reply: str           # Full NPC reply text
    chat_summary: str = ""   # Optional dialogue summary from frontend (inferred by backend if omitted)


@agent_router.post("/api/ai/npc-chat/commit")
def npc_chat_commit(request:NPCChatCommitRequest):
    from agent_operations import npc_commit
    return npc_commit(request)


@agent_router.get("/api/ai/npc-chat/history")
def npc_chat_history(npc_name:str="",limit:int=200,entity_uid:str|None=None,timeline_id:int|None=None):
    from agent_operations import chat_history
    return chat_history(npc_name,limit,entity_uid,timeline_id)

