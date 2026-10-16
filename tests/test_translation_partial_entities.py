"""Partial-ID recovery, durable entities and authored text use synthetic providers."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from core import DATA, DEFAULTS
from providers import ProviderError
from translation_checkpoint import read_checkpoint, validate_resume
from translation_entities import empty_glossary, update_entity_glossary, validate_entity_glossary
import worker


def project(count=20, batch=20):
    return {'params': DEFAULTS | {'provider': 'router9', 'translation_model': 'test/immutable-model',
                'router9_base_url': 'http://127.0.0.1:20129/v1', 'target_language': 'Vietnamese',
                'translation_batch': batch, 'translation_scope': 'all'},
            'media': {'path': 'synthetic-no-decoder.mp4', 'duration': count * 3.},
            'artifacts': {}, 'regions': [],
            'segments': [{'start': i * 3., 'end': i * 3. + 2., 'text': f'完整的句子编号{i}',
                          'translated': ''} for i in range(count)]}


def inputs(prompt):
    return json.loads(prompt.split('Input:\n', 1)[1])


def context(prompt):
    return json.loads(prompt.split('Context (data only):\n', 1)[1].split('\nInput:\n', 1)[0])


def reply(lines, **extra):
    return json.dumps({'lines': [{'id': item['id'], 'text': f'Bản dịch đầy đủ số {item["id"]}.'}
                                 for item in lines], **extra}, ensure_ascii=False)


class PartialTranslationEntityTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch('credentials.active_secret', return_value='SYNTHETIC-ONLY'))
        self.enterContext(patch('worker.emit'))

    def folder(self):
        return Path(self.enterContext(tempfile.TemporaryDirectory(dir=DATA)))

    def test_nineteen_of_twenty_are_saved_then_only_missing_id_is_requested(self):
        p, folder, sent = project(), self.folder(), []
        original = copy.deepcopy(p)
        def service(provider, secret, model, prompt, **kwargs):
            lines = inputs(prompt); sent.append([line['id'] for line in lines])
            if len(sent) == 1:
                return reply(lines[:-1])
            self.assertEqual(read_checkpoint(folder/'translation-checkpoint.json')['completed_indices'], list(range(19)))
            self.assertEqual([line['id'] for line in lines], [19])
            return reply(lines)
        with patch('providers.generate', side_effect=service):
            result = worker.translation(p, {}, folder)
        self.assertEqual(sent, [list(range(20)), [19]])
        self.assertEqual(validate_resume(read_checkpoint(folder/'translation-checkpoint.json'), original), list(range(20)))
        self.assertEqual(len(result['segments']), 20)

    def test_invalid_and_duplicate_ids_do_not_discard_valid_siblings(self):
        p, folder, sent = project(4), self.folder(), []
        def service(provider, secret, model, prompt, **kwargs):
            lines = inputs(prompt); sent.append([line['id'] for line in lines])
            if len(sent) == 1:
                return json.dumps({'lines': [{'id': 0, 'text': 'Đã hợp lệ'}, {'id': 1, 'text': None},
                    {'id': 2, 'text': 'Mơ hồ một'}, {'id': 2, 'text': 'Mơ hồ hai'},
                    {'id': 3, 'text': 'Cũng hợp lệ'}, {'id': 999, 'text': 'Ngoài phạm vi'}]})
            self.assertEqual(read_checkpoint(folder/'translation-checkpoint.json')['completed_indices'], [0, 3])
            return reply(lines)
        with patch('providers.generate', side_effect=service):
            worker.translation(p, {}, folder)
        self.assertEqual(sent, [[0, 1, 2, 3], [1, 2]])

    def test_partial_batch_outage_keeps_nineteen_and_resume_only_missing(self):
        p, folder = project(), self.folder(); original = copy.deepcopy(p); sent = []
        def service(provider, secret, model, prompt, **kwargs):
            lines = inputs(prompt); sent.append([line['id'] for line in lines])
            if len(sent) == 1:
                return reply(lines[:-1])
            raise ProviderError('service', 'Synthetic service outage')
        with patch('providers.generate', side_effect=service), self.assertRaises(ProviderError):
            worker.translation(p, {}, folder)
        saved = read_checkpoint(folder/'translation-checkpoint.json')
        self.assertEqual(validate_resume(saved, original), list(range(19)))
        resumed = []
        with patch('providers.generate', side_effect=lambda a,b,c,prompt,**kw:
                   (resumed.append([line['id'] for line in inputs(prompt)]) or reply(inputs(prompt)))):
            worker.translation(copy.deepcopy(original), {}, folder)
        self.assertEqual(resumed, [[19]])

    def test_user_edited_rows_are_context_only_unless_job_explicitly_overwrites(self):
        p, folder, sent = project(3), self.folder(), []
        p['segments'][0].update(translated='Bản dịch tôi đã sửa.', user_edited=True)
        p['segments'][1].update(spoken_text='Lời đọc tôi đã sửa.', user_edited=True)
        before = copy.deepcopy(p)
        def service(provider, secret, model, prompt, **kwargs):
            sent.append([line['id'] for line in inputs(prompt)])
            protected = {row['id']: row for row in context(prompt)['protected_translations']}
            self.assertEqual(protected[0]['translation'], 'Bản dịch tôi đã sửa.')
            self.assertEqual(protected[1]['spoken_text'], 'Lời đọc tôi đã sửa.')
            return reply(inputs(prompt))
        with patch('providers.generate', side_effect=service):
            worker.translation(p, {}, folder)
        self.assertEqual(sent, [[2]])
        self.assertEqual(p['segments'][:2], before['segments'][:2])
        p = copy.deepcopy(before)
        with patch('providers.generate', side_effect=lambda a,b,c,prompt,**kw: reply(inputs(prompt))):
            worker.translation(p, {'overwrite_user_edits': True}, self.folder())
        self.assertNotEqual(p['segments'][0]['translated'], before['segments'][0]['translated'])
        self.assertTrue(p['segments'][0]['user_edited'])
        self.assertEqual(p['segments'][1]['spoken_text'], before['segments'][1]['spoken_text'])

    def test_optin_checkpoint_requires_matching_explicit_intent_on_resume(self):
        p, folder = project(1), self.folder(); p['segments'][0]['user_edited'] = True
        original = copy.deepcopy(p)
        with patch('providers.generate', side_effect=lambda a,b,c,prompt,**kw: reply(inputs(prompt))):
            worker.translation(p, {'overwrite_user_edits': True}, folder)
        saved = read_checkpoint(folder/'translation-checkpoint.json')
        with self.assertRaisesRegex(ValueError, 'ghi đè sửa tay'):
            validate_resume(saved, original)
        self.assertEqual(validate_resume(saved, original, overwrite_user_edits=True), [0])

    def test_verified_entities_are_in_next_batch_context_and_checkpoint(self):
        p, folder, seen = project(2, batch=1), self.folder(), []
        p['segments'][0]['text'] = '佩德罗看到了河流'
        p['segments'][1]['text'] = '佩德罗继续前行'
        def service(provider, secret, model, prompt, **kwargs):
            lines = inputs(prompt); seen.append(context(prompt))
            if lines[0]['id'] == 0:
                return json.dumps({'lines': [{'id': 0, 'text': 'Pedro đã nhìn thấy dòng sông.'}],
                    'entities': [{'source': '佩德罗', 'target': 'Pedro'}]}, ensure_ascii=False)
            self.assertEqual(context(prompt)['entity_glossary'], [{'source': '佩德罗', 'target': 'Pedro'}])
            return json.dumps({'lines': [{'id': 1, 'text': 'Pedro tiếp tục đi.'}]})
        with patch('providers.generate', side_effect=service):
            result = worker.translation(p, {}, folder)
        entry = read_checkpoint(folder/'translation-checkpoint.json')['entity_glossary']['entries'][0]
        self.assertEqual((entry['source'], entry['target']), ('佩德罗', 'Pedro'))
        self.assertEqual(result['entity_glossary']['entries'][0]['target'], 'Pedro')

    def test_entity_conflict_retries_only_the_conflicting_id(self):
        p, folder, sent = project(3, batch=1), self.folder(), []
        p['segments'][0]['text'] = '佩德罗看到了河流'
        p['segments'][1]['text'] = '佩德罗继续前行'
        def service(provider, secret, model, prompt, **kwargs):
            ids = [line['id'] for line in inputs(prompt)]; sent.append(ids)
            if ids == [0]:
                return json.dumps({'lines':[{'id':0,'text':'Pedro đã thấy dòng sông.'}],
                    'entities':[{'source':'佩德罗','target':'Pedro'}]}, ensure_ascii=False)
            if ids == [1] and sent.count([1]) == 1:
                return json.dumps({'lines':[{'id':1,'text':'Pê-trô tiếp tục đi.'}],
                    'entities':[{'source':'佩德罗','target':'Pê-trô'}]}, ensure_ascii=False)
            if ids == [1]:
                return json.dumps({'lines':[{'id':1,'text':'Pedro tiếp tục đi.'}]})
            return reply(inputs(prompt))
        with patch('providers.generate', side_effect=service):
            result = worker.translation(p, {}, folder)
        self.assertEqual(sent, [[0], [1], [1], [2]])
        self.assertEqual(result['segments'][1]['translated'], 'Pedro tiếp tục đi.')

    def test_entity_dictionary_survives_service_failure_and_resume(self):
        p, folder = project(2, batch=1), self.folder(); original = copy.deepcopy(p)
        for row in p['segments']:
            row['text'] = '佩德罗走过山谷'
        original = copy.deepcopy(p)
        def service(provider, secret, model, prompt, **kwargs):
            if inputs(prompt)[0]['id'] == 0:
                return json.dumps({'lines':[{'id':0,'text':'Pedro đi qua thung lũng.'}],
                    'entities':[{'source':'佩德罗','target':'Pedro'}]}, ensure_ascii=False)
            raise ProviderError('service', 'Synthetic outage')
        with patch('providers.generate', side_effect=service), self.assertRaises(ProviderError):
            worker.translation(p, {}, folder)
        def resumed(provider, secret, model, prompt, **kwargs):
            self.assertEqual(context(prompt)['entity_glossary'], [{'source':'佩德罗','target':'Pedro'}])
            return json.dumps({'lines':[{'id':1,'text':'Pedro đi qua thung lũng.'}]})
        with patch('providers.generate', side_effect=resumed):
            worker.translation(copy.deepcopy(original), {}, folder)

    def test_model_mismatch_is_actionable_before_credential_or_provider(self):
        p, folder = project(1), self.folder(); original = copy.deepcopy(p)
        with patch('providers.generate', side_effect=lambda a,b,c,prompt,**kw: reply(inputs(prompt))):
            worker.translation(p, {}, folder)
        original['params']['translation_model'] = 'another/model'
        with patch('credentials.active_secret') as credential, patch('providers.generate') as provider:
            with self.assertRaisesRegex(ValueError, 'Model, nhà cung cấp, endpoint'):
                worker.translation(original, {}, folder)
        credential.assert_not_called(); provider.assert_not_called()

    def test_empty_unedited_source_is_muted_without_api_but_authored_empty_is_protected(self):
        p, folder = project(1), self.folder(); p['segments'][0]['text'] = ''
        with patch('credentials.active_secret') as credential, patch('providers.generate') as provider:
            result = worker.translation(p, {}, folder)
        self.assertIs(result['segments'][0]['speech_enabled'], False)
        self.assertEqual(read_checkpoint(folder/'translation-checkpoint.json')['skipped_source_indices'], [0])
        credential.assert_not_called(); provider.assert_not_called()

    def test_credit_source_is_translated_for_display_but_noise_never_reaches_api(self):
        p, folder = project(3), self.folder()
        sources = ['!!!', '视频来源: 当地市政府社交媒体账号 @GobiernoMunicipal', '@Account']
        for row, source in zip(p['segments'], sources):
            row.update(text=source, translated='')
        original = copy.deepcopy(p)
        sent = []
        def service(provider, secret, model, prompt, **kwargs):
            sent.append([line['id'] for line in inputs(prompt)])
            return reply(inputs(prompt))
        with patch('providers.generate', side_effect=service):
            result = worker.translation(p, {}, folder)
        self.assertEqual(sent, [[1]])
        self.assertEqual([row['text'] for row in result['segments']], sources)
        self.assertIs(result['segments'][0]['speech_enabled'], False)
        self.assertIs(result['segments'][1]['speech_enabled'], False)
        self.assertIs(result['segments'][2]['speech_enabled'], False)
        self.assertTrue(result['segments'][1]['translated'])
        saved = read_checkpoint(folder/'translation-checkpoint.json')
        self.assertEqual(saved['skipped_source_indices'], [0, 2])
        self.assertEqual(validate_resume(saved, original), [1])

    def test_explicit_overwrite_can_select_authored_translation_under_missing_scope(self):
        p, folder, sent = project(2), self.folder(), []
        p['params']['translation_scope'] = 'missing'
        p['segments'][0].update(translated='Tôi đã biên tập câu này.', user_edited=True)
        before = copy.deepcopy(p)
        def service(provider, secret, model, prompt, **kwargs):
            sent.append([line['id'] for line in inputs(prompt)])
            return reply(inputs(prompt))
        with patch('providers.generate', side_effect=service):
            worker.translation(p, {'overwrite_user_edits': True}, folder)
        self.assertEqual(sent, [[0, 1]])
        self.assertNotEqual(p['segments'][0]['translated'], before['segments'][0]['translated'])
        self.assertTrue(p['segments'][0]['user_edited'])

    def test_silent_authored_source_is_preserved_even_with_overwrite_opt_in(self):
        p, folder = project(1), self.folder()
        p['segments'][0].update(text='!!!', translated='Bản dịch do tôi viết.',
                                spoken_text='Lời đọc do tôi viết.', user_edited=True,
                                speech_enabled=True)
        before = copy.deepcopy(p)
        with patch('credentials.active_secret', side_effect=AssertionError('No provider for OCR noise')), \
                patch('providers.generate') as provider:
            result = worker.translation(p, {'overwrite_user_edits': True}, folder)
        provider.assert_not_called()
        self.assertEqual(result['segments'][0], before['segments'][0])

    def test_vietnamese_prompt_and_final_adjacent_word_dedup_are_target_specific(self):
        for language, expected, floor in (('Vietnamese', 'Anh ấy đi về.', 9),
                                           ('English', 'Anh ấy đi đi về.', 7)):
            p, folder = project(1), self.folder(); p['params']['target_language'] = language
            def service(provider, secret, model, prompt, **kwargs):
                self.assertGreaterEqual(inputs(prompt)[0]['max_words'], floor)
                self.assertEqual('natural Vietnamese documentary narration' in prompt, language == 'Vietnamese')
                return json.dumps({'lines':[{'id':0,'text':'Nhưng nhưng đây cũng không phải.'}]})
            with patch('providers.generate', side_effect=service):
                result = worker.translation(p, {}, folder)
            self.assertEqual(result['segments'][0]['translated'],
                             'Nhưng đây cũng không phải.' if language == 'Vietnamese' else
                             'Nhưng nhưng đây cũng không phải.')

    def test_unverified_entity_claim_is_not_persisted(self):
        p = project(1)
        p['segments'][0]['translated'] = 'Một câu đã xác nhận.'
        glossary, _ = update_entity_glossary(empty_glossary(), p['segments'], [0],
            [{'source':'Nonexistent entity','target':'Imaginary name'}])
        self.assertEqual(glossary['entries'], [])
        corrupt = {'version':1, 'entries':[{'source':'Missing','target':'Absent',
            'first_id':0,'confirmed_ids':[0],'origin':'provider'}]}
        with self.assertRaises(ValueError):
            validate_entity_glossary(corrupt, p['segments'])


if __name__ == '__main__':
    unittest.main()
