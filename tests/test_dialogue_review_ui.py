"""Run the production dialogue dialog contract under Node's isolated VM."""
from pathlib import Path
import shutil
import unittest

from core import ROOT, run


class DialogueReviewUITests(unittest.TestCase):
    def test_revision_selection_escaping_and_draft_preservation(self):
        node = shutil.which('node')
        if node is None:
            self.skipTest('Node.js is required for the isolated UI contract.')
        output = run([node, ROOT / 'tests/dialogue_review_ui.cjs'], timeout=30)
        self.assertIn(b'"production_dialog_executed":true', output.stdout)
        self.assertIn(b'"scenarios":7', output.stdout)
