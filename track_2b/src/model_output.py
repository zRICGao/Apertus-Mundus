"""Keep model-facing identities out of prose shown at the table."""
import json
import re


NARRATIVE_RULE = ('Write narrative, options, titles, memories and reasons in English, using character names or natural descriptions. '
                  'Do not expose internal identifiers such as player, player_id, entity_uid or new_npc in prose. '
                  'Use "you" for a single viewpoint, and specific names or "the party" for multiple characters.')


def active_players(conn, timeline_id=None):
    players = [dict(r) for r in conn.execute("SELECT * FROM characters WHERE role='PC' AND status='active'")]
    if timeline_id is not None:
        team = conn.execute('SELECT char_ids FROM timelines WHERE id=?', (timeline_id,)).fetchone()
        ids = {int(x) for x in team[0].split(',') if x.strip().isdigit()} if team else set()
        return [p for p in players if p['id'] in ids]
    assigned = {int(x) for row in conn.execute("SELECT char_ids FROM timelines WHERE status='active'")
                for x in row[0].split(',') if x.strip().isdigit()}
    return [p for p in players if p['id'] not in assigned]


PLAYER_LABELS = {'player', 'player_id', 'playerid', 'pc', '\u73a9\u5bb6', '\u73a9\u5bb6id', '\u4e3b\u89d2', '\u4f60', '\u6211'}


def prose_renderer(conn, timeline_id=None, branch=None):
    players = active_players(conn, timeline_id)
    player_name = players[0]['name'] if len(players) == 1 else 'the party' if players else 'you'
    names = {r['uid']: r['name'] for r in conn.execute('SELECT uid,name FROM world_entities')}
    if branch:
        npc = branch.get('npc')
        if npc:
            names[npc.get('ref', 'new_npc')] = names.get(npc.get('entity_uid'), npc.get('name') or 'the other person')
        for entity in branch.get('new_entities', []):
            if entity.get('ref'):
                names[entity['ref']] = entity['name']
    names.update({key: player_name for key in ('player', 'player_id', 'playerID', '\u73a9\u5bb6ID', 'PC')})
    names.setdefault('NPC', 'the other person')
    # Short refs such as "a" are ordinary words; only replace them in explicit wrappers.
    tokens = {k.casefold(): v for k, v in names.items() if len(k) >= 3 or k == 'PC'}
    pattern = re.compile(r'(?<![a-zA-Z0-9_])(?:' + '|'.join(re.escape(k) for k in sorted(tokens, key=len, reverse=True))
                         + r')(?![a-zA-Z0-9_])', re.I)
    wrapper = re.compile(r'\[\s*(?:entity_uid|ref)\s*=\s*([^\]]+)\s*\]', re.I)
    placeholder = re.compile(r'(?<![a-zA-Z0-9_])(?:(player(?:_?id)?(?:_\d+)?)|'
                             r'(?:new_)?npc(?:_\d+)?|entity_uid)(?![a-zA-Z0-9_])', re.I)

    def render(text):
        text = wrapper.sub(lambda m: names.get(m[1].strip(), 'the other person'), text)
        text = pattern.sub(lambda m: tokens[m[0].casefold()], text)
        return placeholder.sub(lambda m: player_name if m[1] else 'the other person', text)
    return render


def clean_branch_prose(branch, render):
    prose_keys = {'text', 'node_name', 'node_content', 'entity_updates', 'likelihood',
                  'reason', 'memory', 'description', 'backstory'}

    def visit(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in prose_keys and isinstance(item, str):
                    value[key] = render(item)
                elif key not in {'deferred_effects', 'reference_warnings'}:
                    visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
    visit(branch)


def prose_stream(lines, render):
    """Buffer incomplete sentences so even an identifier split over chunks stays private."""
    pending = ''
    for line in lines:
        event = json.loads(line.removeprefix('data: ').strip())
        if event['type'] == 'text':
            pending += event['content']
            ends = list(re.finditer(r'[。！？.!?\n]', pending))
            if not ends:
                continue
            end = ends[-1].end()
            event['content'] = render(pending[:end])
            pending = pending[end:]
        elif event['type'] == 'done':
            if pending:
                yield 'data: ' + json.dumps({'type': 'text', 'content': render(pending)}, ensure_ascii=False) + '\n\n'
                pending = ''
            event['full_text'] = render(event['full_text'])
        yield 'data: ' + json.dumps(event, ensure_ascii=False) + '\n\n'
