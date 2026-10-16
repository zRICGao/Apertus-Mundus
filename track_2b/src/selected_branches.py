"""Prepare only the selected server-owned draft; never generate replacement prose."""
import copy
import json
import uuid
from fastapi import HTTPException
from database import atomic
from world import encode, state_token, now
from inference_contracts import spatial_context, normalize_spatial


def ready(node, fx):
    return {'status': 'success', 'node_id': node['id'], 'phase': fx.get('phase'),
            'node': {'id': node['id'], 'name': node['name'], 'content': node['content'],
                     'expanded_content': node['expanded_content'], 'is_draft': True,
                     'npc_additions': fx.get('npc_additions', []), 'scene_phase': fx.get('phase')},
            'npc_additions': fx.get('npc_additions', [])}


def prepare(req):
    import agent
    import npc_review as review
    with agent.safe_db() as conn, atomic(conn):
        node = conn.execute('SELECT * FROM nodes WHERE id=?', (req.node_id,)).fetchone()
        pending = conn.execute('SELECT payload FROM pending_effects WHERE node_id=?', (req.node_id,)).fetchone()
        if not node:
            raise HTTPException(404, '候选不存在')
        if not node['is_draft'] or not pending:
            raise HTTPException(409, '候选已确认或已失效')
        fx = json.loads(pending[0])
        if fx.get('timeline_id') != req.timeline_id:
            raise HTTPException(409, '候选属于其他队伍')
        token = state_token(conn)
        if fx.get('state_token') != token:
            raise HTTPException(409, '世界已变化，请重新推演')
        if fx.get('phase') in {'READY_TO_EXPAND', 'EXPANDED'} or fx.get('npc_review'):
            review.check_commit(conn, fx, confirmed=True)
            return ready(node, fx)
        # Serialize even requests from different devices/operation keys.
        key = f'branch:{req.node_id}:{token}'
        old = conn.execute('SELECT * FROM npc_generation_reviews WHERE operation_key=?', (key,)).fetchone()
        if old:
            review_id = old['id']
            payload = json.loads(old['payload'])
            if payload['identity'] != pending[0]:
                raise HTTPException(409, '候选已变化，请刷新')
            if not old['lease_until'] and payload.get('judged') and old['status'] != 'complete':
                return review.response(review_id, payload)
        else:
            spatial = spatial_context(conn, req.timeline_id)
            branch = {**copy.deepcopy(fx), 'text': node['summary'] or node['name'],
                      'node_name': node['name'], 'node_content': fx['skeleton']}
            normalize_spatial(branch, spatial, fx.get('action_type', 'mixed'))
            request = {'current_node_id': fx.get('parent_node_id'), 'timeline_id': req.timeline_id,
                       'player_action': fx.get('player_action', ''), 'action_type': fx.get('action_type', 'mixed'),
                       'node_id': req.node_id}
            review_id = uuid.uuid4().hex
            ctx = review.context(conn, request)
            ctx['room_id'] = spatial['room_id']
            payload = {'kind': 'branch', 'request': request, 'identity': pending[0],
                       'parsed': {'branches': [branch]}, 'original_parsed': {'branches': [copy.deepcopy(branch)]},
                       'context': ctx, 'token': token, 'entries': [], 'spatial_context': spatial}
            conn.execute('INSERT INTO npc_generation_reviews(id,operation_key,epoch,state_token,kind,payload,created_at) '
                         'VALUES(?,?,?,?,?,?,?)', (review_id, key, review.epoch(conn), token, 'branch', encode(payload), now()))
    return review.run(review_id)


def canonicalize(payload):
    """Apply identity/space decisions to effects; expansion writes the only full prose."""
    from map_review import canonical_actions
    from map_effects import plan_map_actions
    import npc_review as review
    branch = payload['parsed']['branches'][0]
    mapping, omitted = {}, set()
    for entry in payload['entries']:
        choice = entry['decision']
        if choice.startswith('reuse:'):
            mapping[entry['ref']] = choice[6:]
        elif choice.startswith('same:'):
            mapping[entry['ref']] = choice[5:]
        elif choice == 'omit':
            omitted.add(entry['ref'])
    for source in mapping:
        seen, target = {source}, mapping[source]
        while target in mapping:
            if target in seen:
                raise ValueError('角色复用存在循环')
            seen.add(target)
            target = mapping[target]
        if target in omitted:
            raise ValueError('不能复用已取消人物')
        mapping[source] = target
    removed = set(mapping) | omitted
    if branch.get('npc') and branch['npc'].get('ref', 'new_npc') in removed:
        branch['npc'] = None
    branch['new_entities'] = [e for e in branch.get('new_entities', []) if e.get('ref') not in removed]
    for field in ('stat_changes', 'emotion_deltas', 'npc_memories', 'entity_changes', 'state_changes'):
        kept = []
        for original in branch.get(field, []):
            item = dict(original)
            if any(item.get(k) in omitted for k in ('entity_uid', 'target_uid')):
                continue
            for key in ('entity_uid', 'target_uid'):
                if item.get(key) in mapping:
                    item[key] = mapping[item[key]]
            if field == 'entity_changes' and item.get('entity_uid') in mapping.values():
                for key in ('name', 'description', 'state_desc', 'aliases'):
                    item.pop(key, None)
            kept.append(item)
        branch[field] = kept
    if branch.get('san_delta'):
        uid = branch['san_delta'].get('entity_uid')
        if uid in omitted:
            branch['san_delta'] = None
        elif uid in mapping:
            branch['san_delta']['entity_uid'] = mapping[uid]
    for entry in payload.get('map_entries', []):
        branch['map_actions'] = canonical_actions(entry)
    normalize_spatial(branch, payload['spatial_context'], payload['request']['action_type'])
    with review.db() as conn:
        if state_token(conn) != payload['token']:
            raise HTTPException(409, '世界已变化，请重新推演')
        plan_map_actions(conn, branch, payload['context']['room_id'], payload['context']['timeline_id'])


def complete(conn, payload, review_id):
    import npc_review as review
    from engine import normalize_branch_references
    node_id = payload['request']['node_id']
    pending = conn.execute('SELECT payload FROM pending_effects WHERE node_id=?', (node_id,)).fetchone()
    if not pending or pending[0] != payload['identity']:
        raise HTTPException(409, '候选已变化，审核未保存')
    fx = json.loads(pending[0])
    branch = payload['parsed']['branches'][0]
    normalize_branch_references(conn, branch, tolerant=True, allow_declarations=False,
                                timeline_id=payload['request']['timeline_id'])
    for key in (*review.EFFECT_KEYS, 'deferred_effects', 'reference_warnings'):
        if key not in {'skeleton', 'action_type'} and key in branch:
            fx[key] = branch[key]
    fx.update(phase='READY_TO_EXPAND', spatial_context=payload['spatial_context'],
              review_decisions=[{k: e[k] for k in ('kind', 'ref', 'name', 'decision', 'arrival', 'reason') if k in e}
                                for e in [*payload['entries'], *payload.get('map_entries', [])]])
    review.seal(conn, fx, review_id, 0)
    conn.execute('UPDATE pending_effects SET payload=? WHERE node_id=?', (encode(fx), node_id))
    return ready(conn.execute('SELECT * FROM nodes WHERE id=?', (node_id,)).fetchone(), fx)
