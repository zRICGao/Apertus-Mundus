"""Concrete per-batch decision schemas and explicit, synthetic capability probes."""
import json

FORMATS = {'auto', 'json_schema', 'json_object', 'prompt'}
_verified = {}


def cache_key(settings, model):
    small = model == 'apertus-8b'
    return ((settings.small_base_url or settings.base_url) if small else settings.base_url,
            settings.small_model if small else settings.model,
            (settings.small_api_key or settings.api_key) if small else settings.api_key)


def mode_for(settings, model):
    if settings.decision_response_format != 'auto':
        return settings.decision_response_format, False
    mode = _verified.get(cache_key(settings, model))
    return mode or 'prompt', mode is None


def contract(questions):
    properties, example = {}, {}
    for key, question in questions.items():
        kind = question['type']
        if kind == 'choice':
            value_schema = {'type': 'string', 'enum': list(question['criteria'])}
            value = 'unknown' if 'unknown' in question['criteria'] else next(iter(question['criteria']))
        elif kind == 'noul':
            value_schema = {'type': 'number', 'enum': [0, 0.5, 1]}
            value = 0.5
        elif kind == 'score':
            value_schema = {'type': 'integer', 'minimum': 0, 'maximum': len(question['criteria'])-1}
            value = 0
        else:
            raise ValueError('Unknown decision question type')
        properties[key] = {'type': 'object', 'properties': {kind: value_schema,
                           'evidence': {'type': 'string', 'maxLength': 80}},
                           'required': [kind, 'evidence'], 'additionalProperties': False}
        example[key] = {kind: value, 'evidence': 'Give brief evidence based on the facts'}
    schema = {'type': 'object', 'properties': {'answers': {'type': 'object', 'properties': properties,
              'required': list(questions), 'additionalProperties': False}},
              'required': ['answers'], 'additionalProperties': False}
    return schema, {'answers': example}


def response_format(mode, schema):
    if mode == 'json_schema':
        return {'type': 'json_schema', 'json_schema': {'name': 'decisions', 'strict': True, 'schema': schema}}
    if mode == 'json_object':
        return {'type': 'json_object'}
    return None


def probe(*, model_override=None):
    """No save data. Probing is explicit and never part of a game decision."""
    import time
    from providers import generation, ApertusDecisionProvider
    settings = generation.settings
    model = (None if model_override == 'apertus' else model_override) if model_override is not None else (None if settings.decision_provider == 'apertus' else 'apertus-8b')
    questions = {'intent': {'type': 'choice', 'criteria': {'action': 'Physical action', 'unknown': 'Unknown'},
                           'instructions': 'The character raises a hand; choose action.'},
                 'has_key': {'type': 'noul', 'instructions': 'The inventory explicitly contains a key; answer 1.'},
                 'level': {'type': 'score', 'criteria': ['Low', 'High'], 'instructions': 'The known level is high; answer 1.'}}
    attempts = []
    for mode in ('json_schema', 'json_object', 'prompt'):
        start = time.perf_counter()
        try:
            results = ApertusDecisionProvider(model).evaluate(
                {'action': 'Raise a hand', 'inventory': ['Key'], 'level': 'High'}, questions, format_override=mode)
            valid = all(results[k].status == 'ok' and results[k].value == v and bool(results[k].evidence)
                        for k, v in {'intent': 'action', 'has_key': 1, 'level': 1}.items())
            attempts.append({'format': mode, 'status': 'ok' if valid else 'invalid_answers',
                             'latency_ms': round((time.perf_counter()-start)*1000, 2)})
            if valid:
                _verified[cache_key(settings, model)] = mode
                return {'status': 'success', 'ready': True, 'verified_format': mode, 'attempts': attempts}
        except Exception as exc:
            attempts.append({'format': mode, 'status': 'error', 'reason': getattr(exc, 'code', type(exc).__name__),
                             'latency_ms': round((time.perf_counter()-start)*1000, 2)})
    _verified.pop(cache_key(settings, model), None)
    return {'status': 'error', 'ready': False, 'verified_format': None, 'attempts': attempts}


def status(settings):
    model = None if settings.decision_provider == 'apertus' else 'apertus-8b'
    mode, unverified = mode_for(settings, model)
    return {'requested_format': settings.decision_response_format, 'effective_format': mode,
            'verified': cache_key(settings, model) in _verified, 'unverified': unverified}
