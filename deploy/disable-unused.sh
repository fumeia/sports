#!/usr/bin/env bash
# deploy/disable-unused.sh — turn OFF the torrent/media tools that came
# pre-installed (qBittorrent, Transmission, Jackett, Prowlarr, Lidarr, and
# their usual siblings Sonarr/Radarr/Readarr/Bazarr) so they stop running
# and stop listening on ports. Nothing is uninstalled or deleted: their
# programs and settings stay on disk, and every step is written down with
# the command that undoes it.
#
#   bash /opt/vb/deploy/disable-unused.sh          shows what it found, asks "yes"
#   bash /opt/vb/deploy/disable-unused.sh --undo   turns everything back on
set -uo pipefail

PATTERN='qbittorrent|transmission|jackett|prowlarr|lidarr|sonarr|radarr|readarr|bazarr'
LOG_DIR=/var/backups/vb
UNDO="$LOG_DIR/disabled-services.undo.sh"
[ "$(id -u)" -eq 0 ] || { echo "Run as root."; exit 1; }
mkdir -p "$LOG_DIR" && chmod 700 "$LOG_DIR"

if [ "${1:-}" = "--undo" ]; then
  [ -f "$UNDO" ] || { echo "Nothing to undo ($UNDO not found)."; exit 1; }
  cat "$UNDO"
  read -rp "Run these commands to turn the tools back on? Type yes: " a
  [ "$a" = yes ] && bash "$UNDO" && mv "$UNDO" "$UNDO.done-$(date +%Y%m%d-%H%M%S)"
  exit 0
fi

h() { printf '\n===== %s =====\n' "$*"; }

# ---- what is there
h "systemd services"
mapfile -t UNITS < <(
  { systemctl list-unit-files --type=service --no-legend --plain 2>/dev/null | awk '{print $1}'
    # running processes -> the unit they belong to (catches odd unit names)
    for pid in $(pgrep -if "$PATTERN"); do ps -o unit= -p "$pid" 2>/dev/null; done
  } | grep -iE "$PATTERN" | grep -E '\.service$' | grep -v '@\.service$' | sort -u)
for u in "${UNITS[@]}"; do
  printf '  %-40s %s, %s\n' "$u" "$(systemctl is-active "$u" 2>/dev/null)" "$(systemctl is-enabled "$u" 2>/dev/null)"
done
[ ${#UNITS[@]} -eq 0 ] && echo "  none"

h "docker containers"
CONTAINERS=()
if command -v docker >/dev/null; then
  mapfile -t CONTAINERS < <(docker ps -a --format '{{.Names}} {{.Image}}' 2>/dev/null | grep -iE "$PATTERN" | awk '{print $1}')
  for c in "${CONTAINERS[@]}"; do
    printf '  %-30s %s\n' "$c" "$(docker inspect -f '{{.State.Status}}, restart={{.HostConfig.RestartPolicy.Name}}, image={{.Config.Image}}' "$c")"
  done
fi
[ ${#CONTAINERS[@]} -eq 0 ] && echo "  none"

h "matching processes not run by systemd or docker (left alone, shown for info)"
found=0
for pid in $(pgrep -if "$PATTERN"); do
  [ "$pid" = "$$" ] || [ "$pid" = "$PPID" ] && continue
  case "$(cat /proc/$pid/comm 2>/dev/null)" in grep|pgrep|ps) continue ;; esac
  unit="$(ps -o unit= -p "$pid" 2>/dev/null | tr -d ' ')"
  case "$unit" in *.service) continue ;; esac
  case "$(cat /proc/$pid/cgroup 2>/dev/null)" in *docker*) continue ;; esac
  ps -o user=,pid=,args= -p "$pid" | cut -c1-150; found=1
done
[ $found -eq 0 ] && echo "  none"

h "their ports now"
ss -ltnp 2>/dev/null | grep -iE "$PATTERN" | awk '{print "  " $4 "  " $6}' | sed -E 's/users:\(\("([^"]+)".*/\1/' | sort -u

if [ ${#UNITS[@]} -eq 0 ] && [ ${#CONTAINERS[@]} -eq 0 ]; then
  echo; echo "Nothing to turn off."; exit 0
fi

echo
echo "This turns OFF (stop now + don't start at boot) the services/containers listed above."
echo "Nothing is uninstalled; '--undo' turns them back on."
read -rp "Turn them off? Type yes: " answer
[ "$answer" = yes ] || { echo "Stopped, nothing changed."; exit 0; }

# ---- turn off, writing the undo commands first
{ echo "#!/usr/bin/env bash"; echo "# written by disable-unused.sh on $(date)"; } >> "$UNDO"
for u in "${UNITS[@]}"; do
  was_enabled="$(systemctl is-enabled "$u" 2>/dev/null)"
  was_active="$(systemctl is-active "$u" 2>/dev/null)"
  [ "$was_enabled" = enabled ] && echo "systemctl enable $u" >> "$UNDO"
  [ "$was_active" = active ] && echo "systemctl start $u" >> "$UNDO"
  if systemctl disable --now "$u" >/dev/null 2>&1; then echo "  off: $u"
  else echo "  !! could not turn off $u (see: systemctl status $u)"; fi
done
for c in "${CONTAINERS[@]}"; do
  policy="$(docker inspect -f '{{.HostConfig.RestartPolicy.Name}}' "$c")"
  running="$(docker inspect -f '{{.State.Running}}' "$c")"
  [ -n "$policy" ] && [ "$policy" != no ] && echo "docker update --restart=$policy $c" >> "$UNDO"
  [ "$running" = true ] && echo "docker start $c" >> "$UNDO"
  docker update --restart=no "$c" >/dev/null && docker stop "$c" >/dev/null && echo "  off: container $c"
done
chmod 600 "$UNDO"

h "ports after"
left="$(ss -ltnp 2>/dev/null | grep -iE "$PATTERN")"
[ -z "$left" ] && echo "  none of them listens anymore" || echo "$left"
echo
echo "Undo commands saved in $UNDO (run: bash /opt/vb/deploy/disable-unused.sh --undo)"
echo "Their programs and data are still on disk; see deploy/disable-unused.sh for removing them later."
