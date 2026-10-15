"""Public-connection continuations retain a stable allowance, without API calls."""
import copy
from pathlib import Path
import tempfile
import unittest
import uuid

from core import DATA, DEFAULTS
from provider_usage import (prepare_continuation_lineage, ensure_continuation_lineage_folder,
                            TranslationUsage, lineage_status)


class ContinuationConnectionBudgetTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(self.enterContext(tempfile.TemporaryDirectory(dir=DATA)))
        self.jobs = self.base/'jobs'; self.jobs.mkdir()
        self.source = self.base/'source.mp4'; self.source.write_bytes(b'source fixture')
        self.p = {'id':uuid.uuid4().hex, 'source_revision':'same-source',
                  'media':{'path':str(self.source),'duration':10},
                  'params':DEFAULTS | {'provider':'deepseek','translation_model':'deepseek-chat',
                                      'translation_scope':'missing'},
                  'segments':[{'start':0,'end':2,'text':'Source','translated':''}]}
        self.root = uuid.uuid4().hex; (self.jobs/self.root).mkdir()
        self.history = [self.row(self.root, 'gemini', 'gemini-test')]

    def row(self, jid, provider, model, **extra):
        return {'id':jid,'project_id':self.p['id'],'source_revision':self.p['source_revision'],
                'kind':'pipeline','state':'error','created':1,'stage':'translate',
                'configuration':{'provider':provider,'translation_model':model,'pipeline_translate':True}, **extra}

    def prepare(self, p=None):
        return prepare_continuation_lineage(p or self.p,'pipeline',self.root,self.history,jobs_root=self.jobs)

    def test_first_switch_is_read_only_and_does_not_import_unknown_gemini(self):
        before = set(self.jobs.iterdir())
        descriptor = self.prepare()
        self.assertNotEqual(descriptor['root_id'],self.root)
        self.assertEqual(descriptor['connection_partition_of'],self.root)
        self.assertTrue(lineage_status(descriptor,jobs_root=self.jobs)['execution_allowed'])
        self.assertEqual(descriptor['unknown_job_ids'],[])
        self.assertEqual(set(self.jobs.iterdir()),before)

    def target_attempt(self):
        descriptor = self.prepare()
        ensure_continuation_lineage_folder(descriptor,jobs_root=self.jobs)
        jid = uuid.uuid4().hex; folder = self.jobs/jid; folder.mkdir()
        usage = TranslationUsage(folder,1,20,'deepseek','deepseek-chat',lineage=descriptor,
                                 job_id=jid,jobs_root=self.jobs)
        usage.set_batch([0],'fixture')
        usage.observe({'event':'before_send','attempt':1})
        self.history.append(self.row(jid,'deepseek','deepseek-chat',continuation_from=self.root,
                                     api_usage_lineage=descriptor,created=2))
        return descriptor, usage

    def test_return_to_target_preserves_root_and_count(self):
        original, _ = self.target_attempt()
        returned = self.prepare()
        self.assertEqual(returned['root_id'],original['root_id'])
        self.assertEqual(lineage_status(returned,jobs_root=self.jobs)['lineage_request_attempts'],1)
        self.assertEqual(returned['request_limit'],original['request_limit'])

    def test_changing_model_cannot_reset_target_budget(self):
        original, usage = self.target_attempt()
        altered = copy.deepcopy(self.p); altered['params']['translation_model']='another-model'
        changed = self.prepare(altered)
        self.assertEqual(changed['root_id'], original['root_id'])
        self.assertEqual(changed['request_limit'], original['request_limit'])
        self.assertEqual(lineage_status(changed, jobs_root=self.jobs)['lineage_request_attempts'], 1)
        for _ in range(original['request_limit'] - 1):
            usage.observe({'event':'before_send','attempt':1})
        self.assertEqual(lineage_status(self.prepare(altered), jobs_root=self.jobs)['blocked_code'],
                         'request_budget_exhausted')

    def test_original_unknown_gemini_stays_blocked_after_switch(self):
        self.target_attempt()
        original = copy.deepcopy(self.p)
        original['params'].update(provider='gemini',translation_model='gemini-test')
        descriptor = self.prepare(original)
        self.assertEqual(descriptor['root_id'],self.root)
        self.assertEqual(descriptor['unknown_job_ids'],[self.root])
        self.assertFalse(lineage_status(descriptor,jobs_root=self.jobs)['execution_allowed'])

    def test_unknown_target_history_blocks_instead_of_new_allowance(self):
        jid = uuid.uuid4().hex
        self.history.append(self.row(jid,'deepseek','deepseek-chat',continuation_from=self.root))
        descriptor = self.prepare()
        self.assertEqual(descriptor['unknown_job_ids'],[jid])
        self.assertFalse(lineage_status(descriptor,jobs_root=self.jobs)['execution_allowed'])

    def test_unknown_connection_history_is_not_exempted(self):
        jid = uuid.uuid4().hex
        self.history.append(self.row(jid,None,None,continuation_from=self.root))
        self.assertIn(jid,self.prepare()['unknown_job_ids'])

    def test_endpoint_partitions_but_model_does_not(self):
        router = copy.deepcopy(self.p)
        router['params'].update(provider='router9',translation_model='route/model',
                                router9_base_url='http://127.0.0.1:20129/v1')
        first = self.prepare(router)
        router['params']['translation_model']='route/other'
        self.assertEqual(self.prepare(router)['root_id'],first['root_id'])
        router['params']['router9_base_url']='http://127.0.0.1:20130/v1'
        self.assertNotEqual(self.prepare(router)['root_id'],first['root_id'])

    def test_exhausted_target_stays_exhausted(self):
        descriptor, usage = self.target_attempt()
        for _ in range(descriptor['request_limit']-1):
            usage.observe({'event':'before_send','attempt':1})
        status = lineage_status(self.prepare(),jobs_root=self.jobs)
        self.assertFalse(status['execution_allowed'])
        self.assertEqual(status['blocked_code'],'request_budget_exhausted')
