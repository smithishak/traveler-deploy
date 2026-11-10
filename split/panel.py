import base64
import gzip
import html
import grp
import json
import logging
import os
import re
import secrets
import socket
import struct
import subprocess
import tempfile
import time
import zlib
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urlsplit

import qrcode


CONFIG = Path('/etc/traveler-origin/config.json')
TEMPLATE = Path('/etc/traveler-panel/profile.json')
SOCKET = Path('/run/traveler-panel/panel.sock')
PANEL_HOSTS = {'127.0.0.1:8765', 'localhost:8765', 'traveler-panel:18765'}
TOKEN = secrets.token_hex(32)
LEGACY_NAME = 'traveler'
UNIT = 'traveler-origin.service'


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8'))


def trojan(config):
    return next(inbound for inbound in config['inbounds'] if inbound['type'] == 'trojan')


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


def qr_page(name, profile):
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
<main><a href="/">← К профилям</a>
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
        backup = Path(directory) / 'previous.json'
        candidate.write_text(json.dumps(config, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        backup.write_bytes(original)
        for path in (candidate, backup):
            os.chown(path, metadata.st_uid, metadata.st_gid)
            path.chmod(0o600)
        subprocess.run(
            ['/usr/local/bin/traveler-sing-box', 'check', '-c', str(candidate)],
            check=True, timeout=10, capture_output=True,
        )
        candidate.replace(CONFIG)
        try:
            restart_origin(config)
        except (OSError, subprocess.SubprocessError):
            backup.replace(CONFIG)
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


def page(users):
    rows = []
    for user in users:
        name = user['name']
        safe_name = html.escape(name)
        download = html.escape(urlencode({'device': name}))
        revoke = 'Исходный профиль' if name == LEGACY_NAME else f'''
            <details><summary>Отозвать</summary>
            <form method="post" action="/revoke">
              <input type="hidden" name="token" value="{TOKEN}">
              <input type="hidden" name="device" value="{safe_name}">
              <button class="danger">Подтвердить отзыв</button>
            </form></details>'''
        rows.append(f'''<li><strong>{safe_name}</strong><div class="actions">
            <a href="/download?{download}">Скачать JSON</a>
            <a href="/qr?{download}">QR для iPhone</a>{revoke}</div></li>''')
    return f'''<!doctype html>
<html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Traveler · Профили устройств</title>
<style>
  :root {{color-scheme:dark; font:16px/1.5 system-ui; background:#10151d; color:#edf4ff}}
  body {{margin:0; padding:clamp(20px,6vw,72px) 20px}}
  main {{max-width:720px; margin:auto}}
  h1 {{font-size:clamp(2rem,6vw,3.5rem); line-height:1.05; margin:10px 0}}
  p {{color:#b9c7d8}} small {{color:#89e3c4; letter-spacing:.1em}}
  section {{background:#1a2330; border:1px solid #324256; border-radius:16px; padding:20px; margin:24px 0}}
  h2 {{margin-top:0}} form,.actions {{display:flex; flex-wrap:wrap; align-items:center; gap:12px}}
  input,button,a {{font:inherit; padding:8px 12px; border-radius:8px}}
  input {{border:1px solid #60748c; background:#10151d; color:inherit; min-width:0; flex:1}}
  button {{border:0; background:#89e3c4; color:#10231e; cursor:pointer; font-weight:650}}
  a {{color:#a2efd5; border:1px solid #60748c; text-decoration:none}}
  ul {{padding:0; margin:0; list-style:none}}
  li {{display:flex; flex-wrap:wrap; align-items:center; justify-content:space-between; gap:12px;
       border-top:1px solid #324256; padding:12px 0; overflow-wrap:anywhere}}
  summary {{cursor:pointer; color:#ffbdbd}} details form {{margin-top:8px}}
  .danger {{background:#ffbdbd; color:#351919}}
</style>
<main><small>TRAVELER · ДОСТУП ЧЕРЕЗ SSH</small>
<h1>Профили устройств</h1>
<p>Создайте отдельный профиль для каждого устройства и импортируйте JSON в клиент sing-box.</p>
<section><h2>Новое устройство</h2>
<form method="post" action="/create">
  <input type="hidden" name="token" value="{TOKEN}">
  <label for="device">Название</label>
  <input id="device" name="device" maxlength="60" required placeholder="Например, телефон">
  <button>Создать профиль</button>
</form>
<p>При создании и отзыве профиля текущие соединения могут кратко прерваться.</p>
</section><section><h2>Устройства</h2><ul>{''.join(rows)}</ul></section></main></html>'''


class PanelHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def reply(self, status, body='', content_type='text/plain; charset=utf-8', **headers):
        data = body.encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'same-origin')
        self.send_header(
            'Content-Security-Policy',
            "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'; "
            "form-action 'self'; base-uri 'none'",
        )
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def valid_host(self):
        hosts = self.headers.get_all('Host', [])
        if (len(hosts) == 1 and hosts[0] in PANEL_HOSTS
                and self.headers.get('Sec-Fetch-Site') != 'cross-site'):
            return True
        self.reply(403, 'Доступ закрыт.')
        return False

    def do_GET(self):
        if not self.valid_host():
            return
        request = urlsplit(self.path)
        inbound = trojan(read_json(CONFIG))
        if request.path == '/':
            self.reply(200, page(inbound['users']), 'text/html; charset=utf-8')
        elif request.path in ('/download', '/qr'):
            name = parse_qs(request.query).get('device', [''])[0]
            user = next((user for user in inbound['users'] if user['name'] == name), None)
            if user is None:
                self.reply(404, 'Профиль не найден.')
                return
            profile = export_profile(inbound, user)
            if request.path == '/qr':
                try:
                    body = qr_page(name, profile)
                except qrcode.exceptions.DataOverflowError:
                    self.reply(422, 'Профиль слишком большой для QR. Скачайте JSON.')
                    return
                self.reply(200, body, 'text/html; charset=utf-8')
                return
            filename = quote(f'Traveler-{name}.json', safe='')
            self.reply(
                200, json.dumps(profile, indent=2) + '\n', 'application/json',
                **{'Content-Disposition': f"attachment; filename=\"Traveler.json\"; filename*=UTF-8''{filename}"},
            )
        else:
            self.reply(404, 'Страница не найдена.')

    def do_POST(self):
        if not self.valid_host():
            return
        if self.path not in ('/create', '/revoke'):
            self.reply(404, 'Страница не найдена.')
            return
        if self.headers.get_all('Origin') != ['http://' + self.headers['Host']]:
            self.reply(403, 'Доступ закрыт.')
            return
        if self.headers.get_content_type() != 'application/x-www-form-urlencoded':
            self.reply(415, 'Неверный формат запроса.')
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 2048:
                raise ValueError
            fields = parse_qs(self.rfile.read(length).decode('ascii'), max_num_fields=4)
        except ValueError:
            self.reply(400, 'Неверный формат запроса.')
            return
        token = fields.get('token', [''])[0]
        if not secrets.compare_digest(token.encode('utf-8'), TOKEN.encode('ascii')):
            self.reply(403, 'Откройте панель заново и повторите действие.')
            return
        name = fields.get('device', [''])[0].strip()
        if not re.fullmatch(r'[\w .()\-]{1,60}', name):
            self.reply(400, 'В названии можно использовать буквы, цифры, пробелы, точки, скобки и дефисы.')
            return
        original = CONFIG.read_bytes()
        config = json.loads(original)
        inbound = trojan(config)
        users = inbound['users']
        exists = any(user['name'] == name for user in users)
        if self.path == '/create':
            if exists:
                self.reply(409, 'Профиль с таким названием уже есть.')
                return
            users.append({'name': name, 'password': secrets.token_urlsafe(32)})
        else:
            if name == LEGACY_NAME or not exists:
                self.reply(400, 'Этот профиль нельзя отозвать.')
                return
            inbound['users'] = [user for user in users if user['name'] != name]
        try:
            apply_config(config, original)
        except (OSError, subprocess.SubprocessError) as error:
            logging.error('Failed to apply profile change (%s)', type(error).__name__)
            self.reply(503, 'Изменение не применилось. Проверьте состояние Traveler.')
            return
        self.reply(303, Location='/')


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
    SOCKET.unlink(missing_ok=True)
    with PanelServer(str(SOCKET), PanelHandler) as server:
        server.serve_forever()
