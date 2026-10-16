"""Provider resilience contracts; MockTransport only, never real API keys/calls."""
import copy
import json
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx
import providers


def text_response(provider='gemini', text='{"lines":[]}', **extra):
    if provider == 'gemini':
        return httpx.Response(200, json={'candidates':[{'content':{'parts':[{'text':text}]}, **extra}]})
    return httpx.Response(200, json={'choices':[{'message':{'content':text}, **extra}]})


class ProviderResilienceTests(unittest.TestCase):
    def setUp(self):
        self.sleep = self.enterContext(patch('providers.time.sleep'))
        self.enterContext(patch('providers.random.uniform', return_value=0))

    def client(self, handler):
        return self.enterContext(httpx.Client(transport=httpx.MockTransport(handler)))

    def sequence(self, values):
        calls = []
        def handler(request):
            calls.append(request)
            value = values[min(len(calls)-1, len(values)-1)]
            if isinstance(value, Exception):
                raise value
            return value
        return self.client(handler), calls

    def test_503_retries_same_key_model_prompt_then_succeeds(self):
        client, calls = self.sequence([httpx.Response(503, text='SECRET raw'), httpx.Response(503), text_response()])
        events = []
        self.assertEqual(providers.generate('gemini','SECRET','gemini-3.7-flash','original',json_mode=True,
                                          client=client,on_retry=events.append), '{"lines":[]}')
        self.assertEqual(len(calls), 3)
        self.assertEqual([call.args[0] for call in self.sleep.call_args_list], [1,2])
        self.assertEqual([e['next_attempt'] for e in events], [2,3])
        for event in events:
            self.assertEqual((event['http_status'],event['max_attempts']), (503,5))
            self.assertNotIn('SECRET',json.dumps(event))
        self.assertEqual(len({str(r.url) for r in calls}),1)
        self.assertTrue(all(r.headers['x-goog-api-key']=='SECRET' for r in calls))
        self.assertEqual(len({r.content for r in calls}),1)
        self.assertNotIn('SECRET',str(calls[0].url))

    def test_deepseek_502_retries_without_changing_request(self):
        client,calls = self.sequence([httpx.Response(502),text_response('deepseek','yes')])
        self.assertEqual(providers.generate('deepseek','SECRET','deepseek-flash','hi',client=client),'yes')
        self.assertEqual([r.headers['Authorization'] for r in calls],['Bearer SECRET']*2)
        self.assertEqual(calls[0].content,calls[1].content)

    def test_provider_model_mismatch_is_rejected_before_any_request(self):
        for provider, model in (('gemini', 'deepseek-flash'), ('deepseek', 'gemini-3.7-flash')):
            with self.subTest(provider=provider, model=model):
                client, calls = self.sequence([text_response(provider)])
                with self.assertRaises(providers.ProviderError) as caught:
                    providers.generate(provider, 'SECRET', model, 'hi', client=client)
                self.assertEqual(caught.exception.status, 'model')
                self.assertEqual(calls, [])
                result = providers.check_connection(provider, 'SECRET', model, 'generation', client)
                self.assertEqual((result['ok'], result['status']), (False, 'model'))
                self.assertEqual(calls, [])

    def test_exhausted_503_is_bounded_and_keeps_safe_status(self):
        client,calls = self.sequence([httpx.Response(503,text='SECRET raw')])
        with self.assertRaises(providers.ProviderError) as caught:
            providers.generate('gemini','SECRET','gemini-test','hi',client=client)
        self.assertEqual((caught.exception.status,caught.exception.http_status,caught.exception.attempts),('service',503,5))
        self.assertEqual(len(calls),5)
        self.assertNotIn('SECRET',str(caught.exception))

    def test_call_site_can_lower_transport_attempts_without_changing_generic_default(self):
        client, calls = self.sequence([httpx.Response(503)])
        events = []
        with self.assertRaises(providers.ProviderError) as caught:
            providers.generate('gemini', 'SECRET', 'gemini-test', 'hi', client=client,
                               max_attempts=2, on_retry=events.append)
        self.assertEqual((caught.exception.attempts, len(calls)), (2, 2))
        self.assertEqual(events[0]['max_attempts'], 2)
        for value in (True, 0, 6, 1.5, '2'):
            with self.subTest(value=value):
                client, calls = self.sequence([text_response()])
                with self.assertRaises(ValueError):
                    providers.generate('gemini', 'SECRET', 'gemini-test', 'hi', client=client,
                                       max_attempts=value)
                self.assertFalse(calls)

    def test_generation_probe_never_retries_and_reports_quota_metadata(self):
        client, calls = self.sequence([httpx.Response(503)])
        result = providers.check_connection('gemini', 'SECRET', 'gemini-test', 'generation', client)
        self.assertEqual((result['ok'], result['status'], len(calls)), (False, 'service', 1))
        self.assertEqual(result['api_usage']['request_attempts'], 1)
        self.assertEqual(result['api_usage']['responses_received'], 1)
        self.assertEqual(result['api_usage']['http_statuses'], [503])
        self.assertEqual(result['api_usage']['usage_unknown_requests'], 1)
        self.assertIn('chi phí chưa xác minh', result['message'])
        self.sleep.assert_not_called()

    def test_generation_probe_reports_allowlisted_token_usage_without_secrets(self):
        response = httpx.Response(200, json={'choices':[{'message':{'content':'OK'}}],
            'usage':{'prompt_tokens':7,'completion_tokens':3,'total_tokens':10,
                     'raw_field':'SECRET'}})
        client, calls = self.sequence([response])
        result = providers.check_connection('deepseek', 'SECRET', 'deepseek-test', 'generation', client)
        audit = result['api_usage']
        self.assertTrue(result['ok'])
        self.assertEqual((audit['request_attempts'], audit['responses_received'],
                          audit['usage_reported_requests'], audit['usage_unknown_requests']), (1,1,1,0))
        self.assertEqual(audit['reported_tokens']['total_tokens'], 10)
        self.assertNotIn('raw_field', json.dumps(result))
        self.assertNotIn('SECRET', json.dumps(result))
        self.assertIn('API báo 10 token', result['message'])

    def test_catalog_probe_counts_requests_without_calling_them_generation_usage(self):
        client, calls = self.sequence([httpx.Response(200,json={'models':[
            {'name':'models/gemini-test','supportedGenerationMethods':['generateContent']} ]})])
        result = providers.check_connection('gemini', 'SECRET', mode='catalog', client=client)
        audit = result['api_usage']
        self.assertTrue(result['ok'])
        self.assertEqual((audit['scope'], audit['request_attempts'], audit['responses_received']),
                         ('catalog',1,1))
        self.assertEqual((audit['usage_unknown_requests'], audit['reported_tokens']['total_tokens']), (0,0))
        self.assertIn('không gửi prompt dịch', result['message'])
        self.assertEqual(len(calls), 1)

    def test_request_budget_callback_prevents_send_and_exception_is_not_swallowed(self):
        client, calls = self.sequence([text_response()])
        def event(value):
            raise httpx.ConnectError('budget sentinel')
        with self.assertRaises(httpx.ConnectError):
            providers.generate('gemini', 'SECRET', 'gemini-test', 'private prompt', client=client,
                               on_request=event)
        self.assertEqual(len(calls), 0)
        self.sleep.assert_not_called()

    def test_response_callback_exception_does_not_trigger_another_send(self):
        client, calls = self.sequence([httpx.Response(503)])
        def event(value):
            if value['event'] == 'response':
                raise httpx.ConnectError('ledger sentinel')
        with self.assertRaises(httpx.ConnectError):
            providers.generate('gemini', 'SECRET', 'gemini-test', 'hi', client=client,
                               on_request=event)
        self.assertEqual(len(calls), 1)
        self.sleep.assert_not_called()

    def test_request_ledger_counters_exclude_key_prompt_provider_text_and_unknown_fields(self):
        response = text_response(text='PRIVATE-SUBTITLE')
        data = response.json()
        data['usageMetadata'] = {'promptTokenCount': 123, 'candidatesTokenCount': 45,
                                 'totalTokenCount': 180, 'thoughtsTokenCount': 12,
                                 'cachedContentTokenCount': 80, 'secret': 'PRIVATE-SECRET'}
        client, calls = self.sequence([httpx.Response(503, text='PRIVATE-SECRET'), httpx.Response(200, json=data)])
        events = []
        result = providers.generate('gemini', 'PRIVATE-SECRET', 'gemini-test', 'PRIVATE-PROMPT',
                                    client=client, max_attempts=2, on_request=events.append)
        self.assertEqual(result, 'PRIVATE-SUBTITLE')
        self.assertEqual([item['event'] for item in events],
                         ['before_send', 'response', 'before_send', 'response', 'usage'])
        self.assertEqual([item['http_status'] for item in events if item['event'] == 'response'], [503, 200])
        self.assertEqual(events[-1]['usage'], {'input_tokens': 123, 'output_tokens': 45, 'total_tokens': 180,
                                              'thinking_tokens': 12, 'cached_input_tokens': 80})
        for private in ('PRIVATE-SECRET', 'PRIVATE-PROMPT', 'PRIVATE-SUBTITLE', 'googleapis', 'headers'):
            self.assertNotIn(private, json.dumps(events))
        self.assertTrue(all(item['elapsed_seconds'] >= 0 for item in events if item['event'] == 'response'))

    def test_usage_counter_validation_and_deepseek_mapping(self):
        body = {'choices': [{'message': {'content': 'ok'}}], 'usage':
                {'prompt_tokens': 10, 'completion_tokens': True, 'total_tokens': -1,
                 'prompt_cache_hit_tokens': 3, 'completion_tokens_details': {'reasoning_tokens': 4},
                 'secret': 'PRIVATE-SECRET'}}
        client, calls = self.sequence([httpx.Response(200, json=body)])
        events = []
        providers.generate('deepseek', 'PRIVATE-SECRET', 'deepseek-flash', 'hi', client=client,
                           on_request=events.append)
        self.assertEqual(events[-1]['usage'], {'input_tokens': 10, 'cached_input_tokens': 3, 'thinking_tokens': 4})
        for bad in (True, -1, 2**53, 1.5, '123', None):
            self.assertEqual(providers._usage_counts('gemini', {'usageMetadata': {'promptTokenCount': bad}}), {})

    def test_usage_is_recorded_even_when_successful_response_stops_at_output_limit(self):
        body = text_response(finishReason='MAX_TOKENS').json()
        body['usageMetadata'] = {'totalTokenCount': 200}
        client, calls = self.sequence([httpx.Response(200, json=body)])
        events = []
        with self.assertRaises(providers.ProviderError):
            providers.generate('gemini', 'SECRET', 'gemini-test', 'hi', client=client, on_request=events.append)
        self.assertEqual(events[-1]['event'], 'usage')
        self.assertEqual(events[-1]['usage'], {'total_tokens': 200})
        self.assertEqual(events[-1]['configured_model'], 'gemini-test')
        self.assertEqual(len(calls), 1)

    def test_google_quota_detail_is_classified_without_exposing_identifiers_or_body(self):
        for quota_id, metric, scope in (
                ('GenerateRequestsPerDayPerProjectPerModel-FreeTier', 'generate_content_free_tier_requests', 'daily'),
                ('GenerateRequestsPerMinutePerProjectPerModel', 'requests', 'requests_per_minute'),
                ('GenerateContentInputTokensPerMinutePerProjectPerModel-FreeTier', 'tokens', 'tokens_per_minute'),
                ('PRIVATE-SECRET', 'PRIVATE-SECRET', 'unknown')):
            with self.subTest(scope=scope):
                body = {'error': {'message': 'PRIVATE-SECRET', 'details': [
                    {'@type': 'type.googleapis.com/google.rpc.QuotaFailure', 'violations': [
                        {'quotaId': quota_id, 'quotaMetric': metric, 'description': 'PRIVATE-SECRET'}]},
                    {'@type': 'type.googleapis.com/google.rpc.RetryInfo', 'retryDelay': '34.5s'}]}}
                client, calls = self.sequence([httpx.Response(429, json=body, headers={'Retry-After': '10'})])
                events = []
                with self.assertRaises(providers.ProviderError) as caught:
                    providers.generate('gemini', 'SECRET', 'gemini-test', 'hi', client=client, on_request=events.append)
                self.assertEqual((caught.exception.quota_scope, caught.exception.retry_after, len(calls)), (scope, 34.5, 1))
                self.assertEqual(events[-1]['quota_scope'], scope)
                self.assertEqual(events[-1]['retry_after'], 34.5)
                self.assertNotIn('PRIVATE-SECRET', json.dumps(events))
                self.assertNotIn('PRIVATE-SECRET', str(caught.exception))
                self.sleep.assert_not_called()

    def test_malformed_quota_detail_remains_unknown_and_safe(self):
        for body in ({'error': 'PRIVATE-SECRET'}, {'error': {'details': 'PRIVATE-SECRET'}},
                     {'error': {'details': [{'@type': 'type.googleapis.com/google.rpc.QuotaFailure',
                                            'violations': [{'quotaId': None, 'quotaMetric': {}}]},
                                           {'@type': 'type.googleapis.com/google.rpc.RetryInfo', 'retryDelay': 'nan'}]}}):
            client, calls = self.sequence([httpx.Response(429, json=body)])
            result = providers.check_connection('gemini', 'SECRET', 'gemini-test', 'generation', client)
            self.assertEqual((result['http_status'], result['quota_scope'], len(calls)), (429, 'unknown', 1))
            self.assertIsNone(result['retry_after'])
            self.assertNotIn('PRIVATE-SECRET', json.dumps(result))

    def test_daily_quota_takes_precedence_over_concurrent_minute_limit(self):
        body = {'error': {'details': [{'@type': 'type.googleapis.com/google.rpc.QuotaFailure', 'violations': [
            {'quotaId': 'GenerateRequestsPerMinute'}, {'quotaId': 'GenerateRequestsPerDay'}]}]}}
        client, calls = self.sequence([httpx.Response(429, json=body)])
        result = providers.check_connection('gemini', 'SECRET', 'gemini-test', 'generation', client)
        self.assertEqual(result['quota_scope'], 'daily')

    def test_client_auth_credit_quota_model_and_501_are_not_retried(self):
        for code,status in ((400,'request'),(401,'auth'),(402,'credit'),(403,'permission'),(404,'model'),(429,'quota'),(501,'service')):
            with self.subTest(code=code):
                self.sleep.reset_mock()
                client,calls=self.sequence([httpx.Response(code,text='SECRET raw')])
                with self.assertRaises(providers.ProviderError) as caught:
                    providers.generate('gemini','SECRET','gemini-test','hi',client=client)
                self.assertEqual(caught.exception.status,status)
                self.assertEqual(len(calls),1)
                self.sleep.assert_not_called()
                self.assertNotIn('SECRET',str(caught.exception))

    def test_timeout_connection_and_remote_protocol_are_retried(self):
        for error in (httpx.ReadTimeout('SECRET'),httpx.ConnectError('SECRET'),httpx.RemoteProtocolError('SECRET')):
            with self.subTest(error=type(error).__name__):
                client,calls=self.sequence([error,text_response()])
                events=[]
                providers.generate('gemini','SECRET','gemini-test','hi',client=client,on_retry=events.append)
                self.assertEqual(len(calls),2)
                self.assertNotIn('SECRET',json.dumps(events))

    def test_translation_read_timeout_returns_after_one_request_for_batch_adaptation(self):
        client,calls=self.sequence([httpx.ReadTimeout('SECRET prompt and connection data'),text_response()])
        with self.assertRaises(providers.ProviderError) as caught:
            providers.generate('gemini','SECRET','gemini-3.7-flash','full subtitle',
                               json_mode=True,client=client,read_timeout_attempts=1,latency_mode='translation')
        self.assertEqual((caught.exception.status,caught.exception.timeout_phase,caught.exception.attempts),
                         ('timeout','read',1))
        self.assertEqual(len(calls),1)
        self.sleep.assert_not_called()
        self.assertNotIn('SECRET',str(caught.exception))

    def test_translation_server_deadline_can_adapt_but_generic_504_still_retries(self):
        client,calls=self.sequence([httpx.Response(504,text='SECRET raw deadline')])
        with self.assertRaises(providers.ProviderError) as caught:
            providers.generate('gemini','SECRET','gemini-3.7-flash','hi',client=client,
                               read_timeout_attempts=1,latency_mode='translation')
        self.assertEqual((caught.exception.http_status,caught.exception.attempts),(504,1))
        self.assertEqual(len(calls),1)
        self.sleep.assert_not_called()
        self.assertNotIn('SECRET',str(caught.exception))
        client,calls=self.sequence([httpx.Response(504),text_response()])
        self.assertEqual(providers.generate('gemini','SECRET','gemini-3.7-flash','hi',client=client),'\u007b"lines":[]\u007d')
        self.assertEqual(len(calls),2)

    def test_translation_keeps_connect_and_service_retries_instead_of_splitting_network_failure(self):
        for failure in (httpx.ConnectTimeout('SECRET'),httpx.WriteTimeout('SECRET'),
                        httpx.PoolTimeout('SECRET'),httpx.Response(503)):
            with self.subTest(failure=type(failure).__name__):
                client,calls=self.sequence([failure,text_response()])
                self.assertEqual(providers.generate('gemini','SECRET','gemini-3.7-flash','full subtitle',
                    json_mode=True,client=client,read_timeout_attempts=1,latency_mode='translation'),'\u007b"lines":[]\u007d')
                self.assertEqual(len(calls),2)
                self.assertEqual(calls[0].content,calls[1].content)

    def test_exhausted_connect_timeout_keeps_safe_phase_metadata(self):
        client,calls=self.sequence([httpx.ConnectTimeout('SECRET')])
        with self.assertRaises(providers.ProviderError) as caught:
            providers.generate('gemini','SECRET','gemini-3.7-flash','hi',client=client,read_timeout_attempts=1)
        self.assertEqual((caught.exception.timeout_phase,caught.exception.attempts),('connect',5))
        self.assertNotIn('SECRET',str(caught.exception))

    def test_translation_low_thinking_keeps_complete_output_and_provider_defaults_elsewhere(self):
        for model,mode,expected in (('gemini-3.7-flash','translation',{'thinkingLevel':'low'}),
                                   ('gemini-3.8-flash-preview','translation',{'thinkingLevel':'low'}),
                                   ('gemini-3.7-flash',None,None),
                                   ('gemini-3.7-flash-image','translation',None),
                                   ('gemini-9-test','translation',None),
                                   ('gemini-2.5-flash','translation',None)):
            with self.subTest(model=model,mode=mode):
                client,calls=self.sequence([text_response(text='A complete, unshortened subtitle')])
                result=providers.generate('gemini','SECRET',model,'full subtitle',json_mode=True,
                                          client=client,latency_mode=mode)
                config=json.loads(calls[0].content)['generationConfig']
                self.assertEqual(result,'A complete, unshortened subtitle')
                self.assertEqual(config.get('thinkingConfig'),expected)
                self.assertNotIn('maxOutputTokens',config)
                self.assertEqual(config['responseMimeType'],'application/json')
        client,calls=self.sequence([text_response('deepseek','Complete output')])
        providers.generate('deepseek','SECRET','deepseek-flash','hi',json_mode=True,client=client,
                           latency_mode='translation',read_timeout_attempts=1)
        self.assertNotIn('thinkingConfig',json.loads(calls[0].content))

    def test_translation_owned_timeout_profile_is_bounded_and_generic_profile_is_unchanged(self):
        real_client=httpx.Client
        captured=[]
        def factory(**kwargs):
            captured.append(kwargs)
            return real_client(transport=httpx.MockTransport(lambda request:text_response()),**kwargs)
        with patch('providers.httpx.Client',side_effect=factory):
            providers.generate('gemini','SECRET','gemini-3.7-flash','hi',json_mode=True,
                               latency_mode='translation',read_timeout_attempts=1)
            providers.generate('gemini','SECRET','gemini-3.7-flash','hi')
        profile=captured[0]['timeout']
        self.assertEqual((profile.read,profile.connect,profile.write,profile.pool),(60,15,30,15))
        self.assertEqual(captured[1]['timeout'],120)

    def test_invalid_adaptation_options_do_not_send_any_request(self):
        for options in ({'read_timeout_attempts':True},{'read_timeout_attempts':0},
                        {'read_timeout_attempts':6},{'latency_mode':'unknown'}):
            client,calls=self.sequence([text_response()])
            with self.assertRaises(ValueError):
                providers.generate('gemini','SECRET','gemini-3.7-flash','hi',client=client,**options)
            self.assertFalse(calls)

    def test_timeout_exhaustion_reports_timeout_not_invalid_key(self):
        client,calls=self.sequence([httpx.ReadTimeout('SECRET')])
        result=providers.check_connection('gemini','SECRET',client=client)
        self.assertEqual((result['ok'],result['status']),(False,'timeout'))
        self.assertEqual(len(calls),5)
        self.assertNotIn('SECRET',json.dumps(result))

    def test_invalid_local_protocol_is_not_retried_or_leaked(self):
        client,calls=self.sequence([httpx.UnsupportedProtocol('SECRET')])
        with self.assertRaises(providers.ProviderError) as caught:
            providers.generate('gemini','SECRET','gemini-test','hi',client=client)
        self.assertEqual(len(calls),1)
        self.sleep.assert_not_called()
        self.assertNotIn('SECRET',str(caught.exception))

    def test_retry_after_seconds_is_respected(self):
        client,calls=self.sequence([httpx.Response(503,headers={'Retry-After':'7'}),text_response()])
        events=[]
        providers.generate('gemini','SECRET','gemini-test','hi',client=client,on_retry=events.append)
        self.assertEqual(events[0]['delay'],7)
        self.sleep.assert_called_once_with(7)

    def test_retry_after_http_date_and_malformed_values(self):
        now=datetime(2026,10,9,tzinfo=timezone.utc)
        response=httpx.Response(503,headers={'Retry-After':format_datetime(now+timedelta(seconds=9),usegmt=True)})
        with patch('providers.datetime') as clock:
            clock.now.return_value=now
            self.assertEqual(providers._retry_after(response),9)
        for value in ('garbage','nan','inf'):
            self.assertIsNone(providers._retry_after(httpx.Response(503,headers={'Retry-After':value})))

    def test_large_retry_after_stops_instead_of_retrying_too_early(self):
        client,calls=self.sequence([httpx.Response(503,headers={'Retry-After':'900'})])
        with self.assertRaises(providers.ProviderError) as caught:
            providers.generate('gemini','SECRET','gemini-test','hi',client=client)
        self.assertEqual(len(calls),1)
        self.assertEqual(caught.exception.attempts,1)
        self.sleep.assert_not_called()

    def test_wait_budget_applies_across_retries(self):
        client,calls=self.sequence([httpx.Response(503,headers={'Retry-After':'20'})])
        with self.assertRaises(providers.ProviderError):
            providers.generate('gemini','SECRET','gemini-test','hi',client=client)
        self.assertEqual(len(calls),2)
        self.sleep.assert_called_once_with(20)

    def test_cancellation_before_call_and_during_backoff(self):
        client,calls=self.sequence([httpx.Response(503),text_response()])
        with self.assertRaises(providers.ProviderError) as caught:
            providers.generate('gemini','SECRET','gemini-test','hi',client=client,should_cancel=lambda:True)
        self.assertEqual((caught.exception.status,len(calls)),('cancelled',0))
        cancel=[False]
        def event(value):
            cancel[0]=True
        with self.assertRaises(providers.ProviderError) as caught:
            providers.generate('gemini','SECRET','gemini-test','hi',client=client,on_retry=event,should_cancel=lambda:cancel[0])
        self.assertEqual((caught.exception.status,len(calls)),('cancelled',1))
        self.sleep.assert_not_called()

    def test_callback_cancellation_exception_is_not_wrapped_or_retried(self):
        class Cancelled(Exception):
            pass
        client,calls=self.sequence([httpx.Response(503)])
        def event(value):
            raise Cancelled('stop')
        with self.assertRaises(Cancelled):
            providers.generate('gemini','SECRET','gemini-test','hi',client=client,on_retry=event)
        self.assertEqual(len(calls),1)
        self.sleep.assert_not_called()

    def test_non_json_or_wrong_root_success_is_rejected_without_retry(self):
        for response in (httpx.Response(200,text='SECRET html'),httpx.Response(200,json=['SECRET'])):
            client,calls=self.sequence([response])
            with self.assertRaises(providers.ProviderError) as caught:
                providers.generate('gemini','SECRET','gemini-test','hi',client=client)
            self.assertEqual((caught.exception.status,len(calls)),('response',1))
            self.assertNotIn('SECRET',str(caught.exception))

    def test_catalog_retries_only_current_page_and_preserves_pagination(self):
        client,calls=self.sequence([httpx.Response(503),
            httpx.Response(200,json={'models':[{'name':'models/gemini-a','supportedGenerationMethods':['generateContent']}],'nextPageToken':'second'}),
            httpx.Response(200,json={'models':[{'name':'models/gemini-b','supportedGenerationMethods':['generateContent']}]})])
        self.assertEqual(providers.list_models('gemini','SECRET',client),['gemini-a','gemini-b'])
        self.assertNotIn('pageToken',calls[0].url.params)
        self.assertNotIn('pageToken',calls[1].url.params)
        self.assertEqual(calls[2].url.params['pageToken'],'second')

    def test_invalid_catalog_shapes_report_safe_response_error(self):
        for body in ({'models':None},{'models':['SECRET']},{'data':{}},{'models':[],'nextPageToken':{'SECRET':'SECRET'}},
                     {'models':[{'name':'models/gemini-test','supportedGenerationMethods':'generateContent'}]}):
            provider='deepseek' if 'data' in body else 'gemini'
            client,calls=self.sequence([httpx.Response(200,json=body)])
            result=providers.check_connection(provider,'SECRET',client=client)
            self.assertEqual((result['ok'],result['status'],len(calls)),(False,'response',1))
            self.assertNotIn('SECRET',json.dumps(result))

    def test_catalog_repeated_page_is_not_reported_as_complete(self):
        client,calls=self.sequence([httpx.Response(200,json={'models':[],'nextPageToken':'same'})])
        result=providers.check_connection('gemini','SECRET',client=client)
        self.assertEqual((result['ok'],result['status'],len(calls)),(False,'response',2))

    def test_catalog_page_limit_is_not_reported_as_complete(self):
        calls=[]
        def handler(request):
            calls.append(request)
            return httpx.Response(200,json={'models':[],'nextPageToken':str(len(calls))})
        result=providers.check_connection('gemini','SECRET',client=self.client(handler))
        self.assertEqual((result['ok'],result['status'],len(calls)),(False,'response',10))

    def test_gemini_thought_is_not_returned_as_translation(self):
        client,calls=self.sequence([httpx.Response(200,json={'candidates':[{'content':{'parts':[{'text':'SECRET thought','thought':True},{'text':'answer'}]}}]})])
        self.assertEqual(providers.generate('gemini','SECRET','gemini-test','hi',client=client),'answer')

    def test_blocked_and_truncated_output_fail_without_repeating_generation(self):
        cases=[('gemini',text_response(finishReason='SAFETY'),'blocked'),
               ('gemini',text_response(finishReason='MAX_TOKENS'),'output'),
               ('gemini',httpx.Response(200,json={'promptFeedback':{'blockReason':'SAFETY'}}),'blocked'),
               ('deepseek',text_response('deepseek',finish_reason='length'),'output'),
               ('deepseek',text_response('deepseek',finish_reason='content_filter'),'blocked')]
        for provider,response,status in cases:
            with self.subTest(provider=provider,status=status):
                client,calls=self.sequence([response])
                with self.assertRaises(providers.ProviderError) as caught:
                    providers.generate(provider,'SECRET',f'{provider}-test','hi',client=client)
                self.assertEqual((caught.exception.status,len(calls)),(status,1))

    def test_malformed_candidate_or_choice_does_not_crash_with_attribute_error(self):
        for provider,body in (('gemini',{'candidates':[1]}),('gemini',{'candidates':[{'content':{'parts':['SECRET']}}]}),('deepseek',{'choices':[1]})):
            client,calls=self.sequence([httpx.Response(200,json=body)])
            with self.assertRaises(providers.ProviderError) as caught:
                    providers.generate(provider,'SECRET',f'{provider}-test','hi',client=client)
            self.assertEqual(caught.exception.status,'output')
            self.assertNotIn('SECRET',str(caught.exception))

    def test_gemini_3_uses_sampling_defaults_and_older_models_keep_temperature(self):
        for model in ('gemini-3.7-flash','gemini-2.5-flash'):
            client,calls=self.sequence([text_response(text='OK')])
            providers.generate('gemini','SECRET',model,'hi',json_mode=True,client=client,probe=True)
            config=json.loads(calls[0].content)['generationConfig']
            self.assertEqual(config['responseMimeType'],'application/json')
            self.assertEqual(config['maxOutputTokens'],256)
            if model.startswith('gemini-3'):
                self.assertNotIn('temperature',config)
            else:
                self.assertEqual(config['temperature'],.2)


class ModelJsonSafetyTests(unittest.TestCase):
    def test_valid_json_fences_unicode_and_braces_inside_strings(self):
        document={'lines':[{'id':0,'text':'Tiếng Việt 😀 [\\"{}]'}]}
        text=json.dumps(document,ensure_ascii=False)
        for value in (text,'```json\n'+text+'\n```','```\n'+text+'\n```'):
            self.assertEqual(providers.parse_model_json(value),document)
        self.assertEqual(providers.parse_model_json('"\\ud83d\\ude00"'),'😀')

    def test_duplicate_keys_cannot_change_cue_or_schema_identity(self):
        for text in ('{"lines":[],"lines":[{"id":0,"text":"wrong"}]}',
                     '{"lines":[{"id":1,"id":0,"text":"wrong cue"}]}',
                     '{"lines":[{"id":0,"text":"first","text":"second"}]}'):
            with self.subTest(text=text),self.assertRaises(ValueError):
                providers.parse_model_json(text)

    def test_nonfinite_constants_and_exponent_overflow_are_rejected(self):
        for value in ('NaN','Infinity','-Infinity','1e9999','-1e9999'):
            with self.subTest(value=value),self.assertRaises(ValueError):
                providers.parse_model_json('{"extra":'+value+'}')

    def test_nesting_limit_is_checked_before_decoder_recursion(self):
        self.assertIsInstance(providers.parse_model_json('['*24+'0'+']'*24),list)
        for count in (25,2000):
            with self.subTest(count=count),self.assertRaises(ValueError):
                providers.parse_model_json('['*count+'0'+']'*count)

    def test_utf8_byte_limit_and_invalid_surrogates_are_safe(self):
        with self.assertRaises(ValueError):
            providers.parse_model_json('"ấấ"',max_bytes=7)
        self.assertEqual(providers.parse_model_json('"ấấ"',max_bytes=8),'ấấ')
        for text in ('"\ud800"','"\\ud800"','{"\\udfff":0}'):
            with self.subTest(),self.assertRaises(ValueError):
                providers.parse_model_json(text)
        with self.assertRaises(ValueError):
            providers.parse_model_json(' '* (4*1024**2+1))

    def test_malformed_error_never_contains_raw_text_or_key(self):
        for text in ('{"key":"PRIVATE-SECRET",bad}', '{"PRIVATE-SECRET":0,"PRIVATE-SECRET":1}',
                     '["PRIVATE-SECRET",NaN]'):
            with self.assertRaises(ValueError) as caught:
                providers.parse_model_json(text)
            self.assertNotIn('PRIVATE-SECRET',str(caught.exception))


class SpeechRewriteProviderTests(unittest.TestCase):
    def project(self):
        from core import DEFAULTS
        return {'params':DEFAULTS|{'translation_batch':1},
                'segments':[{'start':0,'end':3,'text':'Giảm 20%','translated':'Giảm 20%'}]}

    def test_rewrite_does_not_retry_exhausted_provider_again(self):
        from speech_rewrite import optimize_spoken
        with tempfile.TemporaryDirectory() as directory, patch('credentials.active_secret',return_value='fake'), \
                patch('providers.generate',side_effect=providers.ProviderError('service','HTTP 503',attempts=3)) as generate:
            with self.assertRaises(providers.ProviderError):
                optimize_spoken(self.project(),{},Path(directory))
            self.assertEqual(generate.call_count,1)
            checkpoint = Path(directory)/'speech-rewrite-checkpoint.json'
            self.assertTrue(checkpoint.exists())
            self.assertEqual(json.loads(checkpoint.read_text(encoding='utf-8'))['completed_indices'], [])

    def test_rewrite_rejects_coercible_ids_without_publishing_false_result(self):
        from speech_rewrite import optimize_spoken
        for identifier in (.7,False,'0'):
            project=self.project()
            original=copy.deepcopy(project)
            with self.subTest(identifier=identifier), tempfile.TemporaryDirectory() as directory, \
                    patch('credentials.active_secret',return_value='fake'),patch('speech_rewrite.time.sleep'), \
                    patch('providers.generate',return_value=json.dumps({'lines':[{'id':identifier,'spoken_text':'Giảm 20%'}]})) as generate:
                with self.assertRaises(ValueError):
                    optimize_spoken(project,{},Path(directory))
                self.assertEqual(generate.call_count,3)
                self.assertEqual(project,original)
                self.assertFalse((Path(directory)/'speech-suggestions.json').exists())

    def test_rewrite_forwards_retry_status_while_preserving_progress(self):
        from speech_rewrite import optimize_spoken
        events=[]
        event={'attempt':1,'next_attempt':2,'max_attempts':3,'delay':2.,'status':'service','http_status':503}
        def generate(*args,**kwargs):
            kwargs['on_retry'](event)
            return '{"lines":[{"id":0,"spoken_text":"Giảm 20%"}]}'
        with tempfile.TemporaryDirectory() as directory,patch('credentials.active_secret',return_value='fake'), \
                patch('providers.generate',side_effect=generate):
            result=optimize_spoken(self.project(),{},Path(directory),emit=lambda **value:events.append(value))
        self.assertEqual(events[0]['stage'],'provider_retry')
        self.assertEqual(events[0]['progress'],0)
        self.assertEqual(events[0]['provider_retry'],event)
        self.assertTrue(result['needs_review'])


if __name__ == '__main__':
    unittest.main()
