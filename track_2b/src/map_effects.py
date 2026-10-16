"""Plan spatial effects without writes, then execute the same validated plan atomically."""
import copy
import re
from spatial import (room_record,resolve_room,party_characters,current_room,set_position,
                     validate_room,validate_edge,placement,bind_scene,EDGE_TYPES)


def adjacent_edge(conn, current, target):
    rows=conn.execute('SELECT * FROM map_edges WHERE (from_id=? AND to_id=?) OR (from_id=? AND to_id=?)',
                       (current,target,target,current)).fetchall()
    if len(rows)!=1: raise ValueError('The destination is not adjacent or the passage is ambiguous; specify the existing passage')
    return rows[0]


def edge_reference(conn, spec, origin):
    if spec.get('edge_id') is not None:
        edge=conn.execute('SELECT * FROM map_edges WHERE id=?',(spec['edge_id'],)).fetchone()
        if not edge or edge['map_id']!=origin['map_id']: raise ValueError('The passage does not exist or belongs to another map')
    else:
        a=resolve_room(conn,spec,'from_room_id','from_label',origin['map_id'])
        b=resolve_room(conn,spec,'to_room_id','to_label',origin['map_id'])
        edge=adjacent_edge(conn,a['id'],b['id'])
    if origin['id'] not in (edge['from_id'],edge['to_id']): raise ValueError('The party is not near this passage')
    validate_edge(conn,origin['map_id'],edge['from_id'],edge['to_id'],edge['edge_type'])
    return edge


def plan_map_actions(conn, parsed, current_room_id, timeline_id=None):
    actions=copy.deepcopy(parsed.get('map_actions') or {})
    if not actions: return {'actions':{},'origin':None,'new':None,'move':None,'unlock':None,'reveal':None,'reveal_edge':None}
    if not isinstance(actions,dict) or set(actions)-{'new_room','movement','unlock_edge','reveal_room','reveal_edge'}:
        raise ValueError('The map changes contain an unsupported action')
    if not current_room_id: raise ValueError('Determine the party location first')
    origin=room_record(conn,current_room_id)
    people=party_characters(conn,timeline_id)
    plan={'actions':actions,'origin':dict(origin),'new':None,'move':None,'unlock':None,'reveal':None,'reveal_edge':None}
    new=actions.get('new_room')
    if new:
        if not isinstance(new,dict): raise ValueError('Invalid new-area format')
        new.setdefault('ref','new_room')
        if not isinstance(new['ref'],str) or not new['ref']: raise ValueError('The new area requires a valid temporary reference')
        new.setdefault('floor',origin['floor']); new.setdefault('edge_type','normal')
        validate_room({'label':new.get('label'),'floor':new['floor'],'map_id':origin['map_id'],'aliases':new.get('aliases',[])})
        if new.get('parent_room_id',origin['id'])!=origin['id']: raise ValueError('The new area must have an explicit connection to the current location')
        if new['edge_type'] not in EDGE_TYPES or (new['floor']!=origin['floor'] and new['edge_type']=='normal'):
            raise ValueError('Areas on different floors must specify stairs, an elevator, or a portal')
        if not isinstance(new.get('description',''),str): raise ValueError('The new-area description must be text')
        new['position']=placement(conn,origin['map_id'],origin['id'],new['floor'])
        plan['new']=new
    reveal=actions.get('reveal_room')
    if reveal:
        target=resolve_room(conn,reveal,'room_id','label',origin['map_id'])
        plan['reveal']=dict(target)
    reveal_edge=actions.get('reveal_edge')
    if reveal_edge:
        plan['reveal_edge']=dict(edge_reference(conn,reveal_edge,origin))
    unlock=actions.get('unlock_edge')
    if unlock:
        edge=edge_reference(conn,unlock,origin)
        if not edge['discovered'] and (not plan['reveal_edge'] or plan['reveal_edge']['id']!=edge['id']):
            raise ValueError('This passage has not been discovered; declare passage revelation first')
        consume=None
        if edge['locked']:
            key=unlock.get('key_used','').strip()
            if edge['key_item'] and key!=edge['key_item']: raise ValueError('The key does not match the passage requirements')
            if key:
                owners=[]
                for char in people:
                    items=[s.strip() for s in re.split('[,，;；\n]',char['inventory'] or '') if s.strip()]
                    if key in items: owners.append((char,items))
                if unlock.get('owner_uid'): owners=[pair for pair in owners if pair[0]['entity_uid']==unlock['owner_uid']]
                if len(owners)!=1: raise ValueError('The party does not have this key, or its owner is ambiguous')
                char,items=owners[0]; items.remove(key)
                if any(change.get('entity_uid')==char['entity_uid'] and change.get('inventory_remove')==key
                       for change in parsed.get('stat_changes',[])):
                    raise ValueError('The unlock action consumes the key; do not consume it again through inventory side effects')
                consume={'char_id':char['id'],'inventory':', '.join(items)}
        plan['unlock']={'edge':dict(edge),'consume':consume}
    move=actions.get('movement')
    if move:
        if not isinstance(move,dict): raise ValueError('Invalid movement-action format')
        ref=move.get('target_room_ref')
        # Legacy generated labels may refer to the new place; canonicalize before validation.
        if new and move.get('target_room_id') is None and move.get('target_room_label')==new['label']:
            ref=new['ref']; move.pop('target_room_label',None); move['target_room_ref']=ref
        if ref:
            if not new or ref!=new['ref']: raise ValueError('Movement references an undeclared new area')
            plan['move']={'new_ref':ref}
        else:
            target=resolve_room(conn,move,'target_room_id','target_room_label',origin['map_id'])
            if target['id']==origin['id']: raise ValueError('The destination is the current location; no movement is needed')
            edge=adjacent_edge(conn,origin['id'],target['id'])
            validate_edge(conn,origin['map_id'],edge['from_id'],edge['to_id'],edge['edge_type'])
            if not edge['discovered'] and (not plan['reveal_edge'] or plan['reveal_edge']['id']!=edge['id']):
                raise ValueError('The destination passage has not been discovered; reveal it first')
            if edge['locked'] and (not plan['unlock'] or plan['unlock']['edge']['id']!=edge['id']):
                raise ValueError('The passage is locked; unlock it first')
            plan['move']=dict(target)
    return plan


def apply_map_actions(conn, parsed, current_room_id, timeline_id=None, scene_node_id=None):
    from database import atomic
    from map import auto_place_room
    with atomic(conn):
        plan=plan_map_actions(conn,parsed,current_room_id,timeline_id)
        result={'moved_to':None,'new_room':None,'unlocked_edge':None,'revealed_room':None,'revealed_edge':None,'errors':[]}
        if not plan['actions']: return result
        if plan['reveal_edge']:
            edge=plan['reveal_edge']; conn.execute('UPDATE map_edges SET discovered=1 WHERE id=?',(edge['id'],))
            result['revealed_edge']={'edge_id':edge['id']}
        if plan['unlock']:
            unlock=plan['unlock']; edge=unlock['edge']
            if unlock['consume']:
                consume=unlock['consume']; conn.execute('UPDATE characters SET inventory=? WHERE id=?',(consume['inventory'],consume['char_id']))
            conn.execute('UPDATE map_edges SET locked=0 WHERE id=?',(edge['id'],))
            result['unlocked_edge']={'edge_id':edge['id']}
        if plan['reveal']:
            target=plan['reveal']
            conn.execute("UPDATE map_rooms SET state=CASE WHEN state='unknown' THEN 'discovered' ELSE state END WHERE id=?",(target['id'],))
            result['revealed_room']={'room_id':target['id'],'label':target['label']}
        new_id=None
        if plan['new']:
            new=plan['new']; parent=plan['origin']
            new_id=auto_place_room(conn,parent['map_id'],parent['id'],new['label'],None,new.get('description',''),
                                  floor=new['floor'],edge_type=new['edge_type'],aliases=new.get('aliases',[]))
            result['new_room']={'room_id':new_id,'label':new['label'],'floor':new['floor']}
        if plan['move']:
            target=plan['move']; target_id=new_id if target.get('new_ref') else target['id']
            target=set_position(conn,target_id,timeline_id)
            result['moved_to']={'room_id':target_id,'label':target['label']}
            if new_id==target_id and scene_node_id:
                conn.execute('UPDATE map_rooms SET node_id=? WHERE id=?',(scene_node_id,new_id))
        if scene_node_id: bind_scene(conn,scene_node_id,current_room(conn,timeline_id))
        return result
