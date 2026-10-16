"""Server-owned operation leases/cache for summary generation, excluding world facts."""
import json
import time
import uuid
from fastapi import HTTPException
from database import atomic
from world import state_token, encode, now


def claim(request):
    if not request.operation_key:
        return None, None
    import agent
    from npc_review import epoch
    with agent.safe_db() as conn, atomic(conn):
        token = state_token(conn)
        lease = time.time()+600
        key = 'summary:' + request.operation_key
        row = conn.execute('SELECT * FROM npc_generation_reviews WHERE operation_key=?', (key,)).fetchone()
        if row:
            if row['epoch'] != epoch(conn) or json.loads(row['payload'])['request_input'] != request.model_dump():
                raise HTTPException(409, '请求键已用于其他推演或世界已载入')
            if row['state_token'] != token:
                raise HTTPException(409, '世界已变化，请重新推演')
            if row['status'] == 'complete':
                return json.loads(row['result']), None
            if row['lease_until'] > time.time():
                raise HTTPException(409, '推演正在进行，请勿重复提交')
            identifier = row['id']
            conn.execute('UPDATE npc_generation_reviews SET lease_until=? WHERE id=?', (lease, identifier))
        else:
            identifier = uuid.uuid4().hex
            conn.execute('INSERT INTO npc_generation_reviews(id,operation_key,epoch,state_token,kind,payload,lease_until,created_at) '
                         'VALUES(?,?,?,?,?,?,?,?)', (identifier, key, epoch(conn), token, 'summary',
                         encode({'request_input': request.model_dump()}), lease, now()))
    return None, (identifier, lease)


def complete(conn, identifier, result):
    if identifier:
        row = conn.execute('SELECT lease_until,state_token FROM npc_generation_reviews WHERE id=?', (identifier[0],)).fetchone()
        if not row or row['lease_until'] != identifier[1] or row['state_token'] != state_token(conn):
            raise HTTPException(409, '推演处理权或世界状态已变化，摘要未保存')
        conn.execute("UPDATE npc_generation_reviews SET status='complete',result=?,lease_until=0 WHERE id=?",
                     (encode(result), identifier[0]))


def release(identifier):
    if identifier:
        import agent
        with agent.safe_db() as conn, atomic(conn):
            conn.execute("UPDATE npc_generation_reviews SET lease_until=0 WHERE id=? AND lease_until=? AND status!='complete'", identifier)
