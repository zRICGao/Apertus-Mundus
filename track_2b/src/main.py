from database import connect_db, atomic
from providers import generation, parse_object
"""
APERTUS-MUNDUS engine — main system module (main.py)
"""


import fastapi
from fastapi import WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field
import uvicorn
import sqlite3
import json
import os
import io
import glob
from datetime import datetime
import urllib.parse
import random
import time
import webbrowser
import threading
import math
from contextlib import closing


app = fastapi.FastAPI(title="Apertus Mundus · World Workbench")

from device_link import DeviceSession, DeviceAccessMiddleware, create_router as create_device_router
device_session = DeviceSession()
app.add_middleware(DeviceAccessMiddleware, session=device_session)

# CORS allows local access only; configure ALLOWED_ORIGINS when deploying
# Example: ALLOWED_ORIGINS=http://localhost:8000,http://192.168.1.100:8000
_default_origins = [
    "http://127.0.0.1:8000",
    "http://localhost:8000",
    "null",  # Support legacy clients opened directly using the file:// protocol
]
_env_origins = os.environ.get("ALLOWED_ORIGINS", "")
_allowed_origins = [o.strip() for o in _env_origins.split(",") if o.strip()] if _env_origins else _default_origins

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=["Content-Type", "Authorization"],
)

# ---------------------------------------------------------
# 【Unified error handling】：All exceptions are normalized to {"status":"error","message":"..."}
# The frontend only needs to check response.status === "error"
# ---------------------------------------------------------
@app.exception_handler(fastapi.HTTPException)
async def http_exception_handler(request, exc: fastapi.HTTPException):
    """Normalize HTTPException responses to JSON instead of FastAPI's default {"detail":"..."}."""
    return JSONResponse(
        status_code=exc.status_code,
        content={"status": "error", "message": exc.detail},
    )

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request, exc: RequestValidationError):
    """Normalize parameter validation errors to JSON"""
    errors = exc.errors()
    msg = "; ".join(f"{e.get('loc',['?'])[-1]}: {e.get('msg','Unknown error')}" for e in errors)
    return JSONResponse(
        status_code=422,
        content={"status": "error", "message": f"Parameter validation failed: {msg}"},
    )

# ---------------------------------------------------------
# 【Modular architecture】：Mount separate map module
# ---------------------------------------------------------
from map import map_router, init_map_tables, set_db_file as map_set_db_file
from map import get_map_context as _get_map_context
from map import auto_place_room as _auto_place_room
from map import export_map_data, import_map_data, clear_map_data
app.include_router(map_router)

# ---------------------------------------------------------
# 【Modular architecture】：Mount the RAG knowledge-base module
# ---------------------------------------------------------
from rag import rag_router, configure_rag, init_rag_tables
from rag import chunk_text as _chunk_text, get_embeddings as _get_embeddings
from rag import rag_retrieve as _rag_retrieve, cosine_similarity as _cosine_similarity
from rag import refresh_vector_cache as _refresh_vector_cache
app.include_router(rag_router)

# ---------------------------------------------------------
# 【Modular architecture】：Mount the AI Agent module (including SSE streaming simulation)
# ---------------------------------------------------------
from agent import agent_router, configure_agent
from agent import build_system_context as get_system_context
app.include_router(agent_router)
from scene_service import scene_router, configure_scene
app.include_router(scene_router)

# ---------------------------------------------------------
# [Modular architecture]: mount the memory system module
# ---------------------------------------------------------
from memory import memory_router, configure_memory
from memory import _l1_append
from memory import l1_get_working_context as _l1_get_working_context
from memory import append_to_memory, _tl_append_memory
from memory import fold_memory_with_ai as _fold_memory_with_ai
from memory import MEMORY_FOLD_THRESHOLD, MEMORY_SUMMARY_LIMIT
app.include_router(memory_router)

# ---------------------------------------------------------
# 【Modular architecture】：Mount trigger module
# ---------------------------------------------------------
from trigger import trigger_router, configure_trigger
app.include_router(trigger_router)

# ---------------------------------------------------------
# [Modular architecture]: mount the world entities module
# ---------------------------------------------------------
from entity import entity_router, configure_entity
from entity import get_world_entities_text as _get_world_entities_text
from entity import ai_extract_and_upsert_entities as _ai_extract_and_upsert_entities
app.include_router(entity_router)

# ---------------------------------------------------------
# 【Modular architecture】：Mount timeline module
# ---------------------------------------------------------
from timeline import timeline_router, configure_timeline
app.include_router(timeline_router)

# ---------------------------------------------------------
# Configuration and absolute-path resolution (fixes blank screens and missing files in packaged builds)
# ---------------------------------------------------------
import sys
import os

# 【Critical fix】：Identify whether running from source or as an executable
if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# [Security]: load API keys from .env or environment variables; never hardcode credentials
# Installation：pip install python-dotenv
# Usage：Create a .env file alongside main.py containing:
#   APERTUS_BASE_URL=https://api.inference.cscs.ch/v1
#   SILICONFLOW_API_KEY=sk-xxx
#   APERTUS_MODEL=swiss-ai/Apertus-v1.5-70B
from runtime_config import load_environment
load_environment(BASE_DIR)

from providers import Settings
generation.configure(Settings.from_env())
client = generation

DB_FILE = os.environ.get("RPG_DB_FILE", os.path.join(BASE_DIR, "rpg_game.db"))
os.makedirs(os.path.dirname(os.path.abspath(DB_FILE)), exist_ok=True)
configure_scene(DB_FILE)
if os.path.isfile(DB_FILE):
    # SQLite backup includes the WAL; a file copy could omit recent confirmed events.
    with closing(sqlite3.connect(DB_FILE)) as _legacy_db:
        _entity_columns={r[1] for r in _legacy_db.execute('PRAGMA table_info(world_entities)')}
        _backup_file=DB_FILE+'.before_world_migration.db'
        if _entity_columns and 'uid' not in _entity_columns and not os.path.exists(_backup_file):
            with closing(sqlite3.connect(_backup_file)) as _backup_db:
                _legacy_db.backup(_backup_db)
        _knowledge_backup=DB_FILE+'.before_encyclopedia_migration.db'
        if _entity_columns and not _legacy_db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='knowledge_sources'").fetchone() and not os.path.exists(_knowledge_backup):
            with closing(sqlite3.connect(_knowledge_backup)) as _backup_db:
                _legacy_db.backup(_backup_db)
CAMPAIGNS_DIR = os.environ.get('RPG_CAMPAIGNS_DIR', os.path.join(BASE_DIR, "campaigns"))
os.makedirs(CAMPAIGNS_DIR, exist_ok=True)

# 【Modular architecture】：Inject DB path into map module
map_set_db_file(DB_FILE)

# 【Modular architecture】：Configure RAG module
configure_rag(DB_FILE, generation.settings.embedding_key, generation.settings.embedding_url)

# [Modular architecture]: configure memory system module
configure_memory(DB_FILE, client, fn_get_embeddings=_get_embeddings)

# [Modular architecture]: configure world entities module
configure_entity(DB_FILE, client)

# 【Modular architecture】：Configure timeline module (defer tl_append_memory injection until startup)
configure_timeline(DB_FILE, client, fn_tl_append_memory=_tl_append_memory,
                   memory_summary_limit=MEMORY_SUMMARY_LIMIT)

# ---------------------------------------------------------
# 【Persona mode】：Load persona_mode.json at startup if present
# ---------------------------------------------------------
def _load_persona_config() -> dict:
    path = os.path.join(BASE_DIR, "persona_mode.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        if cfg.get("enabled"):
            return cfg
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return {}

PERSONA_CONFIG = _load_persona_config()


# ---------------------------------------------------------
# Database initialization
# ---------------------------------------------------------
from contextlib import contextmanager
from logger import get_logger

_log = get_logger("main")

def get_db_connection():
    return connect_db(DB_FILE)


@contextmanager
def safe_db():
    """
    Safe database connection context manager.
    Usage：
        with safe_db() as conn:
            conn.execute(...)
    Ensure connections close on both success and failure, and roll back on exceptions.
    """
    conn = get_db_connection()
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    
    cursor.execute('''CREATE TABLE IF NOT EXISTS nodes (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, summary TEXT, content TEXT)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS options (id INTEGER PRIMARY KEY AUTOINCREMENT, node_id INTEGER, text TEXT, next_node_id INTEGER)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS characters (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, role TEXT, hp INTEGER, san INTEGER, inventory TEXT DEFAULT '', status TEXT DEFAULT 'active')''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS system_state (key TEXT PRIMARY KEY, value TEXT)''')
    
    # [New]: encyclopedia database tables
    cursor.execute('''CREATE TABLE IF NOT EXISTS lorebook (id INTEGER PRIMARY KEY AUTOINCREMENT, keywords TEXT, content TEXT)''')

    # 【Key scene trigger table】
    cursor.execute('''CREATE TABLE IF NOT EXISTS triggers (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        label       TEXT    NOT NULL DEFAULT 'Untitled trigger',
        target_node_id INTEGER NOT NULL,
        mode        TEXT    NOT NULL DEFAULT 'soft',
        cond_type   TEXT    NOT NULL DEFAULT '',
        cond_value  TEXT    NOT NULL DEFAULT '',
        conditions  TEXT    NOT NULL DEFAULT '[]',
        fired       INTEGER NOT NULL DEFAULT 0
    )''')
    # Legacy database compatibility: add the conditions column to triggers if absent
    try:
        cursor.execute("SELECT conditions FROM triggers LIMIT 1")
    except Exception:
        cursor.execute("ALTER TABLE triggers ADD COLUMN conditions TEXT NOT NULL DEFAULT '[]'")

    # 【Parallel timelines】：Timeline table
    cursor.execute('''CREATE TABLE IF NOT EXISTS timelines (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        label           TEXT    NOT NULL DEFAULT 'Timeline',
        color           TEXT    NOT NULL DEFAULT '#5b9cf5',
        current_node_id INTEGER,
        current_room_id INTEGER,
        memory          TEXT    NOT NULL DEFAULT '',
        char_ids        TEXT    NOT NULL DEFAULT '',
        status          TEXT    NOT NULL DEFAULT 'active',
        created_at      TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
    )''')

    # [World entity registry]: shared mutable world state across timelines
    # Core layer preventing the same NPC from being duplicated across timelines
    # entity_type: npc | location | event
    # status:      active(normal)| dead(dead)| moved(left)| resolved(event resolved)
    # last_seen_by: label of the last timeline to interact with the entity, so AI knows who knows what
    cursor.execute('''CREATE TABLE IF NOT EXISTS world_entities (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        entity_type  TEXT NOT NULL DEFAULT 'npc',
        name         TEXT NOT NULL UNIQUE,
        location     TEXT NOT NULL DEFAULT '',
        status       TEXT NOT NULL DEFAULT 'active',
        last_seen_by TEXT NOT NULL DEFAULT '',
        state_desc   TEXT NOT NULL DEFAULT '',
        updated_at   TEXT NOT NULL DEFAULT (datetime('now','localtime')),
        room_id      INTEGER
    )''')

    # 【Local RAG knowledge base】：Managed by the separate rag.py module
    conn.commit()
    init_rag_tables()

    # [Three-level memory system — L1 short-term working memory]
    # Store snapshots for the latest N simulations (scene, player action, AI summary, reasoning)
    # Each record represents a simulation, ordered by time; the oldest are evicted to L3 when over capacity
    cursor.execute('''CREATE TABLE IF NOT EXISTS memory_l1 (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        scene_name  TEXT    NOT NULL DEFAULT '',
        player_action TEXT  NOT NULL DEFAULT '',
        ai_summary  TEXT    NOT NULL DEFAULT '',
        thought_process TEXT NOT NULL DEFAULT '',
        entity_updates TEXT NOT NULL DEFAULT '',
        created_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
    )''')
    # Migration: add timeline_id to legacy databases (silently skip if present)
    try:
        cursor.execute("ALTER TABLE memory_l1 ADD COLUMN timeline_id INTEGER DEFAULT NULL")
    except Exception:
        pass

    # 【Pending side-effects table】：Written in simulation Phase 1, applied in Phase 3 after expansion approval
    # Replaces the old __FX__ mechanism in nodes.summary to prevent damage during manual editing
    cursor.execute('''CREATE TABLE IF NOT EXISTS pending_effects (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        node_id     INTEGER NOT NULL UNIQUE,
        payload     TEXT    NOT NULL DEFAULT '{}',
        created_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
        FOREIGN KEY(node_id) REFERENCES nodes(id) ON DELETE CASCADE
    )''')

    # [Map system]: managed by the separate map.py module
    conn.commit()
    init_map_tables()

    # Smooth migration: add inventory to legacy databases if missing
    try: cursor.execute("ALTER TABLE characters ADD COLUMN inventory TEXT DEFAULT ''")
    except sqlite3.OperationalError: pass
    # Smooth migration: add status to characters
    try: cursor.execute("ALTER TABLE characters ADD COLUMN status TEXT DEFAULT 'active'")
    except sqlite3.OperationalError: pass
    # Smooth migration: add personality to characters (independent of inventory)
    try: cursor.execute("ALTER TABLE characters ADD COLUMN personality TEXT DEFAULT ''")
    except sqlite3.OperationalError: pass
    # Smooth migration: add floor to legacy map_rooms
    try: cursor.execute("ALTER TABLE map_rooms ADD COLUMN floor INTEGER NOT NULL DEFAULT 1")
    except sqlite3.OperationalError: pass
    # Smooth migration: add room_id to world_entities (spatial placement)
    try: cursor.execute("ALTER TABLE world_entities ADD COLUMN room_id INTEGER")
    except sqlite3.OperationalError: pass
    # Smooth migration: add aliases to world_entities (alternate names/descriptions for matching)
    try: cursor.execute("ALTER TABLE world_entities ADD COLUMN aliases TEXT NOT NULL DEFAULT '[]'")
    except sqlite3.OperationalError: pass
    # Smooth migration: add current_room_id to timelines (timeline position)
    try: cursor.execute("ALTER TABLE timelines ADD COLUMN current_room_id INTEGER")
    except sqlite3.OperationalError: pass
    # Smooth migration: add expanded_content to nodes (persist expanded AI narrative)
    try: cursor.execute("ALTER TABLE nodes ADD COLUMN expanded_content TEXT DEFAULT ''")
    except sqlite3.OperationalError: pass

    # 【Mobile chat logs】：Persist private NPC chat messages
    cursor.execute('''CREATE TABLE IF NOT EXISTS npc_chat_logs (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        npc_name   TEXT    NOT NULL,
        sender     TEXT    NOT NULL CHECK(sender IN ('player','npc')),
        message    TEXT    NOT NULL,
        created_at TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
    )''')
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_npc_chat_npc ON npc_chat_logs(npc_name)")
    try: cursor.execute("ALTER TABLE npc_chat_logs ADD COLUMN created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))")
    except sqlite3.OperationalError: pass

    # [Time rollback]: game state snapshots, retaining the latest ten
    cursor.execute('''CREATE TABLE IF NOT EXISTS game_checkpoints (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        from_node_id INTEGER NOT NULL,
        snapshot     TEXT    NOT NULL,
        created_at   TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
    )''')

    # [Battle chronicle]: permanently log every player-visited scene, without compaction or eviction
    # For incremental export-battle-report generation, read only entries with id > last_chronicle_position
    cursor.execute('''CREATE TABLE IF NOT EXISTS chronicle_log (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        scene_id          INTEGER NOT NULL DEFAULT 0,
        scene_name        TEXT    NOT NULL DEFAULT '',
        scene_content     TEXT    NOT NULL DEFAULT '',
        expanded_content  TEXT    NOT NULL DEFAULT '',
        player_action     TEXT    NOT NULL DEFAULT '',
        created_at        TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
    )''')
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_chronicle_scene ON chronicle_log(scene_id)")
    cursor.execute("INSERT OR IGNORE INTO system_state (key, value) VALUES ('last_chronicle_position', '0')")

    # Initialize the local story memory stream
    cursor.execute("INSERT OR IGNORE INTO system_state (key, value) VALUES ('session_memory', '[Tabletop RPG memory log initialized]\n')")
    # Spectator screen synchronizationState
    cursor.execute("INSERT OR IGNORE INTO system_state (key, value) VALUES ('player_current_scene_id', '')")
    cursor.execute("INSERT OR IGNORE INTO system_state (key, value) VALUES ('player_scene_ai_text', '')")
    cursor.execute("INSERT OR IGNORE INTO system_state (key, value) VALUES ('player_bgm_url', '')")
    cursor.execute("INSERT OR IGNORE INTO system_state (key, value) VALUES ('player_bgm_name', '')")

    conn.commit()
    conn.close()

init_db()
from world import init_world, refresh_wiki, snapshot, restore, record_event, link_character, state_token
from encyclopedia_api import router as encyclopedia_router
app.include_router(encyclopedia_router)
from world_api import world_router, configure_world
from config_api import config_router, configure_settings_api
with safe_db() as _world_conn:
    init_world(_world_conn)
    _world_conn.commit()
configure_world(DB_FILE)
configure_settings_api(BASE_DIR, lambda: configure_rag(DB_FILE, generation.settings.embedding_key, generation.settings.embedding_url))
app.include_router(world_router)
app.include_router(config_router)


@app.get('/api/health')
def health():
    """Application/database liveness only; never contacts a model service."""
    with safe_db() as conn:
        conn.execute('SELECT 1').fetchone()
    return {'status': 'success', 'database': 'ok'}

# ---------------------------------------------------------
# 【Modular architecture】：Configure Agent module (inject all dependencies)
# Defer to the startup event so all functions are defined
# ---------------------------------------------------------
@app.on_event("startup")
def _startup_wire_agent():
    configure_agent(
        db_file=DB_FILE,
        generation_client=client,
        persona_config=PERSONA_CONFIG,
        fn_get_map_context=_get_map_context,
        fn_auto_place_room=_auto_place_room,
        fn_process_map_actions=_process_map_actions,
        fn_get_current_room_id=_get_current_room_id,
        fn_ai_extract_and_upsert_entities=_ai_extract_and_upsert_entities,
        fn_build_persona_instruction=_build_persona_instruction,
        fn_l1_append=_l1_append,
        fn_l1_get_working_context=_l1_get_working_context,
        fn_append_to_memory=append_to_memory,
        fn_tl_append_memory=_tl_append_memory,
        fn_get_world_entities_text=_get_world_entities_text,
        fn_rag_retrieve=_rag_retrieve,
        fn_get_embeddings=_get_embeddings,
        fn_refresh_vector_cache=_refresh_vector_cache,
    )
    # Trigger module needs get_system_context (from agent); inject after agent setup
    configure_trigger(
        db_file=DB_FILE,
        generation_client=client,
        fn_get_system_context=get_system_context,
        fn_append_to_memory=append_to_memory,
    )
    from wiki_jobs import start_worker
    start_worker(DB_FILE)

# ---------------------------------------------------------
# Data model definitions (for API request bodies; CRUD models moved to their respective modules)
# ---------------------------------------------------------
class AIContextRequest(BaseModel): scene_name: str = ""; content: str = ""
# DynamicActionRequest Moved to agent.py
class CharUpdateRequest(BaseModel):
    name: str | None = None
    hp: int; san: int; inventory: str = ""; personality: str = ""
    status: str = "active"
class NodeCreateRequest(BaseModel): name: str; summary: str; content: str
class NodeUpdateRequest(BaseModel):
    name: str
    summary: str
    content: str
    expected_fields: list[str] | None = None
class OptionCreateRequest(BaseModel): node_id: int; text: str; next_node_id: int
class StringContentRequest(BaseModel): content: str
class LoadCampaignRequest(BaseModel): filename: str
class LorebookRequest(BaseModel): keywords: str; content: str
class CharCreateRequest(BaseModel):
    name: str
    role: str = "PC"
    hp: int = 100
    san: int = 80
    inventory: str = ""
    personality: str = ""
    status: str = "active"
    entity_uid: str | None = None
    expected_token: str | None = None
class AutoNPCRequest(BaseModel):
    scene_name: str = ''
    scene_content: str = ''
    player_action: str = ''
    current_node_id: int | None = None
    timeline_id: int | None = None
    operation_key: str | None = Field(default=None, min_length=1, max_length=200)


# 【Multi-timeline simulation】：Data models (CRUD models moved to timeline.py)
class TimelineDynamicRequest(BaseModel):
    timeline_id: int | None = None
    current_node_id: int
    scene_name: str = ""
    content: str = ""
    player_action: str = ""
    action_type: str = "auto"

# [Map system]: data models moved to map.py (registered automatically through map_router)

# ---------------------------------------------------------
# API endpoints：Campaign and save management (supports modular folder structure)
# ---------------------------------------------------------
# Folder structure：
#   campaigns/
#     My campaign/
#       campaign.json    <- Campaign (nodes, characters, worldview, triggers, etc.)
#       map.json         ← Map (rooms, passages)
#       knowledge/       ← Knowledge base
#         *.txt / *.md   ← RAG documents
#   *.json               <- Legacy single-file saves stored directly in BASE_DIR
# ---------------------------------------------------------

@app.get("/api/campaigns")
def list_campaigns():
    """List loadable campaigns (legacy single files and new folder-based format)"""
    from campaigns import is_campaign_file
    results = []

    # Legacy: *.json under BASE_DIR (backward compatible)
    for f in glob.glob(os.path.join(BASE_DIR, "*.json")):
        fname = os.path.basename(f)
        if fname.startswith("persona_mode") or not is_campaign_file(f): continue
        results.append({"name": fname, "type": "legacy", "path": fname})

    # New: subfolders under campaigns/ containing campaign.json
    if os.path.isdir(CAMPAIGNS_DIR):
        for d in sorted(os.listdir(CAMPAIGNS_DIR)):
            folder = os.path.join(CAMPAIGNS_DIR, d)
            if os.path.isdir(folder) and is_campaign_file(os.path.join(folder, "campaign.json")):
                has_map = os.path.exists(os.path.join(folder, "map.json"))
                kb_dir = os.path.join(folder, "knowledge")
                kb_count = 0
                if os.path.isdir(kb_dir):
                    kb_count = len(
                        glob.glob(os.path.join(kb_dir, "*.txt"))
                        + glob.glob(os.path.join(kb_dir, "*.md"))
                    )
                results.append({
                    "name": d, "type": "folder",
                    "path": f"campaigns/{d}",
                    "has_map": has_map,
                    "kb_count": kb_count
                })

    # Backward compatibility: also return the old array-of-strings format for unupgraded frontends
    files_legacy = [r["path"] for r in results]
    return {"status": "success", "files": results, "files_legacy": files_legacy}

@app.post("/api/game/load")
def load_campaign(req: LoadCampaignRequest):
    from campaigns import load_save
    try:
        return load_save(DB_FILE, BASE_DIR, req.filename, campaigns_dir=CAMPAIGNS_DIR)
    except (ValueError, OSError) as exc:
        raise fastapi.HTTPException(422, str(exc)) from exc


class ExportSaveRequest(BaseModel):
    save_name: str = ""

@app.post("/api/game/export")
def export_campaign(req: ExportSaveRequest = ExportSaveRequest()):
    from campaigns import export_save
    return export_save(DB_FILE, CAMPAIGNS_DIR, req.save_name)


# ---------------------------------------------------------
# [Memory system]: moved to memory.py (registered via app.include_router(memory_router))
# append_to_memory, _l1_append, _l1_get_working_context, _tl_append_memory Imported at the top
# ---------------------------------------------------------

@app.get("/api/game/lorebook")
def get_lorebook():
    with safe_db() as conn:
        r = [dict(row) for row in conn.execute("SELECT * FROM lorebook").fetchall()]
    return {"status": "success", "lorebook": r}

@app.post("/api/game/lorebook")
def create_lore(req: LorebookRequest):
    with safe_db() as conn, atomic(conn):
        conn.execute("INSERT INTO lorebook (keywords, content) VALUES (?, ?)", (req.keywords, req.content))
        refresh_wiki(conn)
        conn.commit()
    return {"status": "success"}

@app.delete("/api/game/lorebook/{lore_id}")
def delete_lore(lore_id: int):
    with safe_db() as conn, atomic(conn):
        conn.execute("DELETE FROM lorebook WHERE id = ?", (lore_id,))
        refresh_wiki(conn)
        conn.commit()
    return {"status": "success"}

# ---------------------------------------------------------
# API endpoints: lorebook, worldview, and node CRUD (preserved)
# ---------------------------------------------------------
@app.get("/api/game/worldview")
def get_worldview():
    with safe_db() as conn:
        r = conn.execute("SELECT value FROM system_state WHERE key = 'worldview'").fetchone()
    return {"status": "success", "content": r["value"] if r else ""}

@app.put("/api/game/worldview")
def update_worldview(req: StringContentRequest):
    with safe_db() as conn, atomic(conn):
        conn.execute("INSERT OR REPLACE INTO system_state (key, value) VALUES ('worldview', ?)", (req.content,))
        refresh_wiki(conn)
        conn.commit()
    return {"status": "success"}

@app.get("/api/game/stat-labels")
def get_stat_labels():
    """Get custom display labels for HP/SAN."""
    with safe_db() as conn:
        hp_row = conn.execute("SELECT value FROM system_state WHERE key='hp_label'").fetchone()
        san_row = conn.execute("SELECT value FROM system_state WHERE key='san_label'").fetchone()
    return {"hp_label": hp_row["value"] if hp_row else "HP", "san_label": san_row["value"] if san_row else "SAN"}

class StatLabelsRequest(BaseModel):
    hp_label: str = "HP"
    san_label: str = "SAN"

@app.put("/api/game/stat-labels")
def update_stat_labels(req: StatLabelsRequest):
    """Update display labels for HP/SAN."""
    with safe_db() as conn:
        conn.execute("INSERT OR REPLACE INTO system_state (key, value) VALUES ('hp_label', ?)", (req.hp_label[:20],))
        conn.execute("INSERT OR REPLACE INTO system_state (key, value) VALUES ('san_label', ?)", (req.san_label[:20],))
        conn.commit()
    return {"status": "success"}

@app.get("/api/game/state")
def get_game_state():
    with safe_db() as conn:
        n = [dict(row) for row in conn.execute("SELECT * FROM nodes").fetchall()]
        o = [dict(row) for row in conn.execute("SELECT * FROM options").fetchall()]
        c = [dict(row) for row in conn.execute("SELECT * FROM characters").fetchall()]
        wv_row = conn.execute("SELECT value FROM system_state WHERE key='worldview'").fetchone()
        worldview = wv_row["value"] if wv_row else ""
        position = conn.execute("SELECT value FROM system_state WHERE key='player_current_scene_id'").fetchone()
        current_node_id = int(position[0]) if position and str(position[0]).isdigit() else None
        pending = {r['node_id']: json.loads(r['payload']) for r in conn.execute('SELECT node_id,payload FROM pending_effects')}
        # Branch ownership survives commit through the stable branch operation key.
        committed = {int(r['operation_key'].split(':')[1]): r['timeline_id'] for r in conn.execute(
            "SELECT operation_key,timeline_id FROM world_events WHERE kind='branch_committed' AND operation_key LIKE 'branch:%'")}
        token = state_token(conn)
    draft_ids = {node['id'] for node in n if node['is_draft']}
    for opt in o:
        target = opt['next_node_id']
        draft = pending.get(target)
        is_draft = target in draft_ids
        opt['is_generated'] = draft is not None or target in committed or is_draft
        opt['timeline_id'] = draft.get('timeline_id') if draft else committed.get(target)
        opt['available'] = not opt['is_generated'] or (draft is not None and draft.get('state_token') == token)
    for node in n:
        node['npc_additions'] = pending.get(node['id'], {}).get('npc_additions', [])
        node['scene_phase'] = ('EXPANDED' if pending.get(node['id'], {}).get('phase') == 'EXPANDED' else 'DRAFT_SUMMARY') if node['is_draft'] else 'COMMITTED'
        node['all_options'] = [opt for opt in o if opt['node_id'] == node['id']]
        node['options'] = [opt for opt in node['all_options'] if opt['available'] and
                           (not opt['is_generated'] or opt['timeline_id'] is None)]
    return {"status": "success", "nodes": n, "characters": c, "worldview": worldview,
            "current_node_id": current_node_id}

@app.post("/api/game/character/{char_id}")
def update_character(char_id: int, req: CharUpdateRequest):
    with safe_db() as conn, atomic(conn):
        previous=conn.execute('SELECT * FROM characters WHERE id=?',(char_id,)).fetchone()
        if previous is None: raise fastapi.HTTPException(404,'Character not found')
        if req.name is not None and req.name.strip():
            conn.execute("UPDATE characters SET name=?, hp=?, san=?, inventory=?, personality=?, status=? WHERE id=?",
                         (req.name.strip()[:50], req.hp, req.san, req.inventory, req.personality, req.status, char_id))
        else:
            conn.execute("UPDATE characters SET hp=?, san=?, inventory=?, personality=?, status=? WHERE id=?",
                         (req.hp, req.san, req.inventory, req.personality, req.status, char_id))
        uid = link_character(conn, char_id)
        if req.name:
            from world import encode
            entity=conn.execute('SELECT aliases FROM world_entities WHERE uid=?',(uid,)).fetchone()
            aliases=json.loads(entity['aliases'] or '[]')
            if previous['name']!=req.name and previous['name'] not in aliases: aliases.append(previous['name'])
            conn.execute("UPDATE world_entities SET name=?,aliases=? WHERE uid=?", (req.name.strip()[:50],encode(aliases),uid))
        record_event(conn,"character_edit","Update character",{"entity_uid":uid})
        refresh_wiki(conn)
        conn.commit()
    return {"status": "success"}

@app.post("/api/game/node")
def create_node(req: NodeCreateRequest):
    with safe_db() as conn:
        c = conn.execute("INSERT INTO nodes (name, summary, content) VALUES (?,?,?)", (req.name, req.summary, req.content))
        i = c.lastrowid
        conn.commit()
    return {"status": "success", "id": i}

@app.put("/api/game/node/{node_id}")
def update_node(node_id: int, req: NodeUpdateRequest):
    with safe_db() as conn, atomic(conn):
        previous = conn.execute('SELECT name,summary,content FROM nodes WHERE id=?', (node_id,)).fetchone()
        if previous is None:
            raise fastapi.HTTPException(404, 'Scene has been deleted; refresh the workbench')
        if req.expected_fields is not None and req.expected_fields != [previous[key] or '' for key in ('name','summary','content')]:
            raise fastapi.HTTPException(409, 'This scene was modified on another device. Copy your changes, discard this edit to load the latest version, then edit it again.')
        conn.execute("UPDATE nodes SET name=?, summary=?, content=? WHERE id=?", (req.name, req.summary, req.content, node_id))
    return {"status": "success"}

@app.delete("/api/game/node/{node_id}")
def delete_node(node_id: int):
    with safe_db() as conn:
        conn.execute("DELETE FROM nodes WHERE id=?", (node_id,))
        conn.execute("DELETE FROM options WHERE node_id=? OR next_node_id=?", (node_id, node_id))
        conn.commit()
    return {"status": "success"}

@app.post("/api/game/option")
def create_option(req: OptionCreateRequest):
    with safe_db() as conn:
        c = conn.execute("INSERT INTO options (node_id, text, next_node_id) VALUES (?,?,?)", (req.node_id, req.text, req.next_node_id))
        i = c.lastrowid
        conn.commit()
    return {"status": "success", "id": i}

@app.delete("/api/game/option/{option_id}")
def delete_option(option_id: int):
    with safe_db() as conn:
        conn.execute("DELETE FROM options WHERE id=?", (option_id,))
        conn.commit()
    return {"status": "success"}

# ---------------------------------------------------------
# 【New core feature】：Assemble omniscient context across four dimensions
# ---------------------------------------------------------

def _build_persona_instruction() -> str:
    """
    Assemble rules, MBTI traits and quirk pools from persona_mode.json into text that can be injected into a system prompt.
    If persona_mode is disabled, return an empty string without affecting existing logic.
    """
    if not PERSONA_CONFIG:
        return ""
    lines = [PERSONA_CONFIG.get("core_rule", "")]
    npc_rule = PERSONA_CONFIG.get("npc_generation_rule", "")
    if npc_rule:
        lines.append(npc_rule)
    mbti_pool = PERSONA_CONFIG.get("mbti_pool", [])
    if mbti_pool:
        lines.append("【Available MBTI types】" + " | ".join(mbti_pool))
    quirk_pool = PERSONA_CONFIG.get("quirk_pool", [])
    if quirk_pool:
        lines.append("【Available behavioral quirks】" + " | ".join(quirk_pool))
    return "\n".join(filter(None, lines))


# ---------------------------------------------------------
# 【World Entities】：Moved to entity.py
# _get_world_entities_text, _ai_extract_and_upsert_entities Imported at the top
# ---------------------------------------------------------


# =============================================================
# [RAG knowledge-base engine]: moved to rag.py
# =============================================================


# _get_map_context and _auto_place_room were moved to map.py
# Imported above via from map import get_map_context as _get_map_context
# ---------------------------------------------------------
# [Map-AI integration]: process map_actions returned by AI
# ---------------------------------------------------------
def _process_map_actions(conn,parsed,current_room_id,timeline_id=None,scene_node_id=None):
    from map_effects import apply_map_actions
    return apply_map_actions(conn,parsed,current_room_id,timeline_id,scene_node_id)


def _get_current_room_id(conn, timeline_id: int | None = None) -> int | None:
    """Get current room ID (supports timeline mode)"""
    if timeline_id:
        tl = conn.execute(
            "SELECT current_room_id FROM timelines WHERE id=?", (timeline_id,)
        ).fetchone()
        return tl["current_room_id"] if tl and tl["current_room_id"] else None
    else:
        row = conn.execute(
            "SELECT value FROM system_state WHERE key='current_room_id'"
        ).fetchone()
        return int(row["value"]) if row and row["value"] else None


# get_system_context, expand_scene_text, generate_dynamic_options
# Moved to agent.py (registered via app.include_router(agent_router))

# ---------------------------------------------------------
# API endpoints：Character management (create/delete)
# ---------------------------------------------------------
@app.post("/api/game/character")
def create_character(req: CharCreateRequest):
    with safe_db() as conn, atomic(conn):
        from world import state_token
        if req.expected_token and req.expected_token!=state_token(conn):
            raise fastapi.HTTPException(409,'World updated; new character was not created; try again')
        uid = req.entity_uid
        if uid:
            from world import resolve_entity
            resolve_entity(conn, uid=uid)
            if conn.execute("SELECT id FROM characters WHERE entity_uid=?", (uid,)).fetchone():
                raise fastapi.HTTPException(409, "Entity is already linked to a character; use the update endpoint")
        char_id = conn.execute("INSERT INTO characters(name,role,hp,san,inventory,personality,status,entity_uid) VALUES(?,?,?,?,?,?,?,?)",
            (req.name,req.role,req.hp,req.san,req.inventory,req.personality,req.status,uid)).lastrowid
        uid = link_character(conn, char_id, uid, new=True)
        record_event(conn, "character_create", f"Character {req.name} registered", {"entity_uid":uid})
        refresh_wiki(conn)
    return {"status":"success", "id":char_id, "entity_uid":uid}


@app.delete("/api/game/character/{char_id}")
def delete_character(char_id: int):
    with safe_db() as conn:
        result = conn.execute("SELECT id FROM characters WHERE id = ?", (char_id,)).fetchone()
        if not result:
            raise fastapi.HTTPException(status_code=404, detail="Character not found")
        conn.execute("DELETE FROM characters WHERE id = ?", (char_id,))
        conn.commit()
    return {"status": "success"}

@app.post("/api/ai/generate-npc")
def generate_npc(request: AutoNPCRequest):
    from npc_review import generate_npc as reviewed_generation
    try:
        return reviewed_generation(request)
    except ValueError as exc:
        raise fastapi.HTTPException(422, str(exc)) from exc


from npc_review import router as npc_review_router
app.include_router(npc_review_router)


# ---------------------------------------------------------
# API endpoints: automatically check whether a new NPC is required on scene navigation
# ---------------------------------------------------------
class CheckNPCRequest(BaseModel):
    scene_name: str
    scene_content: str

@app.post("/api/ai/check-npc")
def check_npc_on_enter(request:CheckNPCRequest):
    # Named identities need an explicit decision; entering a scene never silently spawns duplicates.
    with safe_db() as conn:
        ctx=get_system_context(conn,request.scene_name,request.scene_content)
    from providers import evaluate
    result=evaluate({"scene":request.scene_content,"entities":ctx[5]}, {"new_npc":{"type":"noul",
          "instructions":"Does this scene explicitly require an unregistered new NPC to appear now? Do not mistake existing entities for new identities."}})['new_npc']
    return {"status":"success","spawned_npc":None,"suggest_generation":result.status=='ok' and result.value>=.9}


# ---------------------------------------------------------
# [Trigger system]: moved to trigger.py (registered via app.include_router(trigger_router))
# ---------------------------------------------------------

# ---------------------------------------------------------
# ---------------------------------------------------------


# ---------------------------------------------------------
# API endpoints：Battle report/novel export
# ---------------------------------------------------------
@app.get("/api/debug/battle-report-state")
def debug_battle_report_state():
    conn = get_db_connection()
    pos_row   = conn.execute("SELECT value FROM system_state WHERE key='last_chronicle_position'").fetchone()
    pend_row  = conn.execute("SELECT value FROM system_state WHERE key='pending_regenerate'").fetchone()
    count_row = conn.execute("SELECT COUNT(*) as cnt, MIN(id) as min_id, MAX(id) as max_id FROM chronicle_log").fetchone()
    conn.close()
    return {
        "last_chronicle_position": pos_row["value"] if pos_row else None,
        "pending_regenerate":      pend_row["value"] if pend_row else None,
        "chronicle_log_count":     count_row["cnt"],
        "chronicle_log_id_range":  f"{count_row['min_id']} ~ {count_row['max_id']}",
    }

# ---------------------------------------------------------
@app.post("/api/ai/export-battle-report")
def export_battle_report():
    """
    Incremental battle-report generation：
      - Inputs are no longer from session_memory, but from new scene snapshots in chronicle_log with id > last_chronicle_position
        (each with scene name, content, expanded_content after investigation, and player action)
      - After generation, advance last_chronicle_position to the highest processed ID
      - Save each output to a new .md file without regenerating previously written text
    """
    conn = get_db_connection()
    pos_row = conn.execute(
        "SELECT value FROM system_state WHERE key='last_chronicle_position'"
    ).fetchone()
    last_pos = int(pos_row["value"]) if pos_row and pos_row["value"].isdigit() else 0

    rows = conn.execute(
        "SELECT * FROM chronicle_log WHERE id > ? ORDER BY id ASC", (last_pos,)
    ).fetchall()

    wv_row = conn.execute("SELECT value FROM system_state WHERE key='worldview'").fetchone()
    chars  = conn.execute("SELECT name, role FROM characters").fetchall()

    if not rows:
        regen_row = conn.execute(
            "SELECT value FROM system_state WHERE key='pending_regenerate'"
        ).fetchone()
        pending = regen_row and regen_row["value"] == "1"

        if not pending:
            # On first request with no new scenes, notify user and set flag for regeneration
            conn.execute(
                "INSERT OR REPLACE INTO system_state (key, value) VALUES ('pending_regenerate', '1')"
            )
            conn.commit()
            conn.close()
            return {
                "status": "success",
                "report": "(No new scenes since the previous generation; nothing to continue.)",
                "filename": "",
            }

        # On second request (user selected regenerate), regenerate all scenes from the previous chapter
        if last_pos > 0:
            rows = conn.execute(
                "SELECT * FROM chronicle_log WHERE id <= ? ORDER BY id ASC", (last_pos,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM chronicle_log ORDER BY id ASC"
            ).fetchall()
        if not rows:
            conn.execute(
                "INSERT OR REPLACE INTO system_state (key, value) VALUES ('pending_regenerate', '0')"
            )
            conn.commit()
            conn.close()
            return {
                "status": "success",
                "report": "(No scene records available for generation.)",
                "filename": "",
            }
        is_regenerate = True
    else:
        is_regenerate = False
        # Clear flag when new scenes become available
        conn.execute(
            "INSERT OR REPLACE INTO system_state (key, value) VALUES ('pending_regenerate', '0')"
        )

    worldview = wv_row["value"] if wv_row else ""
    char_list = ", ".join([c["name"] for c in chars]) or "Unknown characters"

    # Assemble structured input: scene-by-scene in chronological order
    parts = []
    for i, r in enumerate(rows, 1):
        parts.append(f"## Scene {i}：{r['scene_name'] or 'UntitledScene'}")
        if r["player_action"]:
            parts.append(f"**Player action**: {r['player_action']}")
        if r["scene_content"]:
            parts.append(f"**Scene description**:\n{r['scene_content']}")
        if r["expanded_content"]:
            parts.append(f"**Detailed investigation**：\n{r['expanded_content']}")
        parts.append("")  # Separate with blank lines
    structured_input = "\n".join(parts)

    is_continuation = last_pos > 0 and not is_regenerate
    system_prompt = (
        "You are a talented fantasy novelist. Below are several scene snapshots in chronological order,"
        "each containing the player's actions, scene descriptions, and optional in-depth investigation details."
        "Polish them into a beautifully formatted and written fantasy battle-report/novel chapter. Requirements:"
        "- Use Markdown with a title and sectioned paragraphs. Let length follow the number of scenes without arbitrary truncation"
        "- Follow the given scene chronology strictly, preserving names, locations, and key events without changing the plot"
        "- Use details from the in-depth investigation to enrich setting and atmosphere"
        "- Write vividly and immersively"
        + (
            "- This is a continuation chapter. Start with a brief transition from the previous chapter, without repeating its events"
            if is_continuation else
            "- This is the opening chapter"
        )
        + '- End with "(End of chapter)"'
        + f"[Worldview background] {worldview}"
        + f"[Main characters] {char_list}"
    )

    try:
        resp = client.chat.completions.create(
            model=generation.settings.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": f"Here are the scene snapshots to polish:\n\n{structured_input}"}
            ],
            temperature=0.8,
            max_tokens=8000,
        )
        report = resp.choices[0].message.content.strip()

        # Write a new file
        report_dir = os.environ.get('RPG_REPORTS_DIR', os.path.join(BASE_DIR, "battle_report"))
        os.makedirs(report_dir, exist_ok=True)
        filename = f"battle_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
        filepath = os.path.join(report_dir, filename)
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(report)

        # Advance position after successful generation (not when regenerating, to allow further retries)
        if not is_regenerate:
            new_pos = rows[-1]["id"]
            conn.execute(
                "INSERT OR REPLACE INTO system_state (key, value) VALUES ('last_chronicle_position', ?)",
                (str(new_pos),)
            )
        # Clear the flag on success for both normal generation and regeneration
        conn.execute(
            "INSERT OR REPLACE INTO system_state (key, value) VALUES ('pending_regenerate', '0')"
        )
        conn.commit()
        conn.close()
        return {"status": "success", "report": report, "filename": filename}
    except Exception as e:
        conn.close()
        return {"status": "error", "message": str(e), "report": ""}


# ---------------------------------------------------------
# API endpoints: API key configuration (frontend can update credentials in .env dynamically)
# ---------------------------------------------------------


# ---------------------------------------------------------
# ---------------------------------------------------------


# =============================================================
# [Multi-timeline CRUD]: moved to timeline.py
# _tl_append_memory Imported at the top
# =============================================================

@app.post("/api/timelines/{tl_id}/dynamic-options")
def timeline_dynamic_options(tl_id: int, req: TimelineDynamicRequest):
    from agent import DynamicActionRequest, dynamic_options_handler
    return dynamic_options_handler(DynamicActionRequest(current_node_id=req.current_node_id,timeline_id=tl_id,
        scene_name=req.scene_name,content=req.content,player_action=req.player_action,action_type=req.action_type))


# =============================================================
# [World entities]: moved to entity.py (registered via app.include_router(entity_router))
# =============================================================

# =============================================================
# [Timeline CRUD/merge]: moved to timeline.py (registered via app.include_router(timeline_router))
# =============================================================


# =============================================================
# [RAG knowledge base]: moved to rag.py (registered via app.include_router(rag_router))
# =============================================================

# =============================================================
# [Map system]: moved to map.py (registered via app.include_router(map_router))
# =============================================================

# ---------------------------------------------------------
# [Map-AI integration]: node-to-room binding lookup (used by frontend jumpToNode)
# ---------------------------------------------------------
@app.get("/api/map/room-by-node/{node_id}")
def get_room_by_node(node_id: int):
    """Look up the room bound to a scene node; called automatically by jumpToNode."""
    with safe_db() as conn:
        room = conn.execute(
            "SELECT id, label FROM map_rooms WHERE node_id=? LIMIT 1", (node_id,)
        ).fetchone()
    if room:
        return {"status": "success", "room_id": room["id"], "label": room["label"]}
    return {"status": "success", "room_id": None}
# =============================================================

# ---------------------------------------------------------
# API endpoints：Spectator screen synchronization（REST polling and WebSocket live updates）
# ---------------------------------------------------------

# 【Spectator WebSocket】：Connection management and broadcasting
import asyncio

_ws_clients: set[WebSocket] = set()

@app.websocket("/ws/player")
async def ws_player_endpoint(websocket: WebSocket):
    """
    Spectator WebSocket connection.
    Send the complete state once upon connection, then broadcast whenever the GM calls push_player_state.
    Clients only need to listen for onmessage; no messages need to be sent.
    """
    await websocket.accept()
    _ws_clients.add(websocket)
    _log.info("Spectator WebSocket connected (%d clients)", len(_ws_clients))
    try:
        # Immediately send the current state after connection
        state = _build_player_state_snapshot()
        await websocket.send_json(state)
        # Keep the connection open until disconnection (clients need not send data, but WebSocket needs a receive loop)
        while True:
            # Wait for client messages (usually none; loop detects disconnects)
            try:
                await asyncio.wait_for(websocket.receive_text(), timeout=30)
            except asyncio.TimeoutError:
                # Heartbeat: send ping every 30 seconds (no client handling needed)
                try:
                    await websocket.send_json({"type": "ping"})
                except Exception:
                    break
    except WebSocketDisconnect:
        pass
    except Exception as e:
        _log.debug("Spectator WebSocket disconnected unexpectedly: %s", e)
    finally:
        _ws_clients.discard(websocket)
        _log.info("Spectator WebSocket disconnected (%d clients remaining)", len(_ws_clients))


def _build_player_state_snapshot() -> dict:
    """Build a full spectator state snapshot (scene, characters, BGM)."""
    with safe_db() as conn:
        def _get(k):
            r = conn.execute("SELECT value FROM system_state WHERE key=?", (k,)).fetchone()
            return r["value"] if r else ""

        scene_id_raw = _get("player_current_scene_id")
        scene_id = int(scene_id_raw) if scene_id_raw and scene_id_raw.isdigit() else None

        # Include scene details when there is a current scene
        current_scene = None
        if scene_id:
            node = conn.execute("SELECT * FROM nodes WHERE id=?", (scene_id,)).fetchone()
            if node:
                options = [dict(o) for o in conn.execute(
                    "SELECT * FROM options WHERE node_id=?", (scene_id,)
                ).fetchall()]
                current_scene = {**dict(node), "options": options}

        characters = [dict(c) for c in conn.execute("SELECT * FROM characters").fetchall()]
        scene_ai_text = _get('player_scene_ai_text') if scene_id else ''
        bgm_url = _get('player_bgm_url') if scene_id else ''
        bgm_name = _get('player_bgm_name') if scene_id else ''

    return {
        "type": "state_update",
        "current_scene_id": scene_id,
        "current_scene": current_scene,
        "scene_ai_text": scene_ai_text,
        "bgm_url": bgm_url,
        "bgm_name": bgm_name,
        "characters": characters,
    }


async def _broadcast_player_state():
    """Broadcast current state to all connected spectator WebSocket clients."""
    if not _ws_clients:
        return
    state = _build_player_state_snapshot()
    dead_clients = set()
    for ws in _ws_clients.copy():
        try:
            await ws.send_json(state)
        except Exception:
            dead_clients.add(ws)
    # Clean up disconnected clients
    _ws_clients.difference_update(dead_clients)


# ---------------------------------------------------------
# Scene navigation log: track when players enter nodes through predefined options
# ---------------------------------------------------------
class SceneVisitRequest(BaseModel):
    node_id:      int
    node_name:    str
    option_text:  str = ""   # Player-selected option text, empty for direct navigation

@app.post("/api/game/log-scene-visit")
def log_scene_visit(req: SceneVisitRequest):
    # Only log predefined-option jumps; direct jumps do not update the memory stream
    if not req.option_text.strip():
        return {"status": "skipped"}
    conn = get_db_connection()
    node = conn.execute('SELECT is_draft FROM nodes WHERE id=?',(req.node_id,)).fetchone()
    if node is None or node['is_draft']:
        conn.close()
        raise fastapi.HTTPException(409,'Unconfirmed branches cannot be written to story history')
    log = f'Player chose "{req.option_text}" -> entered scene [{req.node_name}]'
    append_to_memory(conn, log)

    # Also record this in the battle chronicle: capture current scene content/expanded_content
    try:
        node = conn.execute(
            "SELECT content, expanded_content FROM nodes WHERE id=?", (req.node_id,)
        ).fetchone()
        scene_content   = (node["content"] if node else "") or ""
        expanded_snap   = (node["expanded_content"] if node else "") or ""
        conn.execute(
            "INSERT INTO chronicle_log (scene_id, scene_name, scene_content, "
            "expanded_content, player_action) VALUES (?,?,?,?,?)",
            (req.node_id, req.node_name, scene_content, expanded_snap, req.option_text)
        )
        conn.commit()
    except Exception as e:
        _log.warning("Failed to write chronicle_log: %s", e)

    conn.close()
    return {"status": "success"}


class PlayerStateRequest(BaseModel):
    current_scene_id: int = 0
    scene_ai_text:    str = ""
    bgm_url:          str = ""
    bgm_name:         str = ""

class CheckpointRequest(BaseModel):
    from_node_id: int

@app.post("/api/game/checkpoint")
def create_checkpoint(req: CheckpointRequest):
    with safe_db() as conn, atomic(conn):
        snap = snapshot(conn)
        conn.execute("INSERT INTO game_checkpoints(from_node_id,snapshot) VALUES(?,?)", (req.from_node_id,json.dumps(snap,ensure_ascii=False)))
        conn.execute("DELETE FROM game_checkpoints WHERE id NOT IN (SELECT id FROM game_checkpoints ORDER BY id DESC LIMIT 10)")
    return {"status":"success"}


@app.post("/api/game/rollback")
def rollback_checkpoint():
    with safe_db() as conn, atomic(conn):
        cp = conn.execute("SELECT * FROM game_checkpoints ORDER BY id DESC LIMIT 1").fetchone()
        if not cp:
            return {"status":"error","message":"No available snapshots"}
        data = json.loads(cp["snapshot"])
        if not isinstance(data.get("system_state"),list):
            raise fastapi.HTTPException(409,"Legacy-format snapshot is incomplete; load a save")
        restore(conn,data)
        conn.execute("DELETE FROM game_checkpoints WHERE id=?",(cp["id"],))
        remaining = conn.execute("SELECT COUNT(*) FROM game_checkpoints").fetchone()[0]
    _refresh_vector_cache()
    return {"status":"success","restored_node_id":cp["from_node_id"],"remaining":remaining}


@app.get("/api/player/state")
def get_player_state():
    """REST polling endpoint (backward compatible; unnecessary after spectator clients migrate to WebSocket)."""
    with safe_db() as conn:
        def _get(k):
            r = conn.execute("SELECT value FROM system_state WHERE key=?", (k,)).fetchone()
            return r["value"] if r else ""
        scene_id_raw = _get("player_current_scene_id")
        result = {
            "current_scene_id": int(scene_id_raw) if scene_id_raw and scene_id_raw.isdigit() else None,
            "scene_ai_text":    _get("player_scene_ai_text"),
            "bgm_url":          _get("player_bgm_url"),
            "bgm_name":         _get("player_bgm_name"),
        }
    return result

@app.post("/api/player/state")
async def push_player_state(req: PlayerStateRequest):
    """GM pushes spectator state; after writing to DB, broadcast to all spectator clients via WebSocket."""
    with safe_db() as conn:
        node=conn.execute('SELECT is_draft FROM nodes WHERE id=?',(req.current_scene_id,)).fetchone()
        if node is None or node['is_draft']: raise fastapi.HTTPException(409,'Confirm the scene result first')
        conn.execute("INSERT OR REPLACE INTO system_state (key,value) VALUES ('player_current_scene_id',?)", (str(req.current_scene_id),))
        conn.execute("INSERT OR REPLACE INTO system_state (key,value) VALUES ('player_scene_ai_text',?)",    (req.scene_ai_text,))
        conn.execute("INSERT OR REPLACE INTO system_state (key,value) VALUES ('player_bgm_url',?)",          (req.bgm_url,))
        conn.execute("INSERT OR REPLACE INTO system_state (key,value) VALUES ('player_bgm_name',?)",         (req.bgm_name,))
        conn.commit()
    # WebSocket broadcast (asynchronous and nonblocking)
    await _broadcast_player_state()
    return {"status": "success"}

# ---------------------------------------------------------
# Frontend static file serving (Vite build output -> static-frontend/)
# ---------------------------------------------------------
def _device_current_scene():
    with safe_db() as conn:
        # Explicit scene pushes also represent browsing; ignore an obsolete saved ID.
        row = conn.execute("SELECT n.id FROM nodes n JOIN system_state s ON s.value=CAST(n.id AS TEXT) "
                           "WHERE s.key='player_current_scene_id' AND n.is_draft=0").fetchone()
        if row:
            return row[0]
        first = conn.execute('SELECT id FROM nodes WHERE is_draft=0 ORDER BY id LIMIT 1').fetchone()
        return first[0] if first else None

app.include_router(create_device_router(device_session, _device_current_scene))

_FRONTEND_DIR = os.path.join(BASE_DIR, "static-frontend")
_FRONTEND_ASSETS = os.path.join(_FRONTEND_DIR, "assets")

if os.path.isdir(_FRONTEND_ASSETS):
    app.mount("/assets", StaticFiles(directory=_FRONTEND_ASSETS), name="assets")

app.mount("/frontend_vendor", StaticFiles(directory=os.path.join(BASE_DIR, "frontend_vendor")), name="frontend_vendor")

def _serve_frontend_index():
    spa = os.path.join(_FRONTEND_DIR, "index.html")
    if os.path.exists(spa):
        return FileResponse(spa)
    # Fallback: legacy index.html alongside main.py
    legacy = os.path.join(BASE_DIR, "index.html")
    if os.path.exists(legacy):
        return FileResponse(legacy)
    return JSONResponse({"error": "Frontend file not found; run cd frontend && npm run build first"}, status_code=404)

@app.get("/")
def serve_index():
    return _serve_frontend_index()

@app.get("/frontend_api.js")
def serve_frontend_api():
    return FileResponse(os.path.join(BASE_DIR, "frontend_api.js"), media_type="text/javascript")

@app.get('/frontend_npc_review.js')
def serve_frontend_npc_review():
    return FileResponse(os.path.join(BASE_DIR, 'frontend_npc_review.js'), media_type='text/javascript')

@app.get("/frontend_ui.js")
def serve_frontend_ui():
    return FileResponse(os.path.join(BASE_DIR, "frontend_ui.js"), media_type="text/javascript")

@app.get("/frontend_workbench.css")
def serve_frontend_workbench_css():
    return FileResponse(os.path.join(BASE_DIR, "frontend_workbench.css"), media_type="text/css")

@app.get('/frontend_sync.js')
def serve_frontend_sync():
    return FileResponse(os.path.join(BASE_DIR, 'frontend_sync.js'), media_type='text/javascript')

@app.get('/connect')
def serve_device_connect():
    return FileResponse(os.path.join(BASE_DIR, 'connect.html'))

@app.get('/device_connect.css')
def serve_device_css():
    return FileResponse(os.path.join(BASE_DIR, 'device_connect.css'), media_type='text/css')

@app.get("/favicon.svg")
def serve_favicon():
    for d in (_FRONTEND_DIR, BASE_DIR):
        p = os.path.join(d, "favicon.svg")
        if os.path.exists(p):
            return FileResponse(p, media_type="image/svg+xml")
    raise fastapi.HTTPException(status_code=404, detail="not found")

@app.get("/frontend_encyclopedia.js")
def encyclopedia_script():
    return FileResponse(os.path.join(BASE_DIR,"frontend_encyclopedia.js"),media_type="application/javascript")

@app.get("/frontend_encyclopedia.css")
def encyclopedia_style():
    return FileResponse(os.path.join(BASE_DIR,"frontend_encyclopedia.css"),media_type="text/css")

@app.get("/{filename}")
def serve_static(filename: str):
    # Legacy standalone .html compatibility
    if "/" in filename or "\\" in filename or ".." in filename:
        raise fastapi.HTTPException(status_code=400, detail="Invalid filename")
    if filename.endswith(".html"):
        path = os.path.normpath(os.path.join(BASE_DIR, filename))
        if path.startswith(os.path.normpath(BASE_DIR)) and os.path.isfile(path):
            return FileResponse(path)
    # SPA catch-all: serve frontend index.html for every non-/api route
    return _serve_frontend_index()

# ---------------------------------------------------------
# Program entry point
# ---------------------------------------------------------
if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    
    _log.info("=====================================================")
    _log.info("-APERTUS MUNDUS- Starting up...")
    _log.info("Current working directory: %s", BASE_DIR)
    _log.info("=====================================================")
    _log.info("Do not close this window! Closing it will stop the game engine.")
    
    # LAN requests remain blocked until the local user enables and pairs a device.
    from server_startup import run_local_server
    run_local_server(app, _log)
