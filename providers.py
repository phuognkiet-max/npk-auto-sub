"""Public provider APIs only. No raw HTTP bodies, headers or secrets in errors."""
import re
import json
import time
import math
import random
import base64
import binascii
import ipaddress
from urllib.parse import urlsplit, urlunsplit
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import httpx
from request_pacing import request_slot

PROVIDERS = ('gemini', 'deepseek', 'router9')
ROUTER9_BASE_URL = 'http://127.0.0.1:20128/v1'
CATALOG_SEEDS = {
    'gemini': ['gemini-3.8-flash', 'gemini-3.7-flash', 'gemini-3.6-flash', 'gemini-2.5-flash'],
    'deepseek': ['deepseek-flash', 'deepseek-v4-pro'],
    # Route IDs belong to the user's router catalog; never guess an upstream.
    'router9': [],
}


RETRY_ATTEMPTS = 5  # One request plus four bounded retries for transient failures.
RETRY_WAIT_BUDGET = 30.0
TRANSIENT_HTTP = frozenset((408, 500, 502, 503, 504))


class ProviderError(RuntimeError):
    def __init__(self, status, message, *, http_status=None, attempts=1,
                 timeout_phase=None, output_reason=None, retry_after=None, quota_scope=None):
        self.status = status
        self.http_status = http_status
        self.attempts = attempts
        self.timeout_phase = timeout_phase
        self.output_reason = output_reason
        self.retry_after = retry_after
        self.quota_scope = quota_scope
        super().__init__(message)


def ensure_provider(provider):
    if provider not in PROVIDERS:
        raise ValueError('Nhà cung cấp không hợp lệ.')


def safe_model(model, provider=None):
    if not isinstance(model, str):
        raise ValueError('Model ID phải là văn bản trong danh mục model.')
    if provider in ('gemini', 'deepseek') or (provider is None and model.startswith('models/gemini-')):
        model = model.removeprefix('models/')
    if provider in ('gemini', 'deepseek'):
        valid = re.fullmatch(r'[a-zA-Z0-9._-]{1,120}', model)
    else:
        # Routers can namespace IDs and publish bracket/parenthesis suffixes
        # such as cx/gpt-6-luna[1m](max). Preserve these exact JSON values;
        # accepting syntax does not replace catalog membership checks.
        segment = r'[a-zA-Z0-9][a-zA-Z0-9._-]*(?:(?:\[[a-zA-Z0-9][a-zA-Z0-9._-]*\])|(?:\([a-zA-Z0-9][a-zA-Z0-9._-]*\)))*'
        valid = (1 <= len(model) <= 200 and re.fullmatch(
            segment + r'(?:(?:/|:)' + segment + r')*', model))
    if not valid:
        raise ValueError('Model ID không hợp lệ; chọn ID an toàn trong danh mục model.')
    return model


def ensure_model_provider(provider, model):
    """Fail closed when a model ID belongs to a different provider."""
    ensure_provider(provider)
    model = safe_model(model, provider)
    if provider == 'router9':
        return
    expected_prefix = f'{provider}-'
    if not model.lower().startswith(expected_prefix):
        raise ProviderError('model', 'Model không khớp nhà cung cấp đã chọn. Hãy tải danh mục model và chọn lại.')


def router_base_url(base_url=None):
    """Normalize a router endpoint without allowing credentials or insecure WAN HTTP."""
    value = ROUTER9_BASE_URL if base_url in (None, '') else base_url
    error = 'Địa chỉ 9Router không hợp lệ. Dùng HTTPS hoặc HTTP loopback và không kèm tài khoản, query hay fragment.'
    if not isinstance(value, str) or len(value) > 2048 or re.search(r'[\x00-\x20\x7f\\]', value):
        raise ValueError(error)
    try:
        parts = urlsplit(value)
        host = parts.hostname
        port = parts.port
        if (parts.scheme not in ('http', 'https') or not host or parts.username is not None or
                parts.password is not None or parts.query or parts.fragment or '?' in value or '#' in value):
            raise ValueError(error)
        if port is not None and not 1 <= port <= 65535:
            raise ValueError(error)
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = host.lower() == 'localhost'
        if parts.scheme == 'http' and not loopback:
            raise ValueError(error)
        path = parts.path.rstrip('/') or '/v1'
        if not re.fullmatch(r'(?:/[a-zA-Z0-9_-][a-zA-Z0-9._-]*)+', path) or any(
                segment in ('.', '..') for segment in path.split('/')):
            raise ValueError(error)
    except (ValueError, UnicodeError):
        raise ValueError(error) from None
    return urlunsplit((parts.scheme, parts.netloc, path, '', ''))


def _image_parts(images):
    """Validate bounded inline evidence. Images never become URLs fetched by this client."""
    if images is None:
        return []
    if not isinstance(images, list) or len(images) > 8:
        raise ValueError('Tối đa 8 ảnh bằng chứng cho mỗi yêu cầu API.')
    result = []
    for item in images:
        if not isinstance(item, dict) or set(item) != {'mime_type', 'data'} or item['mime_type'] not in (
                'image/jpeg', 'image/png', 'image/webp') or not isinstance(item['data'], str) or not (
                0 < len(item['data']) <= 5592408):
            raise ValueError('Ảnh bằng chứng API không hợp lệ hoặc vượt 4 MiB.')
        try:
            decoded = base64.b64decode(item['data'], validate=True)
        except (ValueError, binascii.Error):
            raise ValueError('Ảnh bằng chứng API không phải base64 hợp lệ.') from None
        if not decoded or len(decoded) > 4 * 1024**2:
            raise ValueError('Ảnh bằng chứng API không hợp lệ hoặc vượt 4 MiB.')
        result.append(dict(item))
    return result


def ensure_response(response):
    if response.status_code == 200:
        return
    code = response.status_code
    status, message = {
        400: ('request', 'Yêu cầu hoặc model không được chấp nhận; kiểm tra key, vùng và tham số.'),
        401: ('auth', 'Key không hợp lệ hoặc đã bị thu hồi.'),
        402: ('credit', 'Tài khoản không đủ số dư để gọi API.'),
        403: ('permission', 'Key/tài khoản không có quyền, bị giới hạn vùng hoặc bị chặn.'),
        404: ('model', 'Không tìm thấy model hoặc endpoint cho tài khoản này.'),
        429: ('quota', 'Đã chạm giới hạn tốc độ hoặc hạn mức. Chờ/cấp thêm quota; không tự xoay key.'),
    }.get(code, ('service', 'Dịch vụ chưa xử lý được yêu cầu. Thử lại sau.'))
    quota_scope, retry_after = _quota_details(response) if code == 429 else (None, None)
    raise ProviderError(status, f'HTTP {code} · {message}', http_status=code,
                        quota_scope=quota_scope, retry_after=retry_after)


def parse_model_json(text, *, max_bytes=4 * 1024**2, max_depth=24):
    """Parse bounded model JSON without ambiguous keys or unsafe constants.

    Model text and checkpoint files are data, never diagnostic excerpts. Check
    lexical nesting before decoding so hostile/deep output fails predictably.
    """
    if not isinstance(text, str) or len(text) > max_bytes:
        raise ValueError('Phản hồi JSON vượt giới hạn kích thước hoặc không phải văn bản.')
    try:
        encoded_size = len(text.encode('utf-8'))
    except UnicodeError:
        raise ValueError('Phản hồi JSON có văn bản Unicode không hợp lệ.') from None
    if encoded_size > max_bytes:
        raise ValueError('Phản hồi JSON vượt giới hạn kích thước.')
    value = text.strip()
    if value.startswith('```json') and value.endswith('```'):
        value = value[7:-3].strip()
    elif value.startswith('```') and value.endswith('```'):
        value = value[3:-3].strip()
    depth, in_string, escaped = 0, False, False
    for char in value:
        if in_string:
            if escaped:
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char in '[{':
            depth += 1
            if depth > max_depth:
                raise ValueError('Phản hồi JSON lồng quá sâu; không tự áp dụng.')
        elif char in ']}':
            depth -= 1

    def object_pairs(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError('Phản hồi JSON có trường trùng; không tự áp dụng.')
            result[key] = item
        return result

    def finite_float(number):
        parsed = float(number)
        if not math.isfinite(parsed):
            raise ValueError('Phản hồi JSON có số không hữu hạn; không tự áp dụng.')
        return parsed

    def invalid_constant(number):
        raise ValueError('Phản hồi JSON có số không hữu hạn; không tự áp dụng.')

    try:
        parsed = json.loads(value, object_pairs_hook=object_pairs,
                            parse_float=finite_float, parse_constant=invalid_constant)
    except (ValueError, TypeError, RecursionError, OverflowError):
        raise ValueError('Phản hồi JSON không hợp lệ; giữ phần đã hoàn tất và không tự áp dụng bản lỗi.') from None
    pending, count = [(parsed, 0)], 0
    while pending:
        item, nesting = pending.pop()
        count += 1
        if nesting > max_depth or count > 1000000:
            raise ValueError('Phản hồi JSON quá phức tạp; không tự áp dụng.')
        if isinstance(item, str) and re.search(r'[\ud800-\udfff]', item):
            raise ValueError('Phản hồi JSON có văn bản Unicode không hợp lệ.')
        if isinstance(item, dict):
            pending.extend((key, nesting + 1) for key in item)
            pending.extend((child, nesting + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, nesting + 1) for child in item)
    return parsed


def _response_json(response):
    # HTML gateway pages and malformed JSON are not successful API responses.
    # Never include JSON decoder excerpts, raw bodies or exception text in logs.
    try:
        data = parse_model_json(response.text)
    except (ValueError, UnicodeError):
        raise ProviderError('response', 'Dịch vụ trả phản hồi không phải JSON hợp lệ.') from None
    if not isinstance(data, dict):
        raise ProviderError('response', 'Cấu trúc phản hồi API không hợp lệ.')
    return data


def _retry_after(response):
    value = response.headers.get('Retry-After', '').strip()
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            moment = parsedate_to_datetime(value)
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
            seconds = (moment - datetime.now(timezone.utc)).total_seconds()
        except (ValueError, TypeError, OverflowError):
            return None
    return max(0., seconds) if math.isfinite(seconds) else None


def _quota_details(response):
    """Return a small quota category/delay, never provider body or identifiers."""
    try:
        data = _response_json(response)
    except ProviderError:
        return 'unknown', None
    error = data.get('error')
    details = error.get('details') if isinstance(error, dict) else None
    if not isinstance(details, list):
        return 'unknown', None
    scopes, retry_after = set(), None
    for detail in details[:100]:
        if not isinstance(detail, dict):
            continue
        if detail.get('@type') == 'type.googleapis.com/google.rpc.QuotaFailure':
            violations = detail.get('violations')
            if not isinstance(violations, list):
                continue
            for violation in violations[:100]:
                if not isinstance(violation, dict):
                    continue
                # The provider's description/message can contain arbitrary data.
                # Only documented quota ID/metric fields decide this category.
                identifiers = [violation.get(key) for key in ('quotaId', 'quotaMetric')]
                compact = ''.join(re.sub('[^a-z0-9]', '', value.lower())
                                  for value in identifiers if isinstance(value, str) and len(value) <= 2048)
                if 'perday' in compact:
                    scopes.add('daily')
                elif 'perminute' in compact and 'token' in compact:
                    scopes.add('tokens_per_minute')
                elif 'perminute' in compact and 'request' in compact:
                    scopes.add('requests_per_minute')
        elif detail.get('@type') == 'type.googleapis.com/google.rpc.RetryInfo':
            delay = detail.get('retryDelay')
            if isinstance(delay, str) and re.fullmatch(r'\d{1,10}(?:\.\d{1,9})?s', delay):
                seconds = float(delay[:-1])
                if math.isfinite(seconds):
                    retry_after = max(retry_after or 0., seconds)
    # A daily ceiling continues to block after a minute-level limit expires.
    scope = next((item for item in ('daily', 'tokens_per_minute', 'requests_per_minute')
                  if item in scopes), 'unknown')
    return scope, retry_after


def _usage_counts(provider, data):
    """Allowlisted nonnegative integer counters for a redacted request ledger."""
    source = data.get('usageMetadata' if provider == 'gemini' else 'usage')
    if not isinstance(source, dict):
        return {}
    fields = ({'promptTokenCount': 'input_tokens', 'candidatesTokenCount': 'output_tokens',
               'totalTokenCount': 'total_tokens', 'thoughtsTokenCount': 'thinking_tokens',
               'cachedContentTokenCount': 'cached_input_tokens'} if provider == 'gemini' else
              {'prompt_tokens': 'input_tokens', 'completion_tokens': 'output_tokens',
               'total_tokens': 'total_tokens', 'prompt_cache_hit_tokens': 'cached_input_tokens'})
    result = {name: source[key] for key, name in fields.items()
              if type(source.get(key)) is int and 0 <= source[key] <= 2**53 - 1}
    if provider != 'gemini':
        detail = source.get('completion_tokens_details')
        value = detail.get('reasoning_tokens') if isinstance(detail, dict) else None
        if type(value) is int and 0 <= value <= 2**53 - 1:
            result['thinking_tokens'] = value
        prompt_detail = source.get('prompt_tokens_details')
        cached = prompt_detail.get('cached_tokens') if isinstance(prompt_detail, dict) else None
        if type(cached) is int and 0 <= cached <= 2**53 - 1:
            result['cached_input_tokens'] = cached
    return result


def _validate_max_attempts(max_attempts):
    if type(max_attempts) is not int or not 1 <= max_attempts <= RETRY_ATTEMPTS:
        raise ValueError('Số lần gọi API tối đa không hợp lệ.')


def _check_cancel(should_cancel):
    if should_cancel is not None and should_cancel():
        raise ProviderError('cancelled', 'Đã hủy yêu cầu API trước lần thử tiếp theo.')


def _wait_retry(delay, should_cancel):
    if should_cancel is None:
        time.sleep(delay)
        return
    # Poll while waiting, so cancellation need not wait for all Retry-After.
    remaining = delay
    while remaining > 0:
        _check_cancel(should_cancel)
        step = min(.2, remaining)
        time.sleep(step)
        remaining -= step
    _check_cancel(should_cancel)


def _request(send, *, on_retry=None, should_cancel=None, read_timeout_attempts=None,
             max_attempts=RETRY_ATTEMPTS, on_request=None, pacing_provider=None, pacing_endpoint=None,
             quota_probe=False, probe_success_hooks=None):
    _validate_max_attempts(max_attempts)
    if read_timeout_attempts is not None and (type(read_timeout_attempts) is not int or
                                             not 1 <= read_timeout_attempts <= RETRY_ATTEMPTS):
        raise ValueError('Giới hạn thử lại khi chờ phản hồi không hợp lệ.')
    waited = 0.
    read_timeouts = 0
    for attempt in range(1, max_attempts + 1):
        _check_cancel(should_cancel)
        retry_after = None
        # Admission/budget callbacks must fail before the network request, and
        # their exceptions must not be mistaken for provider/network failures.
        started = None
        sending = False
        def admitted_send():
            nonlocal started, sending
            with request_slot(pacing_provider, pacing_endpoint,
                    check_cancel=lambda: _check_cancel(should_cancel), on_wait=on_request,
                    allow_quota_probe=quota_probe) as note_quota:
                _check_cancel(should_cancel)
                if on_request is not None:
                    on_request({'event': 'before_send', 'attempt': attempt, 'max_attempts': max_attempts})
                started = time.perf_counter()
                sending = True
                response = send()
                if response.status_code == 429 and note_quota is not None:
                    scope, delay = _quota_details(response)
                    header_delay = _retry_after(response)
                    if header_delay is not None:
                        delay = max(header_delay, delay or 0.)
                    try:
                        note_quota(scope, delay)
                    except BaseException:
                        response.close()
                        raise
                elif response.status_code == 200 and quota_probe and note_quota is not None:
                    # HTTP 200 alone does not prove usable generation. The
                    # caller commits recovery only after validating the output.
                    if probe_success_hooks is not None:
                        probe_success_hooks.append(note_quota)
                return response
        try:
            response = admitted_send()
        except httpx.TimeoutException as exc:
            if not sending:
                raise
            phase = ('read' if isinstance(exc, httpx.ReadTimeout) else
                     'connect' if isinstance(exc, httpx.ConnectTimeout) else
                     'write' if isinstance(exc, httpx.WriteTimeout) else
                     'pool' if isinstance(exc, httpx.PoolTimeout) else None)
            message = {'read':'Hết thời gian chờ phản hồi dịch vụ',
                       'connect':'Hết thời gian thiết lập kết nối tới dịch vụ',
                       'write':'Hết thời gian gửi yêu cầu tới dịch vụ',
                       'pool':'Hết thời gian chờ kết nối API sẵn sàng'}.get(phase, 'Hết thời gian chờ dịch vụ')
            error = ProviderError('timeout', message + '; chưa thể kết luận key sai.',
                                  attempts=attempt, timeout_phase=phase)
            if phase == 'read':
                read_timeouts += 1
                if read_timeout_attempts is not None and read_timeouts >= read_timeout_attempts:
                    raise error from None
        except (httpx.NetworkError, httpx.RemoteProtocolError):
            if not sending:
                raise
            error = ProviderError('network', 'Kết nối API bị gián đoạn. Kiểm tra mạng/proxy; chưa kết luận key sai.', attempts=attempt)
        except httpx.RequestError:
            if not sending:
                raise
            # Invalid protocol, proxy configuration or local request errors are
            # not made transient by retrying, and their text can expose secrets.
            raise ProviderError('network', 'Không kết nối được dịch vụ. Kiểm tra mạng/proxy và cấu hình API.', attempts=attempt) from None
        else:
            if on_request is not None:
                try:
                    event = {'event': 'response', 'attempt': attempt,
                             'http_status': response.status_code,
                             'elapsed_seconds': max(0., time.perf_counter() - started)}
                    if response.status_code == 429:
                        scope, detail_delay = _quota_details(response)
                        header_delay = _retry_after(response)
                        event['quota_scope'] = scope
                        delays = [value for value in (header_delay, detail_delay) if value is not None]
                        if delays:
                            event['retry_after'] = max(delays)
                    on_request(event)
                except BaseException:
                    response.close()
                    raise
            if response.status_code == 200:
                return response
            try:
                ensure_response(response)
            except ProviderError as exc:
                error = exc
                error.attempts = attempt
            header_delay = _retry_after(response)
            retry_after = max(value for value in (header_delay, error.retry_after) if value is not None) \
                if header_delay is not None or error.retry_after is not None else None
            error.retry_after = retry_after
            if response.status_code == 504 and read_timeout_attempts is not None and retry_after is None:
                read_timeouts += 1
                if read_timeouts >= read_timeout_attempts:
                    response.close()
                    raise error from None
            if response.status_code not in TRANSIENT_HTTP:
                response.close()
                raise error from None
            response.close()
        if attempt == max_attempts:
            raise ProviderError(error.status, f'{error} Đã thử {attempt} lần; giữ phần đã hoàn tất để tiếp tục sau.',
                                http_status=error.http_status, attempts=attempt,
                                timeout_phase=error.timeout_phase, output_reason=error.output_reason,
                                retry_after=error.retry_after, quota_scope=error.quota_scope) from None
        delay = 2. ** (attempt - 1) + random.uniform(0., .5)
        if retry_after is not None:
            delay = max(delay, retry_after)
        if waited + delay > RETRY_WAIT_BUDGET:
            raise ProviderError(error.status, f'{error} Thời gian chờ vượt giới hạn tự thử; hãy tiếp tục sau.',
                                http_status=error.http_status, attempts=attempt,
                                timeout_phase=error.timeout_phase, output_reason=error.output_reason,
                                retry_after=error.retry_after, quota_scope=error.quota_scope) from None
        if on_retry is not None:
            on_retry({'attempt': attempt, 'next_attempt': attempt + 1, 'max_attempts': max_attempts,
                      'delay': round(delay, 3), 'status': error.status,
                      'http_status': error.http_status,
                      'message': 'Dịch vụ tạm gián đoạn; đang chờ thử lại cùng key và model.'})
        _wait_retry(delay, should_cancel)
        waited += delay


def list_models(provider, secret, client=None, *, on_retry=None, should_cancel=None, on_request=None,
                base_url=None):
    ensure_provider(provider)
    endpoint = router_base_url(base_url) if provider == 'router9' else None
    if not secret:
        raise ProviderError('missing', 'Chưa có API key.')
    if client is None:
        options = {'trust_env': False} if provider == 'router9' else {}
        with httpx.Client(timeout=20, follow_redirects=False, **options) as owned:
            return list_models(provider, secret, owned, on_retry=on_retry, should_cancel=should_cancel,
                               on_request=on_request, base_url=endpoint)
    models, page, pages_seen = [], None, set()
    for _ in range(10):
        if provider == 'gemini':
            response = _request(lambda: client.get('https://generativelanguage.googleapis.com/v1beta/models',
                headers={'x-goog-api-key': secret}, params={'pageSize': 1000, **({'pageToken': page} if page else {})}),
                on_retry=on_retry, should_cancel=should_cancel, on_request=on_request,
                pacing_provider=provider, pacing_endpoint=endpoint)
        elif provider == 'deepseek':
            response = _request(lambda: client.get('https://api.deepseek.com/models', headers={'Authorization': f'Bearer {secret}'}),
                on_retry=on_retry, should_cancel=should_cancel, on_request=on_request,
                pacing_provider=provider, pacing_endpoint=endpoint)
        else:
            response = _request(lambda: client.get(endpoint + '/models', headers={'Authorization': f'Bearer {secret}'},
                                                  follow_redirects=False),
                on_retry=on_retry, should_cancel=should_cancel, on_request=on_request,
                pacing_provider=provider, pacing_endpoint=endpoint)
        try:
            data = _response_json(response)
        finally:
            response.close()
        entries = data.get('models' if provider == 'gemini' else 'data')
        if not isinstance(entries, list) or any(not isinstance(item, dict) for item in entries):
            raise ProviderError('response', 'Danh mục model API không hợp lệ.')
        for item in entries:
            if provider == 'gemini':
                methods = item.get('supportedGenerationMethods', [])
                if not isinstance(methods, list) or any(not isinstance(method, str) for method in methods):
                    raise ProviderError('response', 'Khả năng model trong danh mục API không hợp lệ.')
                if 'generateContent' not in methods:
                    continue
            try:
                name = safe_model(item.get('name' if provider == 'gemini' else 'id', ''), provider)
            except ValueError:
                continue
            # Audio/image-only endpoints are not subtitle translation models.
            if any(part in name for part in ('-tts', '-image', '-live', 'embedding', 'transcribe')):
                continue
            models.append(name)
        page = data.get('nextPageToken') if provider == 'gemini' else None
        if page is not None and (not isinstance(page, str) or len(page) > 8192):
            raise ProviderError('response', 'Phân trang danh mục model API không hợp lệ.')
        if not page:
            break
        if page in pages_seen:
            raise ProviderError('response', 'Danh mục model lặp lại trang; chưa đọc được danh mục đầy đủ.')
        pages_seen.add(page)
    else:
        raise ProviderError('response', 'Danh mục model vượt giới hạn phân trang; chưa đọc được danh mục đầy đủ.')
    return sorted(set(models))


def generate(provider, secret, model, prompt, *, json_mode=False, client=None, probe=False,
             on_retry=None, should_cancel=None, read_timeout_attempts=None, latency_mode=None,
             max_attempts=RETRY_ATTEMPTS, on_request=None, base_url=None, images=None,
             max_output_tokens=None):
    ensure_provider(provider)
    model = safe_model(model, provider)
    ensure_model_provider(provider, model)
    endpoint = router_base_url(base_url) if provider == 'router9' else None
    image_parts = _image_parts(images)
    if image_parts and provider == 'deepseek':
        raise ValueError('Kết nối DeepSeek hiện chỉ gửi văn bản. Chọn model có hỗ trợ ảnh qua 9Router.')
    if max_output_tokens is not None and (type(max_output_tokens) is not int or not 1 <= max_output_tokens <= 65536):
        raise ValueError('Giới hạn token đầu ra phải là số nguyên từ 1 đến 65536.')
    if not secret:
        raise ProviderError('missing', 'Chưa chọn API key đã lưu.')
    if latency_mode not in (None, 'translation'):
        raise ValueError('Chế độ độ trễ API không hợp lệ.')
    _validate_max_attempts(max_attempts)
    if read_timeout_attempts is not None and (type(read_timeout_attempts) is not int or
                                             not 1 <= read_timeout_attempts <= RETRY_ATTEMPTS):
        raise ValueError('Giới hạn thử lại khi chờ phản hồi không hợp lệ.')
    if client is None:
        timeout = httpx.Timeout(60, connect=15, write=30, pool=15) if latency_mode == 'translation' else 120
        options = {'trust_env': False} if provider == 'router9' else {}
        with httpx.Client(timeout=timeout, follow_redirects=False, **options) as owned:
            return generate(provider, secret, model, prompt, json_mode=json_mode, client=owned, probe=probe,
                            on_retry=on_retry, should_cancel=should_cancel,
                            read_timeout_attempts=read_timeout_attempts, latency_mode=latency_mode,
                            max_attempts=max_attempts, on_request=on_request, base_url=endpoint,
                            images=image_parts, max_output_tokens=max_output_tokens)
    probe_success_hooks = []
    if provider == 'gemini':
        # Gemini 3.x documentation recommends provider defaults for sampling.
        # Older models keep the established low-variance translation setting.
        config = {} if model.startswith('gemini-3') else {'temperature': .2}
        # Google documents LOW support for these text model families. Keep
        # unknown models and generic/probe calls on their provider defaults.
        low_thinking = ('gemini-3.8-flash', 'gemini-3.7-flash', 'gemini-3.6-flash',
                        'gemini-3.5-flash', 'gemini-3-flash', 'gemini-3.1-pro', 'gemini-3-pro')
        if latency_mode == 'translation' and json_mode and not probe and '-image' not in model and \
                any(model == prefix or model.startswith(prefix + '-') for prefix in low_thinking):
            config['thinkingConfig'] = {'thinkingLevel': 'low'}
        if json_mode:
            config['responseMimeType'] = 'application/json'
        if probe:
            config['maxOutputTokens'] = 256
        elif max_output_tokens is not None:
            config['maxOutputTokens'] = max_output_tokens
        parts = [{'text': prompt}]
        parts.extend({'inline_data': item} for item in image_parts)
        response = _request(lambda: client.post(f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent',
            headers={'x-goog-api-key': secret}, json={'contents': [{'parts': parts}], 'generationConfig': config}),
            on_retry=on_retry, should_cancel=should_cancel, read_timeout_attempts=read_timeout_attempts,
            max_attempts=max_attempts, on_request=on_request,
            pacing_provider=provider, pacing_endpoint=endpoint, quota_probe=probe,
            probe_success_hooks=probe_success_hooks)
    else:
        content = ([{'type': 'text', 'text': prompt}] + [
            {'type': 'image_url', 'image_url': {'url': 'data:' + item['mime_type'] + ';base64,' + item['data']}}
            for item in image_parts]) if image_parts else prompt
        body = {'model': model, 'messages': [{'role': 'user', 'content': content}]}
        if json_mode:
            body['response_format'] = {'type': 'json_object'}
        if probe:
            body['max_tokens'] = 256
        elif max_output_tokens is not None:
            body['max_tokens'] = max_output_tokens
        url = endpoint + '/chat/completions' if provider == 'router9' else 'https://api.deepseek.com/chat/completions'
        options = {'follow_redirects': False} if provider == 'router9' else {}
        response = _request(lambda: client.post(url,
            headers={'Authorization': f'Bearer {secret}'}, json=body, **options), on_retry=on_retry, should_cancel=should_cancel,
            read_timeout_attempts=read_timeout_attempts, max_attempts=max_attempts, on_request=on_request,
            pacing_provider=provider, pacing_endpoint=endpoint, quota_probe=probe,
            probe_success_hooks=probe_success_hooks)
    try:
        data = _response_json(response)
    finally:
        response.close()
    usage = _usage_counts(provider, data)
    if on_request is not None and usage:
        on_request({'event': 'usage', 'usage': usage})
    try:
        if provider == 'gemini':
            feedback = data.get('promptFeedback')
            if isinstance(feedback, dict) and feedback.get('blockReason') not in (None, '', 'BLOCK_REASON_UNSPECIFIED'):
                raise ProviderError('blocked', 'API đã chặn nội dung nguồn; kiểm tra nội dung trước khi thử lại.')
            candidate = data['candidates'][0]
            if not isinstance(candidate, dict):
                raise KeyError('candidate')
            if candidate.get('finishReason') in ('SAFETY', 'RECITATION', 'BLOCKLIST', 'PROHIBITED_CONTENT', 'SPII', 'OTHER'):
                raise ProviderError('blocked', 'API đã chặn đầu ra; kiểm tra nội dung nguồn. Không tự thử lại cùng yêu cầu.')
            if candidate.get('finishReason') == 'MAX_TOKENS':
                raise ProviderError('output', 'API dừng vì giới hạn đầu ra; giảm số câu mỗi lượt dịch hoặc kiểm tra model.',
                                    output_reason='max_tokens')
            parts = candidate['content']['parts']
            if not isinstance(parts, list) or any(not isinstance(v, dict) for v in parts):
                raise KeyError('parts')
            output = ''.join(v.get('text', '') for v in parts if not v.get('thought'))
        else:
            choice = data['choices'][0]
            if choice.get('finish_reason') == 'length':
                raise ProviderError('output', 'API dừng vì giới hạn đầu ra; giảm số câu mỗi lượt dịch hoặc kiểm tra model.',
                                    output_reason='max_tokens')
            if choice.get('finish_reason') == 'content_filter':
                raise ProviderError('blocked', 'API đã chặn đầu ra; kiểm tra nội dung trước khi thử lại.')
            output = choice['message']['content']
            if provider == 'router9' and isinstance(output, list):
                if any(not isinstance(item, dict) or item.get('type') not in ('text', 'output_text') or
                       not isinstance(item.get('text'), str) for item in output):
                    raise KeyError('content')
                output = ''.join(item['text'] for item in output)
        if not isinstance(output, str) or not output.strip():
            raise KeyError('empty')
        if provider == 'router9' and json_mode:
            try:
                parsed = parse_model_json(output)
                if not isinstance(parsed, dict):
                    raise ValueError('object')
            except ValueError:
                raise ProviderError('output', '9Router trả nội dung JSON không hợp lệ; giữ phần đã hoàn tất.') from None
        for commit in probe_success_hooks:
            commit(probe_succeeded=True)
        return output
    except (KeyError, IndexError, TypeError, AttributeError):
        raise ProviderError('output', 'API không trả văn bản dùng được; có thể bị chặn nội dung hoặc hết giới hạn đầu ra.') from None


def check_connection(provider, secret, model='', mode='catalog', client=None, *, on_retry=None, should_cancel=None,
                     base_url=None, on_request=None):
    from execution_admission import AdmissionError
    ensure_provider(provider)
    if mode not in ('catalog', 'generation'):
        raise ValueError('Chế độ kiểm tra không hợp lệ.')
    base = {'checked_at': time.time(), 'scope': mode, 'model': model}
    if provider == 'router9':
        base['base_url'] = router_base_url(base_url)
    requests = []

    def record_request(event):
        if on_request is not None:
            on_request(event)
        phase = event.get('event')
        if phase == 'before_send':
            requests.append({'http_status': None, 'usage': {}})
        elif phase == 'response' and requests:
            requests[-1]['http_status'] = event.get('http_status')
        elif phase == 'usage' and requests:
            usage = event.get('usage') or {}
            requests[-1]['usage'] = {key: value for key, value in usage.items()
                                     if key in ('input_tokens', 'output_tokens', 'total_tokens',
                                                'thinking_tokens', 'cached_input_tokens') and
                                     type(value) is int and 0 <= value <= 2**53 - 1}

    def audited(result):
        token_keys = ('input_tokens', 'output_tokens', 'total_tokens', 'thinking_tokens', 'cached_input_tokens')
        reported = [row['usage'] for row in requests if row['usage']]
        usage = {'scope': mode, 'request_attempts': len(requests),
                 'responses_received': sum(row['http_status'] is not None for row in requests),
                 'http_statuses': [row['http_status'] for row in requests],
                 'usage_reported_requests': len(reported),
                 'usage_unknown_requests': (max(0, len(requests) - len(reported)) if mode == 'generation' else 0),
                 'reported_tokens': {key: sum(row.get(key, 0) for row in reported) for key in token_keys},
                 'cost_verification': 'unverified' if mode == 'generation' else 'not_applicable_to_generation'}
        result['api_usage'] = usage
        if mode == 'generation' and requests:
            statuses = ', '.join(str(row['http_status']) if row['http_status'] is not None else 'chưa có phản hồi'
                                 for row in requests)
            if reported:
                total = usage['reported_tokens']['total_tokens']
                note = f' Ghi nhận phép thử: {len(requests)} request ({statuses}); API báo {total} token; chi phí chưa xác minh.'
            else:
                note = f' Ghi nhận phép thử: {len(requests)} request ({statuses}); API không báo usage token; chi phí chưa xác minh.'
            result['message'] = (result.get('message') or '') + note
        elif mode == 'catalog':
            result['message'] = (result.get('message') or '') + f' Đã ghi nhận {len(requests)} request tải danh mục; không gửi prompt dịch.'
        return result

    try:
        if mode == 'catalog':
            models = list_models(provider, secret, client, on_retry=on_retry, should_cancel=should_cancel,
                                 on_request=record_request, base_url=base.get('base_url'))
            return audited(base | {'ok': True, 'status': 'catalog_ok', 'models': models,
                'message': 'Đọc được danh mục model. Chưa chứng minh quyền tạo nội dung, số dư hay quota dịch.'})
        generate(provider, secret, model, 'Reply with the single word OK.', client=client, probe=True,
                 on_retry=on_retry, should_cancel=should_cancel, max_attempts=1, on_request=record_request,
                 base_url=base.get('base_url'))
        return audited(base | {'ok': True, 'status': 'ready', 'message': 'Model đã trả văn bản cho yêu cầu thử. Trạng thái đúng tại thời điểm kiểm tra.'})
    except ProviderError as error:
        return audited(base | {'ok': False, 'status': error.status, 'message': str(error),
                       'http_status': error.http_status, 'quota_scope': error.quota_scope,
                       'retry_after': error.retry_after})
    except httpx.TimeoutException:
        return audited(base | {'ok': False, 'status': 'timeout', 'message': 'Hết thời gian chờ dịch vụ; chưa thể kết luận key sai.'})
    except httpx.RequestError:
        return audited(base | {'ok': False, 'status': 'network', 'message': 'Không kết nối được dịch vụ. Kiểm tra mạng/proxy; chưa kết luận key sai.'})
    except AdmissionError:
        raise
    except (ValueError, KeyError, TypeError):
        return audited(base | {'ok': False, 'status': 'response', 'message': 'Model hoặc cấu trúc phản hồi không hợp lệ.'})
