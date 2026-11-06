#!/usr/bin/env bash
# Only for fresh installs, never doctor/update/repeated install.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
. /etc/os-release
[[ "$ID" == ubuntu && ( "$VERSION_ID" == 22.04 || "$VERSION_ID" == 24.04 ) ]] || {
  echo 'Поддерживаются Ubuntu 22.04 и 24.04.' >&2; exit 1;
}
apt-get update -qq
apt-get install -y -qq curl ca-certificates software-properties-common dkms "linux-headers-$(uname -r)"
if ! command -v docker >/dev/null 2>&1; then
  installer=$(mktemp)
  trap 'rm -f "$installer"' EXIT
  curl -fsSL https://get.docker.com -o "$installer"
  sh "$installer"
fi
systemctl enable --now docker
docker compose version >/dev/null
if ! modprobe amneziawg; then
  add-apt-repository -y ppa:amnezia/ppa
  apt-get update -qq
  apt-get install -y amneziawg-dkms amneziawg-tools
  modprobe amneziawg
fi
echo amneziawg > /etc/modules-load.d/amneziawg.conf
