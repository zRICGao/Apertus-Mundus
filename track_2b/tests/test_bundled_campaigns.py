"""Load the supplied examples and execute their real transactional rules offline."""
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / 'src'
SCENARIOS = SOURCE.parent / 'data' / 'scenarios'
sys.path.insert(0, str(SOURCE))


class BundledCampaignTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.environment = dict(os.environ)
        scratch = SOURCE.parent / '.audit_tmp'
        scratch.mkdir(exist_ok=True)
        cls.workspace = tempfile.TemporaryDirectory(prefix='scenario_review_', dir=scratch)
        cls.base = Path(cls.workspace.name)
        os.environ.update(RPG_DB_FILE=str(cls.base / 'world.db'),
                          RPG_CAMPAIGNS_DIR=str(cls.base / 'campaigns'),
                          RPG_CONFIG_FILE=str(cls.base / 'settings.env'),
                          EMBEDDING_ENABLED='false', RERANK_ENABLED='false', WIKI_AUTO_SYNTHESIS='false')
        import main
        cls.log_level = logging.getLogger('apertus_mundus.trigger').level
        logging.getLogger('apertus_mundus.trigger').setLevel(logging.WARNING)
        main._startup_wire_agent()
        cls.app = main
        cls.config = json.loads((SCENARIOS / 'limmat_forbidden_river' / 'campaign.json').read_text(encoding='utf-8'))

    @classmethod
    def tearDownClass(cls):
        logging.getLogger('apertus_mundus.trigger').setLevel(cls.log_level)
        cls.workspace.cleanup()
        os.environ.clear()
        os.environ.update(cls.environment)

    def setUp(self):
        self.decisions = patch('trigger._batch_judge_ai', return_value={})
        self.decisions.start()
        self.addCleanup(self.decisions.stop)
        self.load('limmat_forbidden_river')
        self.sequence = 0

    def load(self, name):
        from campaigns import load_save
        return load_save(self.app.DB_FILE, SOURCE, 'campaigns/' + name,
                         campaigns_dir=self.app.CAMPAIGNS_DIR)

    def row(self, sql, args=()):
        with self.app.safe_db() as conn:
            return conn.execute(sql, args).fetchone()

    def go(self, node_id, team=1, key=None):
        from scene_service import SceneCommitRequest, commit_scene
        self.sequence += 1
        current = self.row('SELECT current_node_id FROM timelines WHERE id=?', (team,))[0]
        request = SceneCommitRequest(node_id=node_id, from_node_id=current, timeline_id=team,
                                     operation_key=key or f'review-{self.sequence}', option_text='Confirmed authored choice')
        with self.app.safe_db() as conn:
            return commit_scene(conn, request)

    def prepare_finale(self):
        for node in [2, 3, 4, 5, 6, 8, 13, 12, 10, 12, 4, 5, 11, 9, 11, 17, 4, 12, 13]:
            self.go(node)

    def test_original_people_and_initial_world_data_survive_loading(self):
        names = {r['name'] for r in self.config['characters'] if r['role'] == 'NPC'}
        self.assertEqual(len(names), 16)
        self.assertTrue({'Li', 'Hou Jiale', 'Xu Sijie', 'Yu Zhihe', 'Zhao Yifei', 'Gao Xing',
                         'Jia Weimeng', 'Liu Jiao', 'Zhang Dingyuan', 'Chen Yuyang', 'Jia Chu',
                         'Liu Runpu', 'Zhang Xindi', 'Huang Sice', 'Liu Zisen', 'Shang Haoliang'} == names)
        original = self.row("SELECT aliases FROM world_entities WHERE name='Gao Xing'")[0]
        self.assertIn('\u9ad8\u5174', json.loads(original))
        for table, count in [('state_definitions', 8), ('entity_states', 8), ('entity_memories', 2),
                             ('entity_relations', 1), ('scene_locations', 27), ('triggers', 26), ('rag_documents', 9)]:
            self.assertEqual(self.row(f'SELECT count(*) FROM {table}')[0], count, table)
        self.assertEqual(self.row("SELECT value FROM system_state WHERE key='player_current_scene_id'")[0], '1')
        self.assertIn('source', self.config['rag_library'][0])
        self.assertNotIn('text', self.config['rag_library'][0])
        with self.app.safe_db() as conn:
            imported = '\n'.join(r[0] for r in conn.execute('SELECT chunk_text FROM rag_chunks WHERE doc_id=1 ORDER BY chunk_index'))
        self.assertIn('Gao Xing', imported)
        entry = next(r for r in self.app.list_campaigns()['files'] if r['path'] == 'campaigns/limmat_forbidden_river')
        self.assertEqual(entry['name'], 'Limmat — The Forbidden River')

    def test_three_endings_have_exact_costs_exclusions_and_idempotence(self):
        for node, outcome, hp, san in [(14, 'eth', 100, 88), (15, 'uzh', 90, 98), (16, 'neutral', 95, 93)]:
            with self.subTest(outcome=outcome):
                self.load('limmat_forbidden_river')
                self.prepare_finale()
                self.assertEqual(self.row('SELECT hp,san FROM characters WHERE id=1')[:], (100, 98))
                self.assertEqual(self.row('SELECT count(*) FROM options WHERE node_id=13 AND next_node_id IN (14,15,16)')[0], 3)
                before = self.row('SELECT hp,san FROM characters WHERE id=2')[:]
                current = self.row('SELECT current_node_id FROM timelines WHERE id=1')[0]
                result = self.go(node, key='ending-' + outcome)
                self.assertEqual(result['status'], 'success')
                self.assertEqual(self.row('SELECT hp,san FROM characters WHERE id=1')[:], (hp, san))
                self.assertEqual(self.row("SELECT value FROM game_flags WHERE key='ending'")[0], outcome)
                self.assertEqual(self.row('SELECT hp,san FROM characters WHERE id=2')[:], before)
                inventory = self.row('SELECT inventory FROM characters WHERE id=1')[0]
                self.assertNotIn('Calibration core', inventory)
                self.assertNotIn('Resonance vial', inventory)
                from scene_service import SceneCommitRequest, commit_scene
                with self.app.safe_db() as conn:
                    repeated = commit_scene(conn, SceneCommitRequest(node_id=node, from_node_id=current, timeline_id=1,
                        operation_key='ending-' + outcome, option_text='Confirmed authored choice'))
                self.assertEqual(repeated['status'], 'already_applied')
                self.go(15 if node != 15 else 16)
                self.assertEqual(self.row('SELECT SUM(fire_count) FROM triggers WHERE id IN (9,10,11)')[0], 1)
                self.assertEqual(self.row("SELECT value FROM game_flags WHERE key='ending'")[0], outcome)

    def test_fold_requires_real_items_and_scopes_its_hard_navigation(self):
        self.go(6)
        self.assertEqual(self.row('SELECT current_node_id FROM timelines WHERE id=1')[0], 6)
        self.assertEqual(self.row('SELECT san FROM characters WHERE id=1')[0], 100)
        self.go(2)
        result = self.go(6)
        self.assertEqual(result['trigger_results']['navigation'], 7)
        self.assertEqual(self.row('SELECT current_node_id,current_room_id FROM timelines WHERE id=1')[:], (7, 106))
        self.assertEqual(self.row('SELECT current_node_id,current_room_id FROM timelines WHERE id=2')[:], (27, 117))
        self.assertNotIn('Midnight ticket', self.row('SELECT inventory FROM characters WHERE id=1')[0])
        self.go(6)
        self.assertEqual(self.row('SELECT fire_count FROM triggers WHERE id=4')[0], 1)
        self.assertEqual(self.row('SELECT san FROM characters WHERE id=1')[0], 94)

    def test_safehouse_ceiling_cooldown_and_party_isolation(self):
        with self.app.safe_db() as conn:
            conn.execute('UPDATE characters SET hp=96,san=94 WHERE id=1')
            conn.commit()
        self.go(17)
        self.assertEqual(self.row('SELECT hp,san FROM characters WHERE id=1')[:], (100, 100))
        self.assertEqual(self.row('SELECT hp,san FROM characters WHERE id=2')[:], (90, 90))
        self.go(17)
        self.assertEqual(self.row('SELECT fire_count FROM triggers WHERE id=12')[0], 1)
        with self.app.safe_db() as conn:
            conn.execute('UPDATE characters SET hp=75,san=70 WHERE id=1')
            conn.execute('UPDATE triggers SET last_fired_at=0 WHERE id=12')
            conn.commit()
        self.go(17)
        self.go(17)
        self.assertEqual(self.row('SELECT hp,san FROM characters WHERE id=1')[:], (83, 78))
        self.assertEqual(self.row('SELECT fire_count FROM triggers WHERE id=12')[0], 2)

    def test_unknown_ai_conditions_do_not_raise_an_alarm_or_drink_tonic(self):
        self.go(2)
        self.go(4)
        self.assertEqual(self.row('SELECT fire_count FROM triggers WHERE id IN (14,18) ORDER BY id')[0], 0)
        self.assertIn('Calming tonic', self.row('SELECT inventory FROM characters WHERE id=1')[0])
        self.assertIsNone(self.row("SELECT value FROM game_flags WHERE key='treaty_alarm'"))

    def test_confirmed_public_cast_records_alarm_without_charging_twice(self):
        self.go(2)
        with patch('trigger._batch_judge_ai', side_effect=lambda values, *a, **k:
                   {v: 'actually casting visible magic' in v for v in values}):
            self.go(4)
        self.assertEqual(self.row("SELECT value FROM game_flags WHERE key='treaty_alarm'")[0], 'raised')
        self.assertEqual(self.row('SELECT san FROM characters WHERE id=1')[0], 100)
        self.assertIn('Calming tonic', self.row('SELECT inventory FROM characters WHERE id=1')[0])

    def test_forbidden_view_requires_confirmation_and_blocks_ordinary_recovery(self):
        self.go(2)
        self.go(6)
        self.go(7)
        self.assertEqual(self.row('SELECT san FROM characters WHERE id=1')[0], 94)
        with patch('trigger._batch_judge_ai', side_effect=lambda values, *a, **k:
                   {v: 'deliberately looking beyond the blinds' in v for v in values}):
            self.go(7)
        self.assertEqual(self.row('SELECT san FROM characters WHERE id=1')[0], 0)
        self.assertEqual(self.row('SELECT fire_count FROM triggers WHERE id=26')[0], 1)
        self.go(17)
        self.assertEqual(self.row('SELECT san FROM characters WHERE id=1')[0], 0)
        self.assertEqual(self.row('SELECT fire_count FROM triggers WHERE id=12')[0], 0)

    def test_optional_wings_have_obtainable_credentials_without_duplicates(self):
        for node in [9, 12, 25, 28, 10, 21, 21, 25, 28]:
            self.go(node)
        inventory = self.row('SELECT inventory FROM characters WHERE id=1')[0].split(', ')
        for item in ['ETH access card', 'UZH verdant mark', 'Abyss Geek key',
                     'Advanced computation core', 'Living bone token']:
            self.assertEqual(inventory.count(item), 1, item)

    def test_watch_report_stays_scoped_to_its_own_party(self):
        self.go(27, team=2)
        self.assertEqual(self.row('SELECT fire_count FROM triggers WHERE id=17')[0], 1)
        watch = self.row('SELECT memory FROM timelines WHERE id=2')[0]
        arrival = self.row('SELECT memory FROM timelines WHERE id=1')[0]
        self.assertIn('dispatch link', watch)
        self.assertNotIn('dispatch link', arrival)
        self.assertEqual(self.row('SELECT current_node_id,current_room_id FROM timelines WHERE id=1')[:], (1, 101))

    def test_map_unlock_consumes_only_the_real_key_and_only_moves_its_party(self):
        from database import atomic
        from map_effects import apply_map_actions
        self.go(2)
        self.go(3)
        with self.app.safe_db() as conn, atomic(conn):
            with self.assertRaisesRegex(ValueError, 'not been discovered'):
                apply_map_actions(conn, {'map_actions': {'movement': {'target_room_id': 103}}}, 102, 1)
            apply_map_actions(conn, {'map_actions': {'reveal_edge': {'edge_id': 202}}}, 102, 1)
            self.assertEqual(conn.execute('SELECT current_room_id FROM timelines WHERE id=1').fetchone()[0], 102)
            with self.assertRaisesRegex(ValueError, 'locked'):
                apply_map_actions(conn, {'map_actions': {'movement': {'target_room_id': 103}}}, 102, 1)
            result = apply_map_actions(conn, {'map_actions': {
                'reveal_edge': {'edge_id': 202},
                'unlock_edge': {'edge_id': 202, 'key_used': 'Rune coin'},
                'movement': {'target_room_id': 103}}}, 102, 1)
            self.assertEqual(result['moved_to']['room_id'], 103)
        self.assertEqual(self.row('SELECT locked,discovered FROM map_edges WHERE id=202')[:], (0, 1))
        self.assertNotIn('Rune coin', self.row('SELECT inventory FROM characters WHERE id=1')[0])
        self.assertEqual(self.row('SELECT current_room_id FROM timelines WHERE id=2')[0], 117)

    def test_authored_map_edges_and_options_reference_registered_places(self):
        from spatial import validate_edge, validate_room
        with self.app.safe_db() as conn:
            for room in conn.execute('SELECT * FROM map_rooms'):
                validate_room(dict(room))
            for edge in conn.execute('SELECT * FROM map_edges'):
                validate_edge(conn, edge['map_id'], edge['from_id'], edge['to_id'], edge['edge_type'])
            for option in conn.execute('SELECT * FROM options'):
                pair = [conn.execute('SELECT room_id FROM scene_locations WHERE node_id=?', (option[key],)).fetchone()[0]
                        for key in ('node_id', 'next_node_id')]
                self.assertTrue(pair[0] == pair[1] or conn.execute(
                    'SELECT id FROM map_edges WHERE (from_id=? AND to_id=?) OR (from_id=? AND to_id=?)',
                    (*pair, *reversed(pair))).fetchone(), dict(option))

    def test_generated_epilogue_is_opt_in_and_provider_failure_rolls_back(self):
        self.go(23)
        self.assertEqual(self.row('SELECT fire_count FROM triggers WHERE id=22')[0], 0)
        with self.app.safe_db() as conn:
            conn.execute("UPDATE characters SET inventory=inventory||', GM epilogue permit' WHERE id=1")
            conn.commit()
        with patch('trigger._generate_node_ai', return_value=None):
            with self.assertRaisesRegex(ValueError, 'generation failed'):
                self.go(23)
        self.assertIn('GM epilogue permit', self.row('SELECT inventory FROM characters WHERE id=1')[0])
        self.assertEqual(self.row('SELECT fire_count FROM triggers WHERE id=22')[0], 0)
        with patch('trigger._generate_node_ai', return_value={'name': 'An Unnamed Shore', 'content': 'You close the mirror cover.'}):
            result = self.go(23)
        self.assertEqual(len(result['trigger_results']['generated_nodes']), 1)
        self.assertNotIn('GM epilogue permit', self.row('SELECT inventory FROM characters WHERE id=1')[0])

    def test_hidden_sources_unlock_but_gm_material_stays_out_of_public_retrieval(self):
        import knowledge
        self.assertEqual(self.row('SELECT hidden FROM rag_documents WHERE id=3')[0], 1)
        self.go(12)
        self.assertEqual(self.row('SELECT hidden FROM rag_documents WHERE id=3')[0], 0)
        self.prepare_finale()
        self.go(16)
        with self.app.safe_db() as conn:
            bundle = knowledge.retrieve(conn, 'GM only mirror home plane', viewer='public', rerank=False)
            self.assertNotIn('GM only: mystery boundaries', bundle.text)
            self.assertIsNone(knowledge.page_allowed(conn, 'document/9', 'public', None, True))

    def test_export_reload_and_blank_remove_all_previous_world_data(self):
        from campaigns import export_save, load_save
        self.go(2)
        exported = export_save(self.app.DB_FILE, self.app.CAMPAIGNS_DIR, 'limmat_roundtrip')
        self.load('blank_campaign')
        self.assertEqual(self.row('SELECT count(*) FROM nodes')[0], 1)
        for table in ['characters', 'triggers', 'map_rooms', 'map_edges', 'rag_documents', 'entity_states',
                      'world_entities', 'game_flags', 'timelines', 'entity_memories', 'entity_relations']:
            self.assertEqual(self.row(f'SELECT count(*) FROM {table}')[0], 0, table)
        self.assertEqual(self.row("SELECT value FROM system_state WHERE key='worldview'")[0], '')
        load_save(self.app.DB_FILE, SOURCE, exported['filename'], campaigns_dir=self.app.CAMPAIGNS_DIR)
        self.assertEqual(self.row('SELECT fire_count FROM triggers WHERE id=1')[0], 1)
        self.assertIn('Rune coin', self.row('SELECT inventory FROM characters WHERE id=1')[0])

    def test_installation_never_overwrites_a_modified_save(self):
        from campaigns import install_bundled_campaigns
        target = self.base / 'install-check'
        installed = install_bundled_campaigns(SOURCE, target)
        self.assertEqual(set(installed), {'blank_campaign', 'limmat_forbidden_river'})
        file = target / 'blank_campaign' / 'campaign.json'
        file.write_text('{"nodes": [], "worldview": "My private draft"}', encoding='utf-8')
        self.assertEqual(install_bundled_campaigns(SOURCE, target), [])
        self.assertIn('My private draft', file.read_text(encoding='utf-8'))

    def test_knowledge_file_metadata_cannot_escape_its_folder(self):
        from campaigns import load_save
        from world import state_token
        unsafe = Path(self.app.CAMPAIGNS_DIR) / 'unsafe_source'
        unsafe.mkdir(exist_ok=True)
        (unsafe / 'campaign.json').write_text(json.dumps({'nodes': [{'id': 1}],
            'rag_library': [{'source': '../outside.md'}]}), encoding='utf-8')
        with self.app.safe_db() as conn:
            before = state_token(conn)
        with self.assertRaisesRegex(ValueError, 'within the campaign knowledge directory'):
            load_save(self.app.DB_FILE, SOURCE, 'campaigns/unsafe_source', campaigns_dir=self.app.CAMPAIGNS_DIR)
        with self.app.safe_db() as conn:
            self.assertEqual(state_token(conn), before)


if __name__ == '__main__':
    logging.disable(logging.CRITICAL)
    unittest.main()
