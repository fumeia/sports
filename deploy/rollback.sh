#!/usr/bin/env bash
# deploy/rollback.sh — go back to how the bot ran before vb.
# Stops vb, puts the old scheduler back, turns the old per-user timer on
# again and takes vb out of Caddy. Moves things aside; deletes nothing.
#   bash /opt/vb/deploy/rollback.sh
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Run as root."; exit 1; }
. /var/lib/vb/setup.conf || { echo "No /var/lib/vb/setup.conf — nothing to roll back."; exit 1; }
TS="$(date +%Y%m%d-%H%M%S)"; BK=/var/backups/vb; mkdir -p "$BK"
read -rp "Stop vb and restore the old scheduler? Type yes: " a; [ "$a" = yes ] || exit 1

systemctl disable --now vb-web vb-scheduler.timer 2>/dev/null || true
U="$(stat -c %U "$SAVED_BOT_DIR")"
old="$(ls -t "$SAVED_BOT_DIR"/asvz_scheduler.py.before-vb-* 2>/dev/null | head -1 || true)"
if [ -n "$old" ]; then
  mv "$SAVED_BOT_DIR/asvz_scheduler.py" "$BK/asvz_scheduler.py.vb.$TS"
  cp -p "$old" "$SAVED_BOT_DIR/asvz_scheduler.py"
  echo "Old scheduler restored from $old"
fi
if [ -f "$(getent passwd "$U" | cut -d: -f6)/.config/systemd/user/asvz-scheduler.timer" ]; then
  systemctl --user -M "$U@" enable --now asvz-scheduler.timer && echo "Old asvz-scheduler.timer on again" \
    || echo "Turn the old timer on yourself: systemctl --user -M $U@ enable --now asvz-scheduler.timer"
fi
if grep -qE '^\s*import\s+/etc/caddy/vb\.caddy' /etc/caddy/Caddyfile; then
  cp -p /etc/caddy/Caddyfile "$BK/Caddyfile.$TS"
  sed -i -E 's|^(\s*import\s+/etc/caddy/vb\.caddy)|# \1  (vb rolled back)|' /etc/caddy/Caddyfile
  [ -f /etc/caddy/vb.caddy ] && mv /etc/caddy/vb.caddy "$BK/vb.caddy.$TS"
  if caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null 2>&1; then
    systemctl reload caddy && echo "vb taken out of Caddy"
  else
    cp -p "$BK/Caddyfile.$TS" /etc/caddy/Caddyfile; mv "$BK/vb.caddy.$TS" /etc/caddy/vb.caddy
    echo "caddy validate failed — Caddy left as it was."
  fi
fi
echo "Done. Unit files stay in /etc/systemd/system/vb-* (inactive); backups in $BK."
