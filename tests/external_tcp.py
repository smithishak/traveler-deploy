"""External TCP client smoke/negative tests. Only binds localhost; no route changes."""
import argparse
import copy
import json
import os
from pathlib import Path
import socket
import subprocess
from contextlib import contextmanager
import time
import uuid


@contextmanager
def scratch(parent):
    path = parent / ('tcp-test-' + uuid.uuid4().hex)
    path.mkdir()
    try:
        yield path
    finally:
        # Only files created by this test, no recursive filesystem deletion.
        for name in ('profile.json', 'client.log'):
            (path / name).unlink(missing_ok=True)
        path.rmdir()


def request(binary, profile, should_work, expected_ip, workdir):
    with scratch(workdir) as tmp:
        config = Path(tmp) / 'profile.json'
        config.write_text(json.dumps(profile))
        checked = subprocess.run([binary, 'check', '-c', str(config)], capture_output=True)
        assert checked.returncode == 0, 'Client config check failed'
        with (Path(tmp) / 'client.log').open('wb') as log:
            process = subprocess.Popen([binary, 'run', '-c', str(config)], stdout=log, stderr=log,
                                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            try:
                for _ in range(50):
                    try:
                        with socket.create_connection(('127.0.0.1', 2080), timeout=0.1):
                            break
                    except OSError:
                        time.sleep(0.1)
                curl = 'curl.exe' if os.name == 'nt' else 'curl'
                result = subprocess.run([curl, '--silent', '--show-error', '--max-time', '20',
                                         '--noproxy', '', '--proxy', 'socks5h://127.0.0.1:2080',
                                         'https://api.ipify.org'], capture_output=True, text=True)
                if should_work:
                    if result.returncode != 0 or result.stdout.strip() != expected_ip:
                        print('curl:', result.returncode, result.stderr[-1000:])
                        log.flush()
                        text = (Path(tmp) / 'client.log').read_text(errors='replace')[-2000:]
                        for outbound in profile['outbounds']:
                            text = text.replace(outbound.get('password', 'unused-secret'), '[redacted]')
                            text = text.replace(outbound.get('transport', {}).get('path', 'unused-path'), '[path]')
                        print(text)
                    assert result.returncode == 0 and result.stdout.strip() == expected_ip, \
                        'Expected VPN server egress through external TCP client'
                else:
                    assert result.returncode != 0, 'Invalid credentials/TLS unexpectedly accepted'
            finally:
                process.terminate()
                process.wait(timeout=10)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--binary', required=True)
    parser.add_argument('--profile', required=True)
    parser.add_argument('--expected-ip', required=True)
    args = parser.parse_args()
    profile = json.loads(Path(args.profile).read_text())
    workdir = Path(args.profile).resolve().parent
    request(args.binary, profile, True, args.expected_ip, workdir)
    print('PASS external HTTPS and remote DNS through TCP/443; egress IP matches server')
    bad_password = copy.deepcopy(profile)
    bad_password['outbounds'][0]['password'] = 'deliberately-invalid-test-password'
    request(args.binary, bad_password, False, args.expected_ip, workdir)
    print('PASS invalid Trojan password rejected')
    bad_tls = copy.deepcopy(profile)
    bad_tls['outbounds'][0]['tls']['server_name'] = 'invalid-certificate-name.example'
    request(args.binary, bad_tls, False, args.expected_ip, workdir)
    print('PASS TLS hostname mismatch rejected; certificate verification is active')


if __name__ == '__main__':
    main()
