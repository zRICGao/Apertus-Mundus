"""Validate draft branches and apply a selected branch once, in one transaction."""
import json
import re
import unicodedata
from pydantic import BaseModel, ConfigDict, Field, field_validator
from fastapi import HTTPException

from database import atomic
from model_output import active_players, PLAYER_LABELS
from entity_service import apply_entity_proposals
from world import (apply_state_changes, create_entity, encode, link_character, record_event,
                   refresh_wiki, resolve_entity, state_token, now)


class Branch(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)
    text: str = Field(min_length=1,max_length=200)
    node_name: str = Field(min_length=1,max_length=100)
    node_content: str = Field(min_length=1,max_length=4000)
    likelihood: str = ""
    stat_changes: list[dict] = Field(default_factory=list)
    emotion_deltas: list[dict] = Field(default_factory=list)
    npc_memories: list[dict] = Field(default_factory=list)
    new_entities: list[dict] = Field(default_factory=list)
    entity_changes: list[dict] = Field(default_factory=list)
    state_definitions: list[dict] = Field(default_factory=list)
    state_changes: list[dict] = Field(default_factory=list)
    map_actions: dict | None = None
    npc: dict | None = None
    san_delta: dict | None = None
    entity_updates: str = ""

    @field_validator("entity_updates", mode="before")
    @classmethod
    def normalize_empty_entity_updates(cls, value):
        # Models sometimes express "no changes" as null or an empty container.
        # Only normalize empty values; substantive effects must keep their schema.
        if value is None or (isinstance(value, (dict, list)) and not value):
            return ""
        return value


def validate_branches(parsed):
    if parsed.get("clarification"):
        question = parsed["clarification"].get("question")
        if not isinstance(question, str) or not question.strip():
            raise ValueError("澄清问题无效")
        return []
    branches = parsed.get("branches")
    if not isinstance(branches, list) or not 1 <= len(branches) <= 6:
        raise ValueError("模型必须返回1至6个完整分支")
    return [Branch.model_validate(branch).model_dump() for branch in branches]


def validate_summaries(parsed):
    """New model output only; saved/legacy branches keep their original contract."""
    branches = validate_branches(parsed)
    if parsed.get('clarification'):
        return branches
    if not 2 <= len(branches) <= 4:
        raise ValueError('摘要必须包含2至4个分支')
    for branch in branches:
        if len(branch['node_content']) > 120:
            raise ValueError('分支摘要不得超过120字符，请重新推演')
        if len(branch['text']) > 40 or len(branch['node_name']) > 30:
            raise ValueError('分支选项或标题过长')
        # Server-owned preparation/approval fields can never come from the model.
        for key in ('npc_review', 'npc_additions', 'review_decisions', 'spatial_context',
                    'preparation', 'phase', 'expanded_content', 'summary_contract'):
            branch.pop(key, None)
    return branches


class UndeclaredEntity(ValueError):
    pass


def normalize_branch_references(conn, branch, *, tolerant=False, timeline_id=None,
                                resolved_entities=None, context='', allow_declarations=True):
    """Resolve model labels; keep uncertain draft effects separate from executable effects."""
    labels, identities, display_names = {}, set(), {}

    def label_key(value):
        return unicodedata.normalize('NFKC', value).strip().casefold()

    def register(uid, name, aliases):
        identities.add(uid)
        display_names[uid] = name
        for label in [uid, name, *aliases]:
            labels.setdefault(label_key(label), set()).add(uid)

    for row in conn.execute('SELECT uid,name,aliases FROM world_entities'):
        register(row['uid'], row['name'], json.loads(row['aliases'] or '[]'))
    for label, uid in (resolved_entities or {}).items():
        if uid in identities:
            labels[label_key(label)] = {uid}
    players = active_players(conn, timeline_id)
    player_refs = set()
    if tolerant:
        def is_player_placeholder(entity):
            if label_key(entity.get('name', '')) not in {'player', 'player_id', 'playerid', 'pc', '玩家id'}:
                return False
            ref = entity.get('ref', 'new_npc')
            player_refs.add(label_key(ref))
            labels[label_key(ref)] = {p['entity_uid'] for p in players}
            return True

        if branch.get('npc') and not branch['npc'].get('entity_uid') and is_player_placeholder(branch['npc']):
            branch['npc'] = None
        branch['new_entities'] = [entity for entity in branch.get('new_entities', [])
                                  if not (entity.get('entity_type') in {'npc', 'pc'} and is_player_placeholder(entity))]
    npc = branch.get('npc')
    if npc and not npc.get('entity_uid'):
        register(npc.get('ref', 'new_npc'), npc['name'], npc.get('aliases', []))
    for entity in branch.get('new_entities', []):
        if entity.get('ref'):
            register(entity['ref'], entity['name'], entity.get('aliases', []))
    narrative = context + '\n' + branch.get('node_content', '')
    groups = ('stat_changes', 'emotion_deltas', 'npc_memories', 'entity_changes', 'state_changes', 'san_delta')
    for group in groups:
        changes = [branch[group]] if group == 'san_delta' and branch.get(group) else branch.get(group, [])
        if not changes:
            continue
        kept = []
        for original in changes:
            change = dict(original)
            problem = None
            for key in ('entity_uid', 'target_uid'):
                value = change.get(key)
                if key == 'entity_uid' and not value:
                    value = change.get('char_name') or change.get('npc_name') or change.get('name')
                if not value:
                    if key == 'entity_uid' and not (group == 'entity_changes' and change.get('new') is True):
                        problem = '状态变更缺少对象'
                    continue
                if not isinstance(value, str):
                    problem = f'引用「{value}」不是人物名称或实体标识，状态变更暂未执行'
                    break
                if value in identities:
                    change[key] = value
                    continue
                matches = labels.get(label_key(value), set())
                if not matches and label_key(value) in PLAYER_LABELS:
                    matches = {p['entity_uid'] for p in players}
                if len(matches) == 1:
                    change[key] = next(iter(matches))
                    continue
                if matches:
                    problem = f'名称「{value}」有歧义，状态变更暂未执行'
                    break
                # A natural name already in the scene can become an ordinary NPC proposal.
                # Opaque IDs and player placeholders never become invented characters.
                if (tolerant and allow_declarations and key == 'entity_uid'
                        and group in {'stat_changes', 'emotion_deltas', 'npc_memories', 'san_delta'}
                        and label_key(value) not in PLAYER_LABELS
                        and label_key(value) not in player_refs
                        and re.search(r'[\u4e00-\u9fff]', value) and len(value) <= 40 and value in narrative):
                    ref = f'inferred_npc_{len(branch.get("new_entities", [])) + 1}'
                    while ref in identities:
                        ref += '_1'
                    branch.setdefault('new_entities', []).append({'ref': ref, 'name': value,
                        'entity_type': 'npc', 'description': f'当前场景中称为「{value}」的人物'})
                    register(ref, value, [])
                    change[key] = ref
                    continue
                problem = f'暂无法确定「{value}」对应的人物或实体，状态变更暂未执行'
                break
            if problem:
                if not tolerant:
                    raise UndeclaredEntity(problem)
                branch.setdefault('deferred_effects', []).append({'field': group, 'effect': original, 'reason': problem})
                warnings = branch.setdefault('reference_warnings', [])
                if problem not in warnings:
                    warnings.append(problem)
            else:
                uid = change.get('entity_uid')
                if uid in display_names:
                    if group in {'stat_changes', 'san_delta'}:
                        change['char_name'] = display_names[uid]
                    elif group in {'emotion_deltas', 'npc_memories'}:
                        change['npc_name'] = display_names[uid]
                kept.append(change)
        if group == 'san_delta':
            branch[group] = kept[0] if kept else None
        else:
            branch[group] = kept
    return branch


def target_entity(conn, change, *, references=None, name_key="char_name"):
    uid = change.get("entity_uid")
    if uid and references:
        uid = references.get(uid, uid)
    return resolve_entity(conn, uid=uid, name=change.get(name_key))


def integer(value, label):
    if type(value) is not int or abs(value) > 1_000_000:
        raise ValueError(f"{label} 必须为范围内的整数")
    return value


def apply_branch(conn, node_id, expected_token=None, expected_timeline=..., confirm_npc_additions=False):
    from agent import (_build_fx_context, _process_map_actions, _get_current_room_id,
                       _append_to_memory, _tl_append_memory, _l1_append, _parse_state_desc)
    from entity import apply_emotion_delta, tick_emotion_decay

    with atomic(conn):
        row = conn.execute("SELECT payload FROM pending_effects WHERE node_id=?", (node_id,)).fetchone()
        if not row:
            event = conn.execute("SELECT id,payload,timeline_id FROM world_events WHERE operation_key=?", (f"branch:{node_id}",)).fetchone()
            if event:
                if expected_timeline is not ... and event['timeline_id'] != expected_timeline:
                    raise HTTPException(409, '该分支属于其他队伍')
                return {**json.loads(event['payload']).get('result',{}), "status": "already_applied", "event_id": event["id"]}
            return {"status": "skipped", "message": "无待执行分支"}
        fx = json.loads(row["payload"])
        if expected_timeline is not ... and fx.get('timeline_id') != expected_timeline:
            raise HTTPException(409, '该分支属于其他队伍')
        token = state_token(conn)
        if (fx.get("state_token") and fx["state_token"] != token) or (expected_token and expected_token != token):
            raise HTTPException(409, "世界状态已变化，请重新推演；该分支尚未执行")
        node = conn.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone()
        if not node:
            raise ValueError("分支节点不存在")
        if fx.get('phase')!='EXPANDED' or not fx.get('expanded_content') or node['expanded_content']!=fx['expanded_content']:
            raise HTTPException(409,'Expand this candidate before confirming the scene')
        from npc_review import check_commit
        check_commit(conn, fx, confirm_npc_additions)
        normalize_branch_references(conn, fx, timeline_id=fx.get('timeline_id'))
        timeline_id = fx.get("timeline_id")
        team = None
        if timeline_id is not None:
            team = conn.execute('SELECT * FROM timelines WHERE id=?',(timeline_id,)).fetchone()
            if not team or team['status']!='active': raise ValueError('队伍不存在或不在行动')
            members = {int(x) for x in team['char_ids'].split(',') if x.strip().isdigit()}
        operation_key = f"branch:{node_id}"
        event_id = record_event(conn, "branch_committed", fx["expanded_content"], fx, timeline_id, operation_key)
        references = {}
        spawned_npcs = []
        npc = fx.get("npc")
        if npc:
            is_new = not npc.get('entity_uid')
            if npc.get("entity_uid"):
                entity = resolve_entity(conn, uid=npc["entity_uid"])
                if entity['entity_type']!='npc': raise ValueError('已有NPC引用指向其他类型实体')
                npc={**npc,'name':entity['name']}
                uid = entity["uid"]
            else:
                uid = create_entity(conn, name=npc["name"], entity_type="npc",
                                    state_desc=encode({"desc": npc.get("backstory", ""), "emotion": {}, "memory": []}),
                                    aliases=npc.get("aliases", []))
            ref = npc.get("ref", "new_npc")
            references[ref] = uid
            char = conn.execute("SELECT id FROM characters WHERE entity_uid=?", (uid,)).fetchone()
            if not char:
                char_id = conn.execute("INSERT INTO characters(name,role,hp,san,inventory,entity_uid) VALUES(?,?,?,?,?,?)",
                    (npc["name"], "NPC", integer(npc.get("hp", 50), "HP"), integer(npc.get("san", 60), "SAN"),
                     npc.get("inventory", ""), uid)).lastrowid
            else:
                char_id = char[0]
            npc = {**npc, "id": char_id, "entity_uid": uid}
            if is_new: spawned_npcs.append(npc)
        for entity in fx.get("new_entities", []):
            ref = entity.get("ref")
            if not ref or ref in references:
                raise ValueError("新实体的临时引用必须唯一")
            references[ref] = create_entity(conn, name=entity["name"], entity_type=entity.get("entity_type", "item"),
                state_desc=encode({"desc": entity.get("description", "")}), aliases=entity.get("aliases", []),
                visibility=entity.get("visibility", "public"), location=entity.get("location", ""))
            if entity.get("entity_type") == "npc":
                char_id = conn.execute("INSERT INTO characters(name,role,hp,san,inventory,entity_uid) VALUES(?,?,?,?,?,?)",
                    (entity["name"], "NPC", integer(entity.get("hp", 50), "HP"), integer(entity.get("san", 60), "SAN"),
                     entity.get("inventory", ""), references[ref])).lastrowid
                spawned_npcs.append({**entity,'id':char_id,'entity_uid':references[ref], 'role':'NPC',
                    'hp':entity.get('hp',50),'san':entity.get('san',60),'inventory':entity.get('inventory',''),
                    'backstory':entity.get('description','')})
        proposals = []
        for change in fx.get("entity_changes", []):
            change = dict(change)
            change["entity_uid"] = references.get(change.get("entity_uid"), change.get("entity_uid"))
            proposals.append(change)
        apply_entity_proposals(conn, proposals, event_id)
        applied = []
        for change in fx.get("stat_changes", []):
            entity = target_entity(conn, change, references=references)
            char = conn.execute("SELECT * FROM characters WHERE entity_uid=?", (entity["uid"],)).fetchone()
            if not char:
                raise ValueError("属性变更对象不是角色")
            if char['role']=='PC' and team and char['id'] not in members:
                raise ValueError('无法变更另一队伍的玩家属性')
            hp_delta, san_delta = integer(change.get("hp_delta", 0), "HP变化"), integer(change.get("san_delta", 0), "SAN变化")
            inv = char["inventory"] or ""
            add, remove = change.get("inventory_append", ""), change.get("inventory_remove", "")
            if fx.get("action_type") == "dialogue" and (hp_delta or add or remove):
                raise ValueError("对话分支不可改变HP或物品")
            if add:
                inv = ", ".join(filter(None, [inv, add]))
            if remove:
                # Exact item matching; a short name must not remove several items.
                parts = [p.strip() for p in inv.replace("，", ",").split(",") if p.strip()]
                if parts.count(remove) != 1:
                    raise ValueError(f"物品「{remove}」不存在或有歧义，请澄清")
                parts.remove(remove)
                inv = ", ".join(parts)
            hp, san = max(0, char["hp"] + hp_delta), max(0, char["san"] + san_delta)
            conn.execute("UPDATE characters SET hp=?,san=?,inventory=? WHERE id=?", (hp, san, inv, char["id"]))
            applied.append({**change, "entity_uid": entity["uid"], "char_name": char["name"], "new_hp": hp, "new_san": san, "new_inv": inv})
        apply_state_changes(conn, fx.get("state_definitions", []), fx.get("state_changes", []), event_id, references)
        emotions, memories = [], []
        for change in fx.get("emotion_deltas", []):
            entity = target_entity(conn, change, references=references, name_key="npc_name")
            sd = _parse_state_desc(entity["state_desc"])
            emo = sd.get("emotion", {})
            tick_emotion_decay(emo)
            for key in ("trust", "fear", "irritation"):
                delta = integer(change.get(key, 0), "情绪变化")
                if delta:
                    apply_emotion_delta(emo, key, delta)
            sd["emotion"] = emo
            conn.execute("UPDATE world_entities SET state_desc=?,updated_at=? WHERE uid=?", (encode(sd), now(), entity["uid"]))
            emotions.append({"entity_uid": entity["uid"], "npc_name": entity["name"], **emo})
        for change in fx.get("npc_memories", []):
            entity = target_entity(conn, change, references=references, name_key="npc_name")
            content = change.get("memory", "")
            if not isinstance(content, str):
                raise ValueError("NPC记忆必须为文本")
            if content:
                conn.execute("INSERT INTO entity_memories(entity_uid,content,timeline_id,visibility,source_event_id) VALUES(?,?,?,?,?)",
                    (entity["uid"], content[:500], timeline_id, f"entity:{entity['uid']}", event_id))
                memories.append({"entity_uid": entity["uid"], "memory": content})
        map_result = {"moved_to": None, "new_room": None, "unlocked_edge": None, "errors": []}
        if fx.get("map_actions"):
            if fx.get("action_type") == "dialogue":
                raise ValueError("对话不可改变空间")
            map_result = _process_map_actions(conn, fx, _get_current_room_id(conn, timeline_id), timeline_id=timeline_id,scene_node_id=node_id)
            if map_result["errors"]:
                raise ValueError("；".join(map_result["errors"]))
        from spatial import bind_scene,current_room
        if fx.get('spatial_context', {}).get('mode') != 'text':
            bind_scene(conn,node_id,current_room(conn,timeline_id))
        narrative=fx['expanded_content']
        memory = f"[{fx.get('scene_name','')}] {fx.get('player_action','')}→{narrative}"
        if timeline_id and _tl_append_memory:
            _tl_append_memory(conn, timeline_id, memory)
        elif _append_to_memory:
            _append_to_memory(conn, memory)
        if _l1_append:
            _l1_append(conn, fx.get("scene_name", ""), fx.get("player_action", ""),
                       narrative, entity_updates=fx.get("entity_updates", ""), timeline_id=timeline_id)
        conn.execute('INSERT INTO chronicle_log(scene_id,scene_name,scene_content,expanded_content,player_action) VALUES(?,?,?,?,?)',
                     (node_id,node['name'],node['content'],narrative,fx.get('player_action','')))
        conn.execute("DELETE FROM pending_effects WHERE node_id=?", (node_id,))
        conn.execute('UPDATE nodes SET is_draft=0 WHERE id=?',(node_id,))
        # Sibling alternatives remain draft nodes, but cannot be selected after a choice.
        parent = fx.get("parent_node_id")
        if parent:
            siblings = conn.execute('SELECT p.node_id,p.payload FROM pending_effects p JOIN options o ON o.next_node_id=p.node_id WHERE o.node_id=?', (parent,)).fetchall()
            for sibling in siblings:
                if json.loads(sibling['payload']).get('timeline_id') == timeline_id:
                    conn.execute('DELETE FROM pending_effects WHERE node_id=?', (sibling['node_id'],))
        if timeline_id:
            conn.execute("UPDATE timelines SET current_node_id=? WHERE id=?", (node_id, timeline_id))
        else:
            conn.execute("INSERT OR REPLACE INTO system_state VALUES('player_current_scene_id',?)", (str(node_id),))
        from trigger import evaluate_triggers
        trigger_results=evaluate_triggers(conn,node_id,node['name'],narrative,timeline_id,event_id)
        refresh_wiki(conn)
        fx_context, action_type = _build_fx_context({"payload": encode(fx)})
        result = {"status": "success", "event_id": event_id, "stat_changes": applied,
                  "spawned_npc": spawned_npcs[0] if spawned_npcs else None, "spawned_npcs": spawned_npcs,
                  "map_result": map_result, "entity_updates": fx.get("entity_updates", ""),
                  "emotion_changes": emotions, "npc_memories": memories, "fx_context": fx_context,
                  "skeleton": fx.get("skeleton", node["content"]), "action_type": action_type, "entity_references": references}
        result['reference_warnings'] = fx.get('reference_warnings', [])
        result['deferred_effects'] = fx.get('deferred_effects', [])
        result['timeline_id'] = timeline_id
        result['trigger_results'] = trigger_results
        conn.execute("UPDATE world_events SET payload=? WHERE id=?", (encode({"effects": fx, "result": result}), event_id))
        from wiki_jobs import enqueue
        enqueue(conn,event_id)
        return result
