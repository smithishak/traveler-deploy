#!/usr/bin/env python3
"""Root-only lifecycle manager. Python 3 standard library; no pip packages."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import time
import uuid

ROOT = Path(__file__).resolve().parent
STATE = ROOT / '.traveler'
IMAGES = STATE / 'images.json'
PENDING = STATE / 'pending.json'
SERVICES = ('wg-easy', 'caddy')
FILES = ('.env', 'docker-compose.yml', 'Caddyfile')


class Failure(RuntimeError):
    pass


def run(*args, check=True, timeout=180, env=None):
    # Never echo argv or subprocess output on failure: compose may include secrets.
    try:
        result = subprocess.run(args, cwd=ROOT, text=True, capture_output=True,
                                timeout=timeout, env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Failure(f'Не удалось выполнить {args[0]} ({type(exc).__name__}).') from exc
    if check and result.returncode:
        raise Failure(f'Команда {args[0]} завершилась с кодом {result.returncode}. '
                      'Проверьте Docker/журнал сервиса; секреты из вывода скрыты.')
    return result


def atomic_json(path, data):
    tmp = path.with_suffix('.tmp')
    with tmp.open('w') as stream:
        stream.write(json.dumps(data, indent=2) + '\n')
        stream.flush()
        os.fsync(stream.fileno())
    tmp.chmod(0o600)
    tmp.replace(path)
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def digest(path):
    result = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def compose(*args, base=False, check=True, timeout=180):
    command = ['docker', 'compose', '--project-directory', str(ROOT),
               '-f', str(ROOT / 'docker-compose.yml')]
    if IMAGES.exists() and not base:
        command += ['-f', str(IMAGES)]
    environment = os.environ.copy()
    # Saved configuration wins over inherited shell values on every later run.
    keys = {'SERVER_IP', 'BIND_IP', 'WG_PORT', 'INIT_USERNAME', 'INIT_PASSWORD', 'INIT_DNS'}
    env_file = ROOT / '.env'
    if env_file.exists():
        keys.update(re.findall(r'^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=',
                               env_file.read_text(), flags=re.MULTILINE))
    for key in keys:
        environment.pop(key, None)
    return run(*command, *args, check=check, timeout=timeout, env=environment)


def config(base=False):
    return json.loads(compose('config', '--format', 'json', base=base).stdout)


def info(service):
    result = run('docker', 'inspect', service, check=False)
    return json.loads(result.stdout)[0] if result.returncode == 0 else None


def volume(name='etc_wireguard'):
    result = run('docker', 'volume', 'inspect', name)
    data = json.loads(result.stdout)[0]
    path = Path(data['Mountpoint']).resolve()
    if data['Driver'] != 'local' or not path.is_dir() or path == Path('/'):
        raise Failure(f'Нужен существующий локальный том {name}.')
    return path


def env_values():
    # Ask Compose to parse dotenv. Do not source user-controlled text as shell code.
    data = config()['services']['wg-easy']['environment']
    port = str(data['INIT_PORT'])
    host = str(data['INIT_HOST'])
    if not port.isdigit() or not 1 <= int(port) <= 65535 or port in ('53', '4500'):
        raise Failure('WG_PORT: нужен порт 1..65535, кроме резервных 53 и 4500.')
    if not re.fullmatch(r'[A-Za-z0-9.-]+', host):
        raise Failure('SERVER_IP должен быть IPv4 или DNS-именем.')
    return host, int(port)


def check_core():
    host, port = env_values()
    for service in SERVICES:
        item = info(service)
        if not item or not item['State']['Running']:
            raise Failure(f'{service}: контейнер не запущен.')
        expected = config()['services'][service]['image']
        image_id = run('docker', 'image', 'inspect', expected, '--format', '{{.Id}}').stdout.strip()
        if item['Image'] != image_id:
            raise Failure(f'{service}: работает другой образ.')
    interface = run('docker', 'exec', 'wg-easy', 'ip', '-d', 'link', 'show', 'wg0').stdout
    if 'amneziawg' not in interface or 'UP' not in interface:
        raise Failure('wg0: интерфейс AmneziaWG не поднят.')
    actual = run('docker', 'exec', 'wg-easy', 'awg', 'show', 'wg0', 'listen-port').stdout.strip()
    if actual != str(port):
        raise Failure('Порт интерфейса не совпадает с настройками Compose.')
    # Check published mappings, not just whether some socket happens to exist.
    bindings = info('wg-easy')['HostConfig']['PortBindings'].get(f'{port}/udp', [])
    if not {str(port), '4500', '53'}.issubset({p['HostPort'] for p in bindings}):
        raise Failure('Отсутствуют публикации UDP-портов.')
    for family in ('iptables', 'ip6tables'):
        nat = run('docker', 'exec', 'wg-easy', family, '-t', 'nat', '-S', 'POSTROUTING').stdout
        if 'MASQUERADE' not in nat:
            raise Failure(f'{family}: нет NAT/MASQUERADE.')
    if run('docker', 'exec', 'wg-easy', 'sysctl', '-n', 'net.ipv4.ip_forward').stdout.strip() != '1':
        raise Failure('IPv4 forwarding выключен.')
    # Local TLS request with correct SNI/Host, works for DNS names and IPv4.
    response = run('curl', '-ksS', '--noproxy', '*', '--connect-timeout', '3', '--max-time', '5',
                   '--resolve', f'{host}:443:127.0.0.1', '-o', '/dev/null',
                   '-w', '%{http_code}', f'https://{host}/login').stdout
    if response != '200':
        raise Failure(f'HTTPS /login: HTTP {response}.')


def wait_ready(seconds):
    deadline = time.monotonic() + seconds
    successes = 0
    last = ''
    while time.monotonic() < deadline:
        try:
            check_core()
            successes += 1
            if successes >= 3:
                return
        except Failure as exc:
            successes = 0
            last = str(exc)
        time.sleep(2)
    raise Failure('Проверка запуска не прошла: ' + last)


def doctor():
    failures = 0

    def test(label, fn):
        nonlocal failures
        try:
            fn()
            print(f'OK   {label}')
        except (Failure, sqlite3.Error, ValueError, KeyError) as exc:
            failures += 1
            print(f'FAIL {label}: {exc}')

    test('Docker daemon', lambda: run('docker', 'info', '--format', '{{.ServerVersion}}'))
    test('Compose и .env', env_values)
    test('Модуль ядра AmneziaWG', lambda: require(Path('/sys/module/amneziawg').exists(),
                                               'Модуль не загружен; проверьте DKMS/modprobe.'))
    test('Контейнеры, wg0, UDP-порты, NAT, forwarding, HTTPS', check_core)

    def database():
        with sqlite3.connect(f'file:{volume() / "wg-easy.db"}?mode=ro', uri=True) as db:
            require(db.execute('PRAGMA quick_check').fetchone()[0] == 'ok', 'База повреждена.')
            mtu = db.execute('SELECT default_mtu FROM user_configs_table').fetchone()[0]
            for family, overhead in (('iptables', 40), ('ip6tables', 60)):
                run('docker', 'exec', 'wg-easy', family, '-t', 'mangle', '-C', 'FORWARD',
                    '-p', 'tcp', '--tcp-flags', 'SYN,RST', 'SYN', '-j', 'TCPMSS',
                    '--set-mss', str(mtu - overhead))
            print(f'     MTU новых клиентов: {mtu}; MSS IPv4/IPv6 соответствует.')
    test('SQLite, MTU и MSS', database)
    test('DNS из контейнера (example.com)', lambda: run('docker', 'exec', 'wg-easy',
                                                       'nslookup', 'example.com', timeout=15))
    peers = run('docker', 'exec', 'wg-easy', 'awg', 'show', 'wg0', 'latest-handshakes', check=False)
    if peers.returncode == 0:
        stamps = [int(row.split()[1]) for row in peers.stdout.splitlines() if len(row.split()) == 2]
        recent = sum(time.time() - stamp < 180 for stamp in stamps if stamp)
        print(f'INFO Клиентов: {len(stamps)}, handshake за 3 минуты: {recent}.')
    print('INFO Внешняя доступность UDP, VPN-трафик и обход DPI требуют внешнего клиента.')
    print(f'Итог: ошибок {failures}. Секреты и ключи в отчёт не включены.')
    return 1 if failures else 0


def require(condition, message):
    if not condition:
        raise Failure(message)


def install():
    if (ROOT / '.env').exists():
        print('Существующая установка: .env, пароль, клиенты и настройки сохраняются.')
        env_values()
        compose('up', '-d', '--pull', 'never')
        wait_ready(90)
        return doctor()
    # Refuse accidental reinitialization when credentials/config were deleted.
    existing = (run('docker', 'volume', 'inspect', 'etc_wireguard', check=False)
                if shutil.which('docker') else None)
    if existing is not None and existing.returncode == 0:
        require(not any(volume().iterdir()),
                'Том VPN уже содержит данные, но .env отсутствует. Восстановите .env из бэкапа.')
    subprocess.run(['bash', str(ROOT / 'setup-host.sh')], check=True)
    host = os.environ.get('SERVER_IP') or run('curl', '-4fsS', '--max-time', '15',
                                             'https://api.ipify.org').stdout.strip()
    require(bool(re.fullmatch(r'[A-Za-z0-9.-]+', host)), 'Некорректный SERVER_IP.')
    route = json.loads(run('ip', '-j', 'route', 'get', '1.1.1.1').stdout)
    values = {'SERVER_IP': host, 'BIND_IP': route[0].get('prefsrc', host),
              'INIT_USERNAME': os.environ.get('WG_ADMIN_USERNAME', 'admin'),
              'INIT_PASSWORD': os.environ.get('WG_ADMIN_PASSWORD') or secrets.token_urlsafe(24),
              'INIT_DNS': os.environ.get('WG_DNS', '1.1.1.1,8.8.8.8'),
              'WG_PORT': os.environ.get('WG_PORT', '443'),
              'WG_MTU': os.environ.get('WG_MTU', '1280')}
    # Single-quoted dotenv values preserve $, # and whitespace; reject ambiguous input.
    require(all(not any(c in str(v) for c in "'\r\n\\") for v in values.values()),
            'Параметры не должны содержать кавычку, обратный слеш или перевод строки.')
    mtu = int(values['WG_MTU'])
    require(1280 <= mtu <= 1420, 'WG_MTU: допустимо 1280..1420.')
    path = ROOT / '.env'
    path.write_text(''.join(f"{k}='{v}'\n" for k, v in values.items()))
    path.chmod(0o600)
    env_values()
    compose('up', '-d', timeout=600)
    wait_ready(90)
    # Only initial installation changes defaults. Repeated install never rewrites DB.
    compose('stop', 'wg-easy')
    try:
        with sqlite3.connect(volume() / 'wg-easy.db') as db:
            db.execute('UPDATE user_configs_table SET default_mtu=?', (mtu,))
            up, down = [], []
            for family, overhead in (('iptables', 40), ('ip6tables', 60)):
                rule = f'FORWARD -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --set-mss {mtu-overhead};'
                up.append(f' {family} -t mangle -A {rule}')
                down.append(f' {family} -t mangle -D {rule}')
            db.execute("UPDATE hooks_table SET post_up=COALESCE(post_up,'') || ?, "
                       "post_down=COALESCE(post_down,'') || ? WHERE id='wg0'",
                       (''.join(up), ''.join(down)))
    finally:
        compose('start', 'wg-easy')
    wait_ready(90)
    print(f'Панель: https://{host}; логин и пароль сохранены в .env (не выводятся в лог).')
    return doctor()


def pinned_images():
    images = {}
    release = uuid.uuid4().hex
    for service in SERVICES:
        item = info(service)
        require(item is not None, f'Не найден контейнер {service}.')
        tag = f'traveler-local/{service}:{release}'
        run('docker', 'tag', item['Image'], tag)
        images[service] = {'image': tag, 'pull_policy': 'never'}
    return {'services': images}


def snapshot():
    target = STATE / 'backups' / (time.strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8])
    target.mkdir(parents=True, mode=0o700)
    for name in FILES:
        shutil.copy2(ROOT / name, target / name)
        (target / name).chmod(0o600)
    atomic_json(target / 'images.json', pinned_images())
    volumes = {'etc_wireguard': volume()}
    for service in SERVICES:
        for mount in info(service)['Mounts']:
            if mount['Type'] == 'volume':
                volumes[mount['Name']] = volume(mount['Name'])
    required = sum(p.stat().st_size for data in volumes.values()
                   for p in data.rglob('*') if p.is_file())
    require(shutil.disk_usage(STATE).free > required * 2 + 128 * 1024**2,
            'Недостаточно места для резервной копии.')
    # Stop writers for a consistent SQLite/WAL + keys/config backup.
    atomic_json(PENDING, {'backup': str(target), 'phase': 'snapshot'})
    try:
        compose('stop')
        manifest = {'volumes': {}, 'files': {
            name: digest(target / name) for name in (*FILES, 'images.json')}}
        for index, (name, data) in enumerate(volumes.items()):
            archive_path = target / f'volume-{index}.tar'
            with tarfile.open(archive_path, 'w') as archive:
                archive.add(data, arcname='.')
            with archive_path.open('rb') as source:
                os.fsync(source.fileno())
            manifest['volumes'][name] = {
                'path': str(data), 'archive': archive_path.name,
                'sha256': digest(archive_path)}
        for name in FILES:
            with (target / name).open('rb') as source:
                os.fsync(source.fileno())
        atomic_json(target / 'manifest.json', manifest)
    finally:
        compose('start')
        PENDING.unlink(missing_ok=True)
    print(f'Бэкап: {target} (содержит секреты; доступ только root).')
    return target


def restore(target, timeout):
    target = target.resolve()
    require(target.parent == (STATE / 'backups').resolve(), 'Бэкап должен быть в .traveler/backups/.')
    manifest = json.loads((target / 'manifest.json').read_text())
    for name in (*FILES, 'images.json'):
        require(digest(target / name) == manifest['files'][name],
                f'Контрольная сумма файла {name} не совпадает.')
    archives = []
    for name, saved in manifest['volumes'].items():
        archive_path = target / saved['archive']
        require(archive_path.resolve().parent == target, 'Неверный путь архива.')
        require(digest(archive_path) == saved['sha256'],
                'Контрольная сумма бэкапа не совпадает.')
        data = volume(name)
        require(str(data) == saved['path'], 'Путь тома изменился; автоматическое удаление запрещено.')
        # Validate before stopping anything. No links/special files are accepted.
        with tarfile.open(archive_path) as archive:
            for entry in archive.getmembers():
                resolved = (data / entry.name).resolve()
                require(resolved == data or data in resolved.parents, 'Небезопасный путь в архиве.')
                require(entry.isfile() or entry.isdir(), 'Ссылки/специальные файлы в архиве запрещены.')
        archives.append((archive_path, data))
    saved_images = json.loads((target / 'images.json').read_text())
    for item in saved_images['services'].values():
        run('docker', 'image', 'inspect', item['image'])
    atomic_json(PENDING, {'backup': str(target)})
    compose('stop')
    # Never remove the Docker volume itself; preserve mountpoint and ownership.
    for archive_path, data in archives:
        for child in data.iterdir():
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
        with tarfile.open(archive_path) as archive:
            archive.extractall(data)  # members validated above; Python 3.10 compatible.
    for name in FILES:
        shutil.copy2(target / name, ROOT / name)
    atomic_json(IMAGES, saved_images)
    compose('up', '-d', '--force-recreate', '--pull', 'never', timeout=180)
    wait_ready(timeout)
    PENDING.unlink(missing_ok=True)
    print('Откат завершён: восстановлены данные, настройки и предыдущие образы.')


def update(args):
    require(not PENDING.exists(), 'Есть незавершённое обновление. Выполните ./deploy.sh rollback.')
    wait_ready(30)
    candidates = config(base=True)['services']
    selected = {'wg-easy': args.wg_image or candidates['wg-easy']['image'],
                'caddy': args.caddy_image or candidates['caddy']['image']}
    # Download before downtime; failure here leaves running containers intact.
    release = uuid.uuid4().hex
    new_images = {'services': {}}
    for service, image in selected.items():
        print(f'Загружаю образ {service}…', flush=True)
        run('docker', 'pull', image, timeout=600)
        tag = f'traveler-local/{service}:{release}'
        run('docker', 'tag', image, tag)
        new_images['services'][service] = {'image': tag, 'pull_policy': 'never'}
    target = snapshot()
    atomic_json(PENDING, {'backup': str(target)})
    try:
        atomic_json(IMAGES, new_images)
        compose('up', '-d', '--force-recreate', '--pull', 'never', timeout=180)
        wait_ready(args.timeout)
        require(doctor() == 0, 'Диагностика после обновления не прошла.')
    except (Exception, KeyboardInterrupt) as exc:
        print(f'Обновление не прошло ({type(exc).__name__}). Возвращаю прежнюю версию.', flush=True)
        restore(target, 90)
        raise Failure('Новая версия отклонена. Прежняя версия восстановлена.') from exc
    PENDING.unlink()
    print('Обновление завершено. Для ручного отката: ./deploy.sh rollback ' + target.name)


def main():
    parser = argparse.ArgumentParser(description='Traveler: install, doctor, update, rollback, backup')
    parser.add_argument('command', nargs='?', default='install',
                        choices=('install', 'doctor', 'update', 'rollback', 'backup'))
    parser.add_argument('backup_id', nargs='?')
    parser.add_argument('--wg-image', help='Образ wg-easy для update')
    parser.add_argument('--caddy-image', help='Образ Caddy для update')
    parser.add_argument('--timeout', type=int, default=90, help='Ожидание новой версии, 10..600 секунд')
    args = parser.parse_args()
    require(os.geteuid() == 0, 'Запустите под root.')
    require(10 <= args.timeout <= 600, '--timeout: допустимо 10..600.')
    os.umask(0o077)
    STATE.mkdir(mode=0o700, exist_ok=True)
    STATE.chmod(0o700)
    # One lock for all commands, including doctor: do not inspect a half-restored DB.
    with (STATE / 'lock').open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Failure('Другая команда traveler уже выполняется.') from exc
        if PENDING.exists() and args.command != 'rollback':
            raise Failure('Обновление было прервано. Выполните ./deploy.sh rollback для восстановления.')
        if args.command == 'install':
            return install()
        if args.command == 'doctor':
            return doctor()
        if args.command == 'update':
            update(args)
        elif args.command == 'backup':
            snapshot()
        elif args.command == 'rollback':
            if PENDING.exists():
                pending = json.loads(PENDING.read_text())
                if pending.get('phase') == 'snapshot':
                    # A killed backup never replaced images/data. Start original services.
                    compose('start')
                    wait_ready(args.timeout)
                    PENDING.unlink()
                    print('Прерванный бэкап: исходные сервисы снова запущены; данные не заменялись.')
                    return 0
            if args.backup_id:
                target = STATE / 'backups' / args.backup_id
            elif PENDING.exists():
                target = Path(json.loads(PENDING.read_text())['backup'])
            else:
                raise Failure('Укажите ID бэкапа: ./deploy.sh rollback <ID>.')
            restore(target, args.timeout)
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (Failure, ValueError, KeyError, sqlite3.Error, OSError,
            subprocess.CalledProcessError) as error:
        print(f'Ошибка: {error}', file=sys.stderr)
        sys.exit(1)
