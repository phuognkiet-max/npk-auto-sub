"""Conservative nonverbal rules never rewrite the displayed subtitle."""
import copy
import unittest

from expressive_text import prepare_expressive_text
from speech_delivery import signature_payload


class ExpressiveTextTests(unittest.TestCase):
    def test_missing_or_disabled_flag_preserves_exact_input(self):
        text = '  Hì hì! Giá 199.000đ, giảm 20%. [cười]\n'
        self.assertEqual(prepare_expressive_text(text), text)
        self.assertEqual(prepare_expressive_text(text, False), text)

    def test_only_complete_laughter_sentences_are_mapped(self):
        cases = {'Hì hì!': '[cười]!', 'hi hi': '[cười]', 'HA HA HA.': '[cười].',
                 'Xin chào. Hì hì! Giá 199.000đ, giảm 20%.': 'Xin chào. [cười]! Giá 199.000đ, giảm 20%.',
                 'Hi hi!\nHa ha.': '[cười]!\n[cười].', '  ha\tha!  ': '  [cười]!  '}
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(prepare_expressive_text(text, True), expected)

    def test_greetings_mentions_quotes_tags_and_screams_are_unchanged(self):
        examples = ['Hi, John!', 'Hi hi, John!', 'Say hi hi to John.', 'Hi hi John.',
                    'Tôi nói hi hi cho vui.', '"Hi hi!"', 'Anh nói: “Hì hì!”',
                    "'ha ha!'", '«Hi hi!»', 'Anh viết "hi hi!', "John's greeting is hi hi.",
                    '[hi hi]', '[cười] [thở dài] [hắng giọng]', 'Á! Á! A!', 'ha', 'ha hi!']
        for text in examples:
            with self.subTest(text=text):
                self.assertEqual(prepare_expressive_text(text, True), text)

    def test_preparation_is_idempotent_and_preserves_surrounding_text(self):
        text = 'Hì hì! Mua 2 chai 500ml, giảm 20%. Ha ha!'
        prepared = prepare_expressive_text(text, True)
        self.assertEqual(prepared, '[cười]! Mua 2 chai 500ml, giảm 20%. [cười]!')
        self.assertEqual(prepare_expressive_text(prepared, True), prepared)

    def test_vietnamese_laughter_prefix_keeps_the_full_greeting(self):
        for text, expected in [('Hì hì, chào anh!', '[cười], chào anh!'),
                               ('Ha ha, gặp anh vui quá!', '[cười], gặp anh vui quá!'),
                               ('Hi hi, chào anh!', '[cười], chào anh!'),
                               ('Xin chào. Hì hì, anh mua 2 chai nhé!', 'Xin chào. [cười], anh mua 2 chai nhé!')]:
            with self.subTest(text=text):
                self.assertEqual(prepare_expressive_text(text, True), expected)
        for text in ['Hi hi, John!', 'Hi hi, hello John!', 'Tôi viết hi hi, chào anh.', '“Hì hì, chào anh!”']:
            with self.subTest(text=text):
                self.assertEqual(prepare_expressive_text(text, True), text)

    def test_signature_flag_does_not_change_legacy_disabled_payloads(self):
        for params in ({'voice_speed': 1, 'voice_id': 'vieneu:test'},
                       {'voice_speed': 1, 'voice_id': 'vieneu:test', 'tts_timing_mode': 'natural'},
                       {'voice_speed': 1, 'voice_id': 'vieneu:test', 'tts_text_source': 'translated', 'tts_timing_mode': 'natural'}):
            project = {'media': {'duration': 3}, 'segments': [dict(start=0, end=2, text='Source', translated='Hì hì!', voice_id='vieneu:test')], 'params': params}
            original = copy.deepcopy(project)
            baseline = signature_payload(project)
            project['params']['tts_expressive'] = False
            self.assertEqual(signature_payload(project), baseline)
            project['params']['tts_expressive'] = True
            self.assertNotEqual(signature_payload(project), baseline)
            self.assertEqual(project['segments'], original['segments'])


if __name__ == '__main__':
    unittest.main()
