#!/usr/bin/env bash
# livesync/setup.sh — a shared Obsidian vault for you and your collaborators,
# synced through your server with the "Self-hosted LiveSync" plugin.
#
#   bash /opt/vb/livesync/setup.sh        install (asks the subdomain), or re-apply
#
# What it sets up (nothing else on the server is changed):
#   /opt/livesync/            CouchDB in Docker, data in ./data, port only on 127.0.0.1:5984
#   database "vault"          the shared vault; you are its admin
#   /etc/caddy/livesync.caddy HTTPS for <sub>.<your domain> (you add the import line)
#   livesync-backup.timer     nightly copy of the data to /var/backups/livesync (14 days)
# Collaborators: livesync/add-user.sh NAME / remove-user.sh NAME
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DIR=/opt/livesync
DB=vault
BK=/var/backups/livesync
ok()   { printf '\033[1;32m ok\033[0m %s\n' "$*"; }
info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31mERR\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Run as root."
command -v docker >/dev/null || die "Docker is missing."
docker compose version >/dev/null 2>&1 || die "docker compose is missing."

# ---- domain
if [ -f "$DIR/site" ]; then
  SITE="$(cat "$DIR/site")"
else
  base=""
  [ -f /var/lib/vb/setup.conf ] && base="$(. /var/lib/vb/setup.conf; echo "${SAVED_DOMAIN#vb.}")"
  read -rp "Subdomain for the sync service [sync]: " sub
  sub="${sub:-sync}"
  read -rp "Your domain [${base:-example.com}]: " dom
  dom="${dom:-$base}"
  SITE="$sub.$dom"
  [[ "$SITE" =~ ^[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$ ]] || die "That doesn't look like a domain: $SITE"
  echo
  echo "  Sync service: https://$SITE  (add an A record at Spaceship: $sub -> this server's IP)"
  echo "  Files: $DIR, /etc/caddy/livesync.caddy, livesync-backup.timer"
  read -rp "Go ahead? Type yes: " a
  [ "$a" = yes ] || die "Stopped, nothing changed."
fi

mkdir -p "$DIR/data" && chmod 750 "$DIR"
echo "$SITE" > "$DIR/site"

# ---- admin password (made once, kept in .env, readable by root only)
if [ ! -f "$DIR/.env" ]; then
  umask 077
  echo "COUCHDB_PASSWORD=$(python3 -c 'import secrets;print(secrets.token_urlsafe(24))')" > "$DIR/.env"
  umask 022
  NEW_ADMIN=1
fi
PW="$(sed -n 's/^COUCHDB_PASSWORD=//p' "$DIR/.env")"

cp "$HERE/local.ini" "$DIR/local.ini"
cp "$HERE/docker-compose.yml" "$DIR/docker-compose.yml"
# CouchDB's start script makes everything under its folders belong to its
# user (uid 5984) and quits silently if it can't, so the settings file
# must be theirs and writable (a read-only mount made it restart forever).
chown -R 5984:5984 "$DIR/data" "$DIR/local.ini"
(cd "$DIR" && docker compose config -q) || die "docker compose rejected the config."
(cd "$DIR" && docker compose up -d --force-recreate 2>&1 | tail -2)

info "Waiting for CouchDB (up to 2 minutes)…"
for _ in $(seq 1 120); do
  curl -fsS -u "admin:$PW" http://127.0.0.1:5984/_up >/dev/null 2>&1 && break
  sleep 1
done
if ! curl -fsS -u "admin:$PW" http://127.0.0.1:5984/_up >/dev/null 2>&1; then
  echo "--- container state:"
  docker inspect livesync-couchdb --format \
    'status={{.State.Status}} exit={{.State.ExitCode}} restarts={{.RestartCount}} oom={{.State.OOMKilled}} error={{.State.Error}}' 2>&1
  echo "--- container log (last 40 lines):"
  docker logs --tail 40 livesync-couchdb 2>&1 | sed -E 's/(PASSWORD=)[^ ]+/\1***/g'
  echo "--- what answers on 127.0.0.1:5984:"
  curl -sS -m 5 http://127.0.0.1:5984/ 2>&1 | head -3
  die "CouchDB didn't answer. Paste the lines above to Claude."
fi
ok "CouchDB running on 127.0.0.1:5984"

cdb() { curl -sS -u "admin:$PW" -H 'Content-Type: application/json' "$@"; }
for sysdb in _users _replicator; do cdb -X PUT "http://127.0.0.1:5984/$sysdb" >/dev/null; done
code="$(curl -s -o /dev/null -w '%{http_code}' -u "admin:$PW" "http://127.0.0.1:5984/$DB")"
if [ "$code" = 404 ]; then cdb -X PUT "http://127.0.0.1:5984/$DB" >/dev/null && ok "Created database $DB"; fi
# Only you are admin of the vault; collaborators get added as members (add-user.sh).
python3 "$HERE/security.py" "$PW" "$DB" >/dev/null && ok "Database $DB: you are its admin"

# ---- Caddy: only our own file; the Caddyfile is yours
tmp="$(mktemp)"
printf '# Installed by livesync/setup.sh — HTTPS for the Obsidian LiveSync service.\n%s {\n\treverse_proxy 127.0.0.1:5984\n}\n' "$SITE" > "$tmp"
if ! cmp -s "$tmp" /etc/caddy/livesync.caddy 2>/dev/null; then
  mv "$tmp" /etc/caddy/livesync.caddy && chmod 644 /etc/caddy/livesync.caddy
  if grep -qE '^\s*import\s+(/etc/caddy/)?livesync\.caddy' /etc/caddy/Caddyfile; then
    caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null 2>&1 && systemctl reload caddy && ok "Caddy reloaded"
  fi
else rm -f "$tmp"; fi

# ---- nightly backup
mkdir -p "$BK" && chmod 700 "$BK"
cat > /etc/systemd/system/livesync-backup.service <<EOF
[Unit]
Description=Back up the Obsidian LiveSync data (14 days kept)
[Service]
Type=oneshot
ExecStart=/bin/sh -c 'tar -czf $BK/vault-\$(date +%%F).tar.gz -C $DIR data && find $BK -name "vault-*.tar.gz" -mtime +14 -delete'
EOF
cat > /etc/systemd/system/livesync-backup.timer <<'EOF'
[Unit]
Description=Nightly backup of the Obsidian LiveSync data
[Timer]
OnCalendar=*-*-* 03:30:00
Persistent=true
[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload && systemctl enable --now livesync-backup.timer >/dev/null 2>&1 && ok "Nightly backup at 03:30 to $BK"

echo
if ! grep -qE '^\s*import\s+(/etc/caddy/)?livesync\.caddy' /etc/caddy/Caddyfile; then
  info "Last step in Caddy (setup doesn't edit your Caddyfile):"
  echo "  1. add this line to /etc/caddy/Caddyfile, next to your other import lines:"
  echo "       import /etc/caddy/livesync.caddy"
  echo "  2. caddy validate --config /etc/caddy/Caddyfile"
  echo "  3. systemctl reload caddy"
  echo
fi
echo "  Server URL : https://$SITE"
echo "  Database   : $DB"
echo "  Your login : admin"
if [ "${NEW_ADMIN:-0}" = 1 ]; then
  echo "  Password   : $PW"
  echo "  (shown once; it's also in $DIR/.env, readable by root only)"
else
  echo "  Password   : in $DIR/.env (COUCHDB_PASSWORD)"
fi
echo
echo "  Collaborators: bash /opt/vb/livesync/add-user.sh <name>"
