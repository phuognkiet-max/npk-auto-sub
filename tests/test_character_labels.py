"""Optional manually reviewed character labels; no automatic assignment or DB writes."""
import copy
from pathlib import Path
import unittest
from unittest.mock import patch

from core import DEFAULTS, ROOT, probe
from project_transfer import export_document, restore_document
import server
from worker import speech_signature


def cue(**changes):
    return {'start': 1, 'end': 3, 'text': 'Hello', 'translated': 'Xin chào.',
            'voice_id': 'piper:vais1000'} | changes


class CharacterLabelTests(unittest.TestCase):
    def test_label_is_optional_and_whitespace_is_not_a_character(self):
        old = server.validate_segments([cue()], 8)[0]
        self.assertNotIn('character_name', old)
        blank = server.validate_segments([cue(character_name='  ')], 8)[0]
        self.assertNotIn('character_name', blank)
        named = server.validate_segments([cue(character_name='  Chị Tiểu Manh  ')], 8)[0]
        self.assertEqual(named['character_name'], 'Chị Tiểu Manh')
        self.assertEqual(named['voice_id'], 'piper:vais1000')

    def test_label_rejects_non_strings_oversize_and_multiline_content(self):
        for invalid in (None, 2, True, [], {}, 'x' * 81, 'Chị\nEm', 'Chị\x00'):
            with self.subTest(value=invalid), self.assertRaisesRegex(ValueError, 'Tên nhân vật'):
                server.validate_segments([cue(character_name=invalid)], 8)
        self.assertEqual(server.validate_segments([cue(character_name='x' * 80)], 8)[0]['character_name'], 'x' * 80)

    def test_changing_only_the_label_never_invalidates_existing_speech(self):
        project = {'media': {'duration': 8}, 'params': DEFAULTS.copy(),
                   'segments': server.validate_segments([cue(character_name='Chị')], 8)}
        before = speech_signature(project)
        project['segments'][0]['character_name'] = 'Chị Tiểu Manh'
        self.assertEqual(speech_signature(project), before)
        project['segments'] = server.validate_segments(project['segments'], 8)
        self.assertEqual(speech_signature(project), before)

    def test_portable_roundtrip_keeps_manual_labels_without_inventing_missing_labels(self):
        source = Path(ROOT / 'tests/fixtures/demo.mp4').resolve()
        project = {'id': 'fixture', 'name': 'Manual labels', 'media': probe(source),
                   'params': DEFAULTS.copy(), 'regions': [], 'artifacts': {},
                   'segments': server.validate_segments([
                       cue(character_name='Chị Tiểu Manh'), cue(start=4, end=7)], 8)}
        document = export_document(project)
        self.assertEqual(document['segments'][0]['character_name'], 'Chị Tiểu Manh')
        self.assertNotIn('character_name', document['segments'][1])
        # Restore validates against a real read-only video but persistence is mocked.
        with (patch('core.create_project', return_value=copy.deepcopy(project)) as created,
              patch('core.save_project_version', side_effect=lambda p: p) as saved):
            restored = restore_document(document, str(source))
        created.assert_called_once()
        saved.assert_called_once()
        self.assertEqual(restored['segments'], project['segments'])
        self.assertEqual(restored['artifacts'], {})
        self.assertEqual(speech_signature(restored), speech_signature(project))
        document['segments'][0]['character_name'] = 'Changed copy'
        self.assertEqual(project['segments'][0]['character_name'], 'Chị Tiểu Manh')


if __name__ == '__main__':
    unittest.main(verbosity=2)
