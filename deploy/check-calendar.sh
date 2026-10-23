#!/usr/bin/env bash
# deploy/check-calendar.sh — READ-ONLY check of the calendar subscription.
# Fetches the feed through your domain + Caddy (the same way Google does),
# lists its next events, and shows whether Google has fetched it (from the
# page's log). The secret part of the link is masked in the output.
#   bash /opt/vb/deploy/check-calendar.sh
set -u
. /var/lib/vb/setup.conf 2>/dev/null || { echo "vb isn't set up (no /var/lib/vb/setup.conf)."; exit 1; }
mask() { sed -E 's#/cal/[A-Za-z0-9_-]+#/cal/<secret>#g'; }

TOKEN="$(python3 -c 'import json;c=json.load(open("/var/lib/vb/calendar.json"));print(c.get("token",""), "on" if c.get("enabled") else "off")' 2>/dev/null)"
read -r TOK STATE <<<"$TOKEN"
echo "===== Calendar link: ${STATE:-not created yet}"
[ -n "${TOK:-}" ] || exit 0
URL="https://$SAVED_DOMAIN/cal/$TOK.ics"
T="$(mktemp)"; trap 'rm -f "$T"' EXIT

echo
echo "===== Fetch through https://$SAVED_DOMAIN (like Google)"
curl -sS -A "Google-Calendar-Importer" -o "$T" -w 'HTTP %{http_code}, %{size_download} bytes, %{time_total}s, type %{content_type}\n' "$URL" 2>&1 | mask
echo "events in the feed: $(grep -c 'BEGIN:VEVENT' "$T")"
echo "next events:"
python3 - "$T" <<'PY'
import re, sys
from datetime import datetime
txt = open(sys.argv[1], encoding="utf-8", errors="replace").read().replace("\r\n ", "")
evs = []
for block in txt.split("BEGIN:VEVENT")[1:]:
    start = re.search(r"DTSTART;TZID=Europe/Zurich:(\d{8}T\d{6})", block)
    summ = re.search(r"SUMMARY:(.*)", block)
    if start:
        evs.append((start.group(1), summ.group(1).strip() if summ else "?"))
now = datetime.now().strftime("%Y%m%dT%H%M%S")
for s, t in sorted(e for e in evs if e[0] >= now)[:8]:
    d = datetime.strptime(s, "%Y%m%dT%H%M%S")
    print(f"  {d:%a %d.%m. %H:%M}  {t}")
if not evs:
    print("  (none)")
PY

echo
echo "===== Plain http (what webcal:// links use)"
curl -sS -o /dev/null -w 'HTTP %{http_code} -> %{redirect_url}\n' "http://$SAVED_DOMAIN/cal/$TOK.ics" 2>&1 | mask

echo
echo "===== Who fetched the feed (page log, last 2 days)"
journalctl -u vb-web --since "2 days ago" --no-pager 2>/dev/null | grep -E '/cal/|Calendar feed' | mask | tail -15
[ "$(journalctl -u vb-web --since '2 days ago' --no-pager 2>/dev/null | grep -c '/cal/')" = 0 ] \
  && echo "  nobody yet — Google hasn't requested this link."
echo
echo "Done. Nothing was changed."
