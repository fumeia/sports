#!/usr/bin/env bash
# deploy/asvz-login.sh — save (or change) the university login the bot uses,
# on the server only, in <bot folder>/.asvz-bot.json (readable by the bot's
# user only). Same file and format the bot and desktop app use.
#   bash /opt/vb/deploy/asvz-login.sh
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Run as root."; exit 1; }
. /var/lib/vb/setup.conf 2>/dev/null || { echo "Run deploy/setup.sh first."; exit 1; }
F="$SAVED_BOT_DIR/.asvz-bot.json"
U="$(stat -c %U "$SAVED_BOT_DIR")"
if [ -f "$F" ]; then
  cp -p "$F" "/var/backups/vb/asvz-bot.json.$(date +%Y%m%d-%H%M%S)"
  echo "(old login file backed up to /var/backups/vb/)"
fi
python3 - "$F" <<'PY'
import getpass, json, os, sys
orgs = ["ETH Zürich", "Universität Zürich",
        "ZHAW - Zürcher Hochschule für Angewandte Wissenschaften",
        "PH Zürich - Pädagogische Hochschule Zürich", "ASVZ", "SWITCH edu-ID"]
for i, o in enumerate(orgs, 1):
    print(f"  {i}) {o}")
org = orgs[int(input("Organisation number: ")) - 1]
user = input("Email / username: ").strip()
pw = getpass.getpass("Password (not shown): ")
fd = os.open(sys.argv[1] + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f:
    json.dump({"organisation": org, "username": user, "password": pw}, f)
os.replace(sys.argv[1] + ".tmp", sys.argv[1])
print("Saved.")
PY
chown "$U" "$F"; chmod 600 "$F"
