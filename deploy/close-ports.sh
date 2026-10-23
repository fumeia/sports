#!/usr/bin/env bash
# deploy/close-ports.sh — make Obsidian and Immich reachable only through
# Caddy (HTTPS), and turn off the printer service.
#
#   bash /opt/vb/deploy/close-ports.sh          shows the plan, asks "yes"
#   bash /opt/vb/deploy/close-ports.sh --undo   puts everything back
#
# How: their Compose files publish ports on all interfaces (0.0.0.0:8080,
# 0.0.0.0:8443, 0.0.0.0:2283), which Docker opens past any ufw firewall.
# Each such line gets "127.0.0.1:" in front, so only Caddy (on the same
# machine) can reach them. Before that it checks that Caddy forwards to
# localhost/127.0.0.1 for at least one of a service's ports; otherwise that
# service is left alone.
# Compose files are backed up first; data volumes are not touched.
set -uo pipefail

PORTS="8080 8443 2283"
COMPOSE_FILES="/root/obsidian/docker-compose.yml /opt/immich/docker-compose.yml"
BK=/var/backups/vb
UNDO="$BK/close-ports.undo.sh"
TS="$(date +%Y%m%d-%H%M%S)"
[ "$(id -u)" -eq 0 ] || { echo "Run as root."; exit 1; }
mkdir -p "$BK" && chmod 700 "$BK"

if [ "${1:-}" = "--undo" ]; then
  [ -f "$UNDO" ] || { echo "Nothing to undo ($UNDO not found)."; exit 1; }
  cat "$UNDO"; read -rp "Run these commands to undo? Type yes: " a
  [ "$a" = yes ] && bash "$UNDO" && mv "$UNDO" "$UNDO.done-$TS"
  exit 0
fi

h() { printf '\n===== %s =====\n' "$*"; }

# ---- 1. Caddy must reach each port on this machine
h "Where Caddy forwards to"
OK_PORTS=""
for p in $PORTS; do
  lines="$(grep -rhE "reverse_proxy[^#]*:$p\b" /etc/caddy/ --include=Caddyfile --include='*.caddy' 2>/dev/null | sed 's/^\s*//')"
  if [ -z "$lines" ]; then
    echo "  port $p: no Caddy site forwards to it — left as it is"
  elif echo "$lines" | grep -qvE "(localhost|127\.0\.0\.1|\[::1\]):$p\b"; then
    echo "  port $p: Caddy forwards to something other than localhost — left as it is:"; echo "$lines" | sed 's/^/      /'
  else
    echo "  port $p: $lines  -> ok"; OK_PORTS="$OK_PORTS $p"
  fi
done

# ---- 2. the Compose lines to change
h "Changes to the Compose files"
PLAN="$(mktemp)"; trap 'rm -f "$PLAN" "$PLAN".*' EXIT
for f in $COMPOSE_FILES; do
  [ -f "$f" ] || { echo "  $f: not found — skipped"; continue; }
  OK_PORTS="$OK_PORTS" ALL_PORTS="$PORTS" python3 - "$f" "$PLAN.$(basename "$(dirname "$f")")" <<'PY'
import os, re, sys
path, out = sys.argv[1], sys.argv[2]
ok = set(os.environ["OK_PORTS"].split())
watched = set(os.environ["ALL_PORTS"].split())
# - 8080:3000   - "8080:3000"   - '2283:2283'   - ${PORT:-2283}:2283   (no host IP yet)
rx = re.compile(r'^(\s*-\s*["\']?)((?:\d+|\$\{[^}]*?(\d+)\}))(:\d+(?:/(?:tcp|udp))?["\']?\s*(?:#.*)?)$')
lines = open(path).read().split("\n")
def host_port(m):
    return m.group(2) if m.group(2).isdigit() else m.group(3)
# If Caddy reaches this app on one of its ports, its other ports (e.g. Obsidian's
# http 8080 next to https 8443) don't need to be public either.
mine = {host_port(m) for m in map(rx.match, lines) if m} & watched
if mine & ok:
    ok |= mine
changed = []
for n, line in enumerate(lines):
    m = rx.match(line)
    if not m:
        continue
    if host_port(m) in ok:
        new = f"{m.group(1)}127.0.0.1:{m.group(2)}{m.group(4)}"
        changed.append((n + 1, line, new))
        lines[n] = new
if changed:
    open(out, "w").write("\n".join(lines))
    print(f"  {path}:")
    for n, old, new in changed:
        print(f"    line {n}: {old.strip()}  ->  {new.strip()}")
else:
    print(f"  {path}: nothing to change")
PY
done

# ---- 3. printer service
h "Printer service (cups)"
CUPS_UNITS=""
for u in cups.service cups.socket cups.path cups-browsed.service; do
  if systemctl list-unit-files "$u" --no-legend 2>/dev/null | grep -q .; then
    CUPS_UNITS="$CUPS_UNITS $u"
    echo "  $u: $(systemctl is-active "$u" 2>/dev/null), $(systemctl is-enabled "$u" 2>/dev/null) -> will be turned off"
  fi
done
[ -z "$CUPS_UNITS" ] && echo "  not installed"

ls "$PLAN".* >/dev/null 2>&1 || [ -n "$CUPS_UNITS" ] || { echo; echo "Nothing to do."; exit 0; }
echo
echo "Obsidian/Immich restart once (a few seconds offline); their data stays."
read -rp "Apply? Type yes: " answer
[ "$answer" = yes ] || { echo "Stopped, nothing changed."; exit 0; }

{ echo "#!/usr/bin/env bash"; echo "# written by close-ports.sh on $(date)"; } > "$UNDO"; chmod 600 "$UNDO"

# ---- apply compose changes, one project at a time, checked before and after
for f in $COMPOSE_FILES; do
  new="$PLAN.$(basename "$(dirname "$f")")"
  [ -f "$new" ] || continue
  dir="$(dirname "$f")"; name="$(basename "$dir")"
  cp -p "$f" "$BK/$name-docker-compose.yml.$TS"
  cat "$new" > "$f"
  if ! (cd "$dir" && docker compose config -q) 2>/tmp/vb-compose.err; then
    cp -p "$BK/$name-docker-compose.yml.$TS" "$f"
    echo "  !! $f: docker compose rejected the change — put back as it was:"; sed 's/^/     /' /tmp/vb-compose.err
    continue
  fi
  echo "cp -p '$BK/$name-docker-compose.yml.$TS' '$f' && (cd '$dir' && docker compose up -d)" >> "$UNDO"
  (cd "$dir" && docker compose up -d 2>&1 | tail -3 | sed 's/^/     /')
  echo "  $name: restarted with ports on 127.0.0.1 (backup $BK/$name-docker-compose.yml.$TS)"
done

for u in $CUPS_UNITS; do
  [ "$(systemctl is-enabled "$u" 2>/dev/null)" = enabled ] && echo "systemctl enable $u" >> "$UNDO"
  [ "$(systemctl is-active "$u" 2>/dev/null)" = active ] && echo "systemctl start $u" >> "$UNDO"
done
[ -n "$CUPS_UNITS" ] && systemctl disable --now $CUPS_UNITS >/dev/null 2>&1 && echo "  cups: turned off"

# ---- check
sleep 5
h "Open to the internet now"
ss -ltnpH 2>/dev/null | awk '$4 ~ /^(0\.0\.0\.0|\*|\[::\]):/ {print}' \
  | sed -E 's/users:\(\("([^"]+)".*/\1/' | awk '{printf "  %-22s %s\n", $4, $6}' | sort -u
h "Still answering locally (for Caddy)"
for p in $OK_PORTS; do
  proto=http; [ "$p" = 8443 ] && proto=https
  printf '  127.0.0.1:%s -> HTTP %s\n' "$p" "$(curl -sk -o /dev/null -m 10 -w '%{http_code}' "$proto://127.0.0.1:$p/")"
done
echo
echo "Now open your Obsidian and Immich addresses (the https ones) on the phone to check."
echo "Undo: bash /opt/vb/deploy/close-ports.sh --undo"
