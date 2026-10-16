"""9Router contracts: isolated state, synthetic tokens and MockTransport only."""
import base64
import json
import unittest
from unittest.mock import patch

import httpx

import credentials
import providers


def completion(content='{"lines":[]}', **extra):
    return httpx.Response(200, json={'choices': [{'message': {'content': content}, **extra}]})


class Router9ProviderTests(unittest.TestCase):
    def setUp(self):
        self.sleep = self.enterContext(patch('providers.time.sleep'))
        self.enterContext(patch('providers.random.uniform', return_value=0))

    def transport(self, values, *, follow_redirects=False):
        calls = []

        def handler(request):
            calls.append(request)
            return values[min(len(calls) - 1, len(values) - 1)]

        client = self.enterContext(httpx.Client(transport=httpx.MockTransport(handler),
                                                follow_redirects=follow_redirects))
        return client, calls

    def test_catalog_uses_configured_endpoint_and_exact_namespaced_ids(self):
        body = {'data': [{'id': 'cx/gpt-test'}, {'id': 'gg:gemini-test'}, {'id': 'models/custom'},
                         {'id': 'cx/gpt-6-luna(max)'}, {'id': 'cx/gpt-6-luna[1m](max)'},
                         {'id': 'cx/gpt-test(max'}, {'id': 'cx/gpt-test[]'},
                         {'id': '../wrong'}, {'id': 'cx//wrong'}, {'id': 'cx/bad?token=secret'},
                         {'id': 'cx/audio-transcribe'}]}
        client, calls = self.transport([httpx.Response(200, json=body)])
        events = []
        result = providers.list_models('router9', 'TEST-TOKEN', client,
                                       base_url='http://127.0.0.1:20129/v1/', on_request=events.append)
        self.assertEqual(result, sorted(['cx/gpt-test', 'gg:gemini-test', 'models/custom',
                                         'cx/gpt-6-luna(max)', 'cx/gpt-6-luna[1m](max)']))
        self.assertEqual(str(calls[0].url), 'http://127.0.0.1:20129/v1/models')
        self.assertEqual(calls[0].headers['authorization'], 'Bearer TEST-TOKEN')
        self.assertEqual([event['event'] for event in events], ['before_send', 'response'])
        self.assertEqual(providers.CATALOG_SEEDS['router9'], [])

    def test_endpoint_normalization_and_https_or_explicit_loopback_only(self):
        for source, expected in (
            (None, providers.ROUTER9_BASE_URL),
            ('http://localhost:20129', 'http://localhost:20129/v1'),
            ('http://127.0.0.2:20129/v1/', 'http://127.0.0.2:20129/v1'),
            ('http://[::1]:20129/v1', 'http://[::1]:20129/v1'),
            ('https://router.example/api/openai/v1', 'https://router.example/api/openai/v1')):
            with self.subTest(source=source):
                self.assertEqual(providers.router_base_url(source), expected)

    def test_unsafe_endpoint_is_rejected_before_transport_or_budget(self):
        values = ('http://router.example/v1', 'ftp://localhost/v1', 'https://name:PRIVATE@router.example/v1',
                  'https://router.example/v1?token=PRIVATE', 'https://router.example/v1#PRIVATE',
                  'https://router.example/v1?', 'https://router.example/v1#',
                  'https://router.example/../v1', 'https://router.example/api/%2e%2e/v1',
                  'https://router.example/v1//wrong', 'http://localhost.evil.example/v1',
                  'http://127.0.0.1:0/v1', 'http://127.0.0.1:65536/v1',
                  'http://localhost\\@router.example/v1', 'https://router.example/\nv1')
        for value in values:
            with self.subTest(value=value):
                client, calls = self.transport([completion()])
                events = []
                with self.assertRaises(ValueError) as caught:
                    providers.generate('router9', 'TEST-TOKEN', 'cx/gpt-test', 'PRIVATE', client=client,
                                       base_url=value, on_request=events.append)
                self.assertFalse(calls)
                self.assertFalse(events)
                self.assertNotIn('PRIVATE', str(caught.exception))

    def test_model_routes_allow_namespaces_without_weakening_explicit_direct_providers(self):
        for model in ('cx/gpt-test', 'gg/gemini-test', 'a:b/c-d.1_2', 'models/custom',
                      'cx/gpt-6-luna(max)', 'cx/gpt-6-luna[1m](max)'):
            self.assertEqual(providers.safe_model(model, 'router9'), model)
            self.assertEqual(providers.safe_model(model), model)
            providers.ensure_model_provider('router9', model)
        for model in ('../evil', 'a/../evil', '/cx/test', 'cx/test/', 'cx//test', 'a::b',
                      'a?b', 'a#b', 'a%2fb', 'a\\b', 'a b', 'cx/test(max', 'cx/test[1m)',
                      'cx/test()', 'cx/test[]', 'cx/test((max))', 'cx/test[../x]',
                      'cx/test(max)&x', 'cx/test(\"max\")', 'cx/test\n',
                      'x/' + 'y' * 200, None, True, 123, {}):
            with self.subTest(model=model), self.assertRaises(ValueError):
                providers.safe_model(model, 'router9')
        for provider, model in (('gemini', 'gg/gemini-test'), ('deepseek', 'ds:deepseek-test')):
            with self.subTest(provider=provider), self.assertRaises(ValueError):
                providers.safe_model(model, provider)
        with self.assertRaises(providers.ProviderError):
            providers.ensure_model_provider('gemini', 'deepseek-test')
        self.assertEqual(providers.safe_model('models/gemini-test', 'gemini'), 'gemini-test')

    def test_generation_preserves_catalog_decorated_route_exactly(self):
        for model in ('cx/gpt-6-luna(max)', 'cx/gpt-6-luna[1m](max)'):
            with self.subTest(model=model):
                client, calls = self.transport([completion('OK')])
                self.assertEqual(providers.generate('router9', 'TEST-TOKEN', model, 'hi', client=client), 'OK')
                self.assertEqual(json.loads(calls[0].content)['model'], model)
        for provider, model in (('gemini', 'gemini-test(max)'), ('deepseek', 'deepseek-test[1m]')):
            with self.subTest(provider=provider), self.assertRaises(ValueError):
                providers.safe_model(model, provider)

    def test_generation_retries_exact_route_and_records_allowlisted_usage(self):
        ok = completion().json()
        ok['usage'] = {'prompt_tokens': 11, 'completion_tokens': 7, 'total_tokens': 18,
                       'prompt_tokens_details': {'cached_tokens': 5},
                       'completion_tokens_details': {'reasoning_tokens': 2}, 'private': 'PRIVATE'}
        client, calls = self.transport([httpx.Response(503, text='PRIVATE'), httpx.Response(200, json=ok)])
        events = []
        text = providers.generate('router9', 'TEST-TOKEN', 'cx/gpt-test', 'PRIVATE-PROMPT',
                                  json_mode=True, client=client, base_url='http://127.0.0.1:20129/v1',
                                  on_request=events.append, max_attempts=2)
        self.assertEqual(text, '{"lines":[]}')
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0].content, calls[1].content)
        self.assertEqual(str(calls[0].url), 'http://127.0.0.1:20129/v1/chat/completions')
        body = json.loads(calls[0].content)
        self.assertEqual(body['model'], 'cx/gpt-test')
        self.assertEqual(body['response_format'], {'type': 'json_object'})
        self.assertEqual(events[-1], {'event': 'usage', 'usage': {'input_tokens': 11, 'output_tokens': 7,
            'total_tokens': 18, 'cached_input_tokens': 5, 'thinking_tokens': 2}})
        for private in ('TEST-TOKEN', 'PRIVATE', 'PRIVATE-PROMPT', 'cx/gpt-test', '127.0.0.1'):
            self.assertNotIn(private, json.dumps(events))

    def test_text_content_parts_are_joined_and_invalid_parts_fail_closed(self):
        client, calls = self.transport([completion([{'type': 'text', 'text': 'Xin '},
                                                   {'type': 'output_text', 'text': 'chào'}])])
        self.assertEqual(providers.generate('router9', 'TEST-TOKEN', 'cx/gpt-test', 'hi', client=client), 'Xin chào')
        for content in ([{'type': 'text', 'text': 1}], [{'type': 'image_url', 'image_url': {}}], []):
            with self.subTest(content=content):
                client, calls = self.transport([completion(content)])
                with self.assertRaises(providers.ProviderError):
                    providers.generate('router9', 'TEST-TOKEN', 'cx/gpt-test', 'hi', client=client)
                self.assertEqual(len(calls), 1)

    def test_json_output_is_validated_once_and_usage_is_kept_on_invalid_output(self):
        for text in ('not json PRIVATE', '{"same":1,"same":2}', '[1]', 'null', 'true',
                     '{"value":NaN}'):
            with self.subTest(text=text):
                response = completion(text).json()
                response['usage'] = {'total_tokens': 12}
                client, calls = self.transport([httpx.Response(200, json=response)])
                events = []
                with self.assertRaises(providers.ProviderError) as caught:
                    providers.generate('router9', 'TEST-TOKEN', 'cx/gpt-test', 'hi', json_mode=True,
                                       client=client, on_request=events.append)
                self.assertEqual(caught.exception.status, 'output')
                self.assertEqual(caught.exception.output_reason, 'invalid_json')
                self.assertNotIn('PRIVATE', str(caught.exception))
                self.assertEqual(len(calls), 1)
                self.assertEqual(events[-1], {'event': 'usage', 'usage': {'total_tokens': 12}})

    def test_invalid_json_does_not_retry_or_confirm_probe_recovery(self):
        from contextlib import contextmanager
        hooks = []

        @contextmanager
        def slot(*args, **kwargs):
            def receipt(*args, **kwargs):
                hooks.append(kwargs)
            yield receipt

        client, calls = self.transport([completion('[1]'), completion('{"lines":[]}')])
        with patch('providers.request_slot', slot):
            with self.assertRaises(providers.ProviderError) as caught:
                providers.generate('router9', 'TEST-TOKEN', 'cx/gpt-test', 'hi', json_mode=True,
                                   probe=True, client=client, max_attempts=5)
        self.assertEqual(caught.exception.output_reason, 'invalid_json')
        self.assertEqual(len(calls), 1)
        self.assertFalse(hooks)

    def test_owned_clients_disable_environment_proxy_and_redirects(self):
        native_client, options, calls = httpx.Client, [], []

        def handler(request):
            calls.append(request)
            return (httpx.Response(200, json={'data': [{'id': 'cx/gpt-test'}]})
                    if request.url.path.endswith('/models') else completion('OK'))

        def factory(**kwargs):
            options.append(kwargs.copy())
            return native_client(**kwargs, transport=httpx.MockTransport(handler))

        with patch('providers.httpx.Client', side_effect=factory):
            self.assertEqual(providers.list_models('router9', 'TEST-TOKEN'), ['cx/gpt-test'])
            self.assertEqual(providers.generate('router9', 'TEST-TOKEN', 'cx/gpt-test', 'hi'), 'OK')
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(item['trust_env'] is False and item['follow_redirects'] is False for item in options))

    def test_router_redirects_are_never_followed_even_with_injected_following_client(self):
        for mode in ('catalog', 'generation'):
            with self.subTest(mode=mode):
                client, calls = self.transport([httpx.Response(302, headers={'Location': 'https://other.example/steal'})],
                                                follow_redirects=True)
                result = providers.check_connection('router9', 'TEST-TOKEN', 'cx/gpt-test', mode, client)
                self.assertFalse(result['ok'])
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0].url.host, '127.0.0.1')

    def test_probe_proof_is_bound_to_normalized_endpoint_and_has_request_counters(self):
        response = completion('OK').json()
        response['usage'] = {'prompt_tokens': 4, 'completion_tokens': 1, 'total_tokens': 5}
        client, calls = self.transport([httpx.Response(200, json=response)])
        result = providers.check_connection('router9', 'TEST-TOKEN', 'cx/gpt-test', 'generation', client,
                                             base_url='http://localhost:20129/v1/')
        self.assertTrue(result['ok'])
        self.assertEqual(result['base_url'], 'http://localhost:20129/v1')
        self.assertEqual(result['api_usage']['request_attempts'], 1)
        self.assertEqual(result['api_usage']['reported_tokens']['total_tokens'], 5)
        self.assertNotIn('TEST-TOKEN', json.dumps(result))
        self.assertEqual(json.loads(calls[0].content)['max_tokens'], 256)

    def test_catalog_check_does_not_send_generation_prompt(self):
        client, calls = self.transport([httpx.Response(200, json={'data': []})])
        result = providers.check_connection('router9', 'TEST-TOKEN', mode='catalog', client=client)
        self.assertTrue(result['ok'])
        self.assertEqual(result['models'], [])
        self.assertEqual((calls[0].method, calls[0].content), ('GET', b''))
        self.assertEqual(result['api_usage']['request_attempts'], 1)

    def test_multimodal_evidence_and_output_bound_are_explicit(self):
        image = {'mime_type': 'image/jpeg', 'data': base64.b64encode(b'synthetic-jpeg-fixture').decode()}
        client, calls = self.transport([completion()])
        providers.generate('router9', 'TEST-TOKEN', 'gg/gemini-test', 'classify json', json_mode=True,
                           client=client, images=[image], max_output_tokens=16384)
        body = json.loads(calls[0].content)
        self.assertEqual(body['max_tokens'], 16384)
        self.assertEqual(body['messages'][0]['content'], [
            {'type': 'text', 'text': 'classify json'},
            {'type': 'image_url', 'image_url': {'url': 'data:image/jpeg;base64,' + image['data']}}])

    def test_invalid_evidence_or_token_bound_is_rejected_without_network(self):
        for images in ([{'mime_type': 'image/svg+xml', 'data': 'aGVsbG8='}],
                       [{'mime_type': 'image/jpeg', 'data': 'https://private.example'}],
                       [{'mime_type': 'image/jpeg', 'data': ''}],
                       [{'mime_type': 'image/jpeg', 'data': 'aGVsbG8='}] * 9):
            with self.subTest(images=images):
                client, calls = self.transport([completion()])
                with self.assertRaises(ValueError):
                    providers.generate('router9', 'TEST-TOKEN', 'cx/gpt-test', 'hi', client=client, images=images)
                self.assertFalse(calls)
        for count in (True, 0, 65537, 1.5, '16384'):
            client, calls = self.transport([completion()])
            with self.assertRaises(ValueError):
                providers.generate('router9', 'TEST-TOKEN', 'cx/gpt-test', 'hi', client=client, max_output_tokens=count)
            self.assertFalse(calls)

    def test_quota_credit_and_auth_are_bounded_and_safe(self):
        for status in (401, 402, 429):
            with self.subTest(status=status):
                client, calls = self.transport([httpx.Response(status, json={'error': {'message': 'PRIVATE'}})])
                result = providers.check_connection('router9', 'TEST-TOKEN', 'cx/gpt-test', 'generation', client)
                self.assertFalse(result['ok'])
                self.assertEqual(result['http_status'], status)
                self.assertEqual(len(calls), 1)
                self.assertEqual(result['api_usage']['usage_unknown_requests'], 1)
                self.assertNotIn('PRIVATE', json.dumps(result))
                self.assertNotIn('TEST-TOKEN', json.dumps(result))

    def test_budget_or_cancellation_stops_request_before_send(self):
        client, calls = self.transport([completion()])

        def budget(event):
            raise RuntimeError('local request cap')

        with self.assertRaisesRegex(RuntimeError, 'local request cap'):
            providers.generate('router9', 'TEST-TOKEN', 'cx/gpt-test', 'hi', client=client, on_request=budget)
        with self.assertRaises(providers.ProviderError) as caught:
            providers.generate('router9', 'TEST-TOKEN', 'cx/gpt-test', 'hi', client=client, should_cancel=lambda: True)
        self.assertEqual(caught.exception.status, 'cancelled')
        self.assertFalse(calls)

    def test_router_credentials_have_distinct_windows_vault_namespace(self):
        store, metadata = {}, {}

        def save(service, name, secret):
            self.assertEqual(service, 'VietDubStudio')
            store[name] = secret

        with patch.object(credentials.keyring, 'set_password', side_effect=save), \
             patch.object(credentials.keyring, 'get_password', side_effect=lambda service, name: store.get(name)), \
             patch.object(credentials, 'get', side_effect=lambda bucket, name: metadata.get((bucket, name))), \
             patch.object(credentials, 'put', side_effect=lambda bucket, name, value: metadata.update({(bucket, name): value})):
            result = credentials.store_credential('router9', 'SYNTHETIC-TOKEN')
            self.assertTrue(result['present'])
            self.assertTrue(result['read_back'])
            self.assertEqual(store, {'router9': 'SYNTHETIC-TOKEN'})
            self.assertEqual(credentials.active_secret('router9'), 'SYNTHETIC-TOKEN')
            self.assertNotIn('SYNTHETIC-TOKEN', json.dumps(result))
            self.assertEqual(credentials.secret_name('router9', 'a' * 32), 'router9:' + 'a' * 32)


if __name__ == '__main__':
    unittest.main()
