#!/usr/bin/env bash
# deploy/inspect.sh — READ-ONLY look at how the ASVZ bot runs on this server.
# Changes nothing. Prints no passwords (only whether the login file exists).
# Run on the server:  bash /opt/vb/deploy/inspect.sh
set -u

h() { printf '\n===== %s =====\n' "$*"; }

h "System"
grep -E '^(PRETTY_NAME)=' /etc/os-release
echo "user: $(id -un)  python3: $(python3 --version 2>&1)"
timedatectl 2>/dev/null | grep -E 'Local time|Time zone' || date

h "Bot folders (asvz_scheduler.py / asvz-scheduler.py)"
mapfile -t found < <(find / -xdev \( -name asvz_scheduler.py -o -name asvz-scheduler.py \) \
  -not -path '*/venv/*' -not -path '*/.venv/*' -not -path '/opt/vb/*' -not -path '/proc/*' 2>/dev/null)
[ ${#found[@]} -eq 0 ] && echo "none found"
for f in "${found[@]}"; do
  d=$(dirname "$f")
  echo "--- $d (owner: $(stat -c %U "$d"))"
  ls -la "$d"
  for x in asvz_bot.py asvz_scheduler.py asvz-scheduler.py asvz_plan.py keep_awake.py; do
    [ -f "$d/$x" ] && echo "sha256 $(sha256sum "$d/$x" | cut -c1-16)  $x"
  done
  for v in "$d/.venv" "$d/venv" "$d/../.venv" "$d/../venv"; do
    [ -x "$v/bin/python3" ] && echo "venv: $(readlink -f "$v") ($("$v/bin/python3" --version 2>&1))"
  done
  if [ -f "$d/.asvz-bot.json" ]; then
    echo "login file: present, mode $(stat -c %a "$d/.asvz-bot.json"), keys: $(python3 -c 'import json,sys;print(",".join(sorted(json.load(open(sys.argv[1])))))' "$d/.asvz-bot.json" 2>&1)"
  else
    echo "login file: MISSING"
  fi
  [ -f "$d/asvz_schedule.json" ] && echo "queued jobs: $(python3 -c 'import json,sys;print(len(json.load(open(sys.argv[1])).get("jobs",[])))' "$d/asvz_schedule.json" 2>&1)"
  [ -f "$d/asvz_watchers.json" ] && echo "watchers file: $(head -c 300 "$d/asvz_watchers.json" | tr '\n' ' ')"
  for c in "$d/chrome/chrome-linux64/chrome" "$d/../chrome/chrome-linux64/chrome"; do
    [ -x "$c" ] && echo "chrome: $(readlink -f "$c")"
  done
done
for b in /usr/local/bin/google-chrome /usr/local/bin/chromedriver; do
  [ -e "$b" ] && echo "$b -> $(readlink -f "$b")"
done

h "systemd: system units mentioning asvz / vb / xvfb / vnc"
systemctl list-units --all --no-pager --plain 2>/dev/null | grep -iE 'asvz|vb-|xvfb|vnc' || echo "none"
systemctl list-unit-files --no-pager --plain 2>/dev/null | grep -iE 'asvz|vb-|xvfb|vnc' || true

h "systemd: user units (lingering users: $(ls /var/lib/systemd/linger 2>/dev/null | tr '\n' ' '))"
for home in /root /home/*; do
  u=$(basename "$home"); [ "$home" = /root ] && u=root
  ud="$home/.config/systemd/user"
  if ls "$ud"/asvz* >/dev/null 2>&1; then
    echo "--- $u: $ud"
    ls -la "$ud" | grep -i asvz
    grep -hE '^(ExecStart|WorkingDirectory|Environment|OnCalendar)=' "$ud"/asvz* 2>/dev/null
    systemctl --user -M "$u@" list-timers --all --no-pager 2>/dev/null | grep -iE 'asvz|NEXT' || echo "(could not ask $u's user manager)"
    systemctl --user -M "$u@" is-enabled asvz-scheduler.timer 2>/dev/null | sed 's/^/timer enabled: /'
  fi
done

h "cron entries mentioning asvz"
{ crontab -l 2>/dev/null; cat /etc/crontab /etc/cron.d/* 2>/dev/null; } | grep -i asvz || echo "none"

h "Running processes (bot / chrome / screens / tmux)"
ps -eo user,pid,etime,args | grep -iE 'asvz|chrome|xvfb|vnc|tmux|screen' | grep -vE 'grep|inspect.sh' || echo "none"

h "Recent scheduler log (last 15 lines, user journal)"
for home in /root /home/*; do
  u=$(basename "$home"); [ "$home" = /root ] && u=root
  [ -d "$home/.config/systemd/user" ] || continue
  journalctl --user -M "$u@" -u asvz-scheduler -n 15 --no-pager 2>/dev/null | sed "s/^/[$u] /"
done
journalctl -u asvz-scheduler -n 15 --no-pager 2>/dev/null | grep -v '^-- No entries' | sed 's/^/[system] /'

h "Caddy"
caddy version 2>&1 | head -1
systemctl is-active caddy 2>&1 | sed 's/^/caddy service: /'
ls -la /etc/caddy/
grep -nE '^\s*import' /etc/caddy/Caddyfile 2>/dev/null || echo "no import lines in Caddyfile"

h "Listening ports"
ss -ltnp 2>/dev/null | awk 'NR==1 || /LISTEN/' | sed -E 's/users:\(\("([^"]+)".*/\1/'

h "Git / tools"
for t in git curl sudo visudo systemd-run; do printf '%s: %s\n' "$t" "$(command -v $t || echo MISSING)"; done
[ -d /opt/vb/.git ] && echo "/opt/vb: $(git -C /opt/vb log -1 --format='%h %s' 2>&1)"

echo
echo "Done. Nothing was changed. Paste everything above into the chat."
