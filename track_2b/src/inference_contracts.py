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
        raise HTTPException(503, 'Map temporarily unavailable; try again later') from exc
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
        raise ValueError('Map changes must be an object')
    actions = {k: v for k, v in actions.items() if v not in (None, {}, [], '')}
    if spatial['mode'] == 'text':
        if actions:
            branch.setdefault('reference_warnings', []).append('This branch uses text-only space; proposed map actions were not executed')
        branch['map_actions'] = None
        return
    movement = actions.get('movement')
    if isinstance(movement, dict) and movement.get('target_room_id') == spatial['room_id']:
        actions.pop('movement')
    branch['map_actions'] = actions or None


def summary_prompt(context, action_type, spatial, *, gm_correction='', mood='', force_thrust=False):
    from world import encode, WORLD_PROMPT
    labels = ('Worldview', 'Character state', 'State definitions', 'Story memory', 'Recent events', 'Entities', 'Evidence', 'Map')
    system = ('You are a tabletop RPG GM. Write all narrative and explanations in English. '
              'Generate only short candidate summaries; these outcomes have not happened. '
              'Produce 2 to 4 distinct plausible outcomes for the action. Aim for 40 to 80 characters '
              'per node_content and never exceed 100 characters. '
              'Do not output thought_process or analysis. Return only a complete, strict JSON object. '
              'Keep text within 20 characters and node_name within 15 characters; likelihood is a brief qualitative assessment. '
              'Omit unchanged side-effect fields; do not output null, empty arrays or empty objects. '
              'Include only necessary proposed state changes, character declarations and memories inside each branch. '
              'A separate request expands the selected branch as a stream; do not write full prose now.\n' + WORLD_PROMPT)
    system += '\nConfirmed context:\n' + encode(dict(zip(labels, context)))
    system += '\nAction type: ' + action_type
    if spatial['mode'] == 'text' or action_type == 'dialogue':
        system += ('\nMap actions are unavailable; omit map_actions. You may describe a transition in prose, '
                   'but do not claim registered room IDs, coordinates, passages or verified access.')
    else:
        system += ('\nOmit map_actions without actual spatial changes. Observing, discussing or walking within one room is not map movement. '
                   'When needed, include only populated movement/new_room/reveal_room/reveal_edge/unlock_edge actions. '
                   'Use real integer IDs for existing places and declare a ref for a new place. Distinguish discovery from entry; never invent existing IDs.')
    example = {'branches': [
        {'text': 'Check the clues', 'node_name': 'Compare notes', 'node_content': 'You compare clues with your companions and agree on the next lead.', 'likelihood': 'High (careful review)'},
        {'text': 'Keep watching', 'node_name': 'Small details', 'node_content': 'You pause to study details of the surroundings you had overlooked.', 'likelihood': 'Medium (takes time)'}]}
    system += '\nFormat example (adapt to the actual story): ' + encode(example)
    if mood:
        system += '\nNarrative mood: ' + mood
    if force_thrust:
        system += '\nAt least one summary should offer a lead that advances the story; retain a calm option.'
    if gm_correction:
        system += '\nGM correction: ' + gm_correction
    return system
