#!/usr/bin/env bash
# deploy/setup.sh — install "vb", the phone remote for the ASVZ bot.
#
#   bash /opt/vb/deploy/setup.sh              first install (asks questions)
#   bash /opt/vb/deploy/setup.sh --update     re-apply after git pull (no questions;
#                                             deploy/update.sh calls this)
# Options: --bot-dir DIR  --domain vb.example.com  --port 8710
#
# The bot itself is left exactly as it is: no file in the bot folder is
# replaced, added or chmod-ed, and its timer stays the one that runs it.
# What it does:
#   1. finds the bot folder, its venv, its scheduler script and its timer
#   2. backs up the bot folder's code + data  -> /var/backups/vb/
#   3. installs vb-web.service on 127.0.0.1 (runs as the bot's user)
#   4. installs /etc/caddy/vb.caddy, runs caddy validate, reloads Caddy
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_DIR=/var/lib/vb
CONF="$DATA_DIR/setup.conf"
BACKUP_ROOT=/var/backups/vb
TS="$(date +%Y%m%d-%H%M%S)"

# sha256 of the files in the "Server version" release, to say whether the
# server still runs that version (informational only).
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

# ---------------------------------------------------------------- 1. find the bot
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
BOT_UID="$(id -u "$BOT_USER")"
ok "Bot folder: $BOT_DIR (owner $BOT_USER)"

SCHEDULER_SCRIPT=""
for s in asvz_scheduler.py asvz-scheduler.py; do
  [ -f "$BOT_DIR/$s" ] && { SCHEDULER_SCRIPT="$s"; break; }
done
[ -n "$SCHEDULER_SCRIPT" ] || die "No asvz_scheduler.py in $BOT_DIR"
ok "Scheduler: $SCHEDULER_SCRIPT (left unchanged)"

BOT_PYTHON=""
for v in "$BOT_DIR/.venv" "$BOT_DIR/venv" "$BOT_DIR/../.venv" "$BOT_DIR/../venv"; do
  if [ -x "$v/bin/python3" ]; then BOT_PYTHON="$(readlink -f "$v")/bin/python3"; break; fi
done
[ -n "$BOT_PYTHON" ] || die "No venv (.venv or venv) next to the bot."
"$BOT_PYTHON" -c 'import selenium' 2>/dev/null || die "$BOT_PYTHON can't import selenium — is that the right venv?"
ok "Bot Python: $BOT_PYTHON"

sha() { sha256sum "$1" | cut -d' ' -f1; }
VERSION_NOTE=""
[ "$(sha "$BOT_DIR/asvz_bot.py")" = "$SHA_BOT_KNOWN" ] || VERSION_NOTE="asvz_bot.py"
[ "$(sha "$BOT_DIR/$SCHEDULER_SCRIPT")" = "$SHA_SCHED_SERVER" ] || VERSION_NOTE="$VERSION_NOTE $SCHEDULER_SCRIPT"
if [ -n "$VERSION_NOTE" ]; then
  warn "Differs from the 'Server version' release:$VERSION_NOTE — fine if it's your newer working copy;"
  warn "the page only uses its 'add', 'stop-watch' and 'run' commands and the job list."
fi

# The timer that runs the bot today: a user unit of the bot's owner (what the
# bot's setup.sh installs), or else a system unit that runs the scheduler.
USER_HOME="$(getent passwd "$BOT_USER" | cut -d: -f6)"
UNIT_FILE=""; TIMER_UNIT=""; TIMER_SCOPE=""
for f in "$USER_HOME/.config/systemd/user/"*.timer; do
  [ -f "$f" ] || continue
  svc="${f%.timer}.service"
  if [ -f "$svc" ] && grep -q "$SCHEDULER_SCRIPT" "$svc"; then
    TIMER_UNIT="$(basename "${f%.timer}")"; TIMER_SCOPE=user; UNIT_FILE="$svc"; break
  fi
done
if [ -z "$TIMER_UNIT" ]; then
  for f in /etc/systemd/system/*.timer; do
    [ -f "$f" ] || continue
    svc="${f%.timer}.service"
    if [ -f "$svc" ] && grep -q "$SCHEDULER_SCRIPT" "$svc"; then
      TIMER_UNIT="$(basename "${f%.timer}")"; TIMER_SCOPE=system; UNIT_FILE="$svc"; break
    fi
  done
fi
[ -n "$TIMER_UNIT" ] || die "Found no systemd timer that runs $SCHEDULER_SCRIPT. Send the inspect.sh output to Claude."
ok "Bot timer: $TIMER_UNIT.timer ($TIMER_SCOPE unit, $UNIT_FILE)"
if [ "$TIMER_SCOPE" = user ] && [ ! -e "/var/lib/systemd/linger/$BOT_USER" ]; then
  warn "Lingering is off for $BOT_USER: the timer (and the page's control of it) only works while $BOT_USER is logged in."
fi

CHROME_BIN="$(sed -n 's/^Environment=CHROME_BIN=//p' "$UNIT_FILE" | head -1)"
CHROMEDRIVER_BIN="$(sed -n 's/^Environment=CHROMEDRIVER_BIN=//p' "$UNIT_FILE" | head -1)"
ok "Chrome for 'add by number': ${CHROME_BIN:-default of asvz_bot.py, /usr/local/bin/google-chrome}"

if [ ! -f "$BOT_DIR/.asvz-bot.json" ]; then
  warn "No ASVZ login saved in $BOT_DIR/.asvz-bot.json — the bot can't enroll without it."
elif [ "$(stat -c %a "$BOT_DIR/.asvz-bot.json")" != 600 ]; then
  warn "$BOT_DIR/.asvz-bot.json is readable by others (mode $(stat -c %a "$BOT_DIR/.asvz-bot.json")). Not changing it; to fix: chmod 600 $BOT_DIR/.asvz-bot.json"
fi

# ---------------------------------------------------------------- questions
if [ -z "$DOMAIN" ]; then
  read -rp "Your domain at Spaceship (e.g. example.com; the page goes to vb.<domain>): " base
  base="${base#https://}"; base="${base%%/*}"; base="${base#vb.}"
  [[ "$base" =~ ^[A-Za-z0-9.-]+\.[A-Za-z]{2,}$ ]] || die "That doesn't look like a domain: $base"
  DOMAIN="vb.$base"
fi
if [ "$UPDATE" -eq 0 ]; then
  if ss -ltn "sport = :$PORT" 2>/dev/null | grep -q LISTEN && ! systemctl is-active --quiet vb-web; then
    die "Port $PORT is taken. Pass --port with a free one."
  fi
  echo
  echo "  Bot folder : $BOT_DIR — nothing in it is changed"
  echo "  Bot timer  : $TIMER_UNIT.timer — stays; the page can start it / turn it on or off"
  echo "  Page       : https://$DOMAIN  ->  127.0.0.1:$PORT (vb-web, runs as $BOT_USER)"
  echo
  read -rp "Go ahead? Type yes: " answer
  [ "$answer" = "yes" ] || die "Stopped, nothing changed."
fi

# ---------------------------------------------------------------- 2. backup
mkdir -p "$BACKUP_ROOT" && chmod 700 "$BACKUP_ROOT"
BK="$BACKUP_ROOT/bot-$TS.tar.gz"
tar -czf "$BK" -C "$BOT_DIR" --exclude='./venv' --exclude='./.venv' --exclude='./chrome' \
    --exclude='__pycache__' --exclude='./watcher_logs' --exclude='*.png' .
chmod 600 "$BK"
ok "Backup of the bot folder (code + job list + login file): $BK"

# ---------------------------------------------------------------- 3. vb-web
mkdir -p "$DATA_DIR"
chmod 700 "$DATA_DIR"
cat > "$CONF" <<EOF
SAVED_BOT_DIR='$BOT_DIR'
SAVED_DOMAIN='$DOMAIN'
SAVED_PORT='$PORT'
EOF

render() {  # template -> stdout (values passed as env vars, not pasted into code)
  local extra=""
  [ -n "$CHROME_BIN" ] && extra="Environment=VB_CHROME_BIN=$CHROME_BIN"
  [ -n "$CHROMEDRIVER_BIN" ] && extra="$extra"$'\n'"Environment=VB_CHROMEDRIVER_BIN=$CHROMEDRIVER_BIN"
  # A non-root user talks to its own systemd user manager through this.
  if [ "$TIMER_SCOPE" = user ] && [ "$BOT_USER" != root ]; then
    extra="$extra"$'\n'"Environment=XDG_RUNTIME_DIR=/run/user/$BOT_UID"
  fi
  R_BOT_USER="$BOT_USER" R_BOT_DIR="$BOT_DIR" R_BOT_PYTHON="$BOT_PYTHON" R_APP_DIR="$APP_DIR" \
  R_DATA_DIR="$DATA_DIR" R_PORT="$PORT" R_DOMAIN="$DOMAIN" R_SCHEDULER_SCRIPT="$SCHEDULER_SCRIPT" \
  R_TIMER_UNIT="$TIMER_UNIT" R_TIMER_SCOPE="$TIMER_SCOPE" R_EXTRA_ENV="$extra" python3 -c '
import os, re, sys
s = open(sys.argv[1]).read()
s = re.sub(r"@([A-Z_]+)@", lambda m: os.environ["R_" + m.group(1)], s)
sys.stdout.write(re.sub(r"\n{3,}", "\n\n", s))' "$1"
}

put() {  # new-file target  -> 0 if changed (old one backed up)
  if [ -f "$2" ] && cmp -s "$1" "$2"; then rm -f "$1"; return 1; fi
  [ -f "$2" ] && cp -p "$2" "$BACKUP_ROOT/$(basename "$2").$TS"
  mv "$1" "$2"; chmod 644 "$2"; return 0
}

tmp="$(mktemp)"; render "$APP_DIR/deploy/vb-web.service.in" > "$tmp"; put "$tmp" /etc/systemd/system/vb-web.service || true
systemctl daemon-reload

if [ "$TIMER_SCOPE" = system ] && [ "$BOT_USER" != root ]; then
  SUDO_TMP="$(mktemp)"
  cat > "$SUDO_TMP" <<EOF
# Installed by vb deploy/setup.sh: lets the phone page start / switch the bot's timer, nothing else.
$BOT_USER ALL=(root) NOPASSWD: /usr/bin/systemctl start --no-block $TIMER_UNIT.service, /usr/bin/systemctl enable --now $TIMER_UNIT.timer, /usr/bin/systemctl disable --now $TIMER_UNIT.timer
EOF
  visudo -cf "$SUDO_TMP" >/dev/null || die "sudoers check failed"
  install -m 440 "$SUDO_TMP" /etc/sudoers.d/vb-web; rm -f "$SUDO_TMP"
  ok "sudo rule /etc/sudoers.d/vb-web (only start/enable/disable of $TIMER_UNIT)"
fi
if [ "$BOT_USER" != root ]; then
  usermod -aG systemd-journal "$BOT_USER"   # read the bot's log for the Activity page
fi

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

# ---------------------------------------------------------------- 4. Caddy
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
echo "  Logs: journalctl -u vb-web -n 50 --no-pager"
