#!/usr/bin/env bash
# deploy/compare-scheduler.sh — READ-ONLY. Shows how the server's
# asvz_scheduler.py differs from the "Server version" release, the timer
# units that run it (passwords masked), memory use, and how the
# torrent/media tools are installed. Downloads the release into a temp
# folder; changes nothing else.
#   bash /opt/vb/deploy/compare-scheduler.sh [/root/asvz-bot/src]
set -u
BOT_DIR="${1:-/root/asvz-bot/src}"
T="$(mktemp -d)"
trap 'rm -rf "$T"' EXIT
h() { printf '\n===== %s =====\n' "$*"; }

h "Timer units (systemctl cat, passwords masked)"
systemctl cat asvz-scheduler.timer asvz-scheduler.service 2>&1 \
  | sed -E 's/((-p|--password)[ =])[^ ]+/\1***/g; s/(PASSWORD=)[^ ]+/\1***/Ig'

h "Scheduler: release vs. $BOT_DIR"
if curl -fsSL -o "$T/srv.zip" https://github.com/fumeia/sports/releases/download/Serverversion/asvz-bot-server.zip \
   && python3 -m zipfile -e "$T/srv.zip" "$T/srv"; then
  rel="$(find "$T/srv" -name asvz_scheduler.py | head -1)"
  diff -u "$rel" "$BOT_DIR/asvz_scheduler.py" | sed -E 's/((-p|--password)[ =])[^ ]+/\1***/g' | head -400
  echo "(end of diff)"
  for f in asvz_bot.py slot_watcher.py; do
    h "$f: release vs. $BOT_DIR"
    r="$(find "$T/srv" -name "$f" | head -1)"
    if [ -f "$BOT_DIR/$f" ] && [ -n "$r" ]; then
      diff -u "$r" "$BOT_DIR/$f" | sed -E 's/((-p|--password)[ =])[^ ]+/\1***/g' | head -400
      echo "(end of diff)"
    else
      echo "not found"
    fi
  done
else
  echo "Could not download the release zip."
fi

h "Scheduler commands"
grep -nE 'add_parser\(|add_argument\(' "$BOT_DIR/asvz_scheduler.py" | head -40

h "Memory"
free -h

h "Torrent / media tools (status only)"
systemctl list-units --all --no-pager --plain 2>/dev/null | grep -iE 'qbit|jackett|prowlarr|lidarr|sonarr|radarr|transmission' || echo "no systemd units"
docker ps --format '{{.Names}}  {{.Image}}  {{.Ports}}' 2>/dev/null | head -20
ps -eo user,pid,rss,args --sort=-rss | grep -iE 'qbit|jackett|prowlarr|lidarr|transmission' | grep -v grep | awk '{printf "%s pid %s  %d MB  %s\n",$1,$2,$3/1024,$4}'
echo
echo "Done. Nothing was changed. Paste everything above into the chat."
