#!/usr/bin/env python3
"""Explicit opt-in live tests. Runs on a disposable/test server, briefly interrupts VPN."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import traveler as app


def cli(*args, expected=0, env=None):
    result = subprocess.run(['bash', str(ROOT / 'deploy.sh'), *args], cwd=ROOT,
                            text=True, capture_output=True, env=env, timeout=900)
    print(result.stdout, end='', flush=True)
    if result.returncode != expected:
        raise AssertionError(f'{args}: exit={result.returncode}, expected={expected}; {result.stderr}')
    return result


def identity():
    with sqlite3.connect(app.volume() / 'wg-easy.db') as db:
        data = {name: db.execute(f'SELECT * FROM {name} ORDER BY 1').fetchall()
                for name in ('clients_table', 'users_table', 'hooks_table')}
    # Only print pass/fail; never print rows or hashes of passwords/keys.
    return (hashlib.sha256((ROOT / '.env').read_bytes()).digest(),
            hashlib.sha256(json.dumps(data, sort_keys=True).encode()).digest())


def main():
    if sys.argv[1:] != ['--allow-vpn-interruption'] or os.geteuid() != 0:
        sys.exit('Run as root: python3 tests/integration.py --allow-vpn-interruption')
    original = identity()
    cli('doctor')
    env = os.environ.copy()
    env.update(WG_ADMIN_PASSWORD='THIS_MUST_NOT_REPLACE_EXISTING_PASSWORD', WG_PORT='12345')
    cli('install', env=env)
    assert identity() == original, 'Repeated install changed existing state'
    print('PASS repeat install preserves .env, users, clients and hooks', flush=True)

    with (app.STATE / 'lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        cli('doctor', expected=1)
    print('PASS concurrent operation refused', flush=True)

    app.compose('stop', 'caddy')
    try:
        cli('doctor', expected=1)
    finally:
        app.compose('start', 'caddy')
    app.wait_ready(90)
    print('PASS doctor detects stopped panel', flush=True)

    cli('backup')
    latest = sorted((app.STATE / 'backups').iterdir())[-1]
    sentinel = app.volume() / '.traveler-integration-sentinel'
    sentinel.write_text('must disappear after restoring the complete volume')
    cli('rollback', latest.name)
    assert not sentinel.exists(), 'Rollback did not replace volume contents'
    assert identity() == original, 'Manual rollback lost client/account/config data'
    print('PASS manual rollback restores complete volume and credentials', flush=True)

    cli('update', '--wg-image', 'ghcr.io/wg-easy/wg-easy:15.5.0')
    assert identity() == original, 'Successful update changed existing clients/settings'
    cli('doctor')
    print('PASS successful update and post-update doctor', flush=True)

    before_images = {name: app.info(name)['Image'] for name in app.SERVICES}
    # Real downloaded but deliberately incompatible image in the VPN service.
    cli('update', '--wg-image', 'caddy:2-alpine', '--timeout', '15', expected=1)
    assert {name: app.info(name)['Image'] for name in app.SERVICES} == before_images
    assert identity() == original, 'Automatic rollback changed existing client/account/config data'
    assert not app.PENDING.exists(), 'Pending transaction was not cleared'
    cli('doctor')
    print('PASS failed deployment automatically restores previous images and all state', flush=True)

    before_images = {name: app.info(name)['Image'] for name in app.SERVICES}
    cli('update', '--wg-image', 'ghcr.io/wg-easy/wg-easy:traveler-nonexistent-test-tag', expected=1)
    assert {name: app.info(name)['Image'] for name in app.SERVICES} == before_images
    assert identity() == original
    print('PASS image download failure leaves running version intact', flush=True)

    # Simulate a process/host dying in each persisted transaction phase.
    app.atomic_json(app.PENDING, {'backup': str(latest), 'phase': 'snapshot'})
    app.compose('stop')
    cli('doctor', expected=1)
    cli('rollback')
    assert identity() == original
    print('PASS interrupted snapshot resumes original services', flush=True)

    latest = sorted((app.STATE / 'backups').iterdir())[-1]
    app.atomic_json(app.PENDING, {'backup': str(latest)})
    cli('doctor', expected=1)
    cli('rollback')
    assert identity() == original
    cli('doctor')
    print('PASS persisted update marker recovers with rollback without arguments', flush=True)
    print('ALL LIVE TESTS PASSED', flush=True)


if __name__ == '__main__':
    main()
