"""Desktop source launchers share development settings without affecting Docker."""
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from runtime_config import load_environment


class DesktopConfigTests(unittest.TestCase):
    def test_root_settings_and_override_precedence(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'track_2b' / 'src'
            source.mkdir(parents=True)
            (root / 'main.py').touch()
            (root / '.env').write_text(
                'APERTUS_BASE_URL=https://remote.test/v1\nAPERTUS_API_KEY=root-key\n', encoding='utf-8')
            resolved = load_environment(source, {})
            self.assertEqual(resolved['LLM_BASE_URL'], 'https://remote.test/v1')
            self.assertEqual(resolved['LLM_API_KEY'], 'root-key')
            (source / '.env').write_text('LLM_BASE_URL=http://local.test/v1\n', encoding='utf-8')
            self.assertEqual(load_environment(source, {})['LLM_BASE_URL'], 'http://local.test/v1')
            self.assertEqual(load_environment(source, {'LLM_BASE_URL': 'http://llm:8000/v1'})[
                'LLM_BASE_URL'], 'http://llm:8000/v1')

    def test_packaged_install_ignores_parent_settings(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / 'track_2b' / 'src'
            source.mkdir(parents=True)
            (root / '.env').write_text('LLM_API_KEY=unrelated-key\n', encoding='utf-8')
            self.assertNotIn('LLM_API_KEY', load_environment(source, {}))


if __name__ == '__main__':
    unittest.main()
