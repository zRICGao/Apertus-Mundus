"""Check the server boundary independently of HTTP routing and provider failures."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from engine import validate_branches, validate_summaries


def payload(length):
    return {'branches': [{'text': 'Continue', 'node_name': 'Result',
                          'node_content': 'x' * length} for _ in range(2)]}


class SummaryContractTests(unittest.TestCase):
    def test_prompt_target_and_server_tolerance_are_accepted_without_truncation(self):
        for length in (100, 101, 120):
            with self.subTest(length=length):
                branches = validate_summaries(payload(length))
                self.assertEqual(len(branches), 2)
                self.assertEqual(branches[0]['node_content'], 'x' * length)

    def test_summary_above_server_tolerance_is_rejected(self):
        with self.assertRaisesRegex(ValueError, '120'):
            validate_summaries(payload(121))

    def test_legacy_full_branches_keep_their_original_limit(self):
        self.assertEqual(len(validate_branches(payload(121))), 2)


if __name__ == '__main__':
    unittest.main()
