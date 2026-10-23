#!/usr/bin/env bash
# deploy/setup.sh — install "vb", the phone remote for the ASVZ bot.
#
#   bash /opt/vb/deploy/setup.sh              first install (asks questions)
#   bash /opt/vb/deploy/setup.sh --update     re-apply after git pull (no questions;
#                                             deploy/update.sh calls this)
# Options: --bot-dir DIR  --domain vb.example.com  --port 8710
#
# What it does (nothing is deleted; replaced files are backed up first):
#   1. finds the bot folder and checks its files are a version we know
#   2. backs up the bot folder's code + data  -> /var/backups/vb/
#   3. puts the desktop app's asvz_scheduler.py + asvz_plan.py and a
#      server keep_awake.py into the bot folder (asvz_bot.py is untouched)
#   4. installs vb-scheduler.timer (the 2-minute check, as a system service)
#      and turns off the old per-user asvz-scheduler.timer (files kept)
#   5. installs vb-web.service on 127.0.0.1 and /etc/caddy/vb.caddy,
#      runs caddy validate, reloads Caddy
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_DIR=/var/lib/vb
CONF="$DATA_DIR/setup.conf"
BACKUP_ROOT=/var/backups/vb
TS="$(date +%Y%m%d-%H%M%S)"

# sha256 of bot files we know (from the "Server version" and "V1" releases)
SHA_BOT_KNOWN="422454b03640cde353cf21197d678495ce2c7154d0fcfc511d38e9c1c8e032d7"
SHA_SCHED_SERVER="67844f836b60bf5a443371ef5807c82b31cad43c1ea3d6b0de178f8d833af6ed"

info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m ok\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m !!\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31mERR\033[0m %s\n' "$*" >&2; exit 1; }

UPDATE=0; BOT_DIR=""; DOMAIN=""; PORT=""
while [ $# -gt 0 ]; do
  case "$1" in
    --update) UPDATE=1 ;;
    --bot-dir) BOT_DIR="$2"; shift ;;
    --domain) DOMAIN="$2"; shift ;;
    --port) PORT="$2"; shift ;;
    *) die "Unknown option $1" ;;
  esac
  shift
done

[ "$(id -u)" -eq 0 ] || die "Run as root (your prompt should be root@...)."
command -v python3 >/dev/null || die "python3 missing"
python3 -c 'import zoneinfo, hashlib; hashlib.scrypt' 2>/dev/null || die "python3 too old (needs 3.9+)"

if [ -f "$CONF" ]; then
  # shellcheck disable=SC1090
  . "$CONF"
fi
[ "$UPDATE" -eq 1 ] && [ ! -f "$CONF" ] && die "No $CONF yet — run setup.sh without --update first."
BOT_DIR="${BOT_DIR:-${SAVED_BOT_DIR:-}}"
DOMAIN="${DOMAIN:-${SAVED_DOMAIN:-}}"
PORT="${PORT:-${SAVED_PORT:-8710}}"

# ---------------------------------------------------------------- 1. bot folder
if [ -z "$BOT_DIR" ]; then
  info "Looking for the bot folder…"
  mapfile -t cands < <(find / -xdev -name asvz_bot.py -not -path '*/venv/*' -not -path '*/.venv/*' \
                         -not -path "$APP_DIR/*" -not -path '/proc/*' -not -path "$BACKUP_ROOT/*" 2>/dev/null \
                       | xargs -r -n1 dirname | sort -u)
  [ ${#cands[@]} -eq 0 ] && die "No asvz_bot.py found. Pass --bot-dir /path/to/asvz-bot/src"
  [ ${#cands[@]} -gt 1 ] && die "Several bot folders found: ${cands[*]} — pass --bot-dir with the one you use."
  BOT_DIR="${cands[0]}"
fi
BOT_DIR="$(readlink -f "$BOT_DIR")"
[ -f "$BOT_DIR/asvz_bot.py" ] || die "$BOT_DIR has no asvz_bot.py"
BOT_USER="$(stat -c %U "$BOT_DIR")"
ok "Bot folder: $BOT_DIR (owner $BOT_USER)"

SCHED="$BOT_DIR/asvz_scheduler.py"
if [ ! -f "$SCHED" ]; then
  [ -f "$BOT_DIR/asvz-scheduler.py" ] && die "Found asvz-scheduler.py (with a dash), not asvz_scheduler.py. Send the inspect.sh output to Claude before going on."
  die "$SCHED not found"
fi

BOT_PYTHON=""
for v in "$BOT_DIR/.venv" "$BOT_DIR/venv" "$BOT_DIR/../.venv" "$BOT_DIR/../venv"; do
  if [ -x "$v/bin/python3" ]; then BOT_PYTHON="$(readlink -f "$v")/bin/python3"; break; fi
done
[ -n "$BOT_PYTHON" ] || die "No venv (.venv or venv) next to the bot."
"$BOT_PYTHON" -c 'import selenium' 2>/dev/null || die "$BOT_PYTHON can't import selenium — is that the right venv?"
ok "Bot Python: $BOT_PYTHON"

sha() { sha256sum "$1" | cut -d' ' -f1; }
NEW_SCHED_SHA="$(sha "$APP_DIR/bot/asvz_scheduler.py")"
CUR_SCHED_SHA="$(sha "$SCHED")"
if [ "$(sha "$BOT_DIR/asvz_bot.py")" != "$SHA_BOT_KNOWN" ]; then
  die "asvz_bot.py on this server differs from the version in the releases. The new scheduler might not fit it. Send the inspect.sh output to Claude."
fi
case "$CUR_SCHED_SHA" in
  "$NEW_SCHED_SHA") SCHED_ACTION=keep ;;
  "$SHA_SCHED_SERVER") SCHED_ACTION=replace ;;
  *) if [ "$UPDATE" -eq 1 ] && [ -f "$DATA_DIR/installed-scheduler.sha256" ] \
        && [ "$CUR_SCHED_SHA" = "$(cat "$DATA_DIR/installed-scheduler.sha256")" ]; then
       SCHED_ACTION=replace   # an older version installed by us
     else
       die "asvz_scheduler.py is a version I don't know (changed by hand?). Not replacing it. Send the inspect.sh output to Claude."
     fi ;;
esac
ok "Bot files checked (scheduler: $SCHED_ACTION)"

# Chrome paths, as the old per-user unit set them (else the bot's defaults)
USER_HOME="$(getent passwd "$BOT_USER" | cut -d: -f6)"
OLD_UNIT_DIR="$USER_HOME/.config/systemd/user"
CHROME_BIN=""; CHROMEDRIVER_BIN=""
if [ -f "$OLD_UNIT_DIR/asvz-scheduler.service" ]; then
  CHROME_BIN="$(sed -n 's/^Environment=CHROME_BIN=//p' "$OLD_UNIT_DIR/asvz-scheduler.service" | head -1)"
  CHROMEDRIVER_BIN="$(sed -n 's/^Environment=CHROMEDRIVER_BIN=//p' "$OLD_UNIT_DIR/asvz-scheduler.service" | head -1)"
fi
for base in "$BOT_DIR" "$BOT_DIR/.."; do
  [ -z "$CHROME_BIN" ] && [ -x "$base/chrome/chrome-linux64/chrome" ] && CHROME_BIN="$(readlink -f "$base/chrome/chrome-linux64/chrome")"
  [ -z "$CHROMEDRIVER_BIN" ] && [ -x "$base/chrome/chromedriver-linux64/chromedriver" ] && CHROMEDRIVER_BIN="$(readlink -f "$base/chrome/chromedriver-linux64/chromedriver")"
done
[ -n "$CHROME_BIN" ] && [ ! -x "$CHROME_BIN" ] && die "Chrome from the old unit not found: $CHROME_BIN"
ok "Chrome: ${CHROME_BIN:-default (/usr/local/bin/google-chrome)}"

if [ ! -f "$BOT_DIR/.asvz-bot.json" ]; then
  warn "No ASVZ login saved in $BOT_DIR/.asvz-bot.json — the bot can't enroll without it."
  warn "After setup run:  bash $APP_DIR/deploy/asvz-login.sh"
fi

# ---------------------------------------------------------------- questions
if [ -z "$DOMAIN" ]; then
  read -rp "Your domain at Spaceship (e.g. example.com; the page goes to vb.<domain>): " base
  base="${base#vb.}"; base="${base%%/*}"; base="${base#https://}"
  [[ "$base" =~ ^[A-Za-z0-9.-]+\.[A-Za-z]{2,}$ ]] || die "That doesn't look like a domain: $base"
  DOMAIN="vb.$base"
fi
if [ "$UPDATE" -eq 0 ]; then
  if ss -ltn "sport = :$PORT" 2>/dev/null | grep -q LISTEN && ! systemctl is-active --quiet vb-web; then
    die "Port $PORT is taken. Pass --port with a free one."
  fi
  echo
  echo "  Bot folder : $BOT_DIR (runs as $BOT_USER)"
  echo "  Scheduler  : $( [ $SCHED_ACTION = replace ] && echo 'replace with the desktop version (old one backed up)' || echo 'already the desktop version')"
  echo "  Page       : https://$DOMAIN  ->  127.0.0.1:$PORT"
  echo "  Old timer  : $( [ -f "$OLD_UNIT_DIR/asvz-scheduler.timer" ] && echo "$BOT_USER's asvz-scheduler.timer gets turned off (files kept)" || echo 'none found')"
  echo
  read -rp "Go ahead? Type yes: " answer
  [ "$answer" = "yes" ] || die "Stopped, nothing changed."
fi

# ---------------------------------------------------------------- 2. backup
mkdir -p "$BACKUP_ROOT" && chmod 700 "$BACKUP_ROOT"
BK="$BACKUP_ROOT/bot-$TS.tar.gz"
tar -czf "$BK" -C "$BOT_DIR" --exclude='./venv' --exclude='./.venv' --exclude='./chrome' \
    --exclude='__pycache__' --exclude='./watcher_logs' .
chmod 600 "$BK"
ok "Backup of the bot folder (code + data, incl. login file): $BK"

# ---------------------------------------------------------------- 3. bot files
install_bot_file() {  # name
  local src="$APP_DIR/bot/$1" dst="$BOT_DIR/$1"
  if [ -f "$dst" ] && cmp -s "$src" "$dst"; then return; fi
  [ -f "$dst" ] && mv "$dst" "$dst.before-vb-$TS"
  install -m 644 -o "$BOT_USER" -g "$(id -gn "$BOT_USER")" "$src" "$dst"
  ok "Installed $1$( [ -f "$dst.before-vb-$TS" ] && echo " (old one: $1.before-vb-$TS)")"
}
[ "$SCHED_ACTION" = replace ] && install_bot_file asvz_scheduler.py
install_bot_file asvz_plan.py
install_bot_file keep_awake.py
mkdir -p "$DATA_DIR"
sha "$SCHED" > "$DATA_DIR/installed-scheduler.sha256"
[ -f "$BOT_DIR/.asvz-bot.json" ] && chmod 600 "$BOT_DIR/.asvz-bot.json"
# Only create missing data files; existing ones are never rewritten here.
[ -f "$BOT_DIR/asvz_schedule.json" ] || { echo '{"jobs": []}' > "$BOT_DIR/asvz_schedule.json"; chown "$BOT_USER" "$BOT_DIR/asvz_schedule.json"; }
[ -f "$BOT_DIR/asvz_watchers.json" ] || { echo '{}' > "$BOT_DIR/asvz_watchers.json"; chown "$BOT_USER" "$BOT_DIR/asvz_watchers.json"; }

# ---------------------------------------------------------------- 4/5. units
chown "$BOT_USER" "$DATA_DIR"; chmod 700 "$DATA_DIR"
cat > "$CONF" <<EOF
SAVED_BOT_DIR='$BOT_DIR'
SAVED_DOMAIN='$DOMAIN'
SAVED_PORT='$PORT'
EOF

render() {  # template -> stdout (values passed as env vars, not pasted into code)
  local chrome_env="" vb_chrome_env="" watcher_env=""
  if [ -n "$CHROME_BIN" ]; then
    chrome_env="Environment=CHROME_BIN=$CHROME_BIN"; vb_chrome_env="Environment=VB_CHROME_BIN=$CHROME_BIN"
  fi
  if [ -n "$CHROMEDRIVER_BIN" ]; then
    chrome_env="$chrome_env"$'\n'"Environment=CHROMEDRIVER_BIN=$CHROMEDRIVER_BIN"
    vb_chrome_env="$vb_chrome_env"$'\n'"Environment=VB_CHROMEDRIVER_BIN=$CHROMEDRIVER_BIN"
  fi
  # Non-root: the scheduler would try "systemd-run --user" for watchers,
  # which has no user session here; KillMode=process keeps them alive anyway.
  [ "$BOT_USER" != root ] && watcher_env="UnsetEnvironment=INVOCATION_ID"
  R_BOT_USER="$BOT_USER" R_BOT_DIR="$BOT_DIR" R_BOT_PYTHON="$BOT_PYTHON" R_APP_DIR="$APP_DIR" \
  R_DATA_DIR="$DATA_DIR" R_PORT="$PORT" R_DOMAIN="$DOMAIN" R_CHROME_ENV="$chrome_env" \
  R_VB_CHROME_ENV="$vb_chrome_env" R_WATCHER_ENV="$watcher_env" python3 -c '
import os, re, sys
s = open(sys.argv[1]).read()
s = re.sub(r"@([A-Z_]+)@", lambda m: os.environ["R_" + m.group(1)], s)
sys.stdout.write(re.sub(r"\n{3,}", "\n\n", s))' "$1"
}

put() {  # content-file target  -> 0 if changed
  if [ -f "$2" ] && cmp -s "$1" "$2"; then rm -f "$1"; return 1; fi
  [ -f "$2" ] && cp -p "$2" "$BACKUP_ROOT/$(basename "$2").$TS"
  mv "$1" "$2"; chmod 644 "$2"; return 0
}

tmp="$(mktemp)"; render "$APP_DIR/deploy/vb-scheduler.service.in" > "$tmp"; put "$tmp" /etc/systemd/system/vb-scheduler.service || true
tmp="$(mktemp)"; cp "$APP_DIR/deploy/vb-scheduler.timer" "$tmp"; put "$tmp" /etc/systemd/system/vb-scheduler.timer || true
tmp="$(mktemp)"; render "$APP_DIR/deploy/vb-web.service.in" > "$tmp"; put "$tmp" /etc/systemd/system/vb-web.service || true
systemd-analyze verify /etc/systemd/system/vb-scheduler.service /etc/systemd/system/vb-web.service 2>&1 \
  | grep -v -E 'Unknown key name|^$' || true
systemctl daemon-reload

if [ "$BOT_USER" != root ]; then
  SUDO_TMP="$(mktemp)"
  cat > "$SUDO_TMP" <<EOF
# Installed by vb deploy/setup.sh: lets the phone page start/stop only these units.
$BOT_USER ALL=(root) NOPASSWD: /usr/bin/systemctl start --no-block vb-scheduler.service, /usr/bin/systemctl enable --now vb-scheduler.timer, /usr/bin/systemctl disable --now vb-scheduler.timer
EOF
  visudo -cf "$SUDO_TMP" >/dev/null || die "sudoers check failed"
  install -m 440 "$SUDO_TMP" /etc/sudoers.d/vb-web; rm -f "$SUDO_TMP"
  usermod -aG systemd-journal "$BOT_USER"
  ok "sudo rule /etc/sudoers.d/vb-web (only the vb-scheduler start/stop commands)"
fi

# New timer on first, then the old one off (one scheduler run at a time is
# guaranteed by the scheduler's own lock file anyway).
systemctl enable --now vb-scheduler.timer >/dev/null
ok "vb-scheduler.timer on: $(systemctl show vb-scheduler.timer -p NextElapseUSecRealtime --value)"
if [ -f "$OLD_UNIT_DIR/asvz-scheduler.timer" ]; then
  if systemctl --user -M "$BOT_USER@" disable --now asvz-scheduler.timer >/dev/null 2>&1; then
    ok "Old per-user asvz-scheduler.timer turned off (unit files kept in $OLD_UNIT_DIR)"
  else
    warn "Couldn't turn off $BOT_USER's asvz-scheduler.timer automatically. Both timers running is safe (lock file), but turn the old one off later:"
    warn "  systemctl --user -M $BOT_USER@ disable --now asvz-scheduler.timer"
  fi
fi

# ---------------------------------------------------------------- page login
if ! python3 -c 'import json,sys; assert json.load(open(sys.argv[1]))["hash"]' "$DATA_DIR/auth.json" 2>/dev/null; then
  info "Choose the login for the phone page (not your university login):"
  VB_DATA_DIR="$DATA_DIR" python3 "$APP_DIR/web/server.py" set-password
fi
chown -R "$BOT_USER" "$DATA_DIR"

systemctl enable vb-web >/dev/null 2>&1
systemctl restart vb-web
for _ in $(seq 1 20); do
  curl -fsS "http://127.0.0.1:$PORT/api/me" >/dev/null 2>&1 && break
  sleep 0.5
done
curl -fsS "http://127.0.0.1:$PORT/api/me" >/dev/null || die "vb-web did not start. See: journalctl -u vb-web -n 50 --no-pager"
ok "vb-web running on 127.0.0.1:$PORT"

# ---------------------------------------------------------------- Caddy
CADDYFILE=/etc/caddy/Caddyfile
[ -f "$CADDYFILE" ] || die "$CADDYFILE not found"
tmp="$(mktemp)"; render "$APP_DIR/deploy/vb.caddy.in" > "$tmp"
caddy_changed=0
cp -p "$CADDYFILE" "$BACKUP_ROOT/Caddyfile.$TS"
[ -f /etc/caddy/vb.caddy ] && cp -p /etc/caddy/vb.caddy "$BACKUP_ROOT/vb.caddy.$TS"
if put "$tmp" /etc/caddy/vb.caddy; then caddy_changed=1; fi
if ! grep -qE '^\s*import\s+(/etc/caddy/)?(vb\.caddy|\*\.caddy)\s*$' "$CADDYFILE"; then
  printf '\nimport /etc/caddy/vb.caddy\n' >> "$CADDYFILE"
  caddy_changed=1
fi
if [ "$caddy_changed" -eq 1 ]; then
  if caddy validate --config "$CADDYFILE" --adapter caddyfile >/tmp/vb-caddy-validate.log 2>&1; then
    systemctl reload caddy
    ok "Caddy validated and reloaded (backup: $BACKUP_ROOT/Caddyfile.$TS)"
  else
    cp -p "$BACKUP_ROOT/Caddyfile.$TS" "$CADDYFILE"
    if [ -f "$BACKUP_ROOT/vb.caddy.$TS" ]; then cp -p "$BACKUP_ROOT/vb.caddy.$TS" /etc/caddy/vb.caddy
    else mv /etc/caddy/vb.caddy "$BACKUP_ROOT/vb.caddy.rejected.$TS"; fi
    tail -20 /tmp/vb-caddy-validate.log
    die "caddy validate failed — Caddy config put back as it was. Send the lines above to Claude."
  fi
else
  ok "Caddy config unchanged"
fi

# ---------------------------------------------------------------- done
ip="$(getent ahostsv4 "$DOMAIN" 2>/dev/null | awk 'NR==1{print $1}' || true)"
mine="$(hostname -I 2>/dev/null || true)"
echo
if [ -z "$ip" ]; then
  warn "$DOMAIN doesn't resolve yet. At Spaceship add an A record: host 'vb' -> this server's IP. Caddy gets the certificate once it resolves."
elif [[ " $mine " != *" $ip "* ]]; then
  warn "$DOMAIN points to $ip, which isn't this server ($mine). Fix the A record at Spaceship."
else
  ok "$DOMAIN points to this server"
fi
echo
echo "  Open https://$DOMAIN on your phone, log in, then browser menu -> 'Add to Home screen'."
echo "  Logs: journalctl -u vb-web -n 50 --no-pager    journalctl -u vb-scheduler -n 50 --no-pager"
