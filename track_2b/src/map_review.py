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
            'location':f"Floor {row['floor']}",'connection':edges.get(row['id']),
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
            invalid='Dialogue mode cannot change the party position or map; remove spatial changes or switch to action mode'
        new=actions.get('new_room') if isinstance(actions,dict) else None
        if not new and not invalid: continue  # Existing, unambiguous actions need no identity model call.
        mode='new' if isinstance(new,dict) else 'movement' if isinstance(actions,dict) and set(actions)=={'movement'} else 'invalid'
        if payload['request'].get('action_type')=='dialogue':
            mode='invalid'; invalid='Dialogue mode cannot change the party position or map; remove spatial changes or switch to action mode'
        new=new if isinstance(new,dict) else {}
        entry={'key':f'map:{bi}','kind':'map','mode':mode,'branch':bi,'ref':new.get('ref','new_room'),
               'name':new.get('label','Spatial change'),'description':new.get('description',''),
               'original_actions':copy.deepcopy(actions),'candidates':candidates(conn,ctx,new,branch.get('node_content','')),
               'decision':'unknown','reason':invalid,'arrival':'unknown' if mode=='new' else 'enter' if mode=='movement' else 'stay'}
        entry['options']={'omit':'Keep the map unchanged and retain only appropriate background descriptions','unknown':'Location identity or necessity is uncertain'}
        if mode=='new' and not invalid: entry['options']['create']='A distinct new area genuinely needs to be registered'
        if mode!='invalid':
            entry['options'].update({c['choice']:encode(c) for c in entry['candidates']})
        entries.append(entry)
        if mode=='invalid': continue
        questions[entry['key']]={'type':'choice','criteria':entry['options'],
            'instructions':'Review the identity and necessity of the proposed location. Its name, aliases, map, floor, description, and connections jointly establish its identity; '
                'do not merge locations solely because they share a name, or treat an existing location with a different name as a new area. Prefer reusing or revealing existing areas. '
                'Do not create rooms for atmosphere alone or events within the same location. Choose unknown when evidence is insufficient; explain the basis in evidence.'}
        if mode=='new':
            movement=actions.get('movement') or {}
            moving_new=movement.get('target_room_ref')==entry['ref'] or (movement.get('target_room_id') is None and movement.get('target_room_label')==new.get('label'))
            if payload.get('kind')=='map': entry['arrival']='enter' if payload['request'].get('enter') else 'stay'
            elif moving_new: entry['arrival']='enter'
            elif movement: entry['arrival']='stay'
            else:
                questions[entry['key']+':arrival']={'type':'choice','criteria':{'enter':'This branch has already entered the target area','stay':'The destination was only discovered; the party remains in place','unknown':'Cannot determine whether the party entered'},
                    'instructions':'Judge only the party final position in this candidate outcome. Distinguish discovering a place, seeing an entrance, and actually entering; intent alone does not establish entry.'}
    if questions:
        results=evaluate({'confirmed':ctx,'branches':payload['parsed']['branches'], 'places':entries},questions)
        for entry in entries:
            r=results.get(entry['key'])
            if r:
                entry.update(decision=r.value if r.status=='ok' else 'unknown',reason=r.evidence or 'Insufficient evidence for location identity and the need to create it',provider=r.provider,fallback=r.fallback)
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
        if choices[entry['key']] not in allowed: raise HTTPException(422,'Invalid location-review choice')
        if entry['mode']=='new' and choices[entry['key']+':arrival'] not in {'enter','stay'}:
            raise HTTPException(422,'Specify whether the party enters the target area')


def apply_choices(payload,choices):
    for entry in payload.get('map_entries',[]):
        entry.update(decision=choices[entry['key']],manual=True,reason='GM explicitly selected the location handling method')
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
    else: raise ValueError('Location review is not complete')
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
        if state_token(conn)!=payload['token']: raise HTTPException(409,'World state has changed; rerun the simulation')
        for branch in branches: plan_map_actions(conn,branch,payload['context']['room_id'],payload['context']['timeline_id'])
    if changed:
        changed=sorted(set(changed))
        raw=agent._call_ai('You revise candidate map branches. Return only JSON {"branches":[revised branches]}. '
            'Revise branch titles, options, and narrative strictly according to approved map actions. Distinguish discovery from entry and ensure the final location is consistent. '
            'Do not add characters, rewrite unrelated events, or change side effects. If a map change was canceled, do not claim the party entered the canceled new area.',
            encode({'confirmed':payload['context'],'branches':[branches[i] for i in changed],
                    'map_decisions':payload.get('map_entries',[])}),temperature=.1,max_tokens=6000,json_mode=True,
            model_override=payload['request'].get('model'))
        from engine import validate_branches
        from providers import parse_object
        repaired=validate_branches(parse_object(raw))
        if len(repaired)!=len(changed): raise ValueError('The map revision returned a different number of branches')
        for bi,branch in zip(changed,repaired):
            for field in ('text','node_name','node_content','entity_updates'):
                branches[bi][field]=branch[field]
        checks={str(i):{'type':'choice','criteria':{'ok':'Narrative, discovery/entry, and all side effects are consistent','unknown':'Conflicts remain or the outcome is uncertain'},
                       'instructions':'Check that the revised narrative respects the map decision and final position, while preserving character identities and other event facts.'} for i in changed}
        results=evaluate({'branches':{str(i):branches[i] for i in changed},'confirmed':payload['context'],
                          'map_decisions':payload.get('map_entries',[])},checks)
        if any(r.status!='ok' or r.value!='ok' for r in results.values()): raise ValueError('The map narrative still requires GM review')


def auto_room(request):
    import npc_review
    if not request.operation_key: raise HTTPException(422,'Automatically creating an area requires operation_key; use the map editor to create areas manually as GM')
    req=request.model_dump()
    req['current_node_id']=request.current_node_id or request.node_id
    req['player_action']='Discover and enter a new area' if request.enter else 'Discover a new area without entering'
    cached=None
    with npc_review.db() as conn:
        row=conn.execute('SELECT * FROM npc_generation_reviews WHERE operation_key=?',('map:'+request.operation_key,)).fetchone()
        if row:
            if json.loads(row['payload'])['request']!=req or row['epoch']!=npc_review.epoch(conn):
                raise HTTPException(409,'The request key was used for another map request, or the world was reloaded')
            cached=row['id']
    if cached: return npc_review.run(cached)
    with npc_review.db() as conn:
        ctx=npc_review.context(conn,req)
        parent=room_record(conn,request.parent_room_id,request.map_id)
        if ctx['room_id']!=parent['id']: raise HTTPException(409,'The parent location does not match the current party position')
    new={'ref':'new_room','label':request.label,'description':request.description,'edge_type':request.edge_type,'aliases':request.aliases}
    if request.floor is not None: new['floor']=request.floor
    actions={'new_room':new}
    if request.enter: actions['movement']={'target_room_ref':'new_room'}
    parsed={'branches':[{'text':'Discover area','node_name':request.label,'node_content':req['player_action']+': '+request.label,
                         'map_actions':actions}]}
    review_id=npc_review.save_review('map',req,None,parsed,'map:'+request.operation_key)
    return npc_review.run(review_id)


def complete(conn,payload):
    from map_effects import apply_map_actions
    from world import record_event,refresh_wiki
    actions=payload['parsed']['branches'][0].get('map_actions')
    result=apply_map_actions(conn,{'map_actions':actions},payload['context']['room_id'],payload['context']['timeline_id'])
    if actions:
        record_event(conn,'map_change','Approved map change',{'actions':actions,'result':result},payload['context']['timeline_id'])
        refresh_wiki(conn)
    outcome='created' if result['new_room'] else 'reused' if actions else 'not_needed'
    return {'status':'success','outcome':outcome,'id':result['new_room']['room_id'] if result['new_room'] else None,'map_result':result}
