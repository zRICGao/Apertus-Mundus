"""A server lease prevents concurrent prose requests for the same candidate."""
import json
import time
import uuid
from fastapi import HTTPException
from database import atomic
from world import state_token, encode, now


def cached(req):
    import agent
    with agent.safe_db() as conn:
        row = conn.execute('SELECT payload FROM pending_effects WHERE node_id=?', (req.node_id,)).fetchone()
        if not row:
            return None
        fx = json.loads(row[0])
        if fx.get('timeline_id') != req.timeline_id:
            raise HTTPException(409, '候选属于其他队伍')
        if fx.get('phase') != 'EXPANDED':
            return None
        if fx.get('state_token') != state_token(conn):
            raise HTTPException(409, '世界已变化，请重新推演')
        from npc_review import check_commit
        check_commit(conn, fx, confirmed=True)
        return fx['expanded_content']


def claim(req, token, identity):
    import agent
    from npc_review import epoch
    with agent.safe_db() as conn, atomic(conn):
        row = conn.execute('SELECT payload FROM pending_effects WHERE node_id=?', (req.node_id,)).fetchone()
        if not row or row[0] != identity or state_token(conn) != token:
            raise HTTPException(409, '候选或世界已变化')
        key = f'expand:{req.node_id}:{token}'
        previous = conn.execute('SELECT id,lease_until FROM npc_generation_reviews WHERE operation_key=?', (key,)).fetchone()
        if previous and previous['lease_until'] > time.time():
            raise HTTPException(409, '该分支正在扩写，请稍后重试')
        lease = time.time()+600
        identifier = previous['id'] if previous else uuid.uuid4().hex
        if previous:
            conn.execute('UPDATE npc_generation_reviews SET lease_until=? WHERE id=?', (lease, identifier))
        else:
            conn.execute('INSERT INTO npc_generation_reviews(id,operation_key,epoch,state_token,kind,payload,lease_until,created_at) '
                         'VALUES(?,?,?,?,?,?,?,?)', (identifier, key, epoch(conn), token, 'expansion', '{}', lease, now()))
    return identifier, lease


def check(conn, claim):
    if claim is not None:
        row = conn.execute('SELECT lease_until FROM npc_generation_reviews WHERE id=?', (claim[0],)).fetchone()
        if not row or row[0] != claim[1]:
            raise HTTPException(409, '扩写处理权已变化，本次结果未保存')


def release(claim):
    import agent
    if claim:
        with agent.safe_db() as conn, atomic(conn):
            conn.execute('UPDATE npc_generation_reviews SET lease_until=0 WHERE id=? AND lease_until=?', claim)
