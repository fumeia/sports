#!/usr/bin/env bash
# deploy/rollback.sh — remove vb again: stops the page and moves vb.caddy
# aside (your Caddyfile isn't edited; it tells you which line to remove).
# The bot was never changed, so nothing there needs restoring (sign-ups
# queued from the phone stay in its job list; remove them in the text
# menu if you don't want them). Moves things aside; deletes nothing.
#   bash /opt/vb/deploy/rollback.sh
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Run as root."; exit 1; }
TS="$(date +%Y%m%d-%H%M%S)"; BK=/var/backups/vb; mkdir -p "$BK"
read -rp "Stop vb? Type yes: " a; [ "$a" = yes ] || exit 1

systemctl disable --now vb-web 2>/dev/null || true
if grep -qE '^\s*import\s+(/etc/caddy/)?vb\.caddy' /etc/caddy/Caddyfile; then
  # Your Caddyfile is yours to edit; vb.caddy stays until the import is gone
  # (Caddy refuses to load a Caddyfile that imports a missing file).
  echo "Caddy: remove the line 'import /etc/caddy/vb.caddy' from /etc/caddy/Caddyfile, then:"
  echo "  caddy validate --config /etc/caddy/Caddyfile && systemctl reload caddy"
  echo "  mv /etc/caddy/vb.caddy $BK/vb.caddy.$TS"
elif [ -f /etc/caddy/vb.caddy ]; then
  mv /etc/caddy/vb.caddy "$BK/vb.caddy.$TS" && echo "Moved /etc/caddy/vb.caddy to $BK/"
fi
[ -f /etc/sudoers.d/vb-web ] && mv /etc/sudoers.d/vb-web "$BK/sudoers-vb-web.$TS"
echo "Done. vb-web.service stays in /etc/systemd/system (disabled); backups in $BK."
