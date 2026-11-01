#!/usr/bin/env bash
# deploy/patch-bot-prestart.sh — one small fix in the bot's asvz_bot.py.
#
#   bash /opt/vb/deploy/patch-bot-prestart.sh          shows the change, asks "yes"
#   bash /opt/vb/deploy/patch-bot-prestart.sh --undo   puts the backup back
#
# Problem: after logging in (17 min before enrollment), the bot's
# _redirect_back_to_lesson() accepts the lesson page only if the sign-up
# button (or its <app-lessons-enrollment-button> box) is there. For lessons
# that show it only once enrollment opens, that check fails, the run aborts
# ("stuck in membership redirect loop") and is retried every 3 min until
# the job is missed.
# Fix: also accept the page when the lesson details ("Einschreibezeitraum" /
# "Anmeldezeitraum") are shown and there is no Login button (= logged in).
# Nothing else in the bot changes; asvz_scheduler.py is not touched.
set -euo pipefail
BOT_DIR="${BOT_DIR:-/root/asvz-bot/src}"
F="$BOT_DIR/asvz_bot.py"
BK=/var/backups/vb
TS="$(date +%Y%m%d-%H%M%S)"
PY="$BOT_DIR/.venv/bin/python3"; [ -x "$PY" ] || PY=python3
[ -f "$F" ] || { echo "Not found: $F"; exit 1; }

if [ "${1:-}" = "--undo" ]; then
  last="$(ls -t "$BK"/asvz_bot.py.before-prestart-* 2>/dev/null | head -1)"
  [ -n "$last" ] || { echo "No backup found in $BK."; exit 1; }
  echo "Restore $last -> $F"
  read -rp "Type yes: " a; [ "$a" = yes ] || { echo "Stopped."; exit 0; }
  cp -p "$F" "$BK/asvz_bot.py.patched-$TS" && cp -p "$last" "$F" && echo "Restored."
  exit 0
fi

grep -q "PRESTART-FIX" "$F" && { echo "Already patched. (Undo: --undo)"; exit 0; }

NEW="$(mktemp)"; trap 'rm -f "$NEW"' EXIT
"$PY" - "$F" "$NEW" <<'PYEOF'
import sys
src, out = sys.argv[1], sys.argv[2]
s = open(src, encoding="utf-8").read()
old = '''            except:
                logging.info("[RedirectFix] Lesson DOM not detected — retrying.")
                continue
'''
new = '''            except:
                # PRESTART-FIX (vb): before enrollment opens, some lessons show
                # no sign-up button yet. Logged in + lesson details = right page.
                logged_out = driver.find_elements(
                    By.XPATH, "//button[@title='Login' or contains(normalize-space(.),'Login')]")
                details = driver.find_elements(
                    By.XPATH, "//dl[contains(., 'Einschreibezeitraum') or contains(., 'Anmeldezeitraum')]")
                if details and not logged_out:
                    logging.info("[RedirectFix] Lesson page without sign-up button yet "
                                 "(enrollment not open) — OK.")
                else:
                    logging.info("[RedirectFix] Lesson DOM not detected — retrying.")
                    continue
'''
if s.count(old) != 1:
    sys.exit(f"The expected code wasn't found exactly once ({s.count(old)}x) — nothing changed.")
open(out, "w", encoding="utf-8").write(s.replace(old, new))
PYEOF

"$PY" -m py_compile "$NEW" || { echo "Patched file doesn't compile — nothing changed."; exit 1; }
diff -u "$F" "$NEW" || true
echo
read -rp "Apply this change to $F (a backup is made first)? Type yes: " a
[ "$a" = yes ] || { echo "Stopped, nothing changed."; exit 0; }
mkdir -p "$BK" && chmod 700 "$BK"
cp -p "$F" "$BK/asvz_bot.py.before-prestart-$TS"
cat "$NEW" > "$F"   # keeps owner and mode
echo "Done. Backup: $BK/asvz_bot.py.before-prestart-$TS   Undo: bash /opt/vb/deploy/patch-bot-prestart.sh --undo"
