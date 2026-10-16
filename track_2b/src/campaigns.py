"""Versioned saves preserve identities and every authoritative table."""
import json
from pathlib import Path
from datetime import datetime

from database import atomic, connect_db
from world import SNAPSHOT_TABLES, snapshot, restore, encode


def _records(config, key):
    return [dict(r) for r in config.get(key, [])]


def validate_campaign(config):
    """Recognize legacy campaigns and versioned saves before touching the world."""
    if not isinstance(config, dict):
        raise ValueError("这不是有效剧本或存档：文件内容必须是一个对象")
    if "world_snapshot" in config:
        data = config["world_snapshot"]
        if (config.get("schema_version") != 2 or not isinstance(data, dict)
                or not all(key in data for key in ("nodes", "characters", "system_state"))):
            raise ValueError("存档格式无效或版本不受支持")
    else:
        data = config
        if not any(key in data for key in ("nodes", "characters")):
            raise ValueError("这不是有效剧本：缺少场景或角色数据，请选择剧本文件")
    for key in SNAPSHOT_TABLES:
        if key in data and (not isinstance(data[key], list)
                            or any(not isinstance(row, dict) for row in data[key])):
            raise ValueError(f"剧本格式无效：{key} 必须是记录列表")
    return config


def is_campaign_file(path):
    try:
        validate_campaign(json.loads(Path(path).read_text(encoding="utf-8")))
        return True
    except (OSError, ValueError, UnicodeError):
        return False


def load_save(db_file, base_dir, filename, campaigns_dir=None):
    base = Path(base_dir).resolve()
    relative = Path(filename)
    if campaigns_dir is not None and relative.parts and relative.parts[0] == 'campaigns':
        base = Path(campaigns_dir).resolve()
        relative = Path(*relative.parts[1:])
    target = (base / relative).resolve()
    if not target.is_relative_to(base):
        raise ValueError("存档必须在项目目录内")
    folder = target if target.is_dir() else None
    file = target / "campaign.json" if folder else target
    config = validate_campaign(json.loads(file.read_text(encoding="utf-8")))
    conn = connect_db(db_file)
    try:
        with atomic(conn):
            if "world_snapshot" in config:
                restore(conn, config["world_snapshot"])
            else:
                data = {table: [] for table in SNAPSHOT_TABLES}
                data["system_state"] = [{"key": "worldview", "value": config.get("worldview", "【默认世界观】")},
                    {"key": "session_memory", "value": config.get("session_memory", "")},
                    {"key": "player_current_scene_id", "value": ""}]
                for table in ("characters", "nodes", "options", "lorebook", "triggers", "timelines", "world_entities",
                              "memory_l1", "pending_effects", "npc_chat_logs", "map_rooms", "map_edges"):
                    data[table] = _records(config, table)
                for i, row in enumerate(data["characters"], 1):
                    row.setdefault("id", i)
                for i, row in enumerate(data["timelines"], 1):
                    row.setdefault("id", i)
                for trigger in data["triggers"]:
                    for key in ("conditions", "actions", "prerequisite_trigger_ids", "exclude_trigger_ids"):
                        value = trigger.get(key)
                        if isinstance(value, (dict, list)):
                            trigger[key] = encode(value)
                    if not trigger.get("conditions"):
                        trigger["conditions"] = encode([{"type": trigger.get("cond_type", ""), "value": trigger.get("cond_value", "")}])
                    trigger["cond_type"] = trigger.get("cond_type") or ""
                    trigger["cond_value"] = trigger.get("cond_value") or ""
                if folder and (folder / "map.json").exists():
                    map_data = json.loads((folder / "map.json").read_text(encoding="utf-8"))
                    data["map_rooms"], data["map_edges"] = map_data.get("map_rooms", []), map_data.get("map_edges", [])
                raw_documents = list(config.get("rag_library", []))
                if folder and (folder / "knowledge").is_dir() and not raw_documents:
                    for source in sorted((folder / "knowledge").iterdir()):
                        if source.suffix.lower() in {".txt", ".md"}:
                            raw_documents.append({"title": source.stem, "source": source.name, "text": source.read_text(encoding="utf-8")})
                from rag import chunk_text
                chunk_id = 1
                for doc_id, document in enumerate(raw_documents, 1):
                    chunks = document.get("chunks") or [{"index": i, "text": chunk, "embedding": "[]"}
                        for i, chunk in enumerate(chunk_text(document.get("text", "")))]
                    data["rag_documents"].append({"id": doc_id, "title": document.get("title", "未命名"),
                        "source": document.get("source", ""), "chunk_size": len(chunks), "hidden": document.get("hidden", 0),
                        "visibility": document.get("visibility", "public"),"embedding_model":document.get('embedding_model','')})
                    for chunk in chunks:
                        embedding = chunk.get("embedding", "[]")
                        data["rag_chunks"].append({"id": chunk_id, "doc_id": doc_id, "chunk_index": chunk["index"],
                            "chunk_text": chunk["text"], "embedding": encode(embedding) if isinstance(embedding, list) else embedding})
                        chunk_id += 1
                restore(conn, data)
            conn.execute("DELETE FROM game_checkpoints")
        from rag import refresh_vector_cache
        refresh_vector_cache()
        return {"status": "success", "message": f"已加载 {filename}"}
    finally:
        conn.close()


def export_save(db_file, campaigns_dir, save_name=""):
    conn = connect_db(db_file)
    try:
        # A read transaction keeps all tables from the same point in time.
        conn.execute("BEGIN")
        data = snapshot(conn)
        conn.rollback()
    finally:
        conn.close()
    name = "".join(c for c in save_name.strip() if c.isalnum() or c in " _-（）()【】·")[:60]
    name = name or f"save_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"
    root = Path(campaigns_dir).resolve()
    folder = (root / name).resolve()
    if not folder.is_relative_to(root):
        raise ValueError("无效存档名称")
    folder.mkdir(parents=True, exist_ok=True)
    payload = {"schema_version": 2, "world_snapshot": data}
    temporary = folder / "campaign.json.tmp"
    temporary.write_text(encode(payload), encoding="utf-8")
    temporary.replace(folder / "campaign.json")
    return {"status": "success", "message": "存档已保存", "filename": f"campaigns/{name}", "folder": str(folder)}
