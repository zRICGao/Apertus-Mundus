"""Place identity/necessity review, sharing the server-owned world review lifecycle."""
import copy
import json
import re

from fastapi import HTTPException
from providers import evaluate
from world import encode, state_token
from spatial import aliases, room_record, current_room
from map_effects import plan_map_actions


def has_actions(branches):
    return any(b.get('map_actions') for b in branches)


def candidates(conn, ctx, proposed, narrative):
    anchor = room_record(conn, ctx['room_id']) if ctx['room_id'] else None
    if not anchor: return []
    query = ' '.join([str(proposed.get('label','')), str(proposed.get('description','')), narrative, ctx['action']]).lower()
    tokens = set(re.findall(r'[a-z0-9_]{2,}|[\u4e00-\u9fff]{2}', query))
    edges = {r['to_id'] if r['from_id']==anchor['id'] else r['from_id']:dict(r)
             for r in conn.execute('SELECT * FROM map_edges WHERE from_id=? OR to_id=?',(anchor['id'],anchor['id']))}
    ranked=[]
    for row in conn.execute('SELECT * FROM map_rooms WHERE map_id=?',(anchor['map_id'],)):
        labels=[row['label'],*aliases(row['aliases'])]
        text=' '.join([*labels,row['description']]).lower()
        score=sum(20 for label in labels if label.lower() in query)+sum(t in text for t in tokens)
        score+=5 if row['floor']==proposed.get('floor',anchor['floor']) else 0
        score+=4 if row['id'] in edges else 0
        ranked.append((score,{'room_id':row['id'],'name':row['label'],'aliases':labels[1:],
            'floor':row['floor'],'map_id':row['map_id'],'description':row['description'], 'status':row['state'],
            'location':f"第{row['floor']}层",'connection':edges.get(row['id']),
            'choice':('reveal:' if row['state']=='unknown' else 'reuse:')+str(row['id'])}))
    ranked.sort(key=lambda pair:(-pair[0],pair[1]['room_id']))
    return [item for _,item in ranked[:24]]


def judge(conn,payload):
    entries=[]; questions={}; ctx=payload['context']
    for bi,branch in enumerate(payload['parsed']['branches']):
        actions=branch.get('map_actions')
        if not actions: continue
        try:
            plan=plan_map_actions(conn,branch,ctx['room_id'],ctx['timeline_id'])
            invalid=''
        except (ValueError,TypeError,KeyError) as exc:
            invalid=str(exc); plan=None
        if payload['request'].get('action_type')=='dialogue':
            invalid='对话模式不能改变队伍位置或地图，请取消空间变化或改用行动模式'
        new=actions.get('new_room') if isinstance(actions,dict) else None
        if not new and not invalid: continue  # Existing, unambiguous actions need no identity model call.
        mode='new' if isinstance(new,dict) else 'movement' if isinstance(actions,dict) and set(actions)=={'movement'} else 'invalid'
        if payload['request'].get('action_type')=='dialogue':
            mode='invalid'; invalid='对话模式不能改变队伍位置或地图，请取消空间变化或改用行动模式'
        new=new if isinstance(new,dict) else {}
        entry={'key':f'map:{bi}','kind':'map','mode':mode,'branch':bi,'ref':new.get('ref','new_room'),
               'name':new.get('label','空间变化'),'description':new.get('description',''),
               'original_actions':copy.deepcopy(actions),'candidates':candidates(conn,ctx,new,branch.get('node_content','')),
               'decision':'unknown','reason':invalid,'arrival':'unknown' if mode=='new' else 'enter' if mode=='movement' else 'stay'}
        entry['options']={'omit':'不改变地图，仅保留合适的背景描写','unknown':'地点身份或必要性不确定'}
        if mode=='new' and not invalid: entry['options']['create']='确有必要登记一个独立的新区域'
        if mode!='invalid':
            entry['options'].update({c['choice']:encode(c) for c in entry['candidates']})
        entries.append(entry)
        if mode=='invalid': continue
        questions[entry['key']]={'type':'choice','criteria':entry['options'],
            'instructions':'审核拟发生的地点身份与新增必要性。名称、别名、地图、楼层、描述和连接共同识别地点；'
                '不能只因同名合并，也不能把换称呼的旧地点当成新人造区域。优先复用或揭示已有区域。'
                '纯气氛或同一地点内的叙事不建房间。证据不足选unknown；evidence说明依据。'}
        if mode=='new':
            movement=actions.get('movement') or {}
            moving_new=movement.get('target_room_ref')==entry['ref'] or (movement.get('target_room_id') is None and movement.get('target_room_label')==new.get('label'))
            if payload.get('kind')=='map': entry['arrival']='enter' if payload['request'].get('enter') else 'stay'
            elif moving_new: entry['arrival']='enter'
            elif movement: entry['arrival']='stay'
            else:
                questions[entry['key']+':arrival']={'type':'choice','criteria':{'enter':'本分支已经进入目标区域','stay':'仅发现目标，队伍仍在原地','unknown':'无法确定是否进入'},
                    'instructions':'只判断这个候选结果中的队伍最终位置。区分发现、看见入口和实际进入；不得仅因玩家想进入就判已进入。'}
    if questions:
        results=evaluate({'confirmed':ctx,'branches':payload['parsed']['branches'], 'places':entries},questions)
        for entry in entries:
            r=results.get(entry['key'])
            if r:
                entry.update(decision=r.value if r.status=='ok' else 'unknown',reason=r.evidence or '缺少地点身份与新增必要性的依据',provider=r.provider,fallback=r.fallback)
                probs=sorted(r.probabilities.values(),reverse=True)
                if not r.evidence or (len(probs)>1 and probs[0]-probs[1]<.15): entry['decision']='unknown'
            arrival=results.get(entry['key']+':arrival')
            if arrival: entry['arrival']=arrival.value if arrival.status=='ok' else 'unknown'
            entry['model_decision']=entry['decision']; entry['model_reason']=entry['reason']
    payload['map_entries']=entries
    return entries


def choice_keys(payload):
    return {key for e in payload.get('map_entries',[]) for key in
            ([e['key'],e['key']+':arrival'] if e['mode']=='new' else [e['key']])}


def validate_choices(payload,choices):
    for entry in payload.get('map_entries',[]):
        allowed=set(entry['options'])-{'unknown'}
        if choices[entry['key']] not in allowed: raise HTTPException(422,'地点处理选择无效')
        if entry['mode']=='new' and choices[entry['key']+':arrival'] not in {'enter','stay'}:
            raise HTTPException(422,'请明确是否进入目标区域')


def apply_choices(payload,choices):
    for entry in payload.get('map_entries',[]):
        entry.update(decision=choices[entry['key']],manual=True,reason='GM 已明确选择地点处理方式')
        if entry['mode']=='new': entry['arrival']=choices[entry['key']+':arrival']


def unresolved(payload):
    return any(e['decision']=='unknown' or (e['decision']!='omit' and e['mode']=='new' and e['arrival']=='unknown') for e in payload.get('map_entries',[]))


def canonical_actions(entry):
    actions=copy.deepcopy(entry['original_actions']); decision=entry['decision']
    if entry['mode']=='invalid': return None
    new=actions.get('new_room') or {}
    movement=actions.get('movement') or {}
    is_new_move=movement.get('target_room_ref')==entry['ref'] or (movement.get('target_room_id') is None and movement.get('target_room_label')==new.get('label') and bool(new))
    if decision=='omit':
        if entry['mode']=='movement': return None
        actions.pop('new_room',None)
        if is_new_move: actions.pop('movement',None)
    elif decision.startswith(('reuse:','reveal:')):
        uid=int(decision.split(':')[1]); actions.pop('new_room',None)
        if decision.startswith('reveal:'): actions['reveal_room']={'room_id':uid}
        candidate=next(c for c in entry['candidates'] if c['room_id']==uid)
        if candidate['connection'] and not candidate['connection']['discovered']:
            actions['reveal_edge']={'edge_id':candidate['connection']['id']}
        if entry['arrival']=='enter':
            actions['movement']={'target_room_id':uid}
        elif is_new_move: actions.pop('movement',None)
    elif decision=='create':
        new.setdefault('ref',entry['ref']); actions['new_room']=new
        if entry['arrival']=='enter': actions['movement']={'target_room_ref':entry['ref']}
        elif is_new_move: actions.pop('movement',None)
    else: raise ValueError('地点审核尚未完成')
    return actions or None


def repair_and_preflight(payload):
    import agent
    import npc_review
    changed=[]
    branches=payload['parsed']['branches']
    for entry in payload.get('map_entries',[]):
        bi=entry['branch']; actions=canonical_actions(entry)
        original=copy.deepcopy(entry['original_actions'])
        if original and original.get('new_room'):
            original['new_room'].setdefault('ref',entry['ref'])
        if actions!=original: changed.append(bi)
        branches[bi]['map_actions']=actions
    # Validate canonical decisions before spending a generation call on a repair.
    with npc_review.db() as conn:
        if state_token(conn)!=payload['token']: raise HTTPException(409,'世界已变化，请重新推演')
        for branch in branches: plan_map_actions(conn,branch,payload['context']['room_id'],payload['context']['timeline_id'])
    if changed:
        changed=sorted(set(changed))
        raw=agent._call_ai('你是地图候选修正器。只返回JSON {"branches":[修正后的分支]}。'
            '严格按已审核地图动作修正分支标题、选项与正文，区分发现与进入，确保最终位置一致。'
            '不能新增人物、改写无关事件或修改副作用；取消地图变化时不能仍叙述已经进入被取消的新区域。',
            encode({'confirmed':payload['context'],'branches':[branches[i] for i in changed],
                    'map_decisions':payload.get('map_entries',[])}),temperature=.1,max_tokens=6000,json_mode=True,
            model_override=payload['request'].get('model'))
        from engine import validate_branches
        from providers import parse_object
        repaired=validate_branches(parse_object(raw))
        if len(repaired)!=len(changed): raise ValueError('地图修正返回的分支数量不一致')
        for bi,branch in zip(changed,repaired):
            for field in ('text','node_name','node_content','entity_updates'):
                branches[bi][field]=branch[field]
        checks={str(i):{'type':'choice','criteria':{'ok':'正文、发现/进入和全部副作用一致','unknown':'仍有冲突或无法确定'},
                       'instructions':'核对修正后的叙事是否遵守地图决策和最终位置，且保留人物与其他事件事实。'} for i in changed}
        results=evaluate({'branches':{str(i):branches[i] for i in changed},'confirmed':payload['context'],
                          'map_decisions':payload.get('map_entries',[])},checks)
        if any(r.status!='ok' or r.value!='ok' for r in results.values()): raise ValueError('地图正文仍需 GM 核对')


def auto_room(request):
    import npc_review
    if not request.operation_key: raise HTTPException(422,'自动新增区域须提供 operation_key；GM 手动新增请使用地图编辑器')
    req=request.model_dump()
    req['current_node_id']=request.current_node_id or request.node_id
    req['player_action']='发现并进入新区域' if request.enter else '发现新区域，但不进入'
    cached=None
    with npc_review.db() as conn:
        row=conn.execute('SELECT * FROM npc_generation_reviews WHERE operation_key=?',('map:'+request.operation_key,)).fetchone()
        if row:
            if json.loads(row['payload'])['request']!=req or row['epoch']!=npc_review.epoch(conn):
                raise HTTPException(409,'请求键已用于其他地图请求或世界已载入')
            cached=row['id']
    if cached: return npc_review.run(cached)
    with npc_review.db() as conn:
        ctx=npc_review.context(conn,req)
        parent=room_record(conn,request.parent_room_id,request.map_id)
        if ctx['room_id']!=parent['id']: raise HTTPException(409,'父地点与队伍当前位置不一致')
    new={'ref':'new_room','label':request.label,'description':request.description,'edge_type':request.edge_type,'aliases':request.aliases}
    if request.floor is not None: new['floor']=request.floor
    actions={'new_room':new}
    if request.enter: actions['movement']={'target_room_ref':'new_room'}
    parsed={'branches':[{'text':'发现区域','node_name':request.label,'node_content':req['player_action']+'：'+request.label,
                         'map_actions':actions}]}
    review_id=npc_review.save_review('map',req,None,parsed,'map:'+request.operation_key)
    return npc_review.run(review_id)


def complete(conn,payload):
    from map_effects import apply_map_actions
    from world import record_event,refresh_wiki
    actions=payload['parsed']['branches'][0].get('map_actions')
    result=apply_map_actions(conn,{'map_actions':actions},payload['context']['room_id'],payload['context']['timeline_id'])
    if actions:
        record_event(conn,'map_change','已审核的地图变化',{'actions':actions,'result':result},payload['context']['timeline_id'])
        refresh_wiki(conn)
    outcome='created' if result['new_room'] else 'reused' if actions else 'not_needed'
    return {'status':'success','outcome':outcome,'id':result['new_room']['room_id'] if result['new_room'] else None,'map_result':result}
