"""English generation defaults and safe incremental narrative rendering."""
import json
from pathlib import Path
import sqlite3
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from inference_contracts import summary_prompt
from model_output import prose_renderer, prose_stream


class EnglishRuntimeTests(unittest.TestCase):
    def test_candidate_prompt_requests_english_and_preserves_summary_limit(self):
        prompt = summary_prompt([''] * 8, 'action', {'mode': 'text'})
        self.assertIn('in English', prompt)
        self.assertIn('never exceed 100 characters', prompt)
        self.assertIn('omit map_actions', prompt)
        self.assertIn('Check the clues', prompt)

    def test_stream_emits_complete_english_sentences_before_completion(self):
        def event(kind, **data):
            return 'data: ' + json.dumps({'type': kind, **data}) + '\n\n'

        lines = iter([event('text', content='pla'), event('text', content='yer looks up.'),
                      event('done', full_text='player looks up.')])
        output = prose_stream(lines, lambda text: text.replace('player', 'Lin'))
        self.assertEqual(json.loads(next(output)[6:]), {'type': 'text', 'content': 'Lin looks up.'})
        self.assertEqual(json.loads(next(output)[6:]), {'type': 'done', 'full_text': 'Lin looks up.'})

    def test_identity_fallbacks_are_english_and_legacy_aliases_still_resolve(self):
        with sqlite3.connect(':memory:') as conn:
            conn.row_factory = sqlite3.Row
            conn.executescript('CREATE TABLE characters(id, name, role, status);'
                               'CREATE TABLE timelines(char_ids, status);'
                               'CREATE TABLE world_entities(uid, name);')
            render = prose_renderer(conn)
            self.assertEqual(render('player and NPC.'), 'you and the other person.')
            conn.execute("INSERT INTO characters VALUES(1, 'Lin', 'PC', 'active')")
            self.assertEqual(prose_renderer(conn)('player [entity_uid=missing]'), 'Lin the other person')
            self.assertEqual(prose_renderer(conn)('\u73a9\u5bb6ID'), 'Lin')


if __name__ == '__main__':
    unittest.main()
