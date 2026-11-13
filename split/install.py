#!/usr/bin/env python3
"""Install or administer Traveler's two-server TCP setup over OpenSSH."""

import argparse
import base64
import http.client
import io
import ipaddress
import json
import re
import secrets
import shlex
import socket
import ssl
import subprocess
import sys
import tarfile
import time
from pathlib import Path


SOURCE = Path(__file__).resolve().parent
EDGE_CA = '/var/lib/caddy/.local/share/caddy/pki/authorities/local/root.crt'
PANEL_FILES = (
    'configure.py', 'panel.py', 'panel_state.py', 'panel_traffic.py', 'panel_probe.py', 'panel_diagnostics.py',
    'build-traffic-sing-box.sh', 'traveler-panel.service', 'traveler-panel-traffic.service',
    *(f'panel-ui/{name}' for name in (
        'index.html', 'style.css', 'app.js', 'cover.webp', 'hero.webp',
        'phone.webp', 'laptop.webp', 'tablet.webp',
    )),
)
REMOTE_STAGE = (
    'set -eu; umask 077; stage=$(mktemp -d); '
    'trap \'rm -rf -- "$stage"\' EXIT; cd "$stage"; tar -xzf -; '
)


def ipv4(value):
    try:
        return str(ipaddress.IPv4Address(value))
    except ipaddress.AddressValueError as exc:
        raise argparse.ArgumentTypeError('expected an IPv4 address') from exc


def port_number(value):
    port = int(value)
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError('expected a port between 1 and 65535')
    return port


def domain_name(value):
    value = value.lower().rstrip('.')
    if len(value) > 253 or not re.fullmatch(
        r'(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}', value
    ):
        raise argparse.ArgumentTypeError('expected a DNS hostname without scheme or port')
    return value


def public_key(path):
    line = path.read_text(encoding='utf-8-sig').strip()
    parts = line.split()
    if len(line.splitlines()) != 1 or len(parts) < 2 or parts[0] != 'ssh-ed25519':
        raise ValueError(f'{path}: expected a single public Ed25519 key')
    if not re.fullmatch(r'[A-Za-z0-9+/]{68}', parts[1]):
        raise ValueError(f'{path}: invalid Ed25519 public key')
    return (' '.join(parts[:2]) + '\n').encode('ascii')


def ssh_arguments(options, port):
    arguments = [
        'ssh', '-T', '-p', str(port), '-o', 'BatchMode=yes',
        '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=10',
    ]
    if options.identity:
        arguments += ['-i', str(options.identity), '-o', 'IdentitiesOnly=yes']
    return arguments


def remote(options, host, port, command, payload=None, capture=False):
    return subprocess.run(
        [*ssh_arguments(options, port), f'root@{host}', command],
        input=payload, stdout=subprocess.PIPE if capture else None, check=True,
    ).stdout


def bundle(files, generated, site=False):
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode='w:gz') as archive:
        for name in files:
            path = SOURCE / name
            data = path.read_bytes()
            if path.suffix != '.webp':
                data = data.replace(b'\r\n', b'\n')
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
        for name, data in generated.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
        if site:
            archive.add(SOURCE / 'site', arcname='site')
    return payload.getvalue()


def install_on(options, host, port, action, files, generated, site=False):
    remote(options, host, port, REMOTE_STAGE + shlex.join(['bash', 'install.sh', action]),
           bundle(('install.sh', *files), generated, site))


def settings_json(settings):
    return json.dumps(settings).encode('utf-8')


def fetch_edge_ca(options):
    return remote(
        options, options.edge, options.edge_port,
        f'for i in $(seq 1 15); do test ! -s {EDGE_CA} || exec cat {EDGE_CA}; sleep 2; done; '
        'echo "Caddy has not generated its internal CA" >&2; exit 1',
        capture=True,
    )


def verify_websocket(host, path, ca):
    context = ssl.create_default_context(cadata=ca.decode('ascii') if ca else None)
    for attempt in range(15):
        connection = http.client.HTTPSConnection(host, timeout=5, context=context)
        try:
            connection.request('GET', path, headers={
                'Connection': 'Upgrade',
                'Upgrade': 'websocket',
                'Sec-WebSocket-Version': '13',
                'Sec-WebSocket-Key': base64.b64encode(secrets.token_bytes(16)).decode('ascii'),
            })
            status = connection.getresponse().status
            if status != 101:
                raise OSError(f'WebSocket returned HTTP {status}, expected 101')
            return
        except (OSError, http.client.HTTPException):
            if attempt == 14:
                raise
            time.sleep(2)
        finally:
            connection.close()


def deploy(options):
    if options.origin == options.edge:
        raise ValueError('Specify two distinct IPv4 addresses')
    if options.domain:
        addresses = {info[4][0] for info in socket.getaddrinfo(options.domain, 443)}
        if addresses != {options.edge}:
            raise ValueError(f'{options.domain} must resolve only to {options.edge} (A, no AAAA)')
    credentials = {}
    for name, path in [('desktop.pub', options.panel_key), ('iphone.pub', options.iphone_key)]:
        if path:
            credentials[name] = public_key(path)
    for host, port in [(options.origin, options.origin_port), (options.edge, options.edge_port)]:
        remote(options, host, port, 'test "$(id -u)" = 0')

    print('1/4 Preparing the private origin', flush=True)
    install_on(options, options.origin, options.origin_port, 'origin-init',
               ('configure.py', 'panel_probe.py', 'traveler-origin.service'), {})
    origin_key = remote(options, options.origin, options.origin_port,
                        'cat /var/lib/traveler-origin/.ssh/edge_ed25519.pub', capture=True)
    path = remote(options, options.origin, options.origin_port,
                  'python3 -c \'import json; print(json.load(open("/etc/traveler-origin/config.json"))["inbounds"][0]["transport"]["path"])\'',
                  capture=True).decode('ascii').strip()
    edge_key = remote(options, options.edge, options.edge_port,
                      'cat /etc/ssh/ssh_host_ed25519_key.pub', capture=True)
    host = options.domain or options.edge
    settings = settings_json({
        'public_host': host, 'ws_path': path, 'site': options.site,
        'edge_ip': options.edge, 'edge_port': options.edge_port,
    })

    print('2/4 Preparing the public HTTPS edge', flush=True)
    install_on(options, options.edge, options.edge_port, 'edge-install',
               ('configure.py', 'panel_probe.py', '90-traveler-link.conf'),
               {'settings.json': settings, 'origin.pub': origin_key}, site=options.site)
    ca = None if options.domain else fetch_edge_ca(options)
    known_name = options.edge if options.edge_port == 22 else f'[{options.edge}]:{options.edge_port}'
    credentials['edge.known_hosts'] = known_name.encode('ascii') + b' ' + edge_key
    credentials['settings.json'] = settings
    if options.diagnostic_egress_ip:
        credentials['probe-egress-ip'] = (options.diagnostic_egress_ip + '\n').encode('ascii')
    if ca:
        credentials['edge-ca.crt'] = ca

    print('3/4 Starting the reverse SSH link and the private panel', flush=True)
    panel_files = PANEL_FILES + (
        '60-traveler-panel.conf', '61-traveler-panel-ios.conf',
        'traveler-panel-ios.service', 'traveler-panel-ios.socket',
        'traveler-panel-ios-guard.service', 'traveler-panel-ios-guard.nft',
    )
    install_on(options, options.origin, options.origin_port, 'origin-finish', panel_files, credentials)

    print('4/4 Checking the private panel and public TLS/WebSocket', flush=True)
    remote(options, options.origin, options.origin_port,
           'runuser -u traveler-panel-ios -- curl -fsS --retry 4 --retry-connrefused --max-time 5 -o /dev/null '
           '-H "Host: localhost:8765" http://127.0.0.1:18765/')
    verify_websocket(host, path, ca)
    print(f'HTTPS and WebSocket verified at {host}. Open the panel at http://localhost:8765/.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    install = commands.add_parser('install', help='prepare both VPSs')
    enroll = commands.add_parser('add-key', help='authorize another panel SSH public key')
    panel = commands.add_parser('panel', help='open a local tunnel to the panel')
    upgrade = commands.add_parser('upgrade-panel', help='upgrade the private panel and traffic collector in place')
    for command in (install, enroll, panel, upgrade):
        command.add_argument('--origin', type=ipv4, required=True)
        command.add_argument('--origin-port', type=port_number, default=22)
        command.add_argument('--identity', type=Path, help='path to the private SSH key on this computer')
    install.add_argument('--edge', type=ipv4, required=True)
    install.add_argument('--edge-port', type=port_number, default=22)
    install.add_argument('--domain', type=domain_name)
    for command in (install, upgrade):
        command.add_argument('--diagnostic-egress-ip', type=ipv4,
                             help='optionally verify VPN egress against this expected IPv4')
    install.add_argument('--site', action='store_true', help='serve the bundled Kinolitsa site')
    install.add_argument('--panel-key', type=Path, help='public desktop key (.pub)')
    install.add_argument('--iphone-key', type=Path, help='public iPhone key (.pub)')
    enroll.add_argument('--kind', choices=('desktop', 'iphone'), required=True)
    enroll.add_argument('--public-key', type=Path, required=True)
    panel.add_argument('--admin', action='store_true', help='use the existing root SSH login')
    options = parser.parse_args()
    try:
        if options.command == 'install':
            deploy(options)
        elif options.command == 'upgrade-panel':
            generated = {}
            if options.diagnostic_egress_ip:
                generated['probe-egress-ip'] = (options.diagnostic_egress_ip + '\n').encode('ascii')
            install_on(options, options.origin, options.origin_port, 'upgrade-panel', PANEL_FILES, generated)
        elif options.command == 'add-key':
            install_on(options, options.origin, options.origin_port, f'add-{options.kind}-key', (),
                       {f'{options.kind}.pub': public_key(options.public_key)})
            print('Panel SSH key authorized.')
        else:
            user = 'root' if options.admin else 'traveler-panel-access'
            destination = '/run/traveler-panel/panel.sock' if options.admin else '127.0.0.1:18765'
            print('Open http://localhost:8765/ in your browser. Ctrl+C closes the tunnel.', flush=True)
            subprocess.run([
                *ssh_arguments(options, options.origin_port), '-N', '-o', 'ExitOnForwardFailure=yes',
                '-L', f'127.0.0.1:8765:{destination}', f'{user}@{options.origin}',
            ], check=True)
    except KeyboardInterrupt:
        return 130
    except subprocess.CalledProcessError as exc:
        print(f'SSH or remote command failed (exit {exc.returncode}). See the output above.', file=sys.stderr)
        return 1
    except (OSError, ValueError, http.client.HTTPException) as exc:
        print(f'Traveler: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
