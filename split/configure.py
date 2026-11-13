"""Generate server configuration from the installer's validated settings."""

import ipaddress
import json
import re
import secrets
import sys
import tempfile
from pathlib import Path
from panel_probe import ensure_probe, is_probe


def origin():
    path = Path('/etc/traveler-origin/config.json')
    if path.exists():
        return
    config = {
        'log': {'level': 'warn'},
        'inbounds': [{
            'type': 'trojan', 'tag': 'private-in',
            'listen': '127.0.0.1', 'listen_port': 10000,
            'users': [{'name': 'traveler', 'password': secrets.token_urlsafe(32)}],
            'transport': {'type': 'ws', 'path': '/connect-' + secrets.token_hex(24)},
        }],
        'outbounds': [{'type': 'direct', 'tag': 'internet'}],
    }
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                     prefix='.config-', delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(config, stream, indent=2)
            stream.close()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def traffic():
    config = json.loads(Path('/etc/traveler-origin/config.json').read_text(encoding='utf-8'))
    inbound = next(item for item in config['inbounds'] if item['type'] == 'trojan')
    ensure_probe(config, inbound)
    experimental = config.setdefault('experimental', {})
    experimental['v2ray_api'] = {
        'listen': '127.0.0.1:19090',
        'stats': {'enabled': True, 'users': [item['name'] for item in inbound['users'] if not is_probe(item)]},
    }
    Path('traffic-config.json').write_text(json.dumps(config, indent=2) + '\n', encoding='utf-8')


def edge(settings):
    host = settings['public_host']
    path = settings['ws_path']
    if not re.fullmatch(r'/connect-[a-f0-9]{48}', path):
        raise ValueError('Invalid WebSocket path')
    try:
        ipaddress.IPv4Address(host)
        ip_mode = True
    except ValueError:
        ip_mode = False
        if not re.fullmatch(r'(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}', host):
            raise ValueError('Expected IPv4 or a DNS hostname')
    tls = '    tls internal\n' if ip_mode else ''
    ip_options = f'    auto_https disable_redirects\n    default_sni {host}\n' if ip_mode else ''
    public_route = (
        '        root * /srv/traveler-edge-site\n        file_server'
        if settings['site'] else '        respond "Traveler" 200'
    )
    Path('Caddyfile').write_text(f'''# Managed by Traveler split installer
{{
{ip_options}    servers {{
        protocols h1 h2
    }}
}}

https://{host} {{
{tls}    handle {path} {{
        reverse_proxy 127.0.0.1:18443
    }}
    handle {{
{public_route}
    }}
}}
''', encoding='utf-8')


def finish(settings):
    host = settings['public_host']
    tls = {'enabled': True, 'server_name': host, 'insecure': False, 'alpn': ['http/1.1']}
    if Path('edge-ca.crt').is_file():
        tls['certificate'] = Path('edge-ca.crt').read_text(encoding='utf-8')
    template = {
        'log': {'level': 'warn'},
        'inbounds': [{
            'type': 'tun', 'tag': 'tun-in', 'interface_name': 'traveler-tun',
            'address': ['172.19.0.1/30', 'fdfe:dcba:9876::1/126'],
            'auto_route': True, 'strict_route': True, 'stack': 'mixed', 'mtu': 1280,
        }],
        'outbounds': [{
            # Connect by IP to avoid resolving the ingress through its own tunnel.
            'type': 'trojan', 'tag': 'edge', 'server': str(ipaddress.IPv4Address(settings['edge_ip'])),
            'server_port': 443, 'tls': tls,
            'transport': {'type': 'ws', 'headers': {'Host': host}},
        }],
        'dns': {
            'servers': [{'type': 'tcp', 'tag': 'remote', 'server': '1.1.1.1',
                         'server_port': 53, 'detour': 'edge'}],
            'final': 'remote', 'strategy': 'ipv4_only',
        },
        'route': {
            'final': 'edge', 'auto_detect_interface': True, 'default_domain_resolver': 'remote',
            'rules': [{'port': 53, 'action': 'hijack-dns'}],
        },
    }
    Path('profile.json').write_text(json.dumps(template, indent=2) + '\n', encoding='utf-8')
    address = ipaddress.IPv4Address(settings['edge_ip'])
    port = int(settings['edge_port'])
    if not 1 <= port <= 65535:
        raise ValueError('Invalid SSH port')
    Path('traveler-edge-link.service').write_text(f'''[Unit]
Description=Traveler reverse SSH link to HTTPS ingress
After=network-online.target traveler-origin.service
Wants=network-online.target traveler-origin.service

[Service]
User=traveler-origin
ExecStart=/usr/bin/ssh -NT -p {port} -o BatchMode=yes -o IdentitiesOnly=yes -o ExitOnForwardFailure=yes -o StrictHostKeyChecking=yes -o UserKnownHostsFile=/var/lib/traveler-origin/.ssh/known_hosts -o ServerAliveInterval=20 -o ServerAliveCountMax=3 -o ConnectTimeout=10 -i /var/lib/traveler-origin/.ssh/edge_ed25519 -R 127.0.0.1:18443:127.0.0.1:10000 traveler-link@{address}
Restart=always
RestartSec=5
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes

[Install]
WantedBy=multi-user.target
''', encoding='utf-8')


if __name__ == '__main__':
    action = sys.argv[1]
    if action == 'origin':
        origin()
    elif action == 'traffic':
        traffic()
    else:
        settings = json.loads(Path('settings.json').read_text(encoding='utf-8'))
        {'edge': edge, 'finish': finish}[action](settings)
