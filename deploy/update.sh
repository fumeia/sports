#!/usr/bin/env bash
# deploy/update.sh — get the latest vb from GitHub and re-apply it.
#   bash /opt/vb/deploy/update.sh
set -euo pipefail
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[ "$(id -u)" -eq 0 ] || { echo "Run as root."; exit 1; }
before="$(git -C "$APP_DIR" rev-parse --short HEAD)"
git -C "$APP_DIR" pull --ff-only
after="$(git -C "$APP_DIR" rev-parse --short HEAD)"
echo "vb: $before -> $after"
exec bash "$APP_DIR/deploy/setup.sh" --update
