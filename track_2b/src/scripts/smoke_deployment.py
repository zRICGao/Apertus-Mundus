"""Exercise the deployed code on an isolated demo world; --live enables real IO."""
import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def require(response, stage):
    if response.status_code != 200:
        raise AssertionError(f'{stage}: HTTP {response.status_code}')
    data = response.json()
    if data.get('status') not in {'success', 'already_applied'}:
        raise AssertionError(f'{stage}: {data.get("status", "invalid response")}')
    return data


def completion(**kwargs):
    """A deterministic test double, used exclusively without --live."""
    if kwargs.get('max_tokens') == 1400:
        content = 'You check the console against the maintenance records and confirm that power must be disconnected first. Your flashlight lights the cover while your toolkit rests beside you.'
    elif 'decision classifier' in kwargs['messages'][0]['content']:
        questions = json.loads(kwargs['messages'][-1]['content'])['questions']
        content = json.dumps({'answers': {
            key: ({'noul': 1} if value['type'] == 'noul' else
                  {'choice': next(iter(value['criteria']))}) for key, value in questions.items()}})
    else:
        content = json.dumps({'branches': [
            {'text': 'Check records', 'node_name': 'Before repairs', 'node_content': 'You check the alarm records and confirm how to disconnect the console power.', 'likelihood': 'High (careful review)'},
            {'text': 'Inspect console', 'node_name': 'Console cover', 'node_content': 'You light the console with your flashlight and inspect its cover and power switch.', 'likelihood': 'High (tools ready)'}]}, ensure_ascii=False)
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content), finish_reason='stop')])


def verify_restart(db_file, node_id):
    os.environ['RPG_DB_FILE'] = db_file
    import main
    from fastapi.testclient import TestClient
    with TestClient(main.app) as client:
        require(client.get('/api/health'), 'restart health')
        with main.safe_db() as conn:
            node = conn.execute('SELECT is_draft,expanded_content FROM nodes WHERE id=?', (node_id,)).fetchone()
            assert node is not None and not node['is_draft'] and node['expanded_content']
            assert conn.execute('SELECT count(*) FROM world_events').fetchone()[0] > 0


def run(live):
    from runtime_config import load_environment
    # Direct host invocation also supports .env.docker; the Docker image has no .env.
    load_environment(ROOT)
    temporary_root = ROOT / '.audit_tmp' if os.name == 'nt' else None
    if temporary_root is not None:
        temporary_root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='apertus_deploy_smoke_', dir=temporary_root) as folder:
        workspace = Path(folder)
        os.environ.update(RPG_DB_FILE=str(workspace / 'world.db'),
                          RPG_CAMPAIGNS_DIR=str(workspace / 'campaigns'),
                          RPG_CONFIG_FILE=str(workspace / 'settings.env'),
                          RPG_ACCESS_MODE='container-local', WIKI_AUTO_SYNTHESIS='false')
        if not live:
            os.environ.update(EMBEDDING_ENABLED='false', RERANK_ENABLED='false', DECISION_PROVIDER='apertus')
        import main
        import agent
        from fastapi.testclient import TestClient
        from scripts.container_start import initialize_demo
        from providers import generation
        from world import state_token
        initialize_demo(main)
        with ExitStack() as stack:
            if not live:
                stack.enter_context(patch.object(generation, 'create', side_effect=completion))
                stack.enter_context(patch('socket.create_connection', side_effect=AssertionError('Unexpected network IO')))
            client = stack.enter_context(TestClient(main.app, client=('172.20.0.1', 41000)))
            require(client.get('/api/health'), 'health')
            for url in ('/index.html', '/connect', '/frontend_vendor/vue.global.prod.js',
                        '/frontend_vendor/tailwind.js', '/frontend_api.js', '/frontend_workbench.css'):
                assert client.get(url).status_code == 200, url
            assert client.get('/api/devices/state').json()['container_local']
            require(client.post('/api/game/load', json={'filename': 'campaigns/demo'}), 'demo load')
            if live:
                services = require(client.post('/api/config/test-retrieval'), 'retrieval probes')
                assert services['ready'], 'Configured retrieval services did not complete inference'
                from providers import evaluate
                answer = evaluate({'text': 'The light is off.'}, {'q': {'type': 'choice',
                    'instructions': 'Is the light on?', 'criteria': {'off': 'The light is off.', 'on': 'The light is on.'}}})['q']
                assert answer.status == 'ok' and answer.provider == 'kev', 'Kev did not answer directly'
                require(client.post('/api/rag/reindex'), 'demo vectors')
            with main.safe_db() as conn:
                assert conn.execute('SELECT count(*) FROM map_rooms').fetchone()[0] == 2
                assert conn.execute('SELECT count(*) FROM rag_documents').fetchone()[0] > 0
                before = state_token(conn)
                count = conn.execute('SELECT count(*) FROM nodes').fetchone()[0]
            request = {'current_node_id': 1, 'timeline_id': 1, 'action_type': 'action',
                       'player_action': 'Stay in the foyer and check the console against the maintenance records. Do not move or add characters.'}
            # Failure must not create a draft or change confirmed world state.
            with patch.object(agent, '_call_ai', side_effect=RuntimeError('Intentional smoke outage')):
                failed = client.post('/api/ai/dynamic-options', json=request).json()
            assert failed['status'] == 'error'
            with main.safe_db() as conn:
                assert state_token(conn) == before
                assert conn.execute('SELECT count(*) FROM nodes').fetchone()[0] == count
            print('PASS: health, assets, demo, forwarded access and provider-failure isolation', flush=True)
            result = require(client.post('/api/ai/dynamic-options', json=request), 'generation')
            node_id = result['new_options'][0]['next_node_id']
            if any(route.path == '/api/ai/prepare-branch' for route in main.app.routes):
                require(client.post('/api/ai/prepare-branch', json={'node_id': node_id, 'timeline_id': 1,
                        'operation_key': 'smoke-prepare'}), 'selected branch review')
            require(client.post('/api/ai/expand-branch', json={'node_id': node_id, 'timeline_id': 1}), 'expansion')
            commit = {'node_id': node_id, 'from_node_id': 1, 'timeline_id': 1,
                      'operation_key': 'deployment-smoke', 'confirm_npc_additions': True}
            require(client.post('/api/game/commit-scene', json=commit), 'commit')
            require(client.post('/api/game/commit-scene', json=commit), 'idempotent commit')
            save = require(client.post('/api/game/export', json={'save_name': 'smoke-roundtrip'}), 'export')
            require(client.post('/api/game/load', json={'filename': save['filename']}), 'persistent campaign load')
            initialize_demo(main)  # Must not replace the committed world on another startup.
            subprocess.run([sys.executable, str(Path(__file__).resolve()), '--verify-db', main.DB_FILE,
                            '--node', str(node_id)], check=True, env=dict(os.environ))
            print('PASS: generation, selected review, expansion, commit, export/load and fresh-process recovery', flush=True)
            print('Mode: real model' if live else 'Mode: deterministic test double; no model quality claim', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--verify-db', help=argparse.SUPPRESS)
    parser.add_argument('--node', type=int, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.verify_db:
        verify_restart(args.verify_db, args.node)
    else:
        run(args.live)
