"""Shared spatial invariants. IDs identify places; labels and canvas positions do not."""
import json
import math

ROOM_STATES = {'unknown', 'discovered', 'explored', 'locked', 'active'}
EDGE_TYPES = {'normal', 'stairs', 'elevator', 'portal'}


def aliases(value):
    if isinstance(value, str):
        value = json.loads(value or '[]')
    if not isinstance(value, list) or any(not isinstance(s, str) or not s.strip() for s in value):
        raise ValueError('地点别名必须为非空文本列表')
    return list(dict.fromkeys(s.strip() for s in value))


def room_record(conn, room_id, map_id=None):
    row = conn.execute('SELECT * FROM map_rooms WHERE id=?', (room_id,)).fetchone()
    if not row or (map_id is not None and row['map_id'] != map_id):
        raise ValueError('地点不存在或不属于当前地图')
    return row


def resolve_room(conn, data, id_key, label_key, map_id, floor=None):
    if data.get(id_key) is not None:
        return room_record(conn, data[id_key], map_id)
    label = str(data.get(label_key, '')).strip()
    if not label:
        raise ValueError('请指定地点ID或名称')
    rows = [r for r in conn.execute('SELECT * FROM map_rooms WHERE map_id=?', (map_id,))
            if label in [r['label'], *aliases(r['aliases'])]]
    if floor is not None:
        rows = [r for r in rows if r['floor'] == floor]
    if len(rows) != 1:
        raise ValueError('地点名称有歧义或尚未登记，请选择地点ID')
    return rows[0]


def party_characters(conn, timeline_id=None):
    if timeline_id is not None:
        team = conn.execute("SELECT * FROM timelines WHERE id=? AND status='active'", (timeline_id,)).fetchone()
        if not team:
            raise ValueError('队伍不存在或已汇合')
        ids = {int(v) for v in team['char_ids'].split(',') if v.strip().isdigit()}
        return [r for r in conn.execute('SELECT * FROM characters') if r['id'] in ids]
    assigned = {int(v) for team in conn.execute("SELECT char_ids FROM timelines WHERE status='active'")
                for v in team[0].split(',') if v.strip().isdigit()}
    return [r for r in conn.execute("SELECT * FROM characters WHERE role='PC' AND status='active'") if r['id'] not in assigned]


def current_room(conn, timeline_id=None):
    if timeline_id is not None:
        team = conn.execute("SELECT current_room_id FROM timelines WHERE id=? AND status='active'", (timeline_id,)).fetchone()
        if not team: raise ValueError('队伍不存在或已汇合')
        return team[0]
    row = conn.execute("SELECT value FROM system_state WHERE key='current_room_id'").fetchone()
    return int(row[0]) if row and row[0] else None


def set_position(conn, room_id, timeline_id=None):
    people = party_characters(conn, timeline_id)
    target = room_record(conn, room_id) if room_id is not None else None
    if timeline_id is not None:
        conn.execute('UPDATE timelines SET current_room_id=? WHERE id=?', (room_id, timeline_id))
    elif room_id is None:
        conn.execute("DELETE FROM system_state WHERE key='current_room_id'")
    else:
        conn.execute("INSERT OR REPLACE INTO system_state VALUES('current_room_id',?)", (str(room_id),))
    if target:
        conn.execute("UPDATE map_rooms SET state='explored' WHERE id=?", (room_id,))
    for char in people:
        conn.execute('UPDATE world_entities SET room_id=?,location=? WHERE uid=?',
                     (room_id, target['label'] if target else '', char['entity_uid']))
    return target


def bind_scene(conn, node_id, room_id):
    if not node_id or room_id is None: return
    room_record(conn, room_id)
    if not conn.execute('SELECT id FROM nodes WHERE id=?', (node_id,)).fetchone():
        raise ValueError('绑定的剧情节点不存在')
    conn.execute('UPDATE map_rooms SET node_id=NULL WHERE node_id=? AND id!=?',(node_id,room_id))
    conn.execute('INSERT OR REPLACE INTO scene_locations(node_id,room_id) VALUES(?,?)', (node_id, room_id))


def scene_room(conn, node_id):
    mapped = conn.execute('SELECT room_id FROM scene_locations WHERE node_id=?', (node_id,)).fetchone()
    if mapped:
        return room_record(conn, mapped[0])
    rows = conn.execute('SELECT * FROM map_rooms WHERE node_id=?', (node_id,)).fetchall()
    if len(rows) > 1: raise ValueError('场景绑定了多个地点，请先在地图中明确绑定')
    return rows[0] if rows else None


def validate_room(data):
    if not isinstance(data.get('label'), str) or not data['label'].strip() or len(data['label']) > 100:
        raise ValueError('地点名称须为1至100字')
    if data.get('state', 'unknown') not in ROOM_STATES:
        raise ValueError('地点状态无效')
    for key in ('x', 'y', 'w', 'h'):
        value = data.get(key, {'x':0, 'y':0, 'w':120, 'h':80}[key])
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError('地图位置和尺寸必须为有限数值')
        if key in {'w', 'h'} and value <= 0: raise ValueError('房间尺寸必须大于零')
    if type(data.get('floor', 1)) is not int or type(data.get('map_id', 1)) is not int:
        raise ValueError('地图编号与楼层必须为整数')
    aliases(data.get('aliases', []))
    if not math.isfinite(data.get('x',0)+data.get('w',120)) or not math.isfinite(data.get('y',0)+data.get('h',80)):
        raise ValueError('房间位置与尺寸超出可表示范围')


def validate_edge(conn, map_id, a, b, edge_type):
    first, second = room_record(conn, a, map_id), room_record(conn, b, map_id)
    if a == b: raise ValueError('通道两端不能是同一地点')
    if edge_type not in EDGE_TYPES: raise ValueError('通道类型无效')
    if first['floor'] != second['floor'] and edge_type == 'normal':
        raise ValueError('跨楼层通道须指定楼梯、电梯或传送门')
    return first, second


def placement(conn, map_id, parent_room_id, floor):
    parent = room_record(conn, parent_room_id, map_id)
    occupied = conn.execute('SELECT x,y,w,h FROM map_rooms WHERE map_id=? AND floor=?', (map_id, floor)).fetchall()
    def free(x, y):
        return not any(r['x'] < x+120 and r['x']+r['w'] > x and r['y'] < y+80 and r['y']+r['h'] > y for r in occupied)
    # Drawing space never determines whether the fictional passage exists.
    for ring in range(1, min(len(occupied)+3, 64)):
        for dx, dy in ((160*ring,0),(0,120*ring),(-160*ring,0),(0,-120*ring)):
            x, y = round((parent['x']+dx)/40)*40, round((parent['y']+dy)/40)*40
            if free(x, y): return x, y
    right = max([r['x']+r['w'] for r in occupied] or [parent['x']+parent['w']])
    return math.ceil((right+40)/40)*40, round(parent['y']/40)*40


def clear_room_references(conn, room_ids=None):
    ids = list(room_ids) if room_ids is not None else [r[0] for r in conn.execute('SELECT id FROM map_rooms')]
    if not ids: return
    marks = ','.join('?' for _ in ids)
    conn.execute(f'DELETE FROM scene_locations WHERE room_id IN ({marks})', ids)
    conn.execute(f'UPDATE timelines SET current_room_id=NULL WHERE current_room_id IN ({marks})', ids)
    conn.execute(f"UPDATE world_entities SET room_id=NULL,location='' WHERE room_id IN ({marks})", ids)
    row = conn.execute("SELECT value FROM system_state WHERE key='current_room_id'").fetchone()
    if row and str(row[0]) in {str(v) for v in ids}:
        conn.execute("DELETE FROM system_state WHERE key='current_room_id'")
