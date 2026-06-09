# traveler-deploy

Автоустановка self-hosted VPN за одну команду: **WireGuard** с веб-панелью
**[wg-easy](https://github.com/wg-easy/wg-easy)**, обфускацией **AmneziaWG**
(обход DPI-блокировок провайдера) и веб-панелью по **HTTPS** (Caddy,
самоподписанный сертификат — домен не нужен).

## Быстрый старт

На свежем сервере **Ubuntu 22.04/24.04** под **root**:

```bash
apt-get update && apt-get install -y git
git clone https://github.com/smithishak/traveler-deploy.git
cd traveler-deploy
chmod +x deploy.sh && ./deploy.sh
```

Скрипт сам:
1. поставит Docker;
2. соберёт и загрузит kernel-модуль **AmneziaWG**;
3. сгенерирует `.env` (определит публичный IP, создаст пароль админа);
4. поднимет `wg-easy` + `Caddy`;
5. через `INIT_*` создаст администратора, хост и сеть — **без мастера настройки**;
6. применит фиксы устойчивости к DPI: **MTU 1280**, **MSS-клампинг**, порт **443/udp**;
7. в конце напечатает адрес панели, логин и пароль.

## После установки

1. Открой **`https://<IP-сервера>`** → прими самоподписанный сертификат
   («Дополнительно» → «Перейти на сайт»).
2. Войди логином/паролем из вывода скрипта (они же в `./.env`).
3. **New Client** → конфиг + QR. По клиенту на каждое устройство.

> ⚠️ Клиенты ставят приложение **AmneziaWG** (НЕ обычный WireGuard — он не
> подключится к обфусцированному интерфейсу):
> - Android: Google Play → «AmneziaWG» (или «WG Tunnel»)
> - iOS: App Store → «AmneziaWG»
> - Windows/macOS/Linux: **AmneziaVPN** с [amnezia.org](https://amnezia.org)

## Параметры (по желанию)

```bash
SERVER_IP=1.2.3.4 \
WG_ADMIN_USERNAME=admin \
WG_ADMIN_PASSWORD=мой_длинный_пароль \
WG_DNS=1.1.1.1,8.8.8.8 \
WG_PORT=443 \
WG_MTU=1280 \
./deploy.sh
```

| Переменная | Назначение | По умолчанию |
|---|---|---|
| `SERVER_IP` | внешний IP (endpoint для клиентов) | автоопределение |
| `WG_ADMIN_USERNAME` | логин в панель | `admin` |
| `WG_ADMIN_PASSWORD` | пароль в панель | генерируется |
| `WG_DNS` | DNS для клиентов (через запятую) | `1.1.1.1,8.8.8.8` |
| `WG_PORT` | UDP-порт WireGuard (443 = маскировка под QUIC) | `443` |
| `WG_MTU` | MTU клиентов (1280 устойчив к DPI/мобильным сетям) | `1280` |

## Порты

| Порт | Назначение |
|---|---|
| `443/tcp` | веб-панель (HTTPS через Caddy) |
| `443/udp` | WireGuard / AmneziaWG (маскировка под QUIC) |

`443/tcp` (Caddy) и `443/udp` (WireGuard) не конфликтуют — это разные протоколы.
Порт панели `51821` наружу **не** публикуется — доступ только по HTTPS.

## Устойчивость к DPI и «то работает, то нет»

`deploy.sh` сразу закладывает три вещи, лечащие типовые жалобы
(«хендшейк не проходит», «долго коннектится», «подключается, а интернета нет»):

1. **Порт `443/udp`** — трафик выглядит как QUIC/HTTP3, DPI его почти не трогает
   (51820 — «визитка» WireGuard, его режут чаще).
2. **MSS-клампинг** (TCP MSS → `MTU−40`/`MTU−60`) — закреплён в `hooks_table`
   (PostUp), переживает рестарт. Чинит «сайты грузятся через раз» **без**
   переустановки конфигов на устройствах.
3. **MTU `1280`** — по умолчанию и у всех клиентов: AmneziaWG раздувает пакеты,
   1280 надёжно проходит по любому пути.

> Клиент обязан быть именно **AmneziaWG/AmneziaVPN** — обычный WireGuard не
> завершит хендшейк с обфусцированным сервером.

## Полезные команды

```bash
cd traveler-deploy
docker compose ps                       # статус
docker logs wg-easy --tail 30           # логи панели
docker exec wg-easy awg show            # пиры и трафик
docker exec wg-easy ip -d link show wg0 # тип интерфейса (должно быть amneziawg)
docker compose pull && docker compose up -d   # обновление
```

## Диагностика «у одного работает, у других нет»

`docker exec wg-easy awg show` → смотрим `latest handshake` и `transfer`:

- **нет handshake** → блокируется UDP-порт / неверный endpoint;
- **handshake есть, данных почти нет** → MTU или DPI:
  1. пробуем на клиенте `MTU = 1280` в `[Interface]`;
  2. не помогло, и страдают клиенты конкретного провайдера → это **DPI**,
     лечится обфускацией AmneziaWG (она тут уже включена — главное, чтобы
     клиент был именно AmneziaWG-приложением).

## Обновление существующего сервера

```bash
cd traveler-deploy && git pull && docker compose up -d
```

## Безопасность

После настройки: смени root-пароль (`passwd`), перейди на вход по SSH-ключу
(`PasswordAuthentication no` в `/etc/ssh/sshd_config`). Из `.env` после первого
запуска можно убрать `INIT_*` (они работают только при пустом томе).

---

## Что под капотом

- `deploy.sh` — установщик (Docker, модуль AmneziaWG, генерация `.env`, запуск).
- `docker-compose.yml` — сервисы `wg-easy` (v15, `EXPERIMENTAL_AWG=true`) и `caddy`.
- `Caddyfile` — HTTPS-прокси с `tls internal` и `default_sni` (для доступа по IP).
- `.env` — генерируется на сервере, **в git не коммитится** (содержит пароль).

Подробный разбор и грабли — см. историю и комментарии в файлах.
