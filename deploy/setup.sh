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
#   3. installs vb-web.service on 127.0.0.1 as its own user "vb" (no access
#      to the bot folder) + /usr/local/sbin/vb-bot-helper, the only thing vb
#      may run as root (sudo rule); the helper does the bot actions
#   4. writes /etc/caddy/vb.caddy (your Caddyfile is not edited: you add
#      the import line yourself; setup prints it)
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
  # Prefer the folder the bot's timer actually runs in (copies of the bot
  # elsewhere, e.g. an export/ folder, are then ignored).
  for svc in /etc/systemd/system/*.service /root/.config/systemd/user/*.service \
             /home/*/.config/systemd/user/*.service; do
    [ -f "$svc" ] && [ -f "${svc%.service}.timer" ] || continue
    grep -qE 'asvz[_-]scheduler\.py' "$svc" || continue
    wd="$(sed -n 's/^WorkingDirectory=-\{0,1\}//p' "$svc" | head -1 | tr -d '"')"
    [ -z "$wd" ] && wd="$(dirname "$(grep -oE '[^ "]*asvz[_-]scheduler\.py' "$svc" | head -1)")"
    if [ -f "$wd/asvz_bot.py" ]; then BOT_DIR="$wd"; info "Bot folder taken from $svc"; break; fi
  done
fi
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
  echo "  Page       : https://$DOMAIN  ->  127.0.0.1:$PORT (vb-web, user vb: no access to the bot folder)"
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
# The page runs as its own system user "vb", which can't open the bot
# folder (and so not the login file). It reaches the bot only through
# /usr/local/sbin/vb-bot-helper, run as root via a sudo rule.
if ! id vb >/dev/null 2>&1; then
  useradd --system --user-group --home-dir "$DATA_DIR" --no-create-home --shell /usr/sbin/nologin vb
  ok "Created system user vb"
fi
mkdir -p "$DATA_DIR"
chmod 700 "$DATA_DIR"
printf "SAVED_BOT_DIR='%s'\nSAVED_DOMAIN='%s'\nSAVED_PORT='%s'\n" "$BOT_DIR" "$DOMAIN" "$PORT" > "$CONF"

put() {  # new-file target [mode] -> 0 if changed (old one backed up)
  if [ -f "$2" ] && cmp -s "$1" "$2"; then rm -f "$1"; return 1; fi
  [ -f "$2" ] && cp -p "$2" "$BACKUP_ROOT/$(basename "$2").$TS"
  mv "$1" "$2"; chown root:root "$2"; chmod "${3:-644}" "$2"; return 0
}

# helper + its settings (root-owned: vb can't change what they point to)
mkdir -p /etc/vb && chmod 755 /etc/vb
tmp="$(mktemp)"
{
  echo "# Installed by vb deploy/setup.sh — read by /usr/local/sbin/vb-bot-helper."
  echo "BOT_DIR=$BOT_DIR"
  echo "BOT_PYTHON=$BOT_PYTHON"
  echo "SCHEDULER_SCRIPT=$SCHEDULER_SCRIPT"
  echo "TIMER_UNIT=$TIMER_UNIT"
  echo "TIMER_SCOPE=$TIMER_SCOPE"
  echo "TIMER_USER=$BOT_USER"
  echo "CHROME_BIN=$CHROME_BIN"
  echo "CHROMEDRIVER_BIN=$CHROMEDRIVER_BIN"
} > "$tmp"
put "$tmp" /etc/vb/helper.conf 644 || true
tmp="$(mktemp)"; cp "$APP_DIR/deploy/vb-bot-helper" "$tmp"
if put "$tmp" /usr/local/sbin/vb-bot-helper 755; then ok "Installed /usr/local/sbin/vb-bot-helper"; fi

SUDO_TMP="$(mktemp)"
{
  echo "# Installed by vb deploy/setup.sh: the phone page (user vb) may run the"
  echo "# helper as root — nothing else. The helper checks its own arguments."
  echo "Defaults:vb !pam_session, !log_allowed"
  echo "vb ALL=(root) NOPASSWD: /usr/local/sbin/vb-bot-helper"
} > "$SUDO_TMP"
if ! visudo -cf "$SUDO_TMP" >/dev/null 2>&1; then
  # older sudo without log_allowed: same rule, sudo then logs each call
  sed -i 's/^Defaults:vb .*/Defaults:vb !pam_session/' "$SUDO_TMP"
  visudo -cf "$SUDO_TMP" >/dev/null || die "sudoers check failed"
fi
chmod 440 "$SUDO_TMP"
if [ -f /etc/sudoers.d/vb-web ] && cmp -s "$SUDO_TMP" /etc/sudoers.d/vb-web; then
  rm -f "$SUDO_TMP"
else
  [ -f /etc/sudoers.d/vb-web ] && cp -p /etc/sudoers.d/vb-web "$BACKUP_ROOT/sudoers-vb-web.$TS"
  mv "$SUDO_TMP" /etc/sudoers.d/vb-web; chown root:root /etc/sudoers.d/vb-web
  ok "sudo rule /etc/sudoers.d/vb-web (vb may run only the helper)"
fi
if ! sudo -u vb sudo -n /usr/local/sbin/vb-bot-helper info >/dev/null 2>&1; then
  die "The helper doesn't work for user vb. Send the output of: sudo -u vb sudo -n /usr/local/sbin/vb-bot-helper info"
fi
ok "Helper works for user vb"

render() {  # template -> stdout (values passed as env vars, not pasted into code)
  R_BOT_DIR="$BOT_DIR" R_APP_DIR="$APP_DIR" R_DATA_DIR="$DATA_DIR" R_PORT="$PORT" R_DOMAIN="$DOMAIN" \
  R_SCHEDULER_SCRIPT="$SCHEDULER_SCRIPT" R_TIMER_UNIT="$TIMER_UNIT" python3 -c '
import os, re, sys
s = open(sys.argv[1]).read()
s = re.sub(r"@([A-Z_]+)@", lambda m: os.environ["R_" + m.group(1)], s)
sys.stdout.write(re.sub(r"\n{3,}", "\n\n", s))' "$1"
}

tmp="$(mktemp)"; render "$APP_DIR/deploy/vb-web.service.in" > "$tmp"; put "$tmp" /etc/systemd/system/vb-web.service || true
systemctl daemon-reload

if ! python3 -c 'import json,sys; assert json.load(open(sys.argv[1]))["hash"]' "$DATA_DIR/auth.json" 2>/dev/null; then
  info "Choose the login for the phone page (not your university login):"
  VB_DATA_DIR="$DATA_DIR" python3 "$APP_DIR/web/server.py" set-password
fi
chown -R vb:vb "$DATA_DIR"

systemctl enable vb-web >/dev/null 2>&1
systemctl restart vb-web
for _ in $(seq 1 20); do
  curl -fsS "http://127.0.0.1:$PORT/api/me" >/dev/null 2>&1 && break
  sleep 0.5
done
curl -fsS "http://127.0.0.1:$PORT/api/me" >/dev/null || die "vb-web did not start. See: journalctl -u vb-web -n 50 --no-pager"
ok "vb-web running on 127.0.0.1:$PORT as user vb"

# ---------------------------------------------------------------- 4. Caddy
# Only our own file /etc/caddy/vb.caddy is written. The Caddyfile is never
# edited: you add the import line yourself (printed at the end).
CADDYFILE=/etc/caddy/Caddyfile
[ -f "$CADDYFILE" ] || die "$CADDYFILE not found"
IMPORTED=0
grep -qE '^\s*import\s+(/etc/caddy/)?(vb\.caddy|\*\.caddy)\s*$' "$CADDYFILE" && IMPORTED=1
tmp="$(mktemp)"; render "$APP_DIR/deploy/vb.caddy.in" > "$tmp"
[ -f /etc/caddy/vb.caddy ] && cp -p /etc/caddy/vb.caddy "$BACKUP_ROOT/vb.caddy.$TS"
if put "$tmp" /etc/caddy/vb.caddy; then
  ok "Wrote /etc/caddy/vb.caddy"
  if [ "$IMPORTED" -eq 1 ]; then
    # Already in use: check the new version before Caddy loads it.
    if caddy validate --config "$CADDYFILE" --adapter caddyfile >/tmp/vb-caddy-validate.log 2>&1; then
      systemctl reload caddy
      ok "Caddy validated and reloaded"
    else
      if [ -f "$BACKUP_ROOT/vb.caddy.$TS" ]; then cp -p "$BACKUP_ROOT/vb.caddy.$TS" /etc/caddy/vb.caddy; fi
      tail -20 /tmp/vb-caddy-validate.log
      die "caddy validate failed — /etc/caddy/vb.caddy put back as it was, Caddy not reloaded. Send the lines above to Claude."
    fi
  fi
else
  ok "/etc/caddy/vb.caddy unchanged"
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
if [ "$IMPORTED" -eq 0 ]; then
  echo
  info "Last step, in Caddy (setup doesn't edit your Caddyfile):"
  echo "  1. add this line to $CADDYFILE, next to your other import lines:"
  echo "       import /etc/caddy/vb.caddy"
  echo "  2. caddy validate --config $CADDYFILE"
  echo "  3. systemctl reload caddy"
fi
echo
echo "  Open https://$DOMAIN on your phone, log in, then browser menu -> 'Add to Home screen'."
echo "  Logs: journalctl -u vb-web -n 50 --no-pager"
