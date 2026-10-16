"""Bounded summary prompts and deterministic, server-owned spatial capability."""
import sqlite3
from fastapi import HTTPException
from spatial import current_room

SUMMARY_TOKENS = 3000


def summary_format(spatial=None, action_type=None):
    """Only send a provider format that was explicitly verified for the 70B model."""
    from decision_contracts import _verified, cache_key
    from providers import generation
    mode = _verified.get(cache_key(generation.settings, None))
    if mode == 'json_object':
        return {'type': 'json_object'}
    if mode != 'json_schema':
        return None
    from engine import Branch
    branch = Branch.model_json_schema()
    branch['properties']['node_content']['maxLength'] = 100
    branch['properties']['text']['maxLength'] = 40
    branch['properties']['node_name']['maxLength'] = 30
    # Extra model metadata is not an approval surface. Known fields alone are
    # sufficient for generation; historical branch validation remains permissive.
    branch['additionalProperties'] = False
    if (spatial or {}).get('mode') == 'text' or action_type == 'dialogue':
        branch['properties'].pop('map_actions', None)
    branch.pop('title', None)
    schema = {'type': 'object', 'properties': {
        'branches': {'type': 'array', 'minItems': 2, 'maxItems': 4, 'items': branch},
        'clarification': {'type': 'object', 'properties': {'question': {'type': 'string', 'minLength': 1},
                          'candidates': {'type': 'array', 'items': {'type': 'object'}}}, 'required': ['question']}},
        'anyOf': [{'required': ['branches']}, {'required': ['clarification']}], 'additionalProperties': False}
    return {'type': 'json_schema', 'json_schema': {'name': 'short_summaries', 'strict': False, 'schema': schema}}


def spatial_context(conn, timeline_id=None):
    try:
        count = conn.execute('SELECT count(*) FROM map_rooms').fetchone()[0]
        origin = current_room(conn, timeline_id)
        row = conn.execute('SELECT id,map_id FROM map_rooms WHERE id=?', (origin,)).fetchone() if origin else None
    except (sqlite3.DatabaseError, TypeError, ValueError) as exc:
        raise HTTPException(503, '地图暂时无法读取，请稍后重试') from exc
    if row:
        return {'mode': 'mapped', 'room_id': row['id'], 'map_id': row['map_id']}
    return {'mode': 'text', 'room_id': None,
            'reason': 'empty_map' if not count else 'invalid_origin' if origin else 'unbound_origin'}


def normalize_spatial(branch, spatial, action_type='mixed'):
    actions = branch.get('map_actions')
    if not actions:
        branch['map_actions'] = None
        return
    if not isinstance(actions, dict):
        raise ValueError('地图变化必须为对象')
    actions = {k: v for k, v in actions.items() if v not in (None, {}, [], '')}
    if spatial['mode'] == 'text':
        if actions:
            branch.setdefault('reference_warnings', []).append('本分支采用文字空间模式，未执行模型提出的地图动作')
        branch['map_actions'] = None
        return
    movement = actions.get('movement')
    if isinstance(movement, dict) and movement.get('target_room_id') == spatial['room_id']:
        actions.pop('movement')
    branch['map_actions'] = actions or None


def summary_prompt(context, action_type, spatial, *, gm_correction='', mood='', force_thrust=False):
    from world import encode, WORLD_PROMPT
    labels = ('世界观', '角色状态', '状态定义', '剧情记忆', '近期事件', '实体', '证据', '地图')
    system = ('你是跑团GM。本次只生成候选短摘要，不写完整叙事；这些结果尚未发生。'
              '根据行动自然生成2至4个不同结果。每个node_content目标40至80字，绝不超过100字符。'
              '不要写thought_process或分析过程。只返回完整严格JSON对象。'
              'text不超过20字，node_name不超过15字，likelihood为简短定性判断。'
              '无变化的副作用字段必须省略，不输出null、空数组或空对象。'
              '只保留实际必要的候选状态变化、人物声明和记忆；副作用位于各自分支内。'
              '完整正文仅在玩家点选后由另一请求流式扩写，本次禁止写长文本。\n' + WORLD_PROMPT)
    system += '\n已确认上下文：\n' + encode(dict(zip(labels, context)))
    system += '\n行动类型：' + action_type
    if spatial['mode'] == 'text' or action_type == 'dialogue':
        system += ('\n本次不能执行地图动作，输出中禁止map_actions。可以依据行动描述文字转场，'
                   '但不得声称已登记房间ID、坐标、通道或已验证可通行。')
    else:
        system += ('\n无实际空间变化必须省略map_actions。观察、商量、同一房间内走动不是地图移动。'
                   '确需地图动作时仅输出有内容的movement/new_room/reveal_room/reveal_edge/unlock_edge。'
                   '已有地点用真实整数ID，新地点声明ref；发现与进入分开，不虚构已有ID。')
    example = {'branches': [
        {'text': '谨慎核对线索', 'node_name': '共同核对', 'node_content': '你与同伴核对眼前线索，确认下一步调查方向。', 'likelihood': '较高（谨慎稳妥）'},
        {'text': '继续观察', 'node_name': '留意细节', 'node_content': '你暂缓决定，仔细观察尚未注意到的环境细节。', 'likelihood': '中等（需要时间）'}]}
    system += '\n格式示例（按实际剧情填写）：' + encode(example)
    if mood:
        system += '\n叙事氛围：' + mood
    if force_thrust:
        system += '\n至少一个摘要提出推动剧情的线索，并保留平稳选项。'
    if gm_correction:
        system += '\nGM纠正：' + gm_correction
    return system
