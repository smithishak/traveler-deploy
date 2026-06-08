#!/usr/bin/env bash
#
# traveler-deploy — автоустановка VPN: wg-easy + AmneziaWG (обход DPI) + HTTPS (Caddy)
# Запуск на свежем сервере (Ubuntu 22.04/24.04) под root:
#     chmod +x deploy.sh && ./deploy.sh
#
# Переопределяемые переменные (по желанию):
#     SERVER_IP=1.2.3.4            # если автоопределение не сработало
#     WG_ADMIN_USERNAME=admin      # логин в панель (по умолчанию admin)
#     WG_ADMIN_PASSWORD=...        # пароль панели (по умолчанию генерируется)
#     WG_DNS=1.1.1.1,8.8.8.8       # DNS для клиентов
#     WG_PORT=51820                # UDP-порт WireGuard/AmneziaWG
#
set -euo pipefail

# ---------- настройки ----------
SERVER_IP="${SERVER_IP:-}"
WG_ADMIN_USERNAME="${WG_ADMIN_USERNAME:-admin}"
WG_ADMIN_PASSWORD="${WG_ADMIN_PASSWORD:-}"
WG_DNS="${WG_DNS:-1.1.1.1,8.8.8.8}"
WG_PORT="${WG_PORT:-51820}"
GENERATED_PW=0

log()  { echo -e "\n\033[1;32m>>> $*\033[0m"; }
warn() { echo -e "\033[1;33m!!! $*\033[0m"; }
die()  { echo -e "\033[1;31mXXX $*\033[0m" >&2; exit 1; }

# ---------- проверки ----------
[ "$(id -u)" -eq 0 ] || die "Запусти под root (sudo -i)."
cd "$(dirname "$0")"

# ---------- определить публичный IP ----------
if [ -z "$SERVER_IP" ]; then
  log "Определяю публичный IP сервера..."
  SERVER_IP="$(curl -fsSL https://api.ipify.org 2>/dev/null || curl -fsSL https://ifconfig.me 2>/dev/null || true)"
fi
[ -n "$SERVER_IP" ] || die "Не удалось определить IP. Запусти: SERVER_IP=твой.ip ./deploy.sh"
log "IP сервера: $SERVER_IP"

# ---------- пароль администратора ----------
if [ -z "$WG_ADMIN_PASSWORD" ]; then
  WG_ADMIN_PASSWORD="$(head -c 24 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 20)"
  GENERATED_PW=1
fi

export DEBIAN_FRONTEND=noninteractive

# ---------- 1. базовые пакеты ----------
log "Ставлю базовые пакеты..."
apt-get update -qq
apt-get install -y -qq curl ca-certificates software-properties-common dkms "linux-headers-$(uname -r)" >/dev/null

# ---------- 2. Docker ----------
if command -v docker >/dev/null 2>&1; then
  log "Docker уже установлен ($(docker --version))."
else
  log "Устанавливаю Docker..."
  curl -fsSL https://get.docker.com | sh
fi
systemctl enable --now docker >/dev/null 2>&1 || true

# ---------- 3. kernel-модуль AmneziaWG ----------
if lsmod | grep -q '^amneziawg'; then
  log "Модуль amneziawg уже загружен."
else
  log "Устанавливаю kernel-модуль AmneziaWG (обход DPI)..."
  if ! modprobe amneziawg 2>/dev/null; then
    add-apt-repository -y ppa:amnezia/ppa
    apt-get update -qq
    apt-get install -y amneziawg-dkms amneziawg-tools
    modprobe amneziawg
  fi
fi
echo amneziawg > /etc/modules-load.d/amneziawg.conf   # автозагрузка после ребута
lsmod | grep -q '^amneziawg' || die "Модуль amneziawg не загрузился — обфускация невозможна."

# ---------- 4. .env для docker compose ----------
log "Генерирую .env..."
cat > .env <<EOF
SERVER_IP=$SERVER_IP
INIT_USERNAME=$WG_ADMIN_USERNAME
INIT_PASSWORD=$WG_ADMIN_PASSWORD
INIT_DNS=$WG_DNS
WG_PORT=$WG_PORT
EOF
chmod 600 .env

# ---------- 5. запуск ----------
log "Поднимаю контейнеры (wg-easy + Caddy)..."
docker compose up -d

# ---------- 6. ожидание готовности ----------
log "Жду готовности wg-easy..."
for i in $(seq 1 30); do
  if docker exec wg-easy ip -d link show wg0 2>/dev/null | grep -q amneziawg; then
    break
  fi
  sleep 2
done

# ---------- 7. проверка ----------
IFACE_TYPE="$(docker exec wg-easy ip -d link show wg0 2>/dev/null | grep -o amneziawg || true)"
if [ "$IFACE_TYPE" = "amneziawg" ]; then
  OBF="ВКЛЮЧЕНА ✅"
else
  OBF="НЕ активна ❌ (проверь модуль/логи: docker logs wg-easy)"
fi

# ---------- итог ----------
cat <<EOF

============================================================
  ГОТОВО. VPN-сервер развёрнут.
============================================================
  Веб-панель:   https://$SERVER_IP
                (самоподписанный TLS — браузер ругнётся,
                 жми «Дополнительно» → «Перейти на сайт»)

  Логин:        $WG_ADMIN_USERNAME
  Пароль:       $WG_ADMIN_PASSWORD
EOF
if [ "$GENERATED_PW" = "1" ]; then
  echo "                (пароль сгенерирован автоматически — сохрани!)"
fi
cat <<EOF

  Обфускация AmneziaWG: $OBF
  WireGuard порт:       $WG_PORT/udp
  DNS клиентам:         $WG_DNS

  ВАЖНО — клиенты ставят приложение AmneziaWG (НЕ обычный
  WireGuard!): Android/iOS — «AmneziaWG», ПК — AmneziaVPN.
  Конфиги создаёшь в панели (New Client), импортируешь в AmneziaWG.

  Пароль сохранён в ./.env (chmod 600). После проверки можно
  удалить из него INIT_* — они нужны только при первом запуске.
============================================================
EOF
