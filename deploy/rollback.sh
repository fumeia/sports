#!/usr/bin/env bash
# deploy/rollback.sh — remove vb again: stops the page and takes it out of
# Caddy. The bot was never changed, so nothing there needs restoring
# (sign-ups queued from the phone stay in its job list; remove them in the
# text menu if you don't want them). Moves things aside; deletes nothing.
#   bash /opt/vb/deploy/rollback.sh
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Run as root."; exit 1; }
TS="$(date +%Y%m%d-%H%M%S)"; BK=/var/backups/vb; mkdir -p "$BK"
read -rp "Stop vb and take it out of Caddy? Type yes: " a; [ "$a" = yes ] || exit 1

systemctl disable --now vb-web 2>/dev/null || true
if grep -qE '^\s*import\s+/etc/caddy/vb\.caddy' /etc/caddy/Caddyfile; then
  cp -p /etc/caddy/Caddyfile "$BK/Caddyfile.$TS"
  sed -i -E 's|^(\s*import\s+/etc/caddy/vb\.caddy)|# \1  (vb rolled back)|' /etc/caddy/Caddyfile
  [ -f /etc/caddy/vb.caddy ] && mv /etc/caddy/vb.caddy "$BK/vb.caddy.$TS"
  if caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null 2>&1; then
    systemctl reload caddy && echo "vb taken out of Caddy"
  else
    cp -p "$BK/Caddyfile.$TS" /etc/caddy/Caddyfile
    [ -f "$BK/vb.caddy.$TS" ] && mv "$BK/vb.caddy.$TS" /etc/caddy/vb.caddy
    echo "caddy validate failed — Caddy left as it was."
  fi
fi
[ -f /etc/sudoers.d/vb-web ] && mv /etc/sudoers.d/vb-web "$BK/sudoers-vb-web.$TS"
echo "Done. vb-web.service stays in /etc/systemd/system (disabled); backups in $BK."
