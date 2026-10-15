"""Virtual-clock scheduler contracts; no provider traffic or credential access."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
import providers
from request_pacing import RequestPacer, QUOTA_PROBE_COOLDOWN


class VirtualClock:
    def __init__(self):
        self.now = 1000.
        self.waits = []
    def time(self):
        return self.now
    def sleep(self, value):
        self.waits.append(value)
        self.now += value


class RequestPacingTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.clock = VirtualClock()
        self.pacer = RequestPacer(self.folder, clock=self.clock.time, sleep=self.clock.sleep)

    def client(self, handler):
        return self.enterContext(httpx.Client(transport=httpx.MockTransport(handler)))

    def test_restart_preserves_spacing(self):
        with self.pacer.slot('router9', 'http://127.0.0.1:20129/v1'):
            pass
        restarted = RequestPacer(self.folder, clock=self.clock.time, sleep=self.clock.sleep)
        with restarted.slot('router9', 'http://127.0.0.1:20129/v1/'):
            self.assertGreaterEqual(self.clock.now, 1012.)

    def test_different_connections_do_not_share_wait(self):
        for provider, endpoint in [('gemini', None), ('deepseek', None),
                ('router9', 'http://127.0.0.1:20129/v1'),
                ('router9', 'http://127.0.0.1:20130/v1')]:
            with self.pacer.slot(provider, endpoint):
                self.assertEqual(self.clock.now, 1000.)

    def test_catalog_probe_generation_and_model_switch_share_bucket(self):
        sent, admissions, waits = [], [], []
        def handler(request):
            sent.append(self.clock.now)
            if request.method == 'GET':
                return httpx.Response(200, json={'data': [{'id': 'oc/muse'}]})
            return httpx.Response(200, json={'choices': [{'message': {'content': 'OK'}}]})
        client = self.client(handler)
        def observe(event):
            if event['event'] == 'before_send':
                admissions.append(self.clock.now)
            if event['event'] == 'rate_wait':
                waits.append(event)
        with patch('providers.request_slot', self.pacer.slot):
            providers.list_models('router9', 'fixture', client=client,
                                  base_url='http://127.0.0.1:20129/v1', on_request=observe)
            for model, secret in [('cx/luna', 'fixture'), ('oc/muse', 'different-fixture')]:
                providers.generate('router9', secret, model, 'fixture text', probe=True,
                    client=client, base_url='http://127.0.0.1:20129/v1', on_request=observe)
        self.assertEqual(sent, admissions)
        self.assertGreaterEqual(sent[1] - sent[0], 12.)
        self.assertGreaterEqual(sent[2] - sent[1], 12.)
        self.assertEqual(len(waits), 2)

    def test_retries_are_paced_and_bounded(self):
        sent = []
        def handler(request):
            sent.append(self.clock.now)
            return httpx.Response(503)
        with patch('providers.request_slot', self.pacer.slot), patch('providers.time.sleep', self.clock.sleep):
            with self.assertRaises(providers.ProviderError):
                providers.generate('deepseek', 'fixture', 'deepseek-flash', 'fixture',
                                   client=self.client(handler), max_attempts=2)
        self.assertEqual(len(sent), 2)
        self.assertGreaterEqual(sent[1] - sent[0], 12.)

    def test_wait_cancellation_spends_no_request(self):
        with self.pacer.slot('deepseek'):
            pass
        events, sent = [], []
        with patch('providers.request_slot', self.pacer.slot):
            with self.assertRaises(providers.ProviderError) as caught:
                providers.generate('deepseek', 'fixture', 'deepseek-flash', 'fixture',
                    client=self.client(lambda request: sent.append(request)), on_request=events.append,
                    should_cancel=lambda: self.clock.now > 1000.3)
        self.assertEqual(caught.exception.status, 'cancelled')
        self.assertFalse(sent)
        self.assertFalse([event for event in events if event['event'] == 'before_send'])

    def test_changed_hold_is_checked_after_wait_before_network(self):
        with self.pacer.slot('deepseek'):
            pass
        sent, checked = [], []
        def admission(event):
            if event['event'] == 'before_send':
                checked.append(self.clock.now)
                raise RuntimeError('fixture hold activated during wait')
        with patch('providers.request_slot', self.pacer.slot):
            with self.assertRaisesRegex(RuntimeError, 'hold'):
                providers.generate('deepseek', 'fixture', 'deepseek-flash', 'fixture',
                    client=self.client(lambda request: sent.append(request)), on_request=admission)
        self.assertGreaterEqual(checked[0], 1012.)
        self.assertFalse(sent)

    def test_response_elapsed_excludes_scheduler_wait(self):
        with self.pacer.slot('deepseek'):
            pass
        events = []
        with patch('providers.request_slot', self.pacer.slot):
            providers.generate('deepseek', 'fixture', 'deepseek-flash', 'fixture',
                client=self.client(lambda request: httpx.Response(200, json={
                    'choices': [{'message': {'content': 'OK'}}]})), on_request=events.append)
        response = next(event for event in events if event['event'] == 'response')
        self.assertLess(response['elapsed_seconds'], 2.)

    def test_bad_durable_timestamp_fails_closed(self):
        with self.pacer.slot('deepseek'):
            pass
        self.clock.now = 0.
        with self.assertRaises(ValueError):
            with self.pacer.slot('deepseek'):
                self.fail('must not admit with clock rollback')

    def test_unknown_429_survives_restart_key_model_and_catalog_checks(self):
        sent, observed = [], []
        def handler(request):
            sent.append(request)
            return httpx.Response(429)
        with patch('providers.request_slot', self.pacer.slot):
            result = providers.check_connection('router9', 'fixture-one', 'cx/luna', 'generation',
                self.client(handler), base_url='http://127.0.0.1:20129/v1')
        self.assertEqual(result['api_usage']['request_attempts'], 1)
        restarted = RequestPacer(self.folder, clock=self.clock.time, sleep=self.clock.sleep)
        with patch('providers.request_slot', restarted.slot):
            result = providers.check_connection('router9', 'fixture-two', 'oc/muse', 'generation',
                self.client(handler), base_url='http://127.0.0.1:20129/v1')
            self.assertFalse(result['ok'])
            self.assertEqual(result['api_usage']['request_attempts'], 0)
            with self.assertRaises(providers.ProviderError):
                providers.list_models('router9', 'fixture-two', self.client(handler),
                    base_url='http://127.0.0.1:20129/v1', on_request=observed.append)
        self.assertEqual(len(sent), 1)
        self.assertFalse(observed)

    def test_unknown_quota_needs_explicit_probe_after_cooldown_and_never_auto_opens(self):
        endpoint = 'http://127.0.0.1:20129/v1'
        with self.pacer.slot('router9', endpoint) as note:
            note('unknown', None)
        self.clock.now += QUOTA_PROBE_COOLDOWN + 1
        with self.assertRaises(providers.ProviderError):
            with self.pacer.slot('router9', endpoint):
                self.fail('elapsed time must not prove unknown quota recovered')
        with patch('providers.request_slot', self.pacer.slot):
            result = providers.check_connection('router9', 'fixture', 'oc/muse', 'generation',
                self.client(lambda request: httpx.Response(200, json={
                    'choices':[{'message':{'content':'OK'}}]})), base_url=endpoint)
        self.assertTrue(result['ok'])
        with self.pacer.slot('router9', endpoint):
            pass

    def test_failed_recovery_probe_preserves_quota_and_429_restarts_cooldown(self):
        with self.pacer.slot('deepseek') as note:
            note('daily', None)
        self.clock.now += QUOTA_PROBE_COOLDOWN + 1
        with patch('providers.request_slot', self.pacer.slot):
            result = providers.check_connection('deepseek', 'fixture', 'deepseek-test', 'generation',
                self.client(lambda request: httpx.Response(503)))
            self.assertFalse(result['ok'])
            result = providers.check_connection('deepseek', 'fixture', 'deepseek-test', 'generation',
                self.client(lambda request: httpx.Response(429)))
            self.assertEqual(result['api_usage']['request_attempts'], 1)
            result = providers.check_connection('deepseek', 'fixture', 'deepseek-test', 'generation',
                self.client(lambda request: self.fail('second probe before cooldown')))
            self.assertEqual(result['api_usage']['request_attempts'], 0)

    def test_malformed_200_probe_cannot_clear_quota(self):
        with self.pacer.slot('deepseek') as note:
            note('unknown', None)
        self.clock.now += QUOTA_PROBE_COOLDOWN + 1
        with patch('providers.request_slot', self.pacer.slot):
            result = providers.check_connection('deepseek', 'fixture', 'deepseek-test', 'generation',
                self.client(lambda request: httpx.Response(200, json={'choices':[]})))
        self.assertFalse(result['ok'])
        with self.assertRaises(providers.ProviderError):
            with self.pacer.slot('deepseek'):
                self.fail('malformed output is not recovery evidence')

    def test_delayed_probe_receipt_does_not_clear_newer_429(self):
        with self.pacer.slot('deepseek') as note:
            note('unknown', None)
        self.clock.now += QUOTA_PROBE_COOLDOWN + 1
        with self.pacer.slot('deepseek', allow_quota_probe=True) as successful_probe:
            pass
        with self.pacer.slot('deepseek', allow_quota_probe=True) as newer:
            newer('unknown', None)
        successful_probe(probe_succeeded=True)
        with self.assertRaises(providers.ProviderError):
            with self.pacer.slot('deepseek'):
                self.fail('newer 429 evidence must survive delayed success')

    def test_known_retry_after_is_durable_and_other_endpoint_is_independent(self):
        endpoint = 'http://127.0.0.1:20129/v1'
        with self.pacer.slot('router9', endpoint) as note:
            note('requests_per_minute', 90.)
        with self.assertRaises(providers.ProviderError) as caught:
            with self.pacer.slot('router9', endpoint):
                self.fail('must wait for Retry-After')
        self.assertGreaterEqual(caught.exception.retry_after, 90.)
        with self.pacer.slot('router9', 'http://127.0.0.1:20130/v1'):
            pass
        self.clock.now += 91.
        restarted = RequestPacer(self.folder, clock=self.clock.time, sleep=self.clock.sleep)
        with restarted.slot('router9', endpoint):
            pass
