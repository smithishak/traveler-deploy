"""Persistent panel metadata and recoverable sing-box configuration changes."""

import hashlib
import json
import os
import secrets
import tempfile
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

if __package__:
    from .panel_probe import is_probe
else:
    from panel_probe import is_probe


LEGACY_NAME = 'traveler'
DEVICE_KINDS = {'phone', 'laptop', 'tablet', 'other'}
OWNER_ID = 'owner'
STATE_VERSION = 1
TELEMETRY_MAX_AGE = 180


class ActionError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8'))


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8')


def trojan(config):
    return next(inbound for inbound in config['inbounds'] if inbound['type'] == 'trojan')


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def sync_directory(directory):
    if os.name == 'posix':
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def atomic_write(path, data, owner=None):
    """Write a private file and make its rename durable."""
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.write-', delete=False) as stream:
        temporary = Path(stream.name)
        try:
            if owner is not None and os.name == 'posix':
                os.fchown(stream.fileno(), owner.st_uid, owner.st_gid)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            stream.close()
            temporary.replace(path)
            sync_directory(path.parent)
        finally:
            temporary.unlink(missing_ok=True)


def fresh_state():
    return {'version': STATE_VERSION, 'users': [], 'settings': {'intro': True}}


def make_device(device_id, name, kind, username, created_at):
    return {
        'id': device_id, 'name': name, 'kind': kind,
        'username': username, 'created_at': created_at,
    }


def reconcile(state, credentials):
    """Import pre-existing credentials without renaming or copying passwords."""
    credentials = [credential for credential in credentials if not is_probe(credential)]
    available = {credential['name'] for credential in credentials}
    assigned = set()
    for user in state['users']:
        user['devices'] = [device for device in user['devices'] if device['username'] in available]
        assigned.update(device['username'] for device in user['devices'])
    unassigned = [credential['name'] for credential in credentials
                  if credential['name'] not in assigned]
    if not unassigned:
        return
    owner = next((user for user in state['users'] if user['id'] == OWNER_ID), None)
    if owner is None:
        owner = {'id': OWNER_ID, 'name': 'Владелец', 'created_at': timestamp(), 'devices': []}
        state['users'].insert(0, owner)
    for username in unassigned:
        owner['devices'].append(make_device(secrets.token_hex(16), username, 'other', username, None))


def read_telemetry(path):
    try:
        report = read_json(path)
        updated_at = datetime.fromisoformat(report['updated_at'].replace('Z', '+00:00'))
        age = (datetime.now(timezone.utc) - updated_at).total_seconds()
        users = report['users']
        if age >= -30 and isinstance(users, dict):
            return {
                'updated_at': updated_at.astimezone(timezone.utc).isoformat(),
                'fresh': age <= TELEMETRY_MAX_AGE,
                'users': users,
            }
    except (OSError, ValueError, AttributeError, KeyError, TypeError, OverflowError):
        pass
    return None


def seen_at(value):
    if not isinstance(value, str):
        return None
    try:
        date = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if date.tzinfo is not None:
            return date.astimezone(timezone.utc).isoformat(timespec='seconds')
    except (ValueError, OverflowError):
        pass
    return None


def public_state(state, telemetry):
    users = []
    metrics = telemetry['users'] if telemetry is not None else {}
    for user in state['users']:
        devices = []
        for device in user['devices']:
            username = device['username']
            stats = metrics.get(username)
            stats = stats if isinstance(stats, dict) else {}
            counters = {key: stats.get(key) for key in ('upload', 'download')}
            for key, value in counters.items():
                if type(value) is not int or value < 0:
                    counters[key] = None
            devices.append({
                'id': device['id'], 'name': device['name'], 'kind': device['kind'],
                'created_at': device['created_at'], 'legacy': username == LEGACY_NAME,
                'last_seen': seen_at(stats.get('last_seen')), **counters,
            })
        users.append({
            'id': user['id'], 'name': user['name'], 'created_at': user['created_at'],
            'devices': devices,
        })
    return {
        'users': users, 'settings': state['settings'],
        'telemetry': telemetry['fresh'] if telemetry is not None else False,
        'telemetry_updated_at': telemetry['updated_at'] if telemetry is not None else None,
    }


def require_name(fields, entries):
    name = fields.get('name')
    if not isinstance(name, str):
        raise ActionError('Введите название.')
    name = name.strip()
    if not 1 <= len(name) <= 60 or any(unicodedata.category(char) in ('Cc', 'Cs') for char in name):
        raise ActionError('Введите название длиной от 1 до 60 символов без управляющих знаков.')
    if any(entry['name'].casefold() == name.casefold() for entry in entries):
        raise ActionError('Такое название уже используется.', 409)
    return name


def find_entry(entries, entry_id, message):
    for entry in entries:
        if entry['id'] == entry_id:
            return entry
    raise ActionError(message, 404)


def protect_legacy(devices):
    if any(device['username'] == LEGACY_NAME for device in devices):
        raise ActionError('Исходный профиль защищён от отзыва.', 409)


def mutate(state, config, fields):
    action = fields.get('action')
    users = state['users']
    if action == 'settings':
        if type(fields.get('intro')) is not bool:
            raise ActionError('Укажите, показывать ли вступительный экран.')
        state['settings']['intro'] = fields['intro']
        return
    if action == 'create_user':
        name = require_name(fields, users)
        users.append({'id': secrets.token_hex(16), 'name': name,
                      'created_at': timestamp(), 'devices': []})
        return
    if action not in ('create_device', 'rotate_device', 'delete_device', 'delete_user'):
        raise ActionError('Неизвестное действие.')
    user = find_entry(users, fields.get('user_id'), 'Пользователь не найден.')
    inbound = trojan(config)
    if action == 'rotate_device':
        device = find_entry(user['devices'], fields.get('device_id'), 'Устройство не найдено.')
        username = device['username']
        if username == LEGACY_NAME:
            raise ActionError('Исходный профиль защищён от перевыпуска.', 409)
        credential = next(item for item in inbound['users'] if item['name'] == username)
        credential['password'] = secrets.token_urlsafe(32)
        return device['id']
    issued_device_id = None
    if action == 'create_device':
        name = require_name(fields, user['devices'])
        kind = fields.get('kind')
        if not isinstance(kind, str) or kind not in DEVICE_KINDS:
            raise ActionError('Выберите тип устройства.')
        device_id = secrets.token_hex(16)
        username = 'device-' + device_id
        user['devices'].append(make_device(device_id, name, kind, username, timestamp()))
        inbound['users'].append({'name': username, 'password': secrets.token_urlsafe(32)})
        issued_device_id = device_id
    else:
        deleting_user = action == 'delete_user'
        devices = user['devices'] if deleting_user else [
            find_entry(user['devices'], fields.get('device_id'), 'Устройство не найдено.')
        ]
        protect_legacy(devices)
        revoked = {device['username'] for device in devices}
        inbound['users'] = [credential for credential in inbound['users']
                            if credential['name'] not in revoked]
        if deleting_user:
            users.remove(user)
        else:
            user['devices'].remove(devices[0])
    stats = config.get('experimental', {}).get('v2ray_api', {}).get('stats')
    if stats is not None:
        stats['users'] = [credential['name'] for credential in inbound['users']
                          if not is_probe(credential)]
    return issued_device_id


class StateStore:
    def __init__(self, directory, config, apply_config, restart_origin):
        self.directory = directory
        self.config = config
        self.state_path = directory / 'state.json'
        self.pending_path = directory / 'pending.json'
        self.apply_config = apply_config
        self.restart_origin = restart_origin

    def save(self, state):
        atomic_write(self.state_path, json_bytes(state))

    def clear_pending(self):
        self.pending_path.unlink(missing_ok=True)
        sync_directory(self.directory)

    def recover(self, original):
        try:
            pending = read_json(self.pending_path)
        except FileNotFoundError:
            return
        current = digest(original)
        if current == pending['after']:
            # The process may have stopped after the config rename, before the service restart.
            self.restart_origin(json.loads(original))
            self.save(pending['state'])
        elif current != pending['before']:
            raise ValueError('Pending operation does not match the current origin config')
        self.clear_pending()

    def load(self):
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        original = self.config.read_bytes()
        self.recover(original)
        config = json.loads(original)
        try:
            state = read_json(self.state_path)
        except FileNotFoundError:
            state = fresh_state()
        if state.get('version') != STATE_VERSION:
            raise ValueError('Unsupported panel state version')
        before = json_bytes(state)
        reconcile(state, trojan(config)['users'])
        if json_bytes(state) != before or not self.state_path.exists():
            self.save(state)
        return original, config, state

    def action(self, fields):
        original, config, state = self.load()
        unchanged = json_bytes(config)
        issued_device_id = mutate(state, config, fields)
        replacement = json_bytes(config)
        if replacement == unchanged:
            self.save(state)
            return state, issued_device_id
        pending = {'before': digest(original), 'after': digest(replacement), 'state': state}
        atomic_write(self.pending_path, json_bytes(pending))
        try:
            self.apply_config(config, original)
        except Exception:
            if self.config.read_bytes() == original:
                self.clear_pending()
            raise
        self.save(state)
        self.clear_pending()
        return state, issued_device_id
