"""Optional Trojan/WebSocket/TLS fallback on the existing Caddy TCP/443 listener."""
import base64
import hashlib
import json
import secrets
import socket
import ssl
import struct

import traveler as app

IMAGE = 'ghcr.io/sagernet/sing-box:v1.14.4'


def directory():
    return app.STATE / 'tcp'


def server_config():
    return json.loads((directory() / 'server.json').read_text())


def authority():
    return app.run('docker', 'exec', 'caddy', 'cat',
                   '/data/caddy/pki/authorities/local/root.crt').stdout


def read_exact(stream, count):
    result = b''
    while len(result) < count:
        chunk = stream.recv(count - len(result))
        if not chunk:
            raise app.Failure('TCP fallback: туннель закрыл соединение.')
        result += chunk
    return result


def frame(payload, opcode=2):
    mask = secrets.token_bytes(4)
    length = len(payload)
    header = bytes([0x80 | opcode, 0x80 | length]) if length < 126 else \
        bytes([0x80 | opcode, 0xfe]) + struct.pack('!H', length)
    return header + mask + bytes(value ^ mask[index % 4] for index, value in enumerate(payload))


def check_server():
    app.run('docker', 'exec', 'tcp-fallback', 'sing-box', 'check',
            '-c', '/etc/sing-box/config.json')
    host, _ = app.env_values()
    inbound = server_config()['inbounds'][0]
    path = inbound['transport']['path']
    context = ssl.create_default_context(cadata=authority())
    key = base64.b64encode(secrets.token_bytes(16)).decode()
    try:
        with socket.create_connection(('127.0.0.1', 443), timeout=4) as raw:
            with context.wrap_socket(raw, server_hostname=host) as stream:
                stream.sendall((f'GET {path} HTTP/1.1\r\nHost: {host}\r\n'
                                'Connection: Upgrade\r\nUpgrade: websocket\r\n'
                                f'Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n').encode())
                response = b''
                while not response.endswith(b'\r\n\r\n') and len(response) < 16384:
                    response += read_exact(stream, 1)
                expected = base64.b64encode(hashlib.sha1(
                    (key + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11').encode()).digest())
                app.require(response.startswith(b'HTTP/1.1 101 ') and expected in response,
                            'TCP fallback: WebSocket не отвечает через TLS/443.')
                # Authenticated Trojan CONNECT to the internal login page: no internet dependency.
                password = inbound['users'][0]['password']
                destination = b'wg-easy'
                request = (hashlib.sha224(password.encode()).hexdigest().encode() + b'\r\n' +
                           b'\x01\x03' + bytes([len(destination)]) + destination +
                           struct.pack('!H', 51821) + b'\r\n' +
                           b'GET /login HTTP/1.1\r\nHost: wg-easy\r\nConnection: close\r\n\r\n')
                stream.sendall(frame(request))
                received = b''
                for _ in range(20):
                    first, second = read_exact(stream, 2)
                    length = second & 0x7f
                    if length == 126:
                        length = struct.unpack('!H', read_exact(stream, 2))[0]
                    elif length == 127:
                        length = struct.unpack('!Q', read_exact(stream, 8))[0]
                    app.require(not second & 0x80 and length < 65536,
                                'TCP fallback: неверный WebSocket frame.')
                    payload = read_exact(stream, length)
                    if first & 0x0f == 9:
                        stream.sendall(frame(payload, opcode=10))
                        continue
                    received += payload
                    if b'\r\n' in received:
                        break
                app.require(received.startswith(b'HTTP/1.1 200 '),
                            'TCP fallback: авторизованный запрос через туннель не прошёл.')
    except (OSError, ssl.SSLError) as exc:
        raise app.Failure('TCP fallback: TLS/WebSocket проверка не прошла.') from exc


def client_config(mode):
    app.require(app.TCP_COMPOSE.exists(), 'Сначала выполните tcp-enable.')
    host, _ = app.env_values()
    inbound = server_config()['inbounds'][0]
    profile = {
        'log': {'level': 'warn'},
        'outbounds': [{
            'type': 'trojan', 'tag': 'tcp-fallback', 'server': socket.gethostbyname(host), 'server_port': 443,
            'password': inbound['users'][0]['password'],
            'tls': {'enabled': True, 'server_name': host, 'insecure': False,
                    'certificate': authority(), 'alpn': ['http/1.1']},
            'transport': {'type': 'ws', 'path': inbound['transport']['path'], 'headers': {'Host': host}}}],
        'route': {'final': 'tcp-fallback'}}
    if mode == 'proxy':
        profile['inbounds'] = [{'type': 'mixed', 'listen': '127.0.0.1', 'listen_port': 2080}]
    else:
        profile['inbounds'] = [{'type': 'tun', 'tag': 'tun-in', 'interface_name': 'traveler-tun',
                                'address': ['172.19.0.1/30', 'fdfe:dcba:9876::1/126'], 'auto_route': True,
                                'strict_route': True, 'stack': 'mixed', 'mtu': 1280}]
        profile['dns'] = {'servers': [{'type': 'tcp', 'tag': 'remote', 'server': '1.1.1.1',
                                       'server_port': 53, 'detour': 'tcp-fallback'}],
                          'final': 'remote', 'strategy': 'ipv4_only'}
        profile['route']['rules'] = [{'port': 53, 'action': 'hijack-dns'}]
        profile['route']['auto_detect_interface'] = True
        profile['route']['default_domain_resolver'] = 'remote'
    return profile


def export(mode):
    target_dir = app.STATE / 'exports'
    target_dir.mkdir(mode=0o700, exist_ok=True)
    target = target_dir / f'traveler-tcp-{mode}.json'
    app.atomic_json(target, client_config(mode))
    print(f'Профиль sing-box ({mode}): {target}')
    print('Файл содержит пароль. Сертификат CA встроен; проверка TLS включена.')
    return target


def enable(args):
    app.require((app.ROOT / '.env').exists(), 'Сначала установите основной VPN.')
    if app.TCP_COMPOSE.exists():
        app.wait_ready(args.timeout)
        print('TCP fallback уже включён; пароль и путь сохраняются.')
        export('tun')
        return
    app.wait_ready(30)
    base_caddy = (app.ROOT / 'Caddyfile').read_text()
    app.require(base_caddy.count('reverse_proxy wg-easy:51821') == 1,
                'Caddyfile изменён: автоматическое добавление маршрута небезопасно.')
    image = args.tcp_image or IMAGE
    app.run('docker', 'pull', image, timeout=600)
    target = app.snapshot()
    app.atomic_json(app.PENDING, {'backup': str(target)})
    try:
        folder = directory()
        folder.mkdir(mode=0o700, exist_ok=True)
        password = secrets.token_urlsafe(32)
        path = '/traveler-' + secrets.token_hex(20)
        app.atomic_json(folder / 'server.json', {
            'log': {'level': 'warn'},
            'inbounds': [{'type': 'trojan', 'tag': 'fallback-in', 'listen': '::',
                          'listen_port': 10000, 'users': [{'name': 'fallback', 'password': password}],
                          'transport': {'type': 'ws', 'path': path}}],
            'outbounds': [{'type': 'direct', 'tag': 'direct'}]})
        generated = base_caddy.replace('reverse_proxy wg-easy:51821',
            f'handle {path} {{\n\t\treverse_proxy tcp-fallback:10000\n\t}}\n'
            '\thandle {\n\t\treverse_proxy wg-easy:51821\n\t}')
        (folder / 'Caddyfile').write_text(generated)
        (folder / 'Caddyfile').chmod(0o600)
        app.atomic_json(app.TCP_COMPOSE, {'services': {
            'tcp-fallback': {
                'image': image, 'pull_policy': 'never', 'container_name': 'tcp-fallback',
                'restart': 'unless-stopped', 'command': ['run', '-c', '/etc/sing-box/config.json'],
                'volumes': [{'type': 'bind', 'source': str(folder / 'server.json'),
                             'target': '/etc/sing-box/config.json', 'read_only': True}],
                'read_only': True, 'cap_drop': ['ALL'],
                'security_opt': ['no-new-privileges:true'], 'networks': ['wg']},
            'caddy': {'volumes': [{'type': 'bind', 'source': str(folder / 'Caddyfile'),
                                   'target': '/etc/caddy/Caddyfile', 'read_only': True}]}}})
        app.compose('up', '-d', '--pull', 'never')
        app.wait_ready(args.timeout)
        app.require(app.doctor() == 0, 'Диагностика после включения TCP не прошла.')
    except (Exception, KeyboardInterrupt) as exc:
        print('Включение TCP не удалось; восстанавливаю исходную установку.', flush=True)
        app.restore(target, 90)
        raise app.Failure('TCP не включён. Исходная установка восстановлена.') from exc
    app.PENDING.unlink()
    print('TCP fallback включён на 443; панель и основной AmneziaWG сохранены.')
    export('tun')
    export('proxy')
