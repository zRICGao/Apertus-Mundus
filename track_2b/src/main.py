from database import connect_db, atomic
from providers import generation, parse_object
"""
APERTUS-MUNDUS 引擎 — 主系统模块 (main.py)
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


app = fastapi.FastAPI(title="Apertus Mundus · 世界工作台")

from device_link import DeviceSession, DeviceAccessMiddleware, create_router as create_device_router
device_session = DeviceSession()
app.add_middleware(DeviceAccessMiddleware, session=device_session)

# CORS 仅允许本地访问，部署时通过 ALLOWED_ORIGINS 环境变量配置
# 示例：ALLOWED_ORIGINS=http://localhost:8000,http://192.168.1.100:8000
_default_origins = [
    "http://127.0.0.1:8000",
    "http://localhost:8000",
    "null",  # 支持 file:// 协议直接打开 player.html
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
# 【统一错误处理】：所有异常统一为 {"status":"error","message":"..."}
# 前端只需检查 response.status === "error" 即可
# ---------------------------------------------------------
@app.exception_handler(fastapi.HTTPException)
async def http_exception_handler(request, exc: fastapi.HTTPException):
    """将 HTTPException 统一为 JSON 格式，不再返回 FastAPI 默认的 {"detail":"..."}"""
    return JSONResponse(
        status_code=exc.status_code,
        content={"status": "error", "message": exc.detail},
    )

@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request, exc: RequestValidationError):
    """将参数校验错误统一为 JSON 格式"""
    errors = exc.errors()
    msg = "; ".join(f"{e.get('loc',['?'])[-1]}: {e.get('msg','未知错误')}" for e in errors)
    return JSONResponse(
        status_code=422,
        content={"status": "error", "message": f"参数校验失败: {msg}"},
    )

# ---------------------------------------------------------
# 【模块化】：挂载独立地图模块
# ---------------------------------------------------------
from map import map_router, init_map_tables, set_db_file as map_set_db_file
from map import get_map_context as _get_map_context
from map import auto_place_room as _auto_place_room
from map import export_map_data, import_map_data, clear_map_data
app.include_router(map_router)

# ---------------------------------------------------------
# 【模块化】：挂载 RAG 知识库模块
# ---------------------------------------------------------
from rag import rag_router, configure_rag, init_rag_tables
from rag import chunk_text as _chunk_text, get_embeddings as _get_embeddings
from rag import rag_retrieve as _rag_retrieve, cosine_similarity as _cosine_similarity
from rag import refresh_vector_cache as _refresh_vector_cache
app.include_router(rag_router)

# ---------------------------------------------------------
# 【模块化】：挂载 AI Agent 模块（含 SSE 流式推演）
# ---------------------------------------------------------
from agent import agent_router, configure_agent
from agent import build_system_context as get_system_context
app.include_router(agent_router)
from scene_service import scene_router, configure_scene
app.include_router(scene_router)

# ---------------------------------------------------------
# 【模块化】：挂载记忆系统模块
# ---------------------------------------------------------
from memory import memory_router, configure_memory
from memory import _l1_append
from memory import l1_get_working_context as _l1_get_working_context
from memory import append_to_memory, _tl_append_memory
from memory import fold_memory_with_ai as _fold_memory_with_ai
from memory import MEMORY_FOLD_THRESHOLD, MEMORY_SUMMARY_LIMIT
app.include_router(memory_router)

# ---------------------------------------------------------
# 【模块化】：挂载触发器系统模块
# ---------------------------------------------------------
from trigger import trigger_router, configure_trigger
app.include_router(trigger_router)

# ---------------------------------------------------------
# 【模块化】：挂载世界实体模块
# ---------------------------------------------------------
from entity import entity_router, configure_entity
from entity import get_world_entities_text as _get_world_entities_text
from entity import ai_extract_and_upsert_entities as _ai_extract_and_upsert_entities
app.include_router(entity_router)

# ---------------------------------------------------------
# 【模块化】：挂载时间线模块
# ---------------------------------------------------------
from timeline import timeline_router, configure_timeline
app.include_router(timeline_router)

# ---------------------------------------------------------
# 配置区与绝对路径锁定 (修复打包后变空白、找不到文件的问题)
# ---------------------------------------------------------
import sys
import os

# 【关键修复】：识别当前是源码运行，还是 exe 运行
if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(sys.executable)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# 【安全】：从 .env 文件或系统环境变量中加载 API Key，绝不硬编码
# 安装方式：pip install python-dotenv
# 使用方式：在 main.py 同级目录创建 .env 文件，写入：
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

# 【模块化】：将 DB 路径注入地图模块
map_set_db_file(DB_FILE)

# 【模块化】：配置 RAG 模块
configure_rag(DB_FILE, generation.settings.embedding_key, generation.settings.embedding_url)

# 【模块化】：配置记忆系统模块
configure_memory(DB_FILE, client, fn_get_embeddings=_get_embeddings)

# 【模块化】：配置世界实体模块
configure_entity(DB_FILE, client)

# 【模块化】：配置时间线模块（tl_append_memory 延迟到 startup 注入）
configure_timeline(DB_FILE, client, fn_tl_append_memory=_tl_append_memory,
                   memory_summary_limit=MEMORY_SUMMARY_LIMIT)

# ---------------------------------------------------------
# 【Persona 模式】：启动时加载 persona_mode.json（若存在）
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
# 数据库初始化
# ---------------------------------------------------------
from contextlib import contextmanager
from logger import get_logger

_log = get_logger("main")

def get_db_connection():
    return connect_db(DB_FILE)


@contextmanager
def safe_db():
    """
    安全的数据库连接 context manager。
    用法：
        with safe_db() as conn:
            conn.execute(...)
    保证连接在正常退出和异常时都会关闭，且异常时自动回滚。
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
    
    # 【新增】：百科库数据表
    cursor.execute('''CREATE TABLE IF NOT EXISTS lorebook (id INTEGER PRIMARY KEY AUTOINCREMENT, keywords TEXT, content TEXT)''')

    # 【关键节点触发器表】
    cursor.execute('''CREATE TABLE IF NOT EXISTS triggers (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        label       TEXT    NOT NULL DEFAULT '未命名触发器',
        target_node_id INTEGER NOT NULL,
        mode        TEXT    NOT NULL DEFAULT 'soft',
        cond_type   TEXT    NOT NULL DEFAULT '',
        cond_value  TEXT    NOT NULL DEFAULT '',
        conditions  TEXT    NOT NULL DEFAULT '[]',
        fired       INTEGER NOT NULL DEFAULT 0
    )''')
    # 兼容旧数据库：若 triggers 表缺少 conditions 列则自动添加
    try:
        cursor.execute("SELECT conditions FROM triggers LIMIT 1")
    except Exception:
        cursor.execute("ALTER TABLE triggers ADD COLUMN conditions TEXT NOT NULL DEFAULT '[]'")

    # 【多时间线并行】：时间线表
    cursor.execute('''CREATE TABLE IF NOT EXISTS timelines (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        label           TEXT    NOT NULL DEFAULT '时间线',
        color           TEXT    NOT NULL DEFAULT '#5b9cf5',
        current_node_id INTEGER,
        current_room_id INTEGER,
        memory          TEXT    NOT NULL DEFAULT '',
        char_ids        TEXT    NOT NULL DEFAULT '',
        status          TEXT    NOT NULL DEFAULT 'active',
        created_at      TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
    )''')

    # 【世界实体注册表】：跨时间线共享的可变世界状态
    # 这是解决"同一NPC在不同时间线重复出现"问题的核心层
    # entity_type: npc | location | event
    # status:      active（正常）| dead（死亡）| moved（已离场）| resolved（事件结束）
    # last_seen_by: 最后接触该实体的时间线标签，供AI理解"谁知道这件事"
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

    # 【本地 RAG 知识库】：由独立模块 rag.py 管理
    conn.commit()
    init_rag_tables()

    # 【三级记忆系统 - L1 短期工作区】
    # 保存最近 N 次推演的完整上下文快照（场景名、玩家动作、AI结果摘要、思考过程）
    # 每条记录对应一次推演，按时间排序，超限时最旧的记录被淘汰到 L3
    cursor.execute('''CREATE TABLE IF NOT EXISTS memory_l1 (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        scene_name  TEXT    NOT NULL DEFAULT '',
        player_action TEXT  NOT NULL DEFAULT '',
        ai_summary  TEXT    NOT NULL DEFAULT '',
        thought_process TEXT NOT NULL DEFAULT '',
        entity_updates TEXT NOT NULL DEFAULT '',
        created_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
    )''')
    # 迁移：为旧库补充 timeline_id 列（已有列时静默跳过）
    try:
        cursor.execute("ALTER TABLE memory_l1 ADD COLUMN timeline_id INTEGER DEFAULT NULL")
    except Exception:
        pass

    # 【待执行副作用表】：AI 推演 Phase 1 写入，扩写确认后 Phase 3 执行
    # 替代原来塞在 nodes.summary 中的 __FX__ 机制，避免 GM 手动编辑节点时损坏数据
    cursor.execute('''CREATE TABLE IF NOT EXISTS pending_effects (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        node_id     INTEGER NOT NULL UNIQUE,
        payload     TEXT    NOT NULL DEFAULT '{}',
        created_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
        FOREIGN KEY(node_id) REFERENCES nodes(id) ON DELETE CASCADE
    )''')

    # 【地图系统】：由独立模块 map.py 管理
    conn.commit()
    init_map_tables()

    # 平滑升级：如果旧数据库没有 inventory 字段，尝试加上去
    try: cursor.execute("ALTER TABLE characters ADD COLUMN inventory TEXT DEFAULT ''")
    except sqlite3.OperationalError: pass
    # 平滑升级：characters 加 status 字段
    try: cursor.execute("ALTER TABLE characters ADD COLUMN status TEXT DEFAULT 'active'")
    except sqlite3.OperationalError: pass
    # 平滑升级：characters 加 personality 字段（与 inventory 解耦）
    try: cursor.execute("ALTER TABLE characters ADD COLUMN personality TEXT DEFAULT ''")
    except sqlite3.OperationalError: pass
    # 平滑升级：旧 map_rooms 表加 floor 字段
    try: cursor.execute("ALTER TABLE map_rooms ADD COLUMN floor INTEGER NOT NULL DEFAULT 1")
    except sqlite3.OperationalError: pass
    # 平滑升级：world_entities 加 room_id（实体坐标化）
    try: cursor.execute("ALTER TABLE world_entities ADD COLUMN room_id INTEGER")
    except sqlite3.OperationalError: pass
    # 平滑升级：world_entities 加 aliases（别名/描述词列表，用于同一实体多名称匹配）
    try: cursor.execute("ALTER TABLE world_entities ADD COLUMN aliases TEXT NOT NULL DEFAULT '[]'")
    except sqlite3.OperationalError: pass
    # 平滑升级：timelines 加 current_room_id（时间线坐标）
    try: cursor.execute("ALTER TABLE timelines ADD COLUMN current_room_id INTEGER")
    except sqlite3.OperationalError: pass
    # 平滑升级：nodes 加 expanded_content 字段（AI扩写叙事持久化）
    try: cursor.execute("ALTER TABLE nodes ADD COLUMN expanded_content TEXT DEFAULT ''")
    except sqlite3.OperationalError: pass

    # 【手机聊天记录】：NPC 私聊消息持久化
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

    # 【时间回溯】：游戏状态快照表，保留最近10条
    cursor.execute('''CREATE TABLE IF NOT EXISTS game_checkpoints (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        from_node_id INTEGER NOT NULL,
        snapshot     TEXT    NOT NULL,
        created_at   TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
    )''')

    # 【战报史册】：永久记录玩家经过的每个场景快照（不参与折叠/淘汰）
    # 用于 export-battle-report 增量式生成：每次只读 id > last_chronicle_position 的新条目
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

    # 初始化本地剧情记忆流字段
    cursor.execute("INSERT OR IGNORE INTO system_state (key, value) VALUES ('session_memory', '【跑团记忆日志已初始化】\n')")
    # 投屏端同步状态
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
# 【模块化】：配置 Agent 模块（注入所有依赖函数）
# 使用 startup 事件延迟执行，确保所有函数已定义
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
    # 触发器模块需要 get_system_context（来自 agent），在 agent 配置完成后注入
    configure_trigger(
        db_file=DB_FILE,
        generation_client=client,
        fn_get_system_context=get_system_context,
        fn_append_to_memory=append_to_memory,
    )
    from wiki_jobs import start_worker
    start_worker(DB_FILE)

# ---------------------------------------------------------
# 数据模型定义（供 API 请求体使用，CRUD 模型已迁移至各自模块）
# ---------------------------------------------------------
class AIContextRequest(BaseModel): scene_name: str = ""; content: str = ""
# DynamicActionRequest 已移至 agent.py
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


# 【多时间线推演】：数据模型（CRUD 模型已迁移至 timeline.py）
class TimelineDynamicRequest(BaseModel):
    timeline_id: int | None = None
    current_node_id: int
    scene_name: str = ""
    content: str = ""
    player_action: str = ""
    action_type: str = "auto"

# 【地图系统】：数据模型已移至 map.py（通过 map_router 自动注册）

# ---------------------------------------------------------
# API 接口：剧本与存档管理（支持模块化文件夹结构）
# ---------------------------------------------------------
# 文件夹结构：
#   campaigns/
#     我的剧本/
#       campaign.json    ← 剧目（节点、角色、世界观、触发器等）
#       map.json         ← 地图（房间、通道）
#       knowledge/       ← 知识库
#         *.txt / *.md   ← RAG 文档
#   *.json               ← 兼容旧版单文件存档（平铺在 BASE_DIR 下）
# ---------------------------------------------------------

@app.get("/api/campaigns")
def list_campaigns():
    """列出所有可加载的剧本（兼容旧版单文件 + 新版文件夹结构）"""
    from campaigns import is_campaign_file
    results = []

    # 旧版：BASE_DIR 下的 *.json（向后兼容）
    for f in glob.glob(os.path.join(BASE_DIR, "*.json")):
        fname = os.path.basename(f)
        if fname.startswith("persona_mode") or not is_campaign_file(f): continue
        results.append({"name": fname, "type": "legacy", "path": fname})

    # 新版：campaigns/ 下的子文件夹（含 campaign.json）
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

    # 向后兼容：同时返回旧版字符串数组格式（供未升级的前端使用）
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
# 【记忆系统】：已迁移至 memory.py（通过 app.include_router(memory_router) 自动注册）
# append_to_memory, _l1_append, _l1_get_working_context, _tl_append_memory 通过顶部 import 引入
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
# API 接口：词条库、世界观、节点 CRUD (原样保留)
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
    """获取 HP/SAN 的自定义显示名。"""
    with safe_db() as conn:
        hp_row = conn.execute("SELECT value FROM system_state WHERE key='hp_label'").fetchone()
        san_row = conn.execute("SELECT value FROM system_state WHERE key='san_label'").fetchone()
    return {"hp_label": hp_row["value"] if hp_row else "HP", "san_label": san_row["value"] if san_row else "SAN"}

class StatLabelsRequest(BaseModel):
    hp_label: str = "HP"
    san_label: str = "SAN"

@app.put("/api/game/stat-labels")
def update_stat_labels(req: StatLabelsRequest):
    """更新 HP/SAN 显示名。"""
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
        if previous is None: raise fastapi.HTTPException(404,'角色不存在')
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
        record_event(conn,"character_edit","更新角色",{"entity_uid":uid})
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
            raise fastapi.HTTPException(404, '场景已被删除，请刷新工作台')
        if req.expected_fields is not None and req.expected_fields != [previous[key] or '' for key in ('name','summary','content')]:
            raise fastapi.HTTPException(409, '另一台设备已修改此场景。请先复制保留你的修改，放弃本次编辑以读取最新内容，再重新编辑。')
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
# 【新增核心】：打包提取四大维度的全知上下文
# ---------------------------------------------------------

def _build_persona_instruction() -> str:
    """
    将 persona_mode.json 的核心规则、MBTI 池、癖好池组装为可注入 system prompt 的文本块。
    若 persona_mode 未启用则返回空字符串，对已有逻辑零影响。
    """
    if not PERSONA_CONFIG:
        return ""
    lines = [PERSONA_CONFIG.get("core_rule", "")]
    npc_rule = PERSONA_CONFIG.get("npc_generation_rule", "")
    if npc_rule:
        lines.append(npc_rule)
    mbti_pool = PERSONA_CONFIG.get("mbti_pool", [])
    if mbti_pool:
        lines.append("【可用MBTI类型池】" + " | ".join(mbti_pool))
    quirk_pool = PERSONA_CONFIG.get("quirk_pool", [])
    if quirk_pool:
        lines.append("【可用行为癖好池】" + " | ".join(quirk_pool))
    return "\n".join(filter(None, lines))


# ---------------------------------------------------------
# 【世界实体】：已迁移至 entity.py
# _get_world_entities_text, _ai_extract_and_upsert_entities 通过顶部 import 引入
# ---------------------------------------------------------


# =============================================================
# 【RAG 知识库引擎】：已迁移至 rag.py
# =============================================================


# _get_map_context 和 _auto_place_room 已移至 map.py
# 通过顶部 from map import get_map_context as _get_map_context 引入
# ---------------------------------------------------------
# 【地图-AI 联动引擎】：处理 AI 返回的 map_actions
# ---------------------------------------------------------
def _process_map_actions(conn,parsed,current_room_id,timeline_id=None,scene_node_id=None):
    from map_effects import apply_map_actions
    return apply_map_actions(conn,parsed,current_room_id,timeline_id,scene_node_id)


def _get_current_room_id(conn, timeline_id: int | None = None) -> int | None:
    """获取当前房间 ID（支持时间线模式）"""
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
# 已迁移至 agent.py（通过 app.include_router(agent_router) 自动注册）

# ---------------------------------------------------------
# API 接口：角色管理 (新增/删除)
# ---------------------------------------------------------
@app.post("/api/game/character")
def create_character(req: CharCreateRequest):
    with safe_db() as conn, atomic(conn):
        from world import state_token
        if req.expected_token and req.expected_token!=state_token(conn):
            raise fastapi.HTTPException(409,'世界已更新，新角色尚未创建，请重试')
        uid = req.entity_uid
        if uid:
            from world import resolve_entity
            resolve_entity(conn, uid=uid)
            if conn.execute("SELECT id FROM characters WHERE entity_uid=?", (uid,)).fetchone():
                raise fastapi.HTTPException(409, "实体已关联角色，请使用更新接口")
        char_id = conn.execute("INSERT INTO characters(name,role,hp,san,inventory,personality,status,entity_uid) VALUES(?,?,?,?,?,?,?,?)",
            (req.name,req.role,req.hp,req.san,req.inventory,req.personality,req.status,uid)).lastrowid
        uid = link_character(conn, char_id, uid, new=True)
        record_event(conn, "character_create", f"角色 {req.name} 登记", {"entity_uid":uid})
        refresh_wiki(conn)
    return {"status":"success", "id":char_id, "entity_uid":uid}


@app.delete("/api/game/character/{char_id}")
def delete_character(char_id: int):
    with safe_db() as conn:
        result = conn.execute("SELECT id FROM characters WHERE id = ?", (char_id,)).fetchone()
        if not result:
            raise fastapi.HTTPException(status_code=404, detail="角色不存在")
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
# API 接口：跳转场景时自动检测是否需要生成 NPC
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
          "instructions":"场景是否明确需要一个尚未登记的新NPC立即登场？不要将已有实体判为新身份。"}})['new_npc']
    return {"status":"success","spawned_npc":None,"suggest_generation":result.status=='ok' and result.value>=.9}


# ---------------------------------------------------------
# 【触发器系统】：已迁移至 trigger.py（通过 app.include_router(trigger_router) 自动注册）
# ---------------------------------------------------------

# ---------------------------------------------------------
# ---------------------------------------------------------


# ---------------------------------------------------------
# API 接口：战报/小说导出
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
    增量式战报生成：
      - 输入源不再是 session_memory，而是 chronicle_log 中 id > last_chronicle_position 的所有新场景快照
        （每条包含场景名、content、深入调查后的 expanded_content、玩家动作）
      - 生成完成后将 last_chronicle_position 推进到本次最大 id
      - 每次另存为一个新的 .md 文件，不重复生成之前已写过的部分
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
            # 第一次无新场景：告知用户，并立 flag 等待下次重新生成
            conn.execute(
                "INSERT OR REPLACE INTO system_state (key, value) VALUES ('pending_regenerate', '1')"
            )
            conn.commit()
            conn.close()
            return {
                "status": "success",
                "report": "（自上次生成以来暂无新场景，无需续写。）",
                "filename": "",
            }

        # 第二次（用户点重新生成后触发）：拉取上一章全部场景重新生成
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
                "report": "（暂无可生成的场景记录。）",
                "filename": "",
            }
        is_regenerate = True
    else:
        is_regenerate = False
        # 有新场景时清除 flag
        conn.execute(
            "INSERT OR REPLACE INTO system_state (key, value) VALUES ('pending_regenerate', '0')"
        )

    worldview = wv_row["value"] if wv_row else ""
    char_list = "、".join([c["name"] for c in chars]) or "未知角色"

    # 拼接结构化输入：按时间顺序逐场景列出
    parts = []
    for i, r in enumerate(rows, 1):
        parts.append(f"## 场景 {i}：{r['scene_name'] or '未命名场景'}")
        if r["player_action"]:
            parts.append(f"**玩家动作**：{r['player_action']}")
        if r["scene_content"]:
            parts.append(f"**场景描写**：\n{r['scene_content']}")
        if r["expanded_content"]:
            parts.append(f"**深入调查**：\n{r['expanded_content']}")
        parts.append("")  # 空行分隔
    structured_input = "\n".join(parts)

    is_continuation = last_pos > 0 and not is_regenerate
    system_prompt = (
        "你是一位才华横溢的奇幻小说作者。下面提供按时间顺序排列的若干场景快照，"
        "每个场景包含玩家动作、场景描写、（可选的）深入调查细节。"
        "请将其润色为一段排版精美、文笔优美的奇幻战报/小说章节。要求："
        "- 使用 Markdown 格式，包含标题、段落分节，长度根据场景数量自然延伸，不要强行截断"
        "- 严格按场景给定的时间顺序展开，保留所有人名、地点、关键事件，不得改变剧情走向"
        "- 充分利用「深入调查」中的细节进行环境与氛围描写"
        "- 文笔生动、有代入感"
        + (
            "- 这是续章，请用一个承接上一章的简短开篇过渡几句，不要重复之前章节中已经叙述过的剧情"
            if is_continuation else
            "- 这是开篇第一章"
        )
        + "- 结尾加一句「（本章完）」"
        + f"【世界观背景】{worldview}"
        + f"【主要角色】{char_list}"
    )

    try:
        resp = client.chat.completions.create(
            model=generation.settings.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": f"以下是本次需要润色的场景快照：\n\n{structured_input}"}
            ],
            temperature=0.8,
            max_tokens=8000,
        )
        report = resp.choices[0].message.content.strip()

        # 写入新文件
        report_dir = os.environ.get('RPG_REPORTS_DIR', os.path.join(BASE_DIR, "battle_report"))
        os.makedirs(report_dir, exist_ok=True)
        filename = f"battle_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
        filepath = os.path.join(report_dir, filename)
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(report)

        # 生成成功后推进位置（重新生成时不推进，保留原位置便于再次重试）
        if not is_regenerate:
            new_pos = rows[-1]["id"]
            conn.execute(
                "INSERT OR REPLACE INTO system_state (key, value) VALUES ('last_chronicle_position', ?)",
                (str(new_pos),)
            )
        # 无论正向还是重新生成，成功后都清除 flag
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
# API 接口：API Key 配置（前端可热更新 .env 中的密钥）
# ---------------------------------------------------------


# ---------------------------------------------------------
# ---------------------------------------------------------


# =============================================================
# 【多时间线 CRUD】：已迁移至 timeline.py
# _tl_append_memory 通过顶部 import 引入
# =============================================================

@app.post("/api/timelines/{tl_id}/dynamic-options")
def timeline_dynamic_options(tl_id: int, req: TimelineDynamicRequest):
    from agent import DynamicActionRequest, dynamic_options_handler
    return dynamic_options_handler(DynamicActionRequest(current_node_id=req.current_node_id,timeline_id=tl_id,
        scene_name=req.scene_name,content=req.content,player_action=req.player_action,action_type=req.action_type))


# =============================================================
# 【世界实体】：已迁移至 entity.py（通过 app.include_router(entity_router) 自动注册）
# =============================================================

# =============================================================
# 【时间线 CRUD/合并】：已迁移至 timeline.py（通过 app.include_router(timeline_router) 自动注册）
# =============================================================


# =============================================================
# 【RAG 知识库】：已迁移至 rag.py（通过 app.include_router(rag_router) 自动注册）
# =============================================================

# =============================================================
# 【地图系统】：已迁移至 map.py（通过 app.include_router(map_router) 自动注册）
# =============================================================

# ---------------------------------------------------------
# 【地图-AI 联动】：节点→房间绑定查询（供前端 jumpToNode 调用）
# ---------------------------------------------------------
@app.get("/api/map/room-by-node/{node_id}")
def get_room_by_node(node_id: int):
    """查询某个剧情节点绑定的地图房间。jumpToNode 时自动调用。"""
    with safe_db() as conn:
        room = conn.execute(
            "SELECT id, label FROM map_rooms WHERE node_id=? LIMIT 1", (node_id,)
        ).fetchone()
    if room:
        return {"status": "success", "room_id": room["id"], "label": room["label"]}
    return {"status": "success", "room_id": None}
# =============================================================

# ---------------------------------------------------------
# API 接口：投屏端同步（REST 轮询 + WebSocket 实时推送）
# ---------------------------------------------------------

# 【WebSocket 投屏】：连接管理 + 广播
import asyncio

_ws_clients: set[WebSocket] = set()

@app.websocket("/ws/player")
async def ws_player_endpoint(websocket: WebSocket):
    """
    投屏端 WebSocket 连接。
    连接建立后立即推送一次完整状态，之后 GM 每次 push_player_state 时自动广播。
    客户端只需监听 onmessage，不需要主动发消息。
    """
    await websocket.accept()
    _ws_clients.add(websocket)
    _log.info("投屏 WebSocket 连接建立（当前 %d 个客户端）", len(_ws_clients))
    try:
        # 连接建立后立即推送当前状态
        state = _build_player_state_snapshot()
        await websocket.send_json(state)
        # 保持连接，等待客户端断开（客户端不需要发数据，但 WebSocket 需要 recv 循环保持心跳）
        while True:
            # 等待客户端消息（通常不会收到，但需要这个循环来检测断开）
            try:
                await asyncio.wait_for(websocket.receive_text(), timeout=30)
            except asyncio.TimeoutError:
                # 心跳：每 30 秒发一个 ping（客户端无需处理）
                try:
                    await websocket.send_json({"type": "ping"})
                except Exception:
                    break
    except WebSocketDisconnect:
        pass
    except Exception as e:
        _log.debug("投屏 WebSocket 异常断开: %s", e)
    finally:
        _ws_clients.discard(websocket)
        _log.info("投屏 WebSocket 连接断开（剩余 %d 个客户端）", len(_ws_clients))


def _build_player_state_snapshot() -> dict:
    """构建投屏端需要的完整状态快照（场景+角色+BGM）。"""
    with safe_db() as conn:
        def _get(k):
            r = conn.execute("SELECT value FROM system_state WHERE key=?", (k,)).fetchone()
            return r["value"] if r else ""

        scene_id_raw = _get("player_current_scene_id")
        scene_id = int(scene_id_raw) if scene_id_raw and scene_id_raw.isdigit() else None

        # 如果有当前场景，附带场景详情
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
    """向所有已连接的投屏端 WebSocket 客户端广播当前状态。"""
    if not _ws_clients:
        return
    state = _build_player_state_snapshot()
    dead_clients = set()
    for ws in _ws_clients.copy():
        try:
            await ws.send_json(state)
        except Exception:
            dead_clients.add(ws)
    # 清理已断开的连接
    _ws_clients.difference_update(dead_clients)


# ---------------------------------------------------------
# 场景跳转日志：记录玩家通过预设选项进入节点的行为
# ---------------------------------------------------------
class SceneVisitRequest(BaseModel):
    node_id:      int
    node_name:    str
    option_text:  str = ""   # 玩家点击的选项文本，为空时表示直接跳转

@app.post("/api/game/log-scene-visit")
def log_scene_visit(req: SceneVisitRequest):
    # 只有通过预设选项跳转才记录，直接跳转不写记忆流
    if not req.option_text.strip():
        return {"status": "skipped"}
    conn = get_db_connection()
    node = conn.execute('SELECT is_draft FROM nodes WHERE id=?',(req.node_id,)).fetchone()
    if node is None or node['is_draft']:
        conn.close()
        raise fastapi.HTTPException(409,'未确认的分支不能写入剧情历史')
    log = f"玩家选择「{req.option_text}」→ 进入场景【{req.node_name}】"
    append_to_memory(conn, log)

    # 同步写入战报史册：抓取当前场景 content/expanded_content 作为快照
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
        _log.warning("写入 chronicle_log 失败: %s", e)

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
            return {"status":"error","message":"没有可用快照"}
        data = json.loads(cp["snapshot"])
        if not isinstance(data.get("system_state"),list):
            raise fastapi.HTTPException(409,"旧格式快照不完整，请加载存档")
        restore(conn,data)
        conn.execute("DELETE FROM game_checkpoints WHERE id=?",(cp["id"],))
        remaining = conn.execute("SELECT COUNT(*) FROM game_checkpoints").fetchone()[0]
    _refresh_vector_cache()
    return {"status":"success","restored_node_id":cp["from_node_id"],"remaining":remaining}


@app.get("/api/player/state")
def get_player_state():
    """REST 轮询接口（向后兼容，投屏端已升级为 WebSocket 则不再需要此接口）。"""
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
    """GM 推送投屏状态。写入数据库后立即通过 WebSocket 广播给所有投屏端。"""
    with safe_db() as conn:
        node=conn.execute('SELECT is_draft FROM nodes WHERE id=?',(req.current_scene_id,)).fetchone()
        if node is None or node['is_draft']: raise fastapi.HTTPException(409,'请先确认场景结果')
        conn.execute("INSERT OR REPLACE INTO system_state (key,value) VALUES ('player_current_scene_id',?)", (str(req.current_scene_id),))
        conn.execute("INSERT OR REPLACE INTO system_state (key,value) VALUES ('player_scene_ai_text',?)",    (req.scene_ai_text,))
        conn.execute("INSERT OR REPLACE INTO system_state (key,value) VALUES ('player_bgm_url',?)",          (req.bgm_url,))
        conn.execute("INSERT OR REPLACE INTO system_state (key,value) VALUES ('player_bgm_name',?)",         (req.bgm_name,))
        conn.commit()
    # WebSocket 广播（异步，不阻塞响应）
    await _broadcast_player_state()
    return {"status": "success"}

# ---------------------------------------------------------
# 前端静态文件服务（Vite 构建输出 → static-frontend/）
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
    # fallback：旧版单文件 index.html 与 main.py 同级
    legacy = os.path.join(BASE_DIR, "index.html")
    if os.path.exists(legacy):
        return FileResponse(legacy)
    return JSONResponse({"error": "未找到前端文件，请先执行 cd frontend && npm run build"}, status_code=404)

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
    # 旧版单 .html 文件兼容
    if "/" in filename or "\\" in filename or ".." in filename:
        raise fastapi.HTTPException(status_code=400, detail="非法文件名")
    if filename.endswith(".html"):
        path = os.path.normpath(os.path.join(BASE_DIR, filename))
        if path.startswith(os.path.normpath(BASE_DIR)) and os.path.isfile(path):
            return FileResponse(path)
    # SPA catch-all：所有非 /api 路由返回前端 index.html
    return _serve_frontend_index()

# ---------------------------------------------------------
# 程序入口
# ---------------------------------------------------------
if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    
    _log.info("=====================================================")
    _log.info("-APERTUS MUNDUS- 正在启动...")
    _log.info("当前工作目录: %s", BASE_DIR)
    _log.info("=====================================================")
    _log.info("请不要关闭此窗口！关闭窗口将停止游戏引擎。")
    
    # LAN requests remain blocked until the local user enables and pairs a device.
    from server_startup import run_local_server
    run_local_server(app, _log)
