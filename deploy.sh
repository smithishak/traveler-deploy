#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
if ! command -v python3 >/dev/null 2>&1; then
  echo 'Нужен Python 3: apt-get update && apt-get install -y python3' >&2
  exit 1
fi
exec python3 traveler.py "$@"
