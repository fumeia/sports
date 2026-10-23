#!/usr/bin/env bash
# livesync/add-user.sh NAME — a login for one collaborator (or for yourself:
# use your own login in your Obsidian apps, keep "admin" for managing).
# Prints the four values they enter in Obsidian's LiveSync plugin.
#   bash /opt/vb/livesync/add-user.sh anna
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DIR=/opt/livesync; DB=vault
[ "$(id -u)" -eq 0 ] || { echo "Run as root."; exit 1; }
NAME="${1:-}"
[[ "$NAME" =~ ^[a-z0-9][a-z0-9_-]{1,30}$ ]] || { echo "Usage: add-user.sh <name>  (lowercase letters, digits, - or _)"; exit 1; }
[ "$NAME" != admin ] || { echo "'admin' is your management login."; exit 1; }
[ -f "$DIR/.env" ] || { echo "Run livesync/setup.sh first."; exit 1; }
PW="$(sed -n 's/^COUCHDB_PASSWORD=//p' "$DIR/.env")"
U="http://127.0.0.1:5984/_users/org.couchdb.user:$NAME"
NEWPW="$(python3 -c 'import secrets;print(secrets.token_urlsafe(15))')"

rev="$(curl -sS -u "admin:$PW" "$U" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("_rev",""))')"
if [ -n "$rev" ]; then
  read -rp "$NAME already exists. Give them a new password? Type yes: " a
  [ "$a" = yes ] || exit 0
fi
body="$(NAME="$NAME" NEWPW="$NEWPW" REV="$rev" python3 -c '
import json, os
d = {"_id": "org.couchdb.user:" + os.environ["NAME"], "name": os.environ["NAME"], "type": "user",
     "roles": [], "password": os.environ["NEWPW"]}
if os.environ["REV"]: d["_rev"] = os.environ["REV"]
print(json.dumps(d))')"
curl -fsS -u "admin:$PW" -X PUT -H 'Content-Type: application/json' "$U" -d "$body" >/dev/null
python3 "$HERE/security.py" "$PW" "$DB" add "$NAME" >/dev/null

echo
echo "  For $NAME — Obsidian → Settings → Community plugins → \"Self-hosted LiveSync\":"
echo "    Server URI : https://$(cat "$DIR/site")"
echo "    Username   : $NAME"
echo "    Password   : $NEWPW"
echo "    Database   : $DB"
echo "  Send these privately (not in a group chat). Remove access: bash /opt/vb/livesync/remove-user.sh $NAME"
