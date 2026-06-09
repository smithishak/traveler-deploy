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
#     WG_PORT=443                  # UDP-порт WireGuard/AmneziaWG (443 = маскировка под QUIC/HTTP3)
#     WG_MTU=1280                  # MTU клиентов (1280 — устойчиво к DPI/мобильным сетям)
#
set -euo pipefail

# ---------- настройки ----------
SERVER_IP="${SERVER_IP:-}"
WG_ADMIN_USERNAME="${WG_ADMIN_USERNAME:-admin}"
WG_ADMIN_PASSWORD="${WG_ADMIN_PASSWORD:-}"
WG_DNS="${WG_DNS:-1.1.1.1,8.8.8.8}"
WG_PORT="${WG_PORT:-443}"        # 443/udp маскируется под QUIC — DPI его почти не трогает
WG_MTU="${WG_MTU:-1280}"         # пониженный MTU: AmneziaWG раздувает пакеты, 1280 надёжнее
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

# ---------- 8. фиксы стабильности: MTU 1280 + MSS-клампинг ----------
# Зачем: AmneziaWG добавляет к пакетам «мусор» и меняет заголовки, из-за чего
# крупные TCP-пакеты на мобильных сетях/DPI молча режутся ("подключается, а
# интернет не работает / медленно"). MSS-клампинг гарантированно укладывает TCP
# в путь, MTU 1280 страхует остальное. MSS-правило кладём в hooks_table (PostUp),
# чтобы оно переживало рестарт контейнера, и добавляем вживую для текущей сессии.
log "Применяю фиксы стабильности (MTU $WG_MTU + MSS-клампинг)..."
DB=/var/lib/docker/volumes/etc_wireguard/_data/wg-easy.db
command -v sqlite3 >/dev/null 2>&1 || apt-get install -y -qq sqlite3 >/dev/null
# дождаться, пока INIT создаст таблицы в БД
for i in $(seq 1 30); do
  if [ -f "$DB" ] && sqlite3 "$DB" "select 1 from user_configs_table limit 1;" >/dev/null 2>&1; then
    break
  fi
  sleep 2
done
MSS4=$(( WG_MTU - 40 ))   # IPv4: MTU - 20 (IP) - 20 (TCP)
MSS6=$(( WG_MTU - 60 ))   # IPv6: MTU - 40 (IP) - 20 (TCP)
if [ -f "$DB" ]; then
  # MTU по умолчанию (новые конфиги) и всем существующим клиентам
  sqlite3 "$DB" "UPDATE user_configs_table SET default_mtu=$WG_MTU;" 2>/dev/null || true
  sqlite3 "$DB" "UPDATE clients_table SET mtu=$WG_MTU;" 2>/dev/null || true
  # MSS-клампинг в hooks (идемпотентно — только если ещё не добавлен)
  if ! sqlite3 "$DB" "select post_up from hooks_table where id='wg0';" 2>/dev/null | grep -q TCPMSS; then
    sqlite3 "$DB" "UPDATE hooks_table SET \
      post_up   = post_up   || ' iptables -t mangle -A FORWARD -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --set-mss $MSS4; ip6tables -t mangle -A FORWARD -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --set-mss $MSS6;', \
      post_down = post_down || ' iptables -t mangle -D FORWARD -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --set-mss $MSS4; ip6tables -t mangle -D FORWARD -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --set-mss $MSS6;' \
      WHERE id='wg0';" 2>/dev/null || true
  fi
else
  warn "БД ещё не создана — пропускаю MTU/MSS (примени позже вручную)."
fi
# добавить MSS-правило вживую (без рестарта), чтобы заработало прямо сейчас
docker exec wg-easy sh -c "
  iptables  -t mangle -C FORWARD -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --set-mss $MSS4 2>/dev/null || iptables  -t mangle -A FORWARD -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --set-mss $MSS4
  ip6tables -t mangle -C FORWARD -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --set-mss $MSS6 2>/dev/null || ip6tables -t mangle -A FORWARD -p tcp --tcp-flags SYN,RST SYN -j TCPMSS --set-mss $MSS6
" 2>/dev/null || true

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
  WireGuard порт:       $WG_PORT/udp (маскировка под QUIC)
  MTU клиентов:         $WG_MTU  +  MSS-клампинг (TCP $MSS4 / $MSS6)
  DNS клиентам:         $WG_DNS

  ВАЖНО — клиенты ставят приложение AmneziaWG (НЕ обычный
  WireGuard!): Android/iOS — «AmneziaWG», ПК — AmneziaVPN.
  Конфиги создаёшь в панели (New Client), импортируешь в AmneziaWG.

  Пароль сохранён в ./.env (chmod 600). После проверки можно
  удалить из него INIT_* — они нужны только при первом запуске.
============================================================
EOF
