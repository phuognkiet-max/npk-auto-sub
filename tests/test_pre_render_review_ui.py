"""Run production pre-render review/setup UI contracts in an isolated Node VM."""
from pathlib import Path
import shutil
import unittest

from core import ROOT, run


class PreRenderReviewUITests(unittest.TestCase):
    def test_explicit_review_races_preview_and_original_audio_controls(self):
        node = shutil.which('node')
        if node is None:
            self.skipTest('Node.js is required for the isolated UI contract.')
        result = run([node, ROOT/'tests/pre_render_review_ui.cjs'], timeout=30)
        self.assertIn(b'"production_review_and_setup_executed":true', result.stdout)
        self.assertIn(b'"scenarios":22', result.stdout)


if __name__ == '__main__':
    unittest.main()
