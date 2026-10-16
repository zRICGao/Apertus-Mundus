"""Generation IO is separated from confirmed, atomic world writes."""
import json
import uuid
from fastapi import HTTPException
from fastapi.responses import StreamingResponse
from database import atomic
from providers import parse_object, route_action
from model_output import NARRATIVE_RULE, prose_renderer, prose_stream
from inference_contracts import spatial_context, normalize_spatial, summary_prompt, summary_format, SUMMARY_TOKENS
from world import (encode, state_token, resolve_entity, resolve_mention, entity_state_context,
                   record_event, refresh_wiki, visible, now)


def sse(kind, **payload):
    return 'data: '+encode({'type':kind,**payload})+'\n\n'


def prepare_dynamic(request):
    import agent as a
    request_input = request.model_dump()
    if request.action_type not in {'auto','action','dialogue','mixed'}:
        raise HTTPException(422,'未知行动类型')
    if not request.player_action.strip():
        raise HTTPException(422,'请先填写行动')
    with a.safe_db() as conn:
        node=conn.execute('SELECT * FROM nodes WHERE id=?',(request.current_node_id,)).fetchone()
        if node is None: raise HTTPException(404,'场景不存在')
        if node['is_draft']: raise HTTPException(409,'请先确认当前分支，再继续推演')
        spatial = spatial_context(conn, request.timeline_id)
        # Scene text supplied by clients is not an authoritative fact source.
        request.scene_name=node['name'] or ''
        request.content=node['expanded_content'] or node['content'] or ''
        route=route_action({'player_action':request.player_action,'scene_content':request.content}) if request.action_type=='auto' else {
            'action_type':request.action_type,'sources':['lore','history','entities','map']}
        request.action_type=route['action_type']
        for mention,uid in request.resolved_entities.items():
            resolve_entity(conn,uid=uid)
        for mention in request.mentioned_entities:
            if mention not in request.resolved_entities:
                result=resolve_mention(conn,mention,request.player_action,timeline_id=request.timeline_id)
                if result['status']!='resolved':
                    return {'clarification':{**result,'status':'clarification_required','mention':mention,
                        'question':result.get('question') or f'「{mention}」指的是谁或什么？'}}
                request.resolved_entities[mention]=result['entity_uid']
        context=a.build_system_context(conn,request.scene_name,request.content,request.player_action,
                     timeline_id=request.timeline_id,sources=route['sources'])
        token=state_token(conn)
    system=summary_prompt(context,request.action_type,spatial,
             gm_correction=request.gm_correction,mood=request.mood,force_thrust=request.force_thrust)
    user='已确认指代：'+encode(request.resolved_entities)+'\n'+encode({'场景':request.scene_name,'正文':request.content,'行动':request.player_action})
    return {'system':system,'user':user,'token':token,'request_input':request_input,'spatial_context':spatial}


def finish_dynamic(request, preparation, raw):
    import agent as a
    from engine import normalize_branch_references
    parsed=parse_object(raw)
    parsed.pop('_npc_review_id', None)
    if not parsed.get('clarification'):
        from engine import validate_summaries
        branches=validate_summaries(parsed)
        from npc_review import collect
        collect(branches)
        with a.safe_db() as conn:
            for branch in branches:
                for key in ('deferred_effects', 'reference_warnings'):
                    branch.pop(key, None)
                normalize_branch_references(conn, branch, tolerant=True, timeline_id=request.timeline_id,
                    resolved_entities=request.resolved_entities, context=request.content)
                normalize_spatial(branch, preparation['spatial_context'], request.action_type)
        parsed['branches']=branches
    if parsed.get('clarification'):
        a.validate_branches(parsed)
        return {'status':'clarification_required',**parsed['clarification'],'new_options':[]}
    # No identity/map model IO until the user selects a candidate.
    parsed['thought_process'] = ''
    parsed['_summary_contract'] = 1
    parsed['_spatial_context'] = preparation['spatial_context']
    parsed['_request_input'] = preparation['request_input']
    parsed['_state_token']=preparation['token']
    with a.safe_db() as conn, atomic(conn):
        results=a._post_process_dynamic_result(conn,parsed,request.scene_name,request.player_action,request.current_node_id,
                        timeline_id=request.timeline_id,tl_id_for_memory=request.timeline_id,action_type=request.action_type)
        options,npc,stats,map_result,thought,entities=results
        output = {'status':'success','new_options':options,'spawned_npc':npc,'stat_changes':stats,
                  'map_result':map_result,'thought_process':thought,'entity_updates':entities}
        from summary_operations import complete
        complete(conn, preparation.get('_operation_id'), output)
    return output


def dynamic(request, stream=False):
    import agent as a
    from npc_review import cached_dynamic
    import summary_operations as operations
    cached = cached_dynamic(request)  # Previously saved legacy review operations.
    identifier = None
    if cached is None:
        cached, identifier = operations.claim(request)
    if cached is not None:
        if not stream: return cached
        return StreamingResponse(iter([sse('review' if cached.get('status') == 'review_required' else 'result', data=cached)]), media_type='text/event-stream')
    try:
        prep = prepare_dynamic(request)
        prep['_operation_id'] = identifier
    except ValueError as exc:
        operations.release(identifier)
        raise HTTPException(422, str(exc)) from exc
    except Exception:
        operations.release(identifier)
        raise
    if 'clarification' in prep:
        operations.release(identifier)
        if not stream: return prep['clarification']
        return StreamingResponse(iter([sse('clarification',data=prep['clarification'])]),media_type='text/event-stream')
    if not stream:
        try:
            raw=a._call_ai(prep['system'],prep['user'],temperature=.4,max_tokens=SUMMARY_TOKENS,json_mode=True,model_override=request.model,response_format=summary_format(prep['spatial_context'], request.action_type))
            return finish_dynamic(request,prep,raw)
        except Exception as exc:
            return {'status':'error','message':str(exc),'new_options':[]}
        finally:
            operations.release(identifier)
    def events():
        try:
            raw=''
            for line in a._stream_ai_sse(prep['system'],prep['user'],temperature=.4,max_tokens=SUMMARY_TOKENS,model_override=request.model,json_mode=True,response_format=summary_format(prep['spatial_context'], request.action_type)):
                event=json.loads(line.removeprefix('data: ').strip())
                if event['type']=='done': raw=event['full_text']
                else: yield line
            if raw:
                try:
                    result=finish_dynamic(request,prep,raw)
                    kind = {'clarification_required':'clarification','review_required':'review'}.get(result['status'],'result')
                    yield sse(kind,data=result)
                except Exception as exc: yield sse('error',content=str(exc))
        finally:
            operations.release(identifier)
    return StreamingResponse(events(),media_type='text/event-stream',headers={'Cache-Control':'no-cache','X-Accel-Buffering':'no'})


def prepare_expansion(req):
    import agent as a
    with a.safe_db() as conn:
        node=conn.execute('SELECT * FROM nodes WHERE id=?',(req.node_id,)).fetchone()
        pending=conn.execute('SELECT id,payload FROM pending_effects WHERE node_id=?',(req.node_id,)).fetchone()
        if node is None: raise HTTPException(404,'Scene not found')
        if not node['is_draft'] or not pending: raise HTTPException(409,'Only a pending candidate can be expanded')
        fx=json.loads(pending['payload'])
        if fx.get('timeline_id')!=req.timeline_id: raise HTTPException(409,'Candidate belongs to another squad')
        token=state_token(conn)
        if fx.get('state_token')!=token: raise HTTPException(409,'World changed; regenerate this candidate')
        if fx.get('summary_contract') and fx.get('phase') not in {'READY_TO_EXPAND', 'EXPANDED'}:
            raise HTTPException(409, '请先准备并审核所选分支')
        from npc_review import check_commit
        check_commit(conn, fx, confirmed=True)  # Approval validity only; bulk confirmation belongs to commit.
        req.scene_name=fx.get('scene_name',''); req.player_action=fx.get('player_action','')
        parent=conn.execute('SELECT * FROM nodes WHERE id=?', (fx.get('parent_node_id'),)).fetchone()
        req.scene_content=(parent['expanded_content'] or parent['content'] or '') if parent else ''
        context,action_type=a._build_fx_context(pending)
        context='Candidate outcomes only; these effects have NOT happened.\n'+context
        if fx.get('review_decisions'):
            context += '\n【所选分支的最终审核决定，优先于原摘要；正文必须按此展开】\n' + encode(fx['review_decisions'])
        if fx.get('spatial_context', {}).get('mode') == 'text':
            context += '\n文字空间模式：允许叙事转场，不声称地图坐标、房间ID或通道已经验证。'
        system,user=a._build_expand_prompts(conn,req,node['name'],fx['skeleton'],context,action_type)
        identity=pending['payload']
    return system,user,token,identity


def save_expansion(req, text, token, identity, claim=None):
    import agent as a
    if not isinstance(text,str) or not text.strip(): raise HTTPException(422,'Empty expanded narrative')
    with a.safe_db() as conn, atomic(conn):
        from expansion_operations import check
        check(conn, claim)
        pending=conn.execute('SELECT payload FROM pending_effects WHERE node_id=?',(req.node_id,)).fetchone()
        if state_token(conn)!=token or not pending or pending[0]!=identity:
            raise HTTPException(409,'World or candidate changed; expansion was not saved')
        fx=json.loads(identity)
        fx['phase']='EXPANDED'; fx['expanded_content']=text
        conn.execute('UPDATE pending_effects SET payload=? WHERE node_id=?',(encode(fx),req.node_id))
        conn.execute('UPDATE nodes SET expanded_content=? WHERE id=? AND is_draft=1',(text,req.node_id))
        # No event, memory, chronicle, Wiki or rule is written until explicit commit.


def expand(req, stream=False):
    import agent as a
    import expansion_operations as operations
    previous = operations.cached(req)
    if previous is not None:
        if stream:
            return StreamingResponse(iter([sse('done', full_text=previous, expanded=True)]), media_type='text/event-stream')
        return {'status': 'success', 'content': previous, 'expanded': True}
    system,user,token,identity=prepare_expansion(req)
    with a.safe_db() as conn:
        render=prose_renderer(conn, req.timeline_id, json.loads(identity))
    claim = operations.claim(req, token, identity)
    if not stream:
        try:
            text=render(a._call_ai(system,user,temperature=.7,max_tokens=1400,json_mode=False,model_override=req.model))
            save_expansion(req,text,token,identity,claim)
            return {'status':'success','content':text,'expanded':True}
        finally:
            operations.release(claim)
    def events():
        try:
            raw=''
            for line in prose_stream(a._stream_ai_sse(system,user,temperature=.7,max_tokens=1400,model_override=req.model), render):
                event=json.loads(line.removeprefix('data: ').strip())
                if event['type']=='done': raw=event['full_text']
                else: yield line
            if raw:
                try:
                    save_expansion(req,raw,token,identity,claim)
                    yield sse('done',full_text=raw,expanded=True)
                except Exception as exc: yield sse('error',content=str(exc))
        finally:
            operations.release(claim)
    return StreamingResponse(events(),media_type='text/event-stream')


def npc_entity(conn, request):
    try:
        entity=resolve_entity(conn,uid=request.entity_uid,name=request.npc_name,entity_type='npc')
        if entity['entity_type']!='npc': raise ValueError('目标不是NPC')
        return entity
    except ValueError as exc: raise HTTPException(422,str(exc)) from exc


def npc_chat(request):
    import agent as a
    from knowledge import allowed
    with a.safe_db() as conn:
        entity=npc_entity(conn,request)
        viewer=f"entity:{entity['uid']}"
        sd=a._parse_state_desc(entity['state_desc'])
        memories=[r['content'] for r in conn.execute('SELECT * FROM entity_memories WHERE entity_uid=? ORDER BY id DESC LIMIT 30',(entity['uid'],))
                  if allowed(r['visibility'],r['timeline_id'],viewer,request.timeline_id)]
        history=conn.execute('SELECT sender,message FROM npc_chat_logs WHERE entity_uid=? AND timeline_id IS ? ORDER BY id DESC LIMIT 30',
                            (entity['uid'],request.timeline_id)).fetchall()
        states=entity_state_context(conn,entity['uid'],viewer,request.timeline_id)
        # Never inject omniscient world lore, other NPCs' memories, or other parties' chats.
        render=prose_renderer(conn, request.timeline_id)
        system='你扮演'+entity['name']+'，与玩家私聊。只知道自己的档案、状态和记忆；未知事情自然表达不确定。不能改变世界状态。'+NARRATIVE_RULE+'\n'+encode({
             '档案':sd.get('desc',''),'情绪':sd.get('emotion',{}),'状态':states,'记忆':memories+sd.get('memory',[]),
             '近期对话':[dict(r) for r in reversed(history)]})
    return StreamingResponse(prose_stream(a._stream_ai_sse(system,request.player_message,temperature=.7,max_tokens=1000), render),media_type='text/event-stream')


def npc_commit(request):
    import agent as a
    from engine import integer
    with a.safe_db() as conn:
        entity=npc_entity(conn,request); uid=entity['uid']; token=state_token(conn)
        existing=conn.execute('SELECT id FROM world_events WHERE operation_key=?',(f'npcchat:{request.operation_key}',)).fetchone()
        if existing: return {'status':'already_applied','event_id':existing[0]}
        system='根据已发生对话提取该NPC的情绪变化和第一人称记忆。返回JSON：{"trust_delta":0,"fear_delta":0,"irritation_delta":0,"memory":"记忆"}。不编造。\n'+entity['state_desc']
    raw=a._call_ai(system,encode({'玩家':request.player_message,'NPC':request.npc_reply}),temperature=.1,max_tokens=600)
    parsed=parse_object(raw)
    deltas={key:integer(parsed.get(key+'_delta',0),'情绪变化') for key in ('trust','fear','irritation')}
    if any(abs(v)>25 for v in deltas.values()): raise HTTPException(422,'情绪变化超出范围')
    memory=parsed.get('memory','')
    if not isinstance(memory,str): raise HTTPException(422,'记忆必须是文本')
    with a.safe_db() as conn, atomic(conn):
        existing=conn.execute('SELECT id FROM world_events WHERE operation_key=?',(f'npcchat:{request.operation_key}',)).fetchone()
        if existing: return {'status':'already_applied','event_id':existing[0]}
        if state_token(conn)!=token: raise HTTPException(409,'世界已更新，请重试保存对话')
        entity=resolve_entity(conn,uid=uid); sd=a._parse_state_desc(entity['state_desc']); emo=sd['emotion']
        a.tick_emotion_decay(emo)
        for key,delta in deltas.items(): a.apply_emotion_delta(emo,key,delta)
        sd['emotion']=emo
        event=record_event(conn,'npc_chat',f"{entity['name']}的已确认私聊",{'entity_uid':uid},request.timeline_id,f'npcchat:{request.operation_key}')
        conn.execute('UPDATE world_entities SET state_desc=?,updated_at=? WHERE uid=?',(encode(sd),now(),uid))
        if memory:
            conn.execute('INSERT INTO entity_memories(entity_uid,content,timeline_id,visibility,source_event_id) VALUES(?,?,?,?,?)',
                         (uid,memory,request.timeline_id,f'entity:{uid}',event))
        for sender,text in (('player',request.player_message),('npc',request.npc_reply)):
            conn.execute('INSERT INTO npc_chat_logs(npc_name,entity_uid,timeline_id,sender,message) VALUES(?,?,?,?,?)',
                         (entity['name'],uid,request.timeline_id,sender,text))
        from trigger import evaluate_triggers
        if request.timeline_id is not None:
            current=conn.execute('SELECT current_node_id FROM timelines WHERE id=?',(request.timeline_id,)).fetchone()
        else:
            current=conn.execute("SELECT value FROM system_state WHERE key='player_current_scene_id'").fetchone()
        scene=conn.execute('SELECT * FROM nodes WHERE id=? AND is_draft=0',(current[0],)).fetchone() if current and current[0] else None
        if scene:
            evaluate_triggers(conn,scene['id'],scene['name'],scene['content'],request.timeline_id,event)
        refresh_wiki(conn)
        from wiki_jobs import enqueue
        enqueue(conn,event)
        emo=a._parse_state_desc(resolve_entity(conn,uid=uid)['state_desc'])['emotion']
    return {'status':'ok','emotion':emo,'memory_added':memory,'event_id':event}


def chat_history(npc_name='',limit=200,entity_uid=None,timeline_id=None):
    import agent as a
    from types import SimpleNamespace
    with a.safe_db() as conn:
        entity=npc_entity(conn,SimpleNamespace(entity_uid=entity_uid,npc_name=npc_name))
        rows=conn.execute('SELECT sender,message,created_at FROM npc_chat_logs WHERE entity_uid=? AND timeline_id IS ? ORDER BY id DESC LIMIT ?',
                          (entity['uid'],timeline_id,max(1,min(limit,200)))).fetchall()
        return {'status':'ok','logs':[dict(r) for r in reversed(rows)]}


def preview(request,stream=False):
    """Unconfirmed scene prose is a read-only preview, never an event or a memory."""
    import agent as a
    with a.safe_db() as conn:
        node_id=getattr(request,'node_id',None)
        if node_id is not None:
            node=conn.execute('SELECT * FROM nodes WHERE id=?', (node_id,)).fetchone()
            if not node: raise HTTPException(404,'场景不存在')
            if node['is_draft']: raise HTTPException(409,'候选分支请使用分支扩写后再确认')
            if request.timeline_id is not None:
                team=conn.execute("SELECT current_node_id FROM timelines WHERE id=? AND status='active'", (request.timeline_id,)).fetchone()
                if not team or team[0]!=node_id: raise HTTPException(409,'场景不属于当前队伍')
            request.scene_name=node['name'] or ''
            request.content=node['expanded_content'] or node['content'] or ''
        context=a.build_system_context(conn,request.scene_name,request.content,timeline_id=getattr(request,'timeline_id',None))
        render=prose_renderer(conn, getattr(request,'timeline_id',None))
    system='你是GM。仅根据已知世界与当前描述丰富感官细节，不新增状态变化或事实。输出纯文本。'+NARRATIVE_RULE+'\n'+'\n'.join(context)
    user=encode({'场景':request.scene_name,'描述':request.content})
    if stream:
        return StreamingResponse(prose_stream(a._stream_ai_sse(system,user,max_tokens=1400,model_override=request.model), render),media_type='text/event-stream')
    return {'status':'success','generated_text':render(a._call_ai(system,user,max_tokens=1400,json_mode=False,model_override=request.model))}
