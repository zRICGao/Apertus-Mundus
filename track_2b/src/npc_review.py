"""Server-owned NPC proposals: identity/necessity review before any world write.

Model IO never holds a write transaction. A leased review serializes retries;
approved draft effects are sealed against the server record, not client claims.
"""
import copy
import hashlib
import json
import re
import time
import uuid

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from database import atomic
from providers import evaluate, generation, parse_object
from world import encode, state_token, now, record_event, refresh_wiki, resolve_entity
from logger import get_logger

router = APIRouter(tags=['角色审核'])
log = get_logger('npc_review')
EFFECT_KEYS = ('npc', 'new_entities', 'entity_changes', 'stat_changes', 'emotion_deltas',
               'npc_memories', 'state_definitions', 'state_changes', 'map_actions', 'action_type', 'skeleton')


def db():
    from main import safe_db
    return safe_db()


def epoch(conn):
    return conn.execute("SELECT value FROM system_state WHERE key='world_epoch'").fetchone()[0]


def collect(branches):
    """Canonicalize all automatic NPC creation declarations into the two supported fields."""
    entries = []
    for bi, branch in enumerate(branches):
        kept = []
        for ci, change in enumerate(branch.get('entity_changes', [])):
            if change.get('new') is True and not change.get('entity_uid') and change.get('entity_type', 'npc') == 'npc':
                proposal = {**change, 'ref': change.get('ref') or f'change_npc_{ci}', 'entity_type': 'npc'}
                branch.setdefault('new_entities', []).append(proposal)
            else:
                kept.append(change)
        branch['entity_changes'] = kept
        # Missing temporary refs are formatting omissions, not identity decisions.
        used = {e.get('ref') for e in branch.get('new_entities', [])}
        used.add((branch.get('npc') or {}).get('ref', 'new_npc'))
        for index, entity in enumerate(branch.get('new_entities', [])):
            if not entity.get('ref'):
                ref = f'new_entity_{index + 1}'
                while ref in used:
                    ref += '_1'
                entity['ref'] = ref
                used.add(ref)
        declarations = []
        npc = branch.get('npc')
        if npc and not npc.get('entity_uid'):
            npc.setdefault('ref', 'new_npc')
            declarations.append(npc)
        declarations += [e for e in branch.get('new_entities', []) if e.get('entity_type') == 'npc']
        refs = set()
        for proposal in declarations:
            ref = proposal.get('ref')
            if not isinstance(ref, str) or not ref or ref in refs:
                raise ValueError('新角色的临时引用必须非空且在分支内唯一')
            if not str(proposal.get('name', '')).strip():
                raise ValueError('新角色必须有名称')
            refs.add(ref)
            entries.append({'key': f'{bi}:{ref}', 'branch': bi, 'ref': ref,
                            'name': proposal['name'], 'description': proposal.get('description', proposal.get('backstory', '')),
                            'proposal': copy.deepcopy(proposal)})
    return entries


def context(conn, request):
    node_id = request.get('current_node_id')
    if node_id is None:
        rows = conn.execute('SELECT * FROM nodes WHERE is_draft=0 AND name=? AND content=?',
                            (request.get('scene_name', ''), request.get('scene_content', ''))).fetchall()
        if len(rows) != 1:
            raise HTTPException(422, '请指定唯一的已确认场景节点后重试')
        node = rows[0]
    else:
        node = conn.execute('SELECT * FROM nodes WHERE id=? AND is_draft=0', (node_id,)).fetchone()
        if not node:
            raise HTTPException(409, '场景不存在或尚未确认，请刷新后重试')
    timeline = request.get('timeline_id')
    if timeline is not None:
        team = conn.execute("SELECT * FROM timelines WHERE id=? AND status='active'", (timeline,)).fetchone()
        if not team:
            raise HTTPException(409, '队伍不存在或已汇合')
        room_id = team['current_room_id']
    else:
        row = conn.execute("SELECT value FROM system_state WHERE key='current_room_id'").fetchone()
        room_id = int(row[0]) if row and row[0] else None
    room = conn.execute('SELECT label FROM map_rooms WHERE id=?', (room_id,)).fetchone()
    events = conn.execute('SELECT content FROM world_events WHERE valid=1 AND '
                          '(timeline_id IS NULL OR timeline_id IS ?) ORDER BY id DESC LIMIT 12', (timeline,)).fetchall()
    return {'node_id': node['id'], 'scene': node['name'], 'content': node['expanded_content'] or node['content'],
            'action': request.get('player_action', ''), 'timeline_id': timeline,
            'room_id': room_id, 'location': room[0] if room else '', 'recent_events': [r[0][:1500] for r in events]}


def candidates(conn, entry, ctx):
    # Search the full registry. A bounded ranked result limits model context, not the search corpus.
    from knowledge import allowed
    query = ' '.join([entry['name'], entry['description'], ctx['action'], ctx['content'], *ctx['recent_events']]).lower()
    tokens = set(re.findall(r'[a-z0-9_]{2,}|[\u4e00-\u9fff]{2}', query))
    scored = []
    for row in conn.execute("SELECT * FROM world_entities WHERE entity_type IN ('npc','pc')"):
        if not allowed(row['visibility'], None, 'gm', ctx['timeline_id']):
            continue
        aliases = json.loads(row['aliases'] or '[]')
        labels = [row['name'], *aliases]
        text = ' '.join([*labels, row['location'] or '', row['state_desc'] or '']).lower()
        try:
            description = json.loads(row['state_desc'] or '{}')
            description = description.get('desc', '') if isinstance(description, dict) else str(description)
        except ValueError:
            description = row['state_desc'] or ''
        score = sum(12 for label in labels if label and label.lower() in query)
        score += sum(1 for token in tokens if token in text)
        score += 8 if ctx['room_id'] and row['room_id'] == ctx['room_id'] else 0
        scored.append((score, {'entity_uid': row['uid'], 'name': row['name'], 'aliases': aliases,
                              'location': row['location'], 'status': row['status'],
                              'description': str(description)[:1600], 'type': row['entity_type']}))
    scored.sort(key=lambda pair: (-pair[0], pair[1]['entity_uid']))
    return [item for _, item in scored[:20]]


def judge(conn, payload):
    entries = collect(payload['parsed']['branches'])
    registered = {row[0] for row in conn.execute('SELECT uid FROM world_entities')}
    if any(entry['ref'] in registered for entry in entries):
        raise ValueError('新角色的临时引用不能使用已有实体ID')
    questions = {}
    for entry in entries:
        entry['candidates'] = candidates(conn, entry, payload['context'])
        criteria = {'create': '必要的独立新人物，参与互动或需要持续追踪',
                    'omit': '不需要登记；背景人物即可或不应引入', 'unknown': '身份或新增必要性证据不足'}
        criteria.update({'reuse:' + c['entity_uid']: encode(c) for c in entry['candidates']})
        # Earlier declarations form a DAG: duplicate proposals can reuse one new identity.
        criteria.update({'same:' + other['ref']: '同一人物：' + encode(other['proposal'])
                         for other in entries if other['branch'] == entry['branch'] and other['key'] < entry['key']})
        questions[entry['key']] = {'type': 'choice', 'criteria': criteria,
            'instructions': '审核这个拟新增角色的身份及必要性。优先复用已有身份；不能只因同名合并。'
                            '人物仅换称呼/地点/状态不是新人。不要为了推进剧情无故添加角色。'
                            '明确为不同人物才选择create；仅背景描写选omit；不能确定选unknown。'
                            'evidence说明身份及必要性依据。角色：' + encode(entry['proposal'])}
    # Callers close their read connection before model IO where possible; no write transaction is held.
    results = evaluate({'confirmed': payload['context'], 'branches': payload['parsed']['branches']}, questions)
    for entry in entries:
        result = results[entry['key']]
        entry.update(decision=result.value if result.status == 'ok' else 'unknown', reason=result.evidence,
                     provider=result.provider, fallback=result.fallback)
        probabilities = sorted(result.probabilities.values(), reverse=True)
        if len(probabilities) > 1 and probabilities[0] - probabilities[1] < .15:
            entry['decision'] = 'unknown'
            entry['reason'] = '候选身份或新增必要性的判断过于接近，请 GM 核对'
        if not entry['reason']:
            entry['decision'] = 'unknown'
            entry['reason'] = '缺少身份与新增必要性的依据，请 GM 核对'
        entry['model_decision'], entry['model_reason'] = entry['decision'], entry['reason']
        log.info('NPC review decision=%s provider=%s fallback=%s', entry['decision'], result.provider, result.fallback)
    payload['entries'] = entries
    return entries


def response(review_id, payload):
    entries=[{k:v for k,v in e.items() if k not in {'proposal','original_actions'}}
             for e in [*payload['entries'],*payload.get('map_entries',[])]]
    return {'status': 'review_required', 'review_id': review_id,
            'message': payload.get('message', '请核对地点身份、发现与进入方式' if payload.get('map_entries') else '请核对角色身份与新增必要性'),
            'entries': entries}


def fingerprint(fx):
    effects = {key: fx.get(key) for key in EFFECT_KEYS}
    # Preserve seals on existing saved candidates that predate reference diagnostics.
    effects.update({key: fx[key] for key in ('deferred_effects', 'reference_warnings') if fx.get(key)})
    effects.update({key: fx[key] for key in ('spatial_context', 'review_decisions', 'summary_contract') if key in fx})
    return hashlib.sha256(encode(effects).encode()).hexdigest()


def seal(conn, fx, review_id, branch_index):
    row = conn.execute('SELECT * FROM npc_generation_reviews WHERE id=?', (review_id,)).fetchone()
    if not row or row['epoch'] != epoch(conn) or row['state_token'] != state_token(conn):
        raise HTTPException(409, '角色审核已过期，请重新推演')
    payload = json.loads(row['payload'])
    key = str(branch_index)
    payload.setdefault('seals', {})[key] = fingerprint(fx)
    fx['npc_review'] = {'id': review_id, 'branch': key}
    actual_refs = {e['ref'] for e in collect([copy.deepcopy(fx)])}
    entries = [e for e in payload['entries'] if e['branch'] == branch_index and e['decision'] == 'create' and e['ref'] in actual_refs]
    fx['npc_additions'] = [{'name': e['name'], 'reason': e['reason']} for e in entries]
    conn.execute('UPDATE npc_generation_reviews SET payload=? WHERE id=?', (encode(payload), review_id))


def check_commit(conn, fx, confirmed=False):
    entries = collect([copy.deepcopy(fx)])
    audit = fx.get('npc_review')
    if not entries and not audit and not fx.get('map_actions'):
        return
    row = conn.execute('SELECT * FROM npc_generation_reviews WHERE id=?', (audit.get('id'),)).fetchone() if audit else None
    if not row or row['status'] != 'complete' or row['epoch'] != epoch(conn):
        raise HTTPException(409, '候选缺少有效的世界变化审核，请重新推演')
    payload = json.loads(row['payload'])
    if payload.get('seals', {}).get(audit.get('branch')) != fingerprint(fx):
        raise HTTPException(409, '候选内容与世界变化审核不一致，请重新推演')
    if len(entries) >= 3 and not confirmed:
        raise HTTPException(409, '本分支将新增至少 3 名角色，请查看名单并确认')


def save_review(kind, request, preparation, parsed, operation_key=None):
    with db() as conn, atomic(conn):
        if operation_key:
            old = conn.execute('SELECT * FROM npc_generation_reviews WHERE operation_key=?', (operation_key,)).fetchone()
            if old:
                old_payload = json.loads(old['payload'])
                if old_payload['request'] != request or old['epoch'] != epoch(conn):
                    raise HTTPException(409, '请求键已用于其他请求或世界已载入，请重新发起')
                return old['id']
        ctx = context(conn, request)
        token = state_token(conn)
        if preparation and preparation['token'] != token:
            raise HTTPException(409, '世界已变化，请重新推演')
        review_id = uuid.uuid4().hex
        payload = {'kind':kind,'request': request, 'preparation': preparation, 'parsed': parsed, 'token':token,
                   'original_parsed': copy.deepcopy(parsed), 'context': ctx, 'entries': []}
        conn.execute('INSERT INTO npc_generation_reviews(id,operation_key,epoch,state_token,kind,payload,created_at) '
                     'VALUES(?,?,?,?,?,?,?)', (review_id, operation_key, epoch(conn), token, kind, encode(payload), now()))
    return review_id


def rewrite(payload):
    """Rewrite only affected alternatives, then deterministically enforce identity decisions."""
    import agent
    branches = payload['parsed']['branches']
    affected = sorted({e['branch'] for e in payload['entries'] if e['decision'] != 'create'})
    if not affected:
        return
    original = copy.deepcopy(branches)
    raw = agent._call_ai('你是候选分支修正器。只返回JSON {"branches":[修正后的分支]}。'
        '严格执行角色审核结论，仅修正提供的分支，保持玩家行动和无关结果不变。'
        '复用已有角色必须采用其身份与描述，不覆盖其现有属性。取消新增的背景人物可保留无身份背景描写，'
        '但不能继续参与需要实体的互动。移除其所有副作用与关系引用。禁止添加未审核人物。'
        '保留其他角色的ref；已复用的ref可保留在副作用中，服务器统一映射。',
        encode({'confirmed': payload['context'], 'branches': [branches[i] for i in affected],
                'decisions': [e for e in payload['entries'] if e['branch'] in affected]}),
        temperature=.1, max_tokens=6000, json_mode=True, model_override=payload['request'].get('model'))
    from engine import validate_branches
    repaired = validate_branches(parse_object(raw))
    if len(repaired) != len(affected):
        raise ValueError('角色修正返回的分支数量不一致')
    for bi, branch in zip(affected, repaired):
        entries = [e for e in payload['entries'] if e['branch'] == bi]
        permitted = {e['ref']: e for e in entries}
        for proposed in collect([copy.deepcopy(branch)]):
            if proposed['ref'] not in permitted:
                raise ValueError('修正引入了未经审核的新人物')
        mapping, omitted = {}, set()
        for entry in entries:
            decision = entry['decision']
            if decision.startswith('reuse:'):
                mapping[entry['ref']] = decision[6:]
            elif decision.startswith('same:'):
                mapping[entry['ref']] = decision[5:]
            elif decision == 'omit':
                omitted.add(entry['ref'])
        for source in mapping:
            seen, target = {source}, mapping[source]
            while target in mapping:
                if target in seen: raise ValueError('角色复用存在循环')
                seen.add(target); target = mapping[target]
            if target in omitted: raise ValueError('不能复用已取消的人物')
            mapping[source] = target
        # Approved new profiles remain server-owned; repair cannot swap their identity/details.
        branch['npc'] = copy.deepcopy(original[bi].get('npc'))
        branch['new_entities'] = copy.deepcopy(original[bi].get('new_entities', []))
        if branch['npc'] and branch['npc'].get('ref', 'new_npc') in (mapping.keys() | omitted):
            branch['npc'] = None
        branch['new_entities'] = [e for e in branch['new_entities'] if e.get('ref') not in (mapping.keys() | omitted)]
        for key in ('stat_changes', 'emotion_deltas', 'npc_memories', 'entity_changes', 'state_changes'):
            for change in branch.get(key, []):
                for field in ('entity_uid', 'target_uid'):
                    value = change.get(field)
                    if value in omitted: raise ValueError('修正仍引用已取消角色')
                    if value in mapping: change[field] = mapping[value]
        if branch.get('san_delta'):
            value = branch['san_delta'].get('entity_uid')
            if value in omitted: raise ValueError('修正仍引用已取消角色')
            branch['san_delta']['entity_uid'] = mapping.get(value, value)
        # No alternate creation route may escape the approved declarations.
        if any(c.get('new') and not c.get('entity_uid') for c in branch.get('entity_changes', [])):
            raise ValueError('修正新增了未经审核的实体声明')
        reused = set(mapping.values())
        for change in branch.get('entity_changes', []):
            if change.get('entity_uid') in reused:
                for field in ('name', 'description', 'state_desc', 'aliases'):
                    change.pop(field, None)
        branch['map_actions']=copy.deepcopy(original[bi].get('map_actions'))
        branches[bi] = branch
    # A separate bounded verification checks narrative coherence; failures require GM, never another auto loop.
    checks = {str(bi): {'type': 'choice', 'criteria': {'ok': '正文及副作用符合全部审核决定', 'unknown': '冲突或无法确定'},
                       'instructions': '核对修正后的角色身份、取消新增与相关叙事是否一致，不得出现未声明新互动人物。'}
              for bi in affected}
    result = evaluate({'branches': {str(i): branches[i] for i in affected}, 'decisions': payload['entries']}, checks)
    if any(r.status != 'ok' or r.value != 'ok' for r in result.values()):
        raise ValueError('修正后的叙事仍需要 GM 核对')


def public_existing(conn, uid):
    entity = resolve_entity(conn, uid=uid)
    char = conn.execute('SELECT * FROM characters WHERE entity_uid=?', (uid,)).fetchone()
    return dict(char) if char else {'entity_uid': uid, 'name': entity['name'], 'role': 'NPC'}


def complete_npc(conn, payload):
    entry = payload['entries'][0]
    decision = entry['decision']
    if decision.startswith('reuse:'):
        return {'status': 'success', 'outcome': 'reused', 'npc': public_existing(conn, decision[6:]), 'message': '已复用现有角色，没有新增档案'}
    if decision == 'omit':
        return {'status': 'success', 'outcome': 'not_needed', 'npc': None, 'message': '当前场景无需登记新角色'}
    from world import create_entity
    p = entry['proposal']
    from engine import integer
    uid = create_entity(conn, name=p['name'], entity_type='npc', aliases=p.get('aliases', []),
                        state_desc=encode({'desc': p.get('backstory', p.get('description', ''))}),
                        location=payload['context']['location'], room_id=payload['context']['room_id'])
    char_id = conn.execute("INSERT INTO characters(name,role,hp,san,inventory,personality,entity_uid) VALUES(?,'NPC',?,?,?,?,?)",
                           (p['name'], integer(p.get('hp', 50), 'HP'), integer(p.get('san', 60), 'SAN'),
                            p.get('inventory', ''), p.get('backstory', p.get('description', '')), uid)).lastrowid
    npc = {**p, 'id': char_id, 'entity_uid': uid, 'role': 'NPC'}
    record_event(conn, 'character_create', f"角色 {p['name']} 登记", {'entity_uid': uid, 'review': entry}, payload['context']['timeline_id'])
    refresh_wiki(conn)
    return {'status': 'success', 'outcome': 'created', 'npc': npc}


def run(review_id, choices=None, operation_key=None):
    """Lease across IO; each failed attempt leaves a retryable, authoritative review."""
    with db() as conn, atomic(conn):
        row = conn.execute('SELECT * FROM npc_generation_reviews WHERE id=?', (review_id,)).fetchone()
        if not row: raise HTTPException(404, '角色审核不存在')
        if row['epoch'] != epoch(conn): raise HTTPException(409, '世界已载入或回滚，请重新发起')
        resolution = encode({'choices': choices, 'operation_key': operation_key}) if choices is not None else None
        if row['status'] == 'complete':
            if resolution is not None and resolution != row['resolution']:
                raise HTTPException(409, '审核已完成，不能修改已处理的选择')
            return json.loads(row['result'])
        if row['state_token'] != state_token(conn): raise HTTPException(409, '世界已变化，请重新推演')
        if row['lease_until'] > time.time(): raise HTTPException(409, '角色审核正在处理，请稍后重试')
        payload, kind, token = json.loads(row['payload']), row['kind'], row['state_token']
        lease = time.time() + 600
        conn.execute('UPDATE npc_generation_reviews SET lease_until=? WHERE id=?', (lease, review_id))
    try:
        payload['token']=token
        if not payload.get('judged') and collect(copy.deepcopy(payload['parsed']['branches'])):
            with db() as conn: judge(conn, payload)
        if 'map_entries' not in payload:
            from map_review import judge as judge_map, has_actions
            if has_actions(payload['parsed']['branches']):
                with db() as conn: judge_map(conn,payload)
            else:
                payload['map_entries'] = []
        payload['judged'] = True
        if choices is not None:
            from map_review import choice_keys,validate_choices,apply_choices
            entries = {e['key']: e for e in payload['entries']}
            if set(choices) != set(entries)|choice_keys(payload): raise HTTPException(422, '请为每项世界变化选择处理方式')
            for key in entries:
                decision=choices[key]
                entry = entries[key]
                permitted = {'create', 'omit'} | {'reuse:' + c['entity_uid'] for c in entry['candidates']}
                permitted |= {'same:' + other['ref'] for other in entries.values()
                              if other['branch'] == entry['branch'] and other['key'] < key}
                if decision not in permitted: raise HTTPException(422, '角色处理选择无效')
            validate_choices(payload,choices)
            for key in entries:
                decision=choices[key]
                reason = ('GM 确认需要登记为独立的新人物' if decision == 'create' else
                          'GM 选择不登记此人物' if decision == 'omit' else
                          'GM 确认为本分支中的同一人物' if decision.startswith('same:') else 'GM 选择复用已有身份')
                entries[key].update(decision=decision, reason=reason, manual=True)
            apply_choices(payload,choices)
            log.info('NPC review manual_override count=%s', len(choices))
        from map_review import unresolved
        if any(e['decision'] == 'unknown' for e in payload['entries']) or unresolved(payload):
            return response(review_id, payload)
        if kind == 'npc' and payload['entries'][0]['decision'] == 'create' and not payload.get('profile_ready'):
            raw = generation.generate('根据已审核的新角色需求生成一个NPC档案。仅返回JSON：'
                '{"name":"姓名","hp":50,"san":60,"inventory":"物品","backstory":"背景","aliases":[]}。'
                '不能冒充现有身份。', encode({'context': payload['context'], 'review': payload['entries']}),
                json_mode=True, temperature=.7, max_tokens=1000)
            proposal = {**parse_object(raw), 'ref': 'new_npc'}
            from engine import integer
            if not isinstance(proposal.get('name'), str) or not proposal['name'].strip():
                raise ValueError('生成角色缺少有效姓名，请重试')
            integer(proposal.get('hp', 50), 'HP'); integer(proposal.get('san', 60), 'SAN')
            if any(not isinstance(proposal.get(k, ''), str) for k in ('inventory', 'backstory')):
                raise ValueError('生成角色档案格式无效，请重试')
            profile = copy.deepcopy(payload)
            profile['parsed'] = {'branches': [{'npc': proposal}]}
            profile['profile_ready'] = True
            with db() as conn: judge(conn, profile)
            payload = profile
            if choices is not None:
                # The GM chose creation before seeing this generated identity: review the actual profile if uncertain.
                payload['message'] = '角色档案已生成，请核对实际身份'
            if any(e['decision'] == 'unknown' for e in payload['entries']): return response(review_id, payload)
        if kind in {'dynamic','map','branch'}:
            before_repair = copy.deepcopy(payload['parsed'])
            try:
                if kind == 'branch':
                    from selected_branches import canonicalize
                    canonicalize(payload)
                else:
                    rewrite(payload)
                    from map_review import repair_and_preflight
                    repair_and_preflight(payload)
            except Exception as exc:
                payload['parsed'] = before_repair
                if isinstance(exc,HTTPException) and exc.status_code==409: raise
                payload['message'] = '世界变化修正未完成，请核对后重试：' + str(exc)[:300]
                for e in payload['entries']: e['decision'] = 'unknown'
                for e in payload.get('map_entries',[]): e['decision']='unknown'
                return response(review_id, payload)
        with db() as conn, atomic(conn):
            owned = conn.execute('SELECT lease_until FROM npc_generation_reviews WHERE id=?', (review_id,)).fetchone()
            if not owned or owned[0] != lease: raise HTTPException(409, '审核处理权已变化，请刷新重试')
            if token != state_token(conn): raise HTTPException(409, '世界已变化，审核结果未保存，请重新推演')
            conn.execute('UPDATE npc_generation_reviews SET payload=? WHERE id=?', (encode(payload), review_id))
            if kind == 'dynamic':
                import agent
                from engine import normalize_branch_references
                for branch in payload['parsed']['branches']:
                    normalize_branch_references(conn, branch, tolerant=True, allow_declarations=False,
                        timeline_id=payload['request'].get('timeline_id'),
                        resolved_entities=payload['request'].get('resolved_entities'))
                parsed = {**payload['parsed'], '_state_token': token, '_npc_review_id': review_id}
                req = payload['request']
                result = agent._post_process_dynamic_result(conn, parsed, payload['context']['scene'], req['player_action'],
                    req['current_node_id'], timeline_id=req.get('timeline_id'), tl_id_for_memory=req.get('timeline_id'), action_type=req['action_type'])
                options, npc, stats, map_result, thought, entities = result
                output = {'status': 'success', 'new_options': options, 'spawned_npc': npc, 'stat_changes': stats,
                          'map_result': map_result, 'thought_process': thought, 'entity_updates': entities}
            elif kind == 'branch':
                from selected_branches import complete
                output = complete(conn, payload, review_id)
            elif kind=='map':
                from map_review import complete
                output=complete(conn,payload)
            else:
                output = complete_npc(conn, payload)
            conn.execute("UPDATE npc_generation_reviews SET status='complete',result=?,resolution=?,lease_until=0 WHERE id=?",
                         (encode(output), resolution, review_id))
            log.info('NPC review complete kind=%s approved_new=%s reuse_decisions=%s', kind, sum(e['decision'] == 'create' for e in payload['entries']),
                     sum(e['decision'].startswith('reuse:') for e in payload['entries']))
        return output
    finally:
        with db() as conn, atomic(conn):
            # Do not overwrite seals saved by draft creation in the completion transaction.
            conn.execute("UPDATE npc_generation_reviews SET payload=?,lease_until=0 WHERE id=? AND lease_until=? AND status!='complete'",
                         (encode(payload), review_id, lease))


def review_dynamic(request, preparation, parsed):
    from map_review import has_actions
    if not collect(parsed['branches']) and not has_actions(parsed['branches']): return None
    # Generated metadata is never accepted as server approval.
    for branch in parsed['branches']:
        for key in ('npc_review', 'npc_additions'): branch.pop(key, None)
    review_id = save_review('dynamic', request.model_dump(), preparation, parsed,
                            'dynamic:' + request.operation_key if request.operation_key else None)
    return run(review_id)


def cached_dynamic(request):
    if not request.operation_key: return None
    with db() as conn:
        row = conn.execute('SELECT * FROM npc_generation_reviews WHERE operation_key=?',
                           ('dynamic:' + request.operation_key,)).fetchone()
        if not row: return None
        payload = json.loads(row['payload'])
        if row['epoch'] != epoch(conn) or payload['preparation'].get('request_input') != request.model_dump():
            raise HTTPException(409, '请求键已用于其他请求或世界已载入，请重新发起')
        if row['status'] == 'complete': return json.loads(row['result'])
        if row['state_token'] != state_token(conn): raise HTTPException(409, '世界已变化，请重新推演')
        if row['lease_until'] > time.time(): raise HTTPException(409, '角色审核正在处理，请稍后重试')
        if payload['entries']: return response(row['id'], payload)
    return run(row['id'])


def generate_npc(request):
    req = request.model_dump()
    parsed = {'branches': [{'npc': {'ref': 'new_npc', 'name': '待登记人物',
                                   'backstory': '判断当前场景是否需要一个尚未登记的互动人物；若已有合适人物则复用。'}}]}
    key = 'npc:' + request.operation_key if request.operation_key else None
    review_id = save_review('npc', req, None, parsed, key)
    return run(review_id)


class Resolution(BaseModel):
    choices: dict[str, str]
    operation_key: str = Field(min_length=1, max_length=200)


@router.post('/api/ai/npc-reviews/{review_id}/resolve')
@router.post('/api/ai/map-reviews/{review_id}/resolve')
def resolve_review(review_id: str, request: Resolution):
    try:
        return run(review_id, request.choices, request.operation_key)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
