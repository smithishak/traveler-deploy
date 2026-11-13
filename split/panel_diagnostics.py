"""Probe the public Traveler route from the origin without changing its services."""

import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import http.client
import ipaddress
import json
from pathlib import Path
import re
import secrets
import socket
import ssl
import subprocess
import tempfile
import time

if __package__:
    from .panel_probe import DESTINATIONS
else:
    from panel_probe import DESTINATIONS


SING_BOX = "/usr/local/bin/traveler-sing-box"
CURL = "/usr/bin/curl"
PROBE_URL, IP_URL = (f"https://{domain}/" for domain in DESTINATIONS)
LOCALHOST = "127.0.0.1"
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
CHECKS = (
    ("tls", "Сертификат входа"),
    ("websocket", "WebSocket и туннель"),
    ("egress", "Выход в интернет"),
)


def hostname(value):
    if not isinstance(value, str) or not re.fullmatch(r"[!-~]+", value) or "%" in value:
        raise ValueError("Expected a host")
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        labels = value.split(".")
        if len(value) <= 253 and all(
            re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
            for label in labels
        ):
            return value
        raise ValueError("Expected a host") from None


def probe_outbound(profile):
    [source] = profile["outbounds"]
    tls = source["tls"]
    transport = source["transport"]
    if (source["type"] != "trojan" or tls["enabled"] is not True
            or tls.get("insecure", False) is not False or transport["type"] != "ws"):
        raise ValueError("Expected a verified Trojan WebSocket profile")
    server = hostname(source["server"])
    server_name = hostname(tls.get("server_name") or server)
    port = source["server_port"]
    password = source["password"]
    path = transport["path"]
    if (type(port) is not int or not 1 <= port <= 65535
            or not isinstance(password, str) or not password
            or not isinstance(path, str) or not re.fullmatch(r"/[!-~]{1,2048}", path)):
        raise ValueError("Invalid probe profile")
    host = hostname(transport.get("headers", {}).get("Host", server_name))
    settings = {"enabled": True, "server_name": server_name, "alpn": ["http/1.1"]}
    if tls.get("certificate"):
        certificate = tls["certificate"]
        if isinstance(certificate, list):
            certificate = "\n".join(certificate)
        if not isinstance(certificate, str) or not certificate.isascii():
            raise ValueError("Invalid certificate")
        settings["certificate"] = certificate
    return {
        "type": "trojan",
        "tag": "probe",
        "server": server,
        "server_port": port,
        "password": password,
        "tls": settings,
        "transport": {"type": "ws", "path": path, "headers": {"Host": host}},
    }


def websocket_status(connection, transport):
    key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
    host = transport["headers"]["Host"]
    if ":" in host:
        host = f"[{host}]"
    request = (
        f'GET {transport["path"]} HTTP/1.1\r\nHost: {host}\r\n'
        "Connection: Upgrade\r\nUpgrade: websocket\r\n"
        f"Sec-WebSocket-Version: 13\r\nSec-WebSocket-Key: {key}\r\n\r\n"
    )
    connection.sendall(request.encode("ascii"))
    with http.client.HTTPResponse(connection, method="GET") as response:
        response.begin()
        expected = base64.b64encode(hashlib.sha1((key + WS_GUID).encode("ascii")).digest()).decode("ascii")
        upgrade = response.getheader("Upgrade", "").lower()
        connection_tokens = {value.strip().lower() for value in response.getheader("Connection", "").split(",")}
        if response.status == 101 and (
            upgrade != "websocket" or "upgrade" not in connection_tokens
            or response.getheader("Sec-WebSocket-Accept") != expected
        ):
            raise ValueError("Invalid WebSocket handshake")
        return response.status


def probe_config(outbound, port, password, check_ip):
    domains = list(DESTINATIONS if check_ip else DESTINATIONS[:1])
    return {
        "log": {"disabled": True},
        "dns": {"servers": [{"type": "local", "tag": "system"}]},
        "inbounds": [{
            "type": "mixed", "listen": LOCALHOST, "listen_port": port,
            "users": [{"username": "traveler-probe", "password": password}],
        }],
        "outbounds": [outbound],
        "route": {
            "default_domain_resolver": "system",
            "rules": [
                {"domain": domains, "port": 443, "action": "route", "outbound": "probe"},
                {"action": "reject"},
            ],
        },
    }


@contextmanager
def probe_client(outbound, check_ip):
    password = secrets.token_urlsafe(24)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as reservation:
        reservation.bind((LOCALHOST, 0))
        port = reservation.getsockname()[1]
    with tempfile.TemporaryDirectory(prefix="traveler-probe-") as directory:
        config = Path(directory) / "client.json"
        config.write_text(json.dumps(probe_config(outbound, port, password, check_ip)), encoding="utf-8")
        config.chmod(0o600)
        process = subprocess.Popen(
            [SING_BOX, "run", "-c", str(config)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.monotonic() + 2
            while process.poll() is None:
                try:
                    with socket.create_connection((LOCALHOST, port), timeout=0.2):
                        break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("Probe client did not start") from None
                    time.sleep(0.05)
            if process.poll() is not None:
                raise OSError("Probe client exited")
            yield port, password
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=1)


def proxy_get(port, password, url, read_body=False):
    command = [
        CURL, "--disable", "--silent", "--fail", "--proto", "=https",
        "--proxy", f"http://{LOCALHOST}:{port}", "--noproxy", "",
        "--connect-timeout", "3", "--max-time", "5",
        "--max-filesize", "128" if read_body else "65536",
        "--write-out", "\n%{http_code}", "--config", "-",
    ]
    if not read_body:
        command.extend(("--output", "/dev/null"))
    result = subprocess.run(
        [*command, url], check=True, timeout=6, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        input=f'proxy-user = "traveler-probe:{password}"\n'.encode("ascii"),
    )
    body, _, status = result.stdout.rpartition(b"\n")
    if not re.fullmatch(rb"2\d\d", status):
        raise ValueError("Expected a successful HTTPS response")
    return body


def check_route(profile, *, expected_egress_ip=None):
    """Return sanitized diagnostics. The caller provides an admin-only probe profile."""
    labels = list(CHECKS)
    if expected_egress_ip is not None:
        labels.append(("egress_ip", "Выходной IP"))
    checks = {
        name: {"id": name, "label": label, "status": "skipped", "detail": "Предыдущий этап не пройден."}
        for name, label in labels
    }
    result = {
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "vantage": "Основной VPS",
        "checks": list(checks.values()),
    }

    def record(name, status, detail):
        checks[name].update(status=status, detail=detail)

    try:
        outbound = probe_outbound(profile)
        tls = outbound["tls"]
        context = ssl.create_default_context(cadata=tls.get("certificate"))
    except (AttributeError, KeyError, TypeError, ValueError, ssl.SSLError):
        record("tls", "fail", "Не удалось подготовить TLS из служебного профиля.")
        return result

    try:
        context.set_alpn_protocols(["http/1.1"])
        with socket.create_connection((outbound["server"], outbound["server_port"]), timeout=3) as sock:
            with context.wrap_socket(sock, server_hostname=tls["server_name"]) as connection:
                record("tls", "pass", "Сертификат действителен для имени входа.")
                status = websocket_status(connection, outbound["transport"])
    except (OSError, ValueError, http.client.HTTPException) as error:
        if checks["tls"]["status"] == "pass":
            record("websocket", "fail", "Вход не вернул корректный ответ WebSocket.")
        elif isinstance(error, ssl.SSLCertVerificationError):
            record("tls", "fail", "Сертификат входа не прошёл проверку.")
        else:
            record("tls", "fail", "Вход недоступен или не прошла проверка сертификата.")
        return result
    if status != 101:
        record("websocket", "fail", f"Вход ответил HTTP {status} вместо 101.")
        return result
    record("websocket", "pass", "WebSocket через входной VPS и туннель ответил HTTP 101.")

    try:
        with probe_client(outbound, expected_egress_ip is not None) as (port, password):
            proxy_get(port, password, PROBE_URL)
            record("egress", "pass", "Служебный профиль получил HTTPS-ответ из интернета.")
            if expected_egress_ip is not None:
                expected = ipaddress.ip_address(expected_egress_ip)
                actual = ipaddress.ip_address(proxy_get(port, password, IP_URL, read_body=True).strip().decode("ascii"))
                if actual == expected:
                    record("egress_ip", "pass", "Выходной адрес совпадает с ожидаемым.")
                else:
                    record("egress_ip", "fail", "Выходной адрес отличается от ожидаемого.")
    except (OSError, ValueError, subprocess.SubprocessError):
        if checks["egress"]["status"] == "pass":
            record("egress_ip", "fail", "Не удалось проверить выходной адрес.")
        else:
            record("egress", "fail", "Служебный профиль не получил HTTPS-ответ из интернета.")
    return result
