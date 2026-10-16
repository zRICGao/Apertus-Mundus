from database import connect_db, atomic
from providers import generation, parse_object
"""
APERTUS-MUNDUS engine — map system module (map.py)
Independent FastAPI APIRouter with all map data models, database operations, REST APIs and helper functions.
Mounted by main.py via app.include_router(map_router).
"""

import fastapi
from fastapi import APIRouter
from pydantic import BaseModel, Field
import sqlite3
import json
import os

# ---------------------------------------------------------
# Router instance (no extra prefix needed when including it in main.py)
# ---------------------------------------------------------
map_router = APIRouter(tags=["Map System"])


# ---------------------------------------------------------
# Database connection (injected by main.py at startup)
# ---------------------------------------------------------
from contextlib import contextmanager
from logger import get_logger

_log = get_logger("map")

_db_file: str = ""

def set_db_file(path: str):
    """Called at main.py startup to inject the database path."""
    global _db_file
    _db_file = path

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
# Database table initialization
# ---------------------------------------------------------
def init_map_tables():
    """Create map-related database tables idempotently; called by init_db() in main.py."""
    conn = get_db_connection()
    cursor = conn.cursor()

    cursor.execute('''CREATE TABLE IF NOT EXISTS map_rooms (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        map_id      INTEGER NOT NULL DEFAULT 1,
        label       TEXT    NOT NULL DEFAULT 'Untitled',
        x           REAL    NOT NULL DEFAULT 0,
        y           REAL    NOT NULL DEFAULT 0,
        w           REAL    NOT NULL DEFAULT 120,
        h           REAL    NOT NULL DEFAULT 80,
        description TEXT    NOT NULL DEFAULT '',
        state       TEXT    NOT NULL DEFAULT 'unknown',
        color       TEXT    NOT NULL DEFAULT '#1e3a2f',
        node_id     INTEGER,
        floor       INTEGER NOT NULL DEFAULT 1
    )''')

    cursor.execute('''CREATE TABLE IF NOT EXISTS map_edges (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        map_id      INTEGER NOT NULL DEFAULT 1,
        from_id     INTEGER NOT NULL,
        to_id       INTEGER NOT NULL,
        label       TEXT    NOT NULL DEFAULT '',
        locked      INTEGER NOT NULL DEFAULT 0,
        key_item    TEXT    NOT NULL DEFAULT '',
        edge_type   TEXT    NOT NULL DEFAULT 'normal',
        FOREIGN KEY(from_id) REFERENCES map_rooms(id) ON DELETE CASCADE,
        FOREIGN KEY(to_id)   REFERENCES map_rooms(id) ON DELETE CASCADE
    )''')

    # Smooth migration
    try: cursor.execute("ALTER TABLE map_rooms ADD COLUMN floor INTEGER NOT NULL DEFAULT 1")
    except sqlite3.OperationalError: pass
    try: cursor.execute("ALTER TABLE map_edges ADD COLUMN edge_type TEXT NOT NULL DEFAULT 'normal'")
    except sqlite3.OperationalError: pass
    try: cursor.execute("ALTER TABLE map_rooms ADD COLUMN aliases TEXT NOT NULL DEFAULT '[]'")
    except sqlite3.OperationalError: pass
    try: cursor.execute("ALTER TABLE map_edges ADD COLUMN discovered INTEGER NOT NULL DEFAULT 1")
    except sqlite3.OperationalError: pass

    conn.commit()
    conn.close()


# ---------------------------------------------------------
# Pydantic data models
# ---------------------------------------------------------
class MapRoomCreateRequest(BaseModel):
    map_id:      int    = 1
    label:       str    = "Untitled"
    x:           float  = 0
    y:           float  = 0
    w:           float  = 120
    h:           float  = 80
    description: str    = ""
    state:       str    = "unknown"
    color:       str    = "#1e3a2f"
    node_id:     int | None = None
    floor:       int    = 1
    aliases:     list[str] = Field(default_factory=list)
    expected_token: str | None = None

class MapRoomUpdateRequest(BaseModel):
    label:       str
    x:           float
    y:           float
    w:           float
    h:           float
    description: str
    state:       str
    color:       str
    node_id:     int | None = None
    floor:       int = 1
    aliases:     list[str] | None = None
    expected_token: str | None = None

class MapEdgeCreateRequest(BaseModel):
    map_id:    int   = 1
    from_id:   int
    to_id:     int
    label:     str   = ""
    locked:    bool  = False
    key_item:  str   = ""
    edge_type: str   = "normal"
    discovered: bool = True
    expected_token: str | None = None

class MapEdgeUpdateRequest(BaseModel):
    label:     str
    locked:    bool
    key_item:  str
    edge_type: str = "normal"
    discovered: bool | None = None
    expected_token: str | None = None

class MapAutoRoomRequest(BaseModel):
    """Used to grow the map automatically after simulation"""
    map_id:         int
    parent_room_id: int
    label:          str
    node_id:        int
    description:    str = ""
    current_node_id: int | None = None
    timeline_id: int | None = None
    operation_key: str | None = Field(default=None, min_length=1, max_length=200)
    floor: int | None = None
    edge_type: str = 'normal'
    enter: bool = False
    aliases: list[str] = Field(default_factory=list)


# ---------------------------------------------------------
# Helper functions (used by main.py to build AI simulation context)
# ---------------------------------------------------------
def get_map_context(conn, room_id: int | None, query: str = '') -> str:
    """
    Read the current room and its neighboring rooms; build spatial context for AI simulation.
    If room_id is None or the room does not exist, return an empty string (no effect).
    Multi-floor support: cross-floor connections indicate vertical direction.
    """
    if not room_id:
        return ""
    room = conn.execute("SELECT * FROM map_rooms WHERE id=?", (room_id,)).fetchone()
    if not room:
        return ""

    floor = room["floor"] if "floor" in room.keys() else 1
    lines = [f"Current location: [room_id={room['id']},map_id={room['map_id']}] {room['label']} (floor {floor}, {room['description'] or 'no extra description'})"]

    # Neighboring rooms (bidirectional edges), including edge_type
    edges = conn.execute(
        "SELECT me.*, "
        "  CASE WHEN me.from_id=? THEN me.to_id ELSE me.from_id END AS neighbor_id "
        "FROM map_edges me "
        "WHERE me.from_id=? OR me.to_id=?",
        (room_id, room_id, room_id)
    ).fetchall()

    neighbor_descs = []
    for e in edges:
        nb = conn.execute(
            "SELECT id,label, state, floor FROM map_rooms WHERE id=?",
            (e["neighbor_id"],)
        ).fetchone()
        if not nb:
            continue

        if not e['discovered']:
            continue  # Unrevealed connections are available to the GM review, not as known exits.
        lock_note  = f"(locked, requires: {e['key_item']})" if e["locked"] else ""
        dir_note   = f"{e['label']}→" if e["label"] else "→"
        state_map  = {"unknown": "Not discovered", "discovered": "Discovered but not entered", "explored": "Explored",
                      "locked": "Blocked", "active": "Current"}
        nb_state   = state_map.get(nb["state"], nb["state"])
        nb_floor   = nb["floor"] if "floor" in nb.keys() else 1

        etype = e["edge_type"] if "edge_type" in e.keys() else "normal"
        if nb_floor != floor:
            direction = "up" if nb_floor > floor else "down"
            type_label = {
                "stairs":   f"Stairs ({direction} to floor {nb_floor})",
                "elevator": f"Elevator ({direction} to floor {nb_floor})",
                "portal":   f"Portal (to floor {nb_floor})",
            }.get(etype, f"Passage ({direction} to floor {nb_floor})")
            neighbor_descs.append(
                f"[room_id={nb['id']},edge_id={e['id']}] {dir_note}{nb['label']}[{type_label}]（{nb_state}）{lock_note}"
            )
        else:
            neighbor_descs.append(
                f"[room_id={nb['id']},edge_id={e['id']}] {dir_note}{nb['label']}（{nb_state}）{lock_note}"
            )

    if neighbor_descs:
        lines.append("Accessible destinations: " + " / ".join(neighbor_descs))
    else:
        lines.append("No discovered registered passages; this does not prove the area is a dead end. Unregistered new areas require review.")

    # Known states on the same floor of the same map
    known = conn.execute(
        "SELECT id,label,description,aliases,state,floor FROM map_rooms "
        "WHERE map_id=? AND id!=? AND state!='unknown' ORDER BY floor, id",
        (room["map_id"], room_id)
    ).fetchall()
    if known:
        text=query.lower()
        known=sorted(known,key=lambda r:(-sum(10 for label in [r['label'],*json.loads(r['aliases'])] if label.lower() in text),
                                         r['floor']!=floor,r['id']))
        grouped = {}
        for r in known[:20]:
            fl = r["floor"] if "floor" in r.keys() else 1
            grouped.setdefault(fl, []).append(f"[room_id={r['id']}] {r['label']}[{r['state']}]")
        parts = []
        for fl in sorted(grouped):
            prefix = f"Floor {fl}: " if len(grouped) > 1 else ""
            parts.append(prefix + " / ".join(grouped[fl]))
        lines.append("Known map status: " + " | ".join(parts))
        lines.append('These are relevant location summaries; omitted locations may still exist. New areas will be checked against the full map during review.')

    return "\n".join(lines)


def auto_place_room(conn, map_id: int, parent_room_id: int,
                    label: str, node_id: int | None, description: str = "", *,
                    floor: int | None = None, edge_type: str = 'normal', aliases=None) -> int:
    from spatial import room_record, placement, validate_room, EDGE_TYPES, aliases as clean_aliases, bind_scene
    parent = room_record(conn, parent_room_id, map_id)
    floor = parent['floor'] if floor is None else floor
    validate_room({'label': label, 'floor': floor, 'map_id': map_id, 'aliases': aliases or []})
    if edge_type not in EDGE_TYPES or (floor != parent['floor'] and edge_type == 'normal'):
        raise ValueError('Cross-floor areas must explicitly use stairs, an elevator, or a portal')
    x, y = placement(conn, map_id, parent_room_id, floor)
    uid = conn.execute('INSERT INTO map_rooms(map_id,label,x,y,w,h,description,state,node_id,floor,aliases) '
                       'VALUES(?,?,?,?,120,80,?,?,?,?,?)',
                       (map_id,label.strip(),x,y,description[:2000],'discovered',node_id or None,floor,
                        json.dumps(clean_aliases(aliases or []),ensure_ascii=False))).lastrowid
    conn.execute('INSERT INTO map_edges(map_id,from_id,to_id,edge_type,discovered) VALUES(?,?,?,?,1)',
                 (map_id,parent_room_id,uid,edge_type))
    if node_id: bind_scene(conn,node_id,uid)
    return uid


# ---------------------------------------------------------
# Map import/export (separate map.json file)
# ---------------------------------------------------------
def export_map_data(conn) -> dict:
    """Export map data as a dictionary for separate map.json storage."""
    rooms = [dict(r) for r in conn.execute(
        "SELECT * FROM map_rooms ORDER BY floor, id"
    ).fetchall()]
    edges = [dict(e) for e in conn.execute(
        "SELECT * FROM map_edges ORDER BY id"
    ).fetchall()]
    bindings=[dict(r) for r in conn.execute('SELECT * FROM scene_locations ORDER BY node_id')]
    return {"map_rooms": rooms, "map_edges": edges, "scene_locations":bindings}


def validate_import(conn, data):
    from spatial import validate_room, EDGE_TYPES
    if not isinstance(data,dict) or not isinstance(data.get('map_rooms'),list) or not isinstance(data.get('map_edges'),list):
        raise ValueError('Map file must contain room and passage lists')
    rooms, edges, ids, pairs = [], [], {}, set()
    for source in data['map_rooms']:
        if not isinstance(source,dict) or type(source.get('id')) is not int or source['id'] <= 0:
            raise ValueError('Imported locations must have valid unique IDs')
        room = MapRoomCreateRequest.model_validate({**source,'aliases':json.loads(source.get('aliases','[]')) if isinstance(source.get('aliases',[]),str) else source.get('aliases',[])}).model_dump()
        room['id'] = source['id']; validate_room(room)
        if room['id'] in ids: raise ValueError('Map contains duplicate location IDs')
        if room.get('node_id') and not conn.execute('SELECT id FROM nodes WHERE id=?',(room['node_id'],)).fetchone():
            raise ValueError('Imported map references a nonexistent scene node')
        ids[room['id']] = room; rooms.append(room)
    edge_ids=set()
    for source in data['map_edges']:
        edge = MapEdgeCreateRequest.model_validate(source).model_dump()
        uid=source.get('id')
        if uid is not None and (type(uid) is not int or uid<=0 or uid in edge_ids):
            raise ValueError('Map has invalid or duplicate passage IDs')
        edge_ids.add(uid)
        a,b=ids.get(edge['from_id']),ids.get(edge['to_id'])
        if not a or not b or a['id']==b['id'] or a['map_id']!=edge['map_id'] or b['map_id']!=edge['map_id']:
            raise ValueError('Passage endpoints do not exist, are duplicates, or are on different maps')
        if edge['edge_type'] not in EDGE_TYPES or (a['floor']!=b['floor'] and edge['edge_type']=='normal'):
            raise ValueError('Cross-floor passages must use stairs, an elevator, or a portal')
        pair=tuple(sorted((a['id'],b['id'])))
        if pair in pairs: raise ValueError('Map contains duplicate passages')
        pairs.add(pair); edge['id']=uid; edges.append(edge)
    bindings=data.get('scene_locations',[])
    if not isinstance(bindings,list): raise ValueError('Scene-to-location bindings must be a list')
    seen=set()
    for binding in bindings:
        if not isinstance(binding,dict) or type(binding.get('node_id')) is not int or binding.get('room_id') not in ids:
            raise ValueError('Invalid scene-to-location binding reference')
        if binding['node_id'] in seen or not conn.execute('SELECT id FROM nodes WHERE id=?',(binding['node_id'],)).fetchone():
            raise ValueError('Duplicate scene-to-location binding or nonexistent scene node')
        seen.add(binding['node_id'])
    return rooms,edges


def import_map_data(conn, data: dict):
    from spatial import aliases,bind_scene
    rooms,edges=validate_import(conn,data)
    for rm in rooms:
        conn.execute('INSERT INTO map_rooms(id,map_id,label,x,y,w,h,description,state,color,node_id,floor,aliases) '
                     'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                     (rm['id'],rm['map_id'],rm['label'].strip(),rm['x'],rm['y'],rm['w'],rm['h'],rm['description'],
                      rm['state'],rm['color'],rm['node_id'] or None,rm['floor'],json.dumps(aliases(rm['aliases']),ensure_ascii=False)))
        if rm['node_id']: bind_scene(conn,rm['node_id'],rm['id'])
    for eg in edges:
        conn.execute('INSERT INTO map_edges(id,map_id,from_id,to_id,label,locked,key_item,edge_type,discovered) '
                     'VALUES(?,?,?,?,?,?,?,?,?)',
                     (eg['id'],eg['map_id'],eg['from_id'],eg['to_id'],eg['label'],int(eg['locked']),eg['key_item'],eg['edge_type'],int(eg['discovered'])))
    for binding in data.get('scene_locations',[]): bind_scene(conn,binding['node_id'],binding['room_id'])


def clear_map_data(conn):
    from spatial import clear_room_references
    clear_room_references(conn)
    conn.execute('DELETE FROM map_edges')
    conn.execute('DELETE FROM map_rooms')
    # IDs are never recycled: stale clients must not accidentally select a different place.


@contextmanager
def editing(expected_token=None):
    from world import state_token
    try:
        with safe_db() as conn, atomic(conn):
            if expected_token and expected_token != state_token(conn):
                raise fastapi.HTTPException(409,'Map changed; refresh and try again. This edit was not saved')
            yield conn
    except (ValueError,KeyError,TypeError,sqlite3.IntegrityError) as exc:
        raise fastapi.HTTPException(422,str(exc)) from exc


@map_router.get('/api/map/rooms')
def map_get_rooms(map_id: int=1):
    from world import state_token
    with safe_db() as conn:
        rooms=[dict(r) for r in conn.execute('SELECT * FROM map_rooms WHERE map_id=? ORDER BY floor,id',(map_id,))]
        edges=[dict(r) for r in conn.execute('SELECT * FROM map_edges WHERE map_id=? ORDER BY id',(map_id,))]
        return {'status':'success','rooms':rooms,'edges':edges,'floors':sorted({r['floor'] for r in rooms}) or [1],
                'state_token':state_token(conn)}


@map_router.post('/api/map/rooms')
def map_create_room(req:MapRoomCreateRequest):
    from spatial import validate_room,aliases,bind_scene
    with editing(req.expected_token) as conn:
        validate_room(req.model_dump())
        uid=conn.execute('INSERT INTO map_rooms(map_id,label,x,y,w,h,description,state,color,node_id,floor,aliases) '
                         'VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                         (req.map_id,req.label.strip(),req.x,req.y,req.w,req.h,req.description,req.state,req.color,
                          req.node_id or None,req.floor,json.dumps(aliases(req.aliases),ensure_ascii=False))).lastrowid
        if req.node_id: bind_scene(conn,req.node_id,uid)
    return {'status':'success','id':uid}


@map_router.put('/api/map/rooms/{room_id}')
def map_update_room(room_id:int,req:MapRoomUpdateRequest):
    from spatial import validate_room,aliases,room_record,bind_scene
    with editing(req.expected_token) as conn:
        old=room_record(conn,room_id)
        data={**req.model_dump(),'map_id':old['map_id'],'aliases':req.aliases if req.aliases is not None else aliases(old['aliases'])}
        validate_room(data)
        if req.floor!=old['floor']:
            for edge in conn.execute('SELECT * FROM map_edges WHERE from_id=? OR to_id=?',(room_id,room_id)):
                other=room_record(conn,edge['to_id'] if edge['from_id']==room_id else edge['from_id'])
                if other['floor']!=req.floor and edge['edge_type']=='normal':
                    raise ValueError('Change cross-floor links to stairs, an elevator or a portal first')
        labels=aliases(data['aliases'])
        if req.label!=old['label'] and old['label'] not in labels: labels.append(old['label'])
        conn.execute('UPDATE map_rooms SET label=?,x=?,y=?,w=?,h=?,description=?,state=?,color=?,node_id=?,floor=?,aliases=? WHERE id=?',
                     (req.label.strip(),req.x,req.y,req.w,req.h,req.description,req.state,req.color,req.node_id or None,
                      req.floor,json.dumps(labels,ensure_ascii=False),room_id))
        conn.execute('UPDATE world_entities SET location=? WHERE room_id=?',(req.label.strip(),room_id))
        if old['node_id'] and old['node_id']!=req.node_id:
            conn.execute('DELETE FROM scene_locations WHERE node_id=? AND room_id=?',(old['node_id'],room_id))
        if req.node_id: bind_scene(conn,req.node_id,room_id)
    return {'status':'success'}


@map_router.delete('/api/map/rooms/{room_id}')
def map_delete_room(room_id:int,expected_token:str|None=None):
    from spatial import clear_room_references,room_record
    with editing(expected_token) as conn:
        room_record(conn,room_id); clear_room_references(conn,[room_id])
        conn.execute('DELETE FROM map_edges WHERE from_id=? OR to_id=?',(room_id,room_id))
        conn.execute('DELETE FROM map_rooms WHERE id=?',(room_id,))
    return {'status':'success'}


@map_router.post('/api/map/edges')
def map_create_edge(req:MapEdgeCreateRequest):
    from spatial import validate_edge
    with editing(req.expected_token) as conn:
        validate_edge(conn,req.map_id,req.from_id,req.to_id,req.edge_type)
        exists=conn.execute('SELECT id FROM map_edges WHERE (from_id=? AND to_id=?) OR (from_id=? AND to_id=?)',
                             (req.from_id,req.to_id,req.to_id,req.from_id)).fetchone()
        if exists: return {'status':'exists','id':exists['id']}
        uid=conn.execute('INSERT INTO map_edges(map_id,from_id,to_id,label,locked,key_item,edge_type,discovered) '
                         'VALUES(?,?,?,?,?,?,?,?)',
                         (req.map_id,req.from_id,req.to_id,req.label,int(req.locked),req.key_item,req.edge_type,int(req.discovered))).lastrowid
    return {'status':'success','id':uid}


@map_router.put('/api/map/edges/{edge_id}')
def map_update_edge(edge_id:int,req:MapEdgeUpdateRequest):
    from spatial import validate_edge
    with editing(req.expected_token) as conn:
        edge=conn.execute('SELECT * FROM map_edges WHERE id=?',(edge_id,)).fetchone()
        if not edge: raise fastapi.HTTPException(404,'Passage not found')
        validate_edge(conn,edge['map_id'],edge['from_id'],edge['to_id'],req.edge_type)
        conn.execute('UPDATE map_edges SET label=?,locked=?,key_item=?,edge_type=?,discovered=? WHERE id=?',
                     (req.label,int(req.locked),req.key_item,req.edge_type,int(req.discovered) if req.discovered is not None else edge['discovered'],edge_id))
    return {'status':'success'}


@map_router.delete('/api/map/edges/{edge_id}')
def map_delete_edge(edge_id:int,expected_token:str|None=None):
    with editing(expected_token) as conn:
        if not conn.execute('DELETE FROM map_edges WHERE id=?',(edge_id,)).rowcount:
            raise fastapi.HTTPException(404,'Passage not found')
    return {'status':'success'}


@map_router.post('/api/map/move')
def map_move_to_room(room_id:int|None=None,timeline_id:int|None=None,expected_token:str|None=None):
    from spatial import set_position
    with editing(expected_token) as conn:
        if timeline_id is not None and not conn.execute('SELECT id FROM timelines WHERE id=?',(timeline_id,)).fetchone():
            raise fastapi.HTTPException(404,'Team does not exist')
        target=set_position(conn,room_id,timeline_id)
    return {'status':'success','room_id':room_id,'label':target['label'] if target else ''}


@map_router.post('/api/map/auto-room')
def map_auto_room(req:MapAutoRoomRequest):
    from map_review import auto_room
    try: return auto_room(req)
    except ValueError as exc: raise fastapi.HTTPException(422,str(exc)) from exc


@map_router.put('/api/map/rooms/{room_id}/state')
def map_set_room_state(room_id:int,state:str,expected_token:str|None=None):
    from spatial import ROOM_STATES,room_record
    with editing(expected_token) as conn:
        if state not in ROOM_STATES: raise ValueError('Invalid location state')
        room_record(conn,room_id)
        conn.execute('UPDATE map_rooms SET state=? WHERE id=?',(state,room_id))
    return {'status':'success'}


@map_router.post('/api/map/export-file')
def map_export_file():
    with safe_db() as conn: return {'status':'success','data':export_map_data(conn)}


@map_router.post('/api/map/import-file')
def map_import_file(data:dict):
    expected=data.get('expected_token')
    with editing(expected) as conn:
        validate_import(conn,data)  # Complete validation before any deletion.
        clear_map_data(conn); import_map_data(conn,data)
    return {'status':'success'}
