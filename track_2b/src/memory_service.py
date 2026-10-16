"""Memory writes are transactional; compaction uses compare-and-swap after model IO."""
import json
import threading
from contextlib import contextmanager
from fastapi import APIRouter
from pydantic import BaseModel

from database import atomic, connect_db
from providers import generation
from logger import get_logger

memory_router = APIRouter(tags=["记忆系统"])
_db_file = ""
_get_embeddings = None
_maintenance_lock = threading.Lock()
_log = get_logger("memory")
MEMORY_FOLD_THRESHOLD = 4000
MEMORY_RECENT_LINES = 6
MEMORY_SUMMARY_LIMIT = 800
L1_MAX_ENTRIES = 10
L1_TOKEN_BUDGET = 4000


def configure_memory(db_file, generation_client=None, fn_get_embeddings=None):
    global _db_file, _get_embeddings
    _db_file, _get_embeddings = db_file, fn_get_embeddings


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


def _l1_append(conn, scene_name, player_action, ai_summary, thought_process="", entity_updates="", timeline_id=None):
    conn.execute("INSERT INTO memory_l1(scene_name,player_action,ai_summary,thought_process,entity_updates,timeline_id) VALUES(?,?,?,?,?,?)",
                 (scene_name, player_action, ai_summary, thought_process, entity_updates, timeline_id))
    conn.commit()


def l1_get_working_context(conn, timeline_id=None):
    rows = conn.execute("SELECT * FROM memory_l1 WHERE timeline_id IS ? ORDER BY id DESC LIMIT ?", (timeline_id, L1_MAX_ENTRIES)).fetchall()
    lines, used = [], 0
    # Keep the newest records when budget is exceeded, then restore chronological order.
    for row in rows:
        line = f"[{row['scene_name']}] 玩家:{row['player_action']} → {row['ai_summary']} | {row['entity_updates']}"
        if used + len(line) > L1_TOKEN_BUDGET:
            continue
        lines.append(line)
        used += len(line)
    return "\n".join(reversed(lines))


def append_to_memory(conn, new_log):
    conn.execute("INSERT OR IGNORE INTO system_state VALUES('session_memory','')")
    conn.execute("UPDATE system_state SET value=value||? WHERE key='session_memory'", (f"\n- {new_log}",))
    conn.commit()


def _tl_append_memory(conn, tl_id, log):
    conn.execute("UPDATE timelines SET memory=memory||? WHERE id=?", (f"\n- {log}", tl_id))
    conn.commit()


def fold_memory_with_ai(current_mem):
    lines = current_mem.splitlines()
    if len(lines) <= MEMORY_RECENT_LINES:
        return current_mem
    archived, recent = "\n".join(lines[:-MEMORY_RECENT_LINES]), "\n".join(lines[-MEMORY_RECENT_LINES:])
    summary = generation.generate("将已确认剧情归纳为核心摘要，保留实体ID、状态变化、来源与因果关系。只返回摘要文本。",
                                  archived, max_tokens=1600)
    return f"【剧情摘要】{summary}\n【近期记录】\n{recent}"


def _epoch(conn):
    row = conn.execute("SELECT value FROM system_state WHERE key='world_epoch'").fetchone()
    return row[0] if row else ""


def _async_fold_memory_task(target_type, text_to_fold, tl_id=None, expected_epoch=None):
    conn = get_db_connection()
    try:
        epoch = expected_epoch or _epoch(conn)
        folded = fold_memory_with_ai(text_to_fold)
        with atomic(conn):
            if _epoch(conn) != epoch:
                return
            # New appends or another completed fold prevent a stale overwrite.
            if target_type == "session":
                conn.execute("UPDATE system_state SET value=? WHERE key='session_memory' AND value=?", (folded, text_to_fold))
            else:
                conn.execute("UPDATE timelines SET memory=? WHERE id=? AND memory=?", (folded, tl_id, text_to_fold))
    finally:
        conn.close()


def _l1_evict_to_l3(conn, rows):
    if not rows:
        return
    epoch = _epoch(conn)
    ids = [r["id"] for r in rows]
    raw = "\n".join(f"[{r['scene_name']}] {r['player_action']} → {r['ai_summary']} {r['entity_updates']}" for r in rows)
    try:
        summary = generation.generate("归纳已确认事件，保留实体ID、关键状态与因果。仅输出摘要。", raw, max_tokens=1400)
    except Exception:
        summary = raw  # Preserve content, including when no model is configured.
    try:
        vecs = _get_embeddings([summary]) if _get_embeddings else []
    except Exception:
        vecs = []
    tl = rows[0]["timeline_id"]
    with atomic(conn):
        if _epoch(conn) != epoch:
            return
        current = conn.execute(f"SELECT id FROM memory_l1 WHERE id IN ({','.join('?' for _ in ids)})", ids).fetchall()
        if len(current) != len(ids):
            return
        doc = conn.execute("INSERT INTO rag_documents(title,source,chunk_size,visibility,timeline_id,embedding_model) VALUES(?,?,?,?,?,?)",
                           ("长期记忆", "memory_l3_eviction", 1, f"team:{tl}" if tl else "gm", tl,generation.settings.embedding_model if vecs else '')).lastrowid
        conn.execute("INSERT INTO rag_chunks(doc_id,chunk_index,chunk_text,embedding) VALUES(?,?,?,?)",
                     (doc, 0, summary, json.dumps(vecs[0] if vecs and vecs[0] else [])))
        # Keep the complete source independently of the generated summary.
        conn.execute("INSERT OR REPLACE INTO wiki_sources(source_key,title,content,visibility,created_at) VALUES(?,?,?,?,datetime('now'))",
                     (f"memory/{doc}", "长期记忆原文", raw, f"team:{tl}" if tl else "gm"))
        conn.execute(f"DELETE FROM memory_l1 WHERE id IN ({','.join('?' for _ in ids)})", ids)
        from world import refresh_wiki
        refresh_wiki(conn)
    from rag import refresh_vector_cache
    refresh_vector_cache()


def maintain_memory(timeline_id=None):
    if not _maintenance_lock.acquire(blocking=False):
        return
    conn = get_db_connection()
    try:
        rows = conn.execute("SELECT * FROM memory_l1 WHERE timeline_id IS ? ORDER BY id DESC", (timeline_id,)).fetchall()
        if len(rows) > L1_MAX_ENTRIES:
            _l1_evict_to_l3(conn, list(reversed(rows[L1_MAX_ENTRIES:])))
        row = conn.execute("SELECT memory FROM timelines WHERE id=?", (timeline_id,)).fetchone() if timeline_id else conn.execute(
            "SELECT value FROM system_state WHERE key='session_memory'").fetchone()
        if row and len(row[0]) > MEMORY_FOLD_THRESHOLD:
            _async_fold_memory_task("timeline" if timeline_id else "session", row[0], timeline_id, _epoch(conn))
    except Exception as exc:
        _log.warning("记忆维护失败，原记录保留: %s", exc)
    finally:
        conn.close()
        _maintenance_lock.release()


class StringContentRequest(BaseModel):
    content: str


@memory_router.get("/api/game/memory")
def get_memory():
    with safe_db() as conn:
        row = conn.execute("SELECT value FROM system_state WHERE key='session_memory'").fetchone()
        return {"status": "success", "content": row[0] if row else ""}


@memory_router.put("/api/game/memory")
def update_memory(req: StringContentRequest):
    with safe_db() as conn:
        conn.execute("INSERT OR REPLACE INTO system_state VALUES('session_memory',?)", (req.content,))
        conn.commit()
    return {"status": "success"}


@memory_router.get("/api/game/memory-l1")
def get_memory_l1():
    with safe_db() as conn:
        return {"status": "success", "entries": [dict(r) for r in conn.execute("SELECT * FROM memory_l1 ORDER BY id DESC LIMIT ?", (L1_MAX_ENTRIES,))]}


@memory_router.delete("/api/game/memory-l1")
def clear_memory_l1():
    with safe_db() as conn:
        conn.execute("DELETE FROM memory_l1")
        conn.commit()
    return {"status": "success"}
