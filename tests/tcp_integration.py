"""Live opt-in test: TCP-only isolated TUN client + rollback with fallback enabled."""
import json
from pathlib import Path
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import traveler as app
import tcp_fallback
from integration import identity, cli


def isolated_client():
    host, _ = app.env_values()
    name = 'traveler-tcp-test-' + uuid.uuid4().hex[:8]
    profile = app.STATE / 'exports' / 'traveler-tcp-tun.json'
    image = app.info('tcp-fallback')['Image']
    try:
        app.run('docker', 'run', '-d', '--name', name, '--cap-add', 'NET_ADMIN',
                '--device', '/dev/net/tun', '-v', f'{profile}:/client.json:ro',
                '--entrypoint', '/bin/sh', image, '-c', 'sleep 600')
        app.run('docker', 'exec', name, 'apk', 'add', '--no-cache', 'iptables', 'curl', timeout=180)
        app.run('docker', 'exec', name, 'sh', '-c', 'echo nameserver 1.1.1.1 > /etc/resolv.conf')
        app.run('docker', 'exec', name, 'iptables', '-A', 'OUTPUT', '-o', 'eth0',
                '-p', 'tcp', '-d', host, '--dport', '443', '-j', 'ACCEPT')
        app.run('docker', 'exec', name, 'iptables', '-A', 'OUTPUT', '-o', 'eth0', '-j', 'REJECT')
        app.run('docker', 'exec', name, 'ip6tables', '-A', 'OUTPUT', '-o', 'eth0', '-j', 'REJECT')
        direct = app.run('docker', 'exec', name, 'curl', '-kfsS', '--max-time', '4',
                         'https://1.1.1.1', check=False)
        assert direct.returncode != 0, 'Direct internet unexpectedly accessible'
        print('PASS isolated client allows only TCP/443 to VPN server; direct internet rejected', flush=True)
        app.run('docker', 'exec', '-d', name, 'sing-box', 'run', '-c', '/client.json')
        # Curl retries while TUN routes initialize. Names resolved through tunneled TCP DNS.
        tunneled = app.run('docker', 'exec', name, 'curl', '-4fsS', '--retry', '5',
                           '--retry-all-errors', '--retry-delay', '1', '--max-time', '15',
                           'https://api.ipify.org', timeout=120)
        assert tunneled.stdout.strip() == host, 'TUN egress IP does not match VPN server'
        app.run('docker', 'exec', name, 'nslookup', 'example.com', '1.1.1.1', timeout=20)
        print('PASS real TUN HTTPS, DNS and egress with ALL external UDP blocked', flush=True)
    finally:
        app.run('docker', 'rm', '-f', name, check=False)


def main():
    if sys.argv[1:] == ['--client-only']:
        isolated_client()
        return
    assert sys.argv[1:] == ['--allow-vpn-interruption'], 'Explicit test opt-in required'
    assert app.TCP_COMPOSE.exists(), 'Enable TCP fallback first'
    before = identity()
    credentials = app.digest(tcp_fallback.directory() / 'server.json')
    cli('tcp-enable')
    assert app.digest(tcp_fallback.directory() / 'server.json') == credentials
    cli('tcp-export', '--mode', 'tun')
    cli('tcp-export', '--mode', 'proxy')
    isolated_client()
    images = {name: app.info(name)['Image'] for name in app.active_services()}
    cli('update', '--tcp-image', 'caddy:2-alpine', '--timeout', '15', expected=1)
    assert {name: app.info(name)['Image'] for name in app.active_services()} == images
    assert app.digest(tcp_fallback.directory() / 'server.json') == credentials
    assert identity() == before, 'Existing clients/accounts/hooks changed'
    cli('doctor')
    print('PASS failed fallback update restores all three services and original credentials', flush=True)
    isolated_client()
    print('ALL TCP LIVE TESTS PASSED', flush=True)


if __name__ == '__main__':
    main()
