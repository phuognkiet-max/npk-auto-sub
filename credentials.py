"""Expose proof of local credential storage, never the full secret."""
import hashlib
import hmac
import time
import uuid
import re
import keyring
from core import get, put

PROVIDERS = ('gemini', 'deepseek', 'router9')

def secret_name(provider, key_id='default'):
    if provider not in PROVIDERS or not isinstance(key_id,str) or (key_id != 'default' and not re.fullmatch(r'[a-f0-9]{32}', key_id)):
        raise ValueError('Mã key hoặc nhà cung cấp không hợp lệ.')
    return provider if key_id == 'default' else provider + ':' + key_id


def active_key_id(provider):
    return (get('credential_slots', provider) or {}).get('active', 'default')


def active_secret(provider):
    identifier = active_key_id(provider)
    return None if identifier == 'none' else keyring.get_password('VietDubStudio', secret_name(provider, identifier))


def credential_status(provider, key_id=None):
    if provider not in PROVIDERS:
        raise ValueError('Nhà cung cấp không hợp lệ.')
    key_id = key_id or active_key_id(provider)
    name = secret_name(provider, key_id) if key_id != 'none' else None
    secret = keyring.get_password('VietDubStudio', name) if name else None
    if not secret:
        return {'present': False, 'masked': None, 'last4': None, 'read_back': False,
                'saved_at': None, 'provider_verified': False}
    metadata = get('credential_metadata', name) or {}
    fingerprint = hashlib.sha256(secret.encode()).hexdigest()
    check = metadata.get('check') if metadata.get('fingerprint') == fingerprint else None
    return {'present': True, 'masked': '•••• •••• ' + secret[-4:], 'last4': secret[-4:],
            'read_back': True, 'saved_at': metadata.get('saved_at') if metadata.get('fingerprint') == fingerprint else None,
            'provider_verified': bool(check and check.get('ok') and check.get('scope') == 'generation'), 'check': check}

def store_credential(provider, secret):
    if provider not in PROVIDERS:
        raise ValueError('Nhà cung cấp không hợp lệ.')
    secret = str(secret).strip()
    if secret:
        if not 8 <= len(secret) <= 512:
            raise ValueError('API key cần từ 8 đến 512 ký tự.')
        if any(c.isspace() for c in secret):
            raise ValueError('Key chứa khoảng trắng hoặc xuống dòng. Kiểm tra lại nội dung đã dán.')
        keyring.set_password('VietDubStudio', provider, secret)
        stored = keyring.get_password('VietDubStudio', provider)
        if stored is None or not hmac.compare_digest(stored, secret):
            raise RuntimeError('Chưa xác nhận đọc lại được key sau khi lưu. Không báo thành công.')
        put('credential_metadata', provider, {'saved_at': time.time(), 'fingerprint': hashlib.sha256(secret.encode()).hexdigest()})
    else:
        try:
            keyring.delete_password('VietDubStudio', provider)
        except keyring.errors.PasswordDeleteError:
            pass
        put('credential_metadata', provider, {})
    slots = get('credential_slots', provider) or {'ids': []}
    slots['active'] = 'default'
    put('credential_slots', provider, slots)
    return credential_status(provider)


def credential_list(provider):
    slots = get('credential_slots', provider) or {'ids': [], 'active': 'default'}
    result = []
    for key_id in ['default', *slots.get('ids', [])]:
        status = credential_status(provider, key_id)
        if status['present']:
            result.append(status | {'id': key_id, 'active': key_id == slots.get('active', 'default')})
    return result


def add_credentials(provider, text):
    secret_name(provider)
    secrets = list(dict.fromkeys(s.strip() for s in re.split(r'[,\n\r]+', str(text)) if s.strip()))
    if not secrets or len(secrets) > 20 or any(len(s) < 8 or len(s) > 512 or any(c.isspace() for c in s) for s in secrets):
        raise ValueError('Nhập 1–20 key, cách bằng dấu phẩy/xuống dòng; mỗi key 8–512 ký tự và không chứa khoảng trắng.')
    slots = get('credential_slots', provider) or {'ids': [], 'active': 'default'}
    if len(slots.get('ids', [])) + len(secrets) > 20:
        raise ValueError('Tối đa 20 key bổ sung cho mỗi nhà cung cấp.')
    existing = {hashlib.sha256((keyring.get_password('VietDubStudio', secret_name(provider, k['id'])) or '').encode()).hexdigest() for k in credential_list(provider)}
    for secret in secrets:
        fingerprint = hashlib.sha256(secret.encode()).hexdigest()
        if fingerprint in existing:
            continue
        key_id = uuid.uuid4().hex
        name = secret_name(provider, key_id)
        keyring.set_password('VietDubStudio', name, secret)
        stored = keyring.get_password('VietDubStudio', name)
        if stored is None or not hmac.compare_digest(stored, secret):
            raise RuntimeError('Không đọc lại được key vừa lưu; dừng thêm key.')
        slots.setdefault('ids', []).append(key_id)
        put('credential_metadata', name, {'fingerprint': fingerprint, 'saved_at': time.time()})
        existing.add(fingerprint)
        # Publish one successful addition at a time, so partial failures stay visible.
        if not active_secret(provider):
            slots['active'] = key_id
        put('credential_slots', provider, slots)
    return credential_list(provider)


def select_credential(provider, key_id):
    if not credential_status(provider, key_id)['present']:
        raise ValueError('Key đã chọn không tồn tại.')
    slots = get('credential_slots', provider) or {'ids': []}
    if key_id != 'default' and key_id not in slots.get('ids', []):
        raise ValueError('Key không thuộc danh sách này.')
    slots['active'] = key_id
    put('credential_slots', provider, slots)
    return credential_list(provider)


def delete_credential(provider, key_id):
    name = secret_name(provider, key_id)
    try:
        keyring.delete_password('VietDubStudio', name)
    except keyring.errors.PasswordDeleteError:
        pass
    slots = get('credential_slots', provider) or {'ids': []}
    slots['ids'] = [k for k in slots.get('ids', []) if k != key_id]
    if slots.get('active', 'default') == key_id:
        slots['active'] = 'none'  # Require an explicit choice, not a different paid account.
    put('credential_slots', provider, slots)
    put('credential_metadata', name, {})
    return credential_list(provider)


def record_check(provider, key_id, secret, result):
    name = secret_name(provider, key_id)
    stored = keyring.get_password('VietDubStudio', name)
    if not stored or not hmac.compare_digest(stored, secret):
        return  # A replaced/unsaved key must not inherit another key's check result.
    metadata = get('credential_metadata', name) or {}
    metadata.update(fingerprint=hashlib.sha256(secret.encode()).hexdigest(), check=result)
    put('credential_metadata', name, metadata)
