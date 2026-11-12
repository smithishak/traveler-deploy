import base64
import gzip
import html
import grp
import json
import logging
import os
import secrets
import socket
import struct
import subprocess
import tempfile
import time
import zlib
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

import qrcode

from panel_state import (
    ActionError, StateStore, atomic_write, json_bytes, public_state, read_json, read_telemetry, trojan,
)


CONFIG = Path('/etc/traveler-origin/config.json')
TEMPLATE = Path('/etc/traveler-panel/profile.json')
SOCKET = Path('/run/traveler-panel/panel.sock')
STATE = Path('/var/lib/traveler-panel')
TRAFFIC = Path('/var/lib/traveler-panel-traffic/stats.json')
UI = Path('/opt/traveler-panel/panel-ui')
UI_ASSETS = {
    'style.css': 'text/css; charset=utf-8',
    'app.js': 'text/javascript; charset=utf-8',
    **{f'{name}.webp': 'image/webp' for name in ('cover', 'hero', 'phone', 'laptop', 'tablet')},
}
PANEL_HOSTS = {'127.0.0.1:8765', 'localhost:8765', 'traveler-panel:18765'}
TOKEN = secrets.token_hex(32)
UNIT = 'traveler-origin.service'


def export_profile(inbound, user):
    profile = read_json(TEMPLATE)
    outbound = profile['outbounds'][0]
    outbound['password'] = user['password']
    outbound['transport']['path'] = inbound['transport']['path']
    return profile


def length_prefixed_string(value):
    data = value.encode('utf-8')
    length = len(data)
    prefix = bytearray()
    while length >= 128:
        prefix.append((length & 127) | 128)
        length >>= 7
    prefix.append(length)
    return bytes(prefix) + data


def apple_profile_qrs(name, profile):
    # sing-box ProfileContent v1: message type 3, gzip body, local profile type 0.
    config = json.dumps(profile, separators=(',', ':'), ensure_ascii=False)
    body = length_prefixed_string(name) + struct.pack('>i', 0) + length_prefixed_string(config)
    content = b'\x03\x01' + gzip.compress(body, mtime=0)
    metadata = b'{"contentType":"application/octet-stream"}'
    wrapped = struct.pack('>I', len(metadata)) + metadata + struct.pack('>I', len(content)) + content
    compressed = zlib.compress(wrapped)
    # QRS with one source block, index 0. The checksum includes the block count.
    header = struct.pack('<5I', 1, 0, 1, len(compressed), zlib.crc32(wrapped) ^ 1)
    return base64.b64encode(header + compressed).decode('ascii')


def qr_page(name, profile, owner_id):
    code = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_L)
    code.add_data(apple_profile_qrs(name, profile))
    code.make(fit=True)
    matrix = code.get_matrix()
    size = len(matrix)
    modules = ''.join(
        f'M{x} {y}h1v1h-1z'
        for y, row in enumerate(matrix)
        for x, dark in enumerate(row)
        if dark
    )
    return f'''<!doctype html>
<html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Traveler · QR для iPhone</title>
<style>
  :root {{color-scheme:dark; font:16px/1.5 system-ui; background:#10151d; color:#edf4ff}}
  main {{max-width:580px; margin:36px auto; padding:0 20px}}
  a {{color:#a2efd5}} svg {{display:block; width:100%; height:auto; background:white}}
</style>
<main><a href="/?user={quote(owner_id, safe='')}">← К устройствам пользователя</a>
<h1>QR для iPhone</h1><p>{html.escape(name)}</p>
<p>В sing-box откройте Profiles → ＋ → Scan QR Code и наведите камеру на экран.</p>
<svg viewBox="0 0 {size} {size}" role="img" aria-label="QR для импорта профиля"
     shape-rendering="crispEdges"><path fill="black" d="{modules}"/></svg>
<p>В QR находится пароль устройства. Храните его так же, как JSON-профиль.</p>
</main></html>'''


def apply_config(config, original):
    metadata = CONFIG.stat()
    with tempfile.TemporaryDirectory(prefix='.panel-', dir=CONFIG.parent) as directory:
        candidate = Path(directory) / 'config.json'
        replacement = json_bytes(config)
        atomic_write(candidate, replacement)
        subprocess.run(
            ['/usr/local/bin/traveler-sing-box', 'check', '-c', str(candidate)],
            check=True, timeout=10, capture_output=True,
        )
        if CONFIG.read_bytes() != original:
            raise ValueError('Origin config changed while validating')
        try:
            atomic_write(CONFIG, replacement, owner=metadata)
            restart_origin(config)
        except (OSError, subprocess.SubprocessError):
            if CONFIG.read_bytes() == replacement:
                atomic_write(CONFIG, original, owner=metadata)
                restart_origin(json.loads(original))
            raise


def restart_origin(config):
    subprocess.run(['systemctl', 'restart', UNIT], check=True, timeout=25, capture_output=True)
    inbound = trojan(config)
    address = (inbound['listen'], inbound['listen_port'])
    deadline = time.monotonic() + 5
    while True:
        try:
            with socket.create_connection(address, timeout=0.5):
                return
        except OSError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.15)


def store():
    return StateStore(STATE, CONFIG, apply_config, restart_origin)


def api_state(state):
    return {'csrf': TOKEN, **public_state(state, read_telemetry(TRAFFIC))}


def find_profile(state, inbound, device_id, allow_legacy_name):
    devices = [(user['id'], device) for user in state['users'] for device in user['devices']]
    found = next((item for item in devices if item[1]['id'] == device_id), None)
    if found is None and allow_legacy_name:
        found = next((item for item in devices if item[1]['username'] == device_id), None)
    if found is None:
        raise ActionError('Профиль не найден.', 404)
    owner_id, device = found
    credential = next(user for user in inbound['users'] if user['name'] == device['username'])
    return owner_id, device['name'], export_profile(inbound, credential)


class PanelHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def reply(self, status, body='', content_type='text/plain; charset=utf-8', **headers):
        data = body.encode('utf-8') if isinstance(body, str) else body
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'same-origin')
        self.send_header(
            'Content-Security-Policy',
            "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; font-src 'self'; connect-src 'self'; frame-ancestors 'none'; "
            "form-action 'self'; base-uri 'none'",
        )
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def json_reply(self, status, body):
        self.reply(status, json_bytes(body), 'application/json; charset=utf-8')

    def error(self, status, message):
        if self.path.startswith('/api/'):
            self.json_reply(status, {'error': message})
        else:
            self.reply(status, message)

    def valid_host(self):
        hosts = self.headers.get_all('Host', [])
        if (len(hosts) == 1 and hosts[0] in PANEL_HOSTS
                and self.headers.get('Sec-Fetch-Site') != 'cross-site'):
            return True
        self.error(403, 'Доступ закрыт.')
        return False

    def do_GET(self):
        if not self.valid_host():
            return
        try:
            self.get_route()
        except ActionError as error:
            self.error(error.status, str(error))
        except (OSError, ValueError, KeyError, TypeError, StopIteration, subprocess.SubprocessError) as error:
            logging.error('Failed to read panel (%s)', type(error).__name__)
            self.error(503, 'Панель временно недоступна. Проверьте состояние Traveler.')

    def get_route(self):
        request = urlsplit(self.path)
        if request.path == '/':
            self.reply(200, (UI / 'index.html').read_bytes(), 'text/html; charset=utf-8')
            return
        if request.path.startswith('/panel-ui/'):
            filename = request.path.removeprefix('/panel-ui/')
            if filename in UI_ASSETS:
                self.reply(200, (UI / filename).read_bytes(), UI_ASSETS[filename])
                return
            raise ActionError('Файл не найден.', 404)
        if request.path not in ('/api/state', '/profile', '/download', '/qr'):
            raise ActionError('Страница не найдена.', 404)
        _, config, state = store().load()
        if request.path == '/api/state':
            self.json_reply(200, api_state(state))
            return
        values = parse_qs(request.query).get('device', [])
        if len(values) != 1:
            raise ActionError('Профиль не найден.', 404)
        owner_id, name, profile = find_profile(state, trojan(config), values[0], request.path != '/profile')
        if request.path == '/qr':
            try:
                body = qr_page(name, profile, owner_id)
            except qrcode.exceptions.DataOverflowError:
                self.reply(422, 'Профиль слишком большой для QR. Скачайте JSON.')
                return
            self.reply(200, body, 'text/html; charset=utf-8')
            return
        filename = quote(f'Traveler-{name}.json', safe='')
        self.reply(
            200, json_bytes(profile), 'application/json; charset=utf-8',
            **{'Content-Disposition': f"attachment; filename=\"Traveler.json\"; filename*=UTF-8''{filename}"},
        )

    def do_POST(self):
        if not self.valid_host():
            return
        if self.path != '/api/action':
            self.error(404, 'Страница не найдена.')
            return
        if self.headers.get_all('Origin') != ['http://' + self.headers['Host']]:
            self.error(403, 'Доступ закрыт.')
            return
        if self.headers.get_content_type() != 'application/json':
            self.error(415, 'Неверный формат запроса.')
            return
        try:
            lengths = self.headers.get_all('Content-Length', [])
            if len(lengths) != 1 or self.headers.get_all('Transfer-Encoding'):
                raise ValueError
            length = int(lengths[0])
            if not 0 < length <= 4096:
                raise ValueError
            body = self.rfile.read(length)
            if len(body) != length:
                raise ValueError
            fields = json.loads(body.decode('utf-8'))
            if not isinstance(fields, dict):
                raise ValueError
            token = fields.get('csrf')
            if not isinstance(token, str) or not token.isascii() or not secrets.compare_digest(token, TOKEN):
                self.error(403, 'Откройте панель заново и повторите действие.')
                return
        except (ValueError, OSError, RecursionError):
            self.error(400, 'Неверный формат запроса.')
            return
        try:
            state = store().action(fields)
        except ActionError as error:
            self.error(error.status, str(error))
            return
        except (OSError, ValueError, KeyError, TypeError, StopIteration, subprocess.SubprocessError) as error:
            logging.error('Failed to apply profile change (%s)', type(error).__name__)
            self.error(503, 'Изменение не применилось. Проверьте состояние Traveler.')
            return
        self.json_reply(200, api_state(state))

class PanelServer(HTTPServer):
    address_family = socket.AF_UNIX

    def server_bind(self):
        self.socket.bind(self.server_address)
        group = grp.getgrnam('traveler-panel-access').gr_gid
        os.chown(SOCKET, -1, group)
        SOCKET.chmod(0o660)
        os.chown(SOCKET.parent, -1, group)
        SOCKET.parent.chmod(0o750)

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(5)
        return connection, address


if __name__ == '__main__':
    try:
        store().load()
    except (OSError, ValueError, KeyError, TypeError, StopIteration, subprocess.SubprocessError) as error:
        logging.error('Failed to recover panel state (%s)', type(error).__name__)
    SOCKET.unlink(missing_ok=True)
    with PanelServer(str(SOCKET), PanelHandler) as server:
        server.serve_forever()
