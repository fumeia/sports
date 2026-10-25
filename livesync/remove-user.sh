#!/usr/bin/env bash
# livesync/remove-user.sh NAME — take a collaborator's access away. Their
# login stops working at once; the notes they wrote stay in the vault.
#   bash /opt/vb/livesync/remove-user.sh anna
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DIR=/opt/livesync; DB=vault
[ "$(id -u)" -eq 0 ] || { echo "Run as root."; exit 1; }
NAME="${1:-}"
[[ "$NAME" =~ ^[a-z0-9][a-z0-9_-]{1,30}$ ]] && [ "$NAME" != admin ] || { echo "Usage: remove-user.sh <name>"; exit 1; }
PW="$(sed -n 's/^COUCHDB_PASSWORD=//p' "$DIR/.env")"
U="http://127.0.0.1:5984/_users/org.couchdb.user:$NAME"
python3 "$HERE/security.py" "$PW" "$DB" remove "$NAME" >/dev/null
rev="$(curl -sS -u "admin:$PW" "$U" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("_rev",""))')"
[ -n "$rev" ] && curl -fsS -u "admin:$PW" -X DELETE "$U?rev=$rev" >/dev/null
echo "  $NAME can't sync anymore."
