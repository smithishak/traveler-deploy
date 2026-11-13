#!/usr/bin/env bash
set -euo pipefail
umask 077
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

die() { printf '%s\n' "$*" >&2; exit 1; }

[[ $EUID == 0 ]] || die 'Run as root.'
source /etc/os-release
[[ $ID == ubuntu && $VERSION_ID == 24.04 && $(uname -m) == x86_64 ]] ||
    die 'This installer supports Ubuntu 24.04 LTS on x86-64.'
exec 9>/run/traveler-split-install.lock
flock -n 9 || die 'Another Traveler installation is running.'

packages() {
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "$@"
}

ensure_account() {
    local account=$1
    id "$account" >/dev/null 2>&1 ||
        useradd --system --user-group --create-home --home-dir "/var/lib/$account" --shell /usr/sbin/nologin "$account"
    install -d -m 700 -o "$account" -g "$account" "/var/lib/$account/.ssh"
}

add_key() {
    local account=$1 input=$2 options=$3 label=$4
    local key_type key_blob line key_file
    [[ $(wc -l < "$input") == 1 ]] || die 'Expected one SSH public key.'
    read -r key_type key_blob _ < "$input"
    [[ $key_type == ssh-ed25519 && $key_blob =~ ^[A-Za-z0-9+/]{68}$ ]] ||
        die 'Expected a public Ed25519 key.'
    ssh-keygen -lf "$input" >/dev/null
    key_file="/var/lib/$account/.ssh/authorized_keys"
    line="$options $key_type $key_blob $label"
    touch "$key_file"
    chown "$account:$account" "$key_file"
    chmod 600 "$key_file"
    if ! grep -Fxq -- "$line" "$key_file"; then
        printf '%s\n' "$line" >> "$key_file"
    fi
}

install_sshd_rule() {
    local source=$1 target="/etc/ssh/sshd_config.d/$1"
    sshd -t -f "$source"
    if [[ -f $target ]]; then cp -p -- "$target" sshd-rule.previous; fi
    install -m 644 "$source" "$target"
    if ! sshd -t || ! systemctl reload ssh; then
        if [[ -f sshd-rule.previous ]]; then
            mv -f sshd-rule.previous "$target"
        else
            rm -f -- "$target"
        fi
        die 'The new SSH rule could not be applied.'
    fi
    rm -f sshd-rule.previous
}

origin_init() {
    packages ca-certificates curl openssh-client openssh-server python3 python3-qrcode nftables
    ensure_account traveler-origin
    install -d -m 700 -o traveler-origin -g traveler-origin /etc/traveler-origin
    local binary=/usr/local/bin/traveler-sing-box
    if [[ ! -x $binary ]]; then
        curl -fLsS --retry 2 -o sing-box.tar.gz \
            https://github.com/SagerNet/sing-box/releases/download/v1.14.4/sing-box-1.14.4-linux-amd64.tar.gz
        printf '%s\n' '699a1b510aa4396b2176d32d1181920399ebf1e267835da6f3b598d0f7c24968  sing-box.tar.gz' |
            sha256sum -c -
        tar -xzf sing-box.tar.gz sing-box-1.14.4-linux-amd64/sing-box
        install -m 755 sing-box-1.14.4-linux-amd64/sing-box "$binary"
    fi
    local link_key=/var/lib/traveler-origin/.ssh/edge_ed25519
    if [[ ! -s $link_key ]]; then
        runuser -u traveler-origin -- ssh-keygen -q -t ed25519 -N '' \
            -C traveler-origin-reverse-link -f "$link_key"
    fi
    python3 configure.py origin
    chown traveler-origin:traveler-origin /etc/traveler-origin/config.json
    chmod 600 /etc/traveler-origin/config.json
    "$binary" check -c /etc/traveler-origin/config.json
    install -m 644 traveler-origin.service /etc/systemd/system/
    systemctl daemon-reload
    systemctl enable --now traveler-origin.service
    systemctl is-active traveler-origin.service
}

edge_install() {
    local caddy_config=/etc/caddy/Caddyfile
    if [[ -f $caddy_config ]] && ! grep -Fxq '# Managed by Traveler split installer' "$caddy_config"; then
        local stock_hash current_hash
        stock_hash=$(dpkg-query -W -f='${Conffiles}' caddy 2>/dev/null |
            awk '$1 == "/etc/caddy/Caddyfile" {print $2}') || true
        current_hash=$(md5sum "$caddy_config" | cut -d ' ' -f 1)
        [[ -n ${stock_hash:-} && $stock_hash == "$current_hash" ]] ||
            die 'Existing custom Caddyfile. Use a clean edge or merge the Traveler routes manually.'
    fi
    packages ca-certificates caddy openssh-server python3
    ensure_account traveler-link
    add_key traveler-link origin.pub 'restrict,port-forwarding,permitlisten="127.0.0.1:18443"' traveler-origin
    install_sshd_rule 90-traveler-link.conf
    if [[ -d site ]]; then
        install -d -m 755 /srv/traveler-edge-site
        cp -R -- site/. /srv/traveler-edge-site/
        chmod -R a+rX /srv/traveler-edge-site
    fi
    python3 configure.py edge
    chmod 640 Caddyfile
    caddy validate --config "$PWD/Caddyfile" --adapter caddyfile
    cp -p -- "$caddy_config" Caddyfile.previous
    install -m 640 -o root -g caddy Caddyfile "$caddy_config"
    if ! systemctl enable --now caddy || ! systemctl reload caddy; then
        cp -p -- Caddyfile.previous "$caddy_config"
        systemctl reload caddy || true
        die 'Caddy could not apply the new configuration.'
    fi
}

panel_health() {
    local since=$1 attempt
    for attempt in {1..25}; do
        if systemctl is-active --quiet traveler-panel-traffic.service &&
            python3 -c 'from datetime import datetime; from pathlib import Path; import json, sys
data = json.loads(Path("/var/lib/traveler-panel-traffic/stats.json").read_text())
sys.exit(datetime.fromisoformat(data["updated_at"]).timestamp() < int(sys.argv[1]))' "$since" 2>/dev/null &&
            curl --silent --fail --max-time 2 --unix-socket /run/traveler-panel/panel.sock \
            -H 'Host: localhost:8765' http://localhost/api/state | python3 -c \
            'import json, sys; sys.exit(not json.load(sys.stdin).get("telemetry"))' 2>/dev/null; then
            return 0
        fi
        sleep 2
    done
    return 1
}

upgrade_panel() {
    local origin=/etc/traveler-origin/config.json binary=/usr/local/bin/traveler-sing-box
    local panel=/opt/traveler-panel units=/etc/systemd/system
    local backup file candidate since dependency previous_panel=0 previous_traffic=0 changed_origin=0
    local files=(
        "$origin" "$binary" "$panel/panel.py" "$panel/panel_state.py" "$panel/panel_traffic.py"
        "$panel/panel_probe.py" "$panel/panel_diagnostics.py"
        "$units/traveler-panel.service" "$units/traveler-panel-traffic.service"
        /etc/traveler-panel/probe-egress-ip
        /var/lib/traveler-panel/state.json
    )
    [[ -x $binary && -s $origin && -s /etc/traveler-panel/profile.json ]] ||
        die 'The existing origin and panel profile template must be installed first.'
    systemctl is-active --quiet traveler-origin.service || die 'The origin is not active.'
    for file in panel-ui/*; do files+=("$panel/$file"); done
    local missing=()
    for dependency in ca-certificates curl git python3-grpcio python3-protobuf python3-qrcode; do
        if [[ $(dpkg-query -W -f='${db:Status-Status}' "$dependency" 2>/dev/null) != installed ]]; then
            missing+=("$dependency")
        fi
    done
    if ((${#missing[@]})); then packages "${missing[@]}"; fi
    if ! id traveler-panel-traffic >/dev/null 2>&1; then
        useradd --system --user-group --no-create-home --home-dir /nonexistent \
            --shell /usr/sbin/nologin traveler-panel-traffic
    fi
    candidate=$binary
    if [[ $("$binary" version) != *with_v2ray_api* ]]; then
        candidate="$PWD/sing-box-traffic.candidate"
        if [[ -f $candidate ]]; then
            chmod 700 "$candidate"
            local version_info
            version_info=$("$candidate" version)
            [[ $version_info == *'sing-box version 1.14.4'* &&
                $version_info == *'c005f35300ce76427dc285481ec5695f5b179cd2'* &&
                $version_info == *with_v2ray_api* ]] || die 'Invalid prebuilt statistics candidate.'
        else
            nice -n 10 bash build-traffic-sing-box.sh "$candidate"
        fi
    fi
    python3 -m py_compile panel.py panel_state.py panel_traffic.py panel_probe.py panel_diagnostics.py

    install -d -m 700 /var/backups/traveler-panel
    backup=$(mktemp -d /var/backups/traveler-panel/upgrade.XXXXXXXX)
    install -d -m 700 "$backup/files"
    if systemctl is-active --quiet traveler-panel.service; then previous_panel=1; fi
    if systemctl is-active --quiet traveler-panel-traffic.service; then previous_traffic=1; fi

    rollback_panel_upgrade() {
        local status=$?
        trap - EXIT
        if ((status)); then
            set +e
            systemctl stop traveler-panel.service traveler-panel-traffic.service 2>/dev/null
            while IFS= read -r file; do
                if ((!changed_origin)) && [[ $file == "$origin" || $file == "$binary" ]]; then continue; fi
                if [[ -e "$backup/files$file" ]]; then
                    cp -a --remove-destination -- "$backup/files$file" "$file"
                else
                    rm -f -- "$file"
                fi
            done < "$backup/paths"
            systemctl daemon-reload
            if ((changed_origin)); then systemctl restart traveler-origin.service; fi
            if ((previous_panel)); then systemctl start traveler-panel.service; fi
            if ((previous_traffic)); then systemctl start traveler-panel-traffic.service; fi
            printf 'Upgrade failed. Previous files restored from %s. Check service status.\n' "$backup" >&2
        fi
        exit "$status"
    }

    # Freeze panel writes while capturing the active configuration and releasing the new code.
    : > "$backup/paths"
    trap rollback_panel_upgrade EXIT
    if ((previous_panel)); then systemctl stop traveler-panel.service; fi
    if ((previous_traffic)); then systemctl stop traveler-panel-traffic.service; fi
    [[ ! -e /var/lib/traveler-panel/pending.json ]] ||
        die 'The panel has an unfinished configuration change; restart it and resolve that first.'
    for file in "${files[@]}"; do
        if [[ -e $file ]]; then cp -a --parents "$file" "$backup/files/" || die 'Could not back up the current panel.'; fi
        printf '%s\n' "$file" >> "$backup/paths"
    done
    python3 configure.py traffic
    "$candidate" check -c "$PWD/traffic-config.json"

    install -d -m 755 "$panel" "$panel/panel-ui"
    install -m 644 panel.py panel_state.py panel_traffic.py panel_probe.py panel_diagnostics.py "$panel/"
    install -m 644 panel-ui/* "$panel/panel-ui/"
    install -m 644 traveler-panel.service traveler-panel-traffic.service "$units/"
    if [[ -f probe-egress-ip ]]; then
        install -m 600 probe-egress-ip /etc/traveler-panel/probe-egress-ip
    fi
    if [[ $candidate != "$binary" ]]; then
        changed_origin=1
        install -m 755 "$candidate" "$binary.next"
        mv -f -- "$binary.next" "$binary"
    fi
    if ! python3 -c 'import json, sys
with open(sys.argv[1]) as current, open(sys.argv[2]) as candidate:
    sys.exit(json.load(current) != json.load(candidate))' "$origin" traffic-config.json; then
        changed_origin=1
        install -m 600 -o traveler-origin -g traveler-origin traffic-config.json "$origin.next"
        mv -f -- "$origin.next" "$origin"
    fi
    systemctl daemon-reload
    if ((changed_origin)); then systemctl restart traveler-origin.service; fi
    since=$(date +%s)
    systemctl start traveler-panel-traffic.service traveler-panel.service
    panel_health "$since" || die 'The panel or its traffic API did not respond.'
    systemctl enable traveler-panel.service traveler-panel-traffic.service
    printf 'Panel and traffic collector verified. Backup: %s\n' "$backup"
    trap - EXIT
}

origin_finish() {
    ensure_account traveler-panel-access
    ensure_account traveler-panel-ios
    install -d -m 755 /opt/traveler-panel
    install -d -m 700 /etc/traveler-panel
    install -m 600 -o traveler-origin -g traveler-origin edge.known_hosts /var/lib/traveler-origin/.ssh/known_hosts
    if [[ -f desktop.pub ]]; then
        add_key traveler-panel-access desktop.pub 'restrict,port-forwarding' traveler-desktop
    fi
    if [[ -f iphone.pub ]]; then
        add_key traveler-panel-ios iphone.pub 'restrict,port-forwarding' traveler-iphone
    fi
    install_sshd_rule 60-traveler-panel.conf
    install_sshd_rule 61-traveler-panel-ios.conf
    if ! getent ahostsv4 traveler-panel >/dev/null; then
        printf '127.0.0.1 traveler-panel\n' >> /etc/hosts
    fi
    [[ $(getent ahostsv4 traveler-panel | awk '{print $1}' | sort -u) == 127.0.0.1 ]] ||
        die 'The name traveler-panel must resolve to 127.0.0.1.'
    python3 configure.py finish
    install -m 600 profile.json /etc/traveler-panel/profile.json
    nft -c -f traveler-panel-ios-guard.nft
    install -m 600 traveler-panel-ios-guard.nft /etc/traveler-panel/ios-guard.nft
    install -m 644 traveler-panel-ios.service traveler-panel-ios-guard.service traveler-panel-ios.socket /etc/systemd/system/
    install -m 644 traveler-edge-link.service /etc/systemd/system/
    systemctl daemon-reload
    systemctl restart traveler-panel-ios-guard.service
    systemctl enable --now traveler-panel-ios.socket traveler-edge-link.service
    systemctl restart traveler-edge-link.service
    upgrade_panel
    systemctl is-active traveler-origin traveler-panel traveler-edge-link traveler-panel-ios.socket
}

case ${1:-} in
    origin-init) origin_init ;;
    edge-install) edge_install ;;
    origin-finish) origin_finish ;;
    upgrade-panel) upgrade_panel ;;
    add-desktop-key) ensure_account traveler-panel-access; add_key traveler-panel-access desktop.pub 'restrict,port-forwarding' traveler-desktop ;;
    add-iphone-key) ensure_account traveler-panel-ios; add_key traveler-panel-ios iphone.pub 'restrict,port-forwarding' traveler-iphone ;;
    *) die 'Use split/install.py from the administrator computer.' ;;
esac
