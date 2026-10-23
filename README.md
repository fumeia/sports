# vb — phone remote for the ASVZ bot

A web page (installable on the phone's home screen) that controls the ASVZ
enrollment bot **running on the server**. It has the desktop app's three
pages: **Discover**, **My plan** and **Activity**. Enrollment still happens on
the server, by the bot's own scheduler and its own timer, exactly as before.
The phone only sends commands and shows status.

```
phone (home-screen app) ──HTTPS──▶ Caddy ──▶ vb-web (127.0.0.1:8710)
                                                 │ edits the job list, calls the bot's own commands
                                                 ▼
   bot folder (unchanged): asvz_schedule.json · asvz_watchers.json · asvz_scheduler.py
                           asvz_scheduler.py run  ◀── the bot's existing timer
```

- **The bot is not changed.** Setup replaces, adds and chmods nothing in the
  bot folder, and the bot's timer stays the one that runs it. The page only
  touches what the text menu touches: it adds and removes jobs in
  `asvz_schedule.json`, and calls the scheduler's own `add`, `stop-watch` and
  (through the timer) `run`.
- **Your university login stays on the server** (`.asvz-bot.json` in the bot
  folder). The page never reads or sends the password; it only shows
  "user · organisation".
- **The page has its own login** (name + password you choose during setup,
  stored as a scrypt hash in `/var/lib/vb/auth.json`). After 5 wrong tries from
  one address, or 20 in total, login pauses for 15 minutes. A login lasts 30 days.
- **Every week**: tick your trainings once in Discover with *Every week* on.
  The service queues each new date by itself, **about 8 days ahead**, so the
  job list stays as short as when you added the week by hand every Sunday
  (the text menu's *List* stays quick). The weekly plan lives in
  `/var/lib/vb/asvz_plan.json`, not in the bot folder.
- **The text menu still works** and shows the same jobs.

### Why changes from the phone can wait a few minutes

The bot's scheduler reads the job list when a run starts and writes it back
when the run ends, without a lock. A run can last up to ~20 minutes while it
waits for an enrollment to open, and a job written during that time would be
overwritten. So:

- While a run is active, changes from the phone are held back. *My plan*
  shows them under "Waiting for the bot". They are applied as soon as the run
  ends.
- For 15 minutes after each change, the service checks that a run starting at
  the same moment didn't undo it, and repairs it if one did.
- A lesson whose enrollment is already open gets its enrollment time set to
  "now", because the scheduler drops normal jobs 5 minutes after enrollment
  opens.

(Using the text menu while the timer runs has the same overwrite risk as
before; nothing new there.)

## What's in this repo

| Path | What |
|---|---|
| `web/server.py` | The API + page server (Python standard library only). |
| `web/asvz_plan.py` | Copy of the desktop app's lesson search / weekly plan module (release V1). |
| `web/static/` | The page (HTML/CSS/JS, PWA manifest, service worker, icons). |
| `deploy/inspect.sh` | **Read-only** look at how the bot runs on the server. |
| `deploy/setup.sh` | Install / re-apply (backs up the bot folder first, changes nothing in it). |
| `deploy/update.sh` | `git pull` + re-apply. |
| `deploy/rollback.sh` | Remove vb again (stop page, take it out of Caddy). |
| `deploy/asvz-login.sh` | Only if the bot has no saved login: writes `.asvz-bot.json`. |
| `tests/` | Fake bot, stand-in scheduler and fake ASVZ data, to try the page without a server. |

## Install

Check each command's prompt: **Termux** means your phone, **server** means
`root@…`. Run them one at a time.

**1. DNS (Spaceship website, in the phone browser).** Add an A record: host
`vb`, value = your server's IP.

**2. Termux:** log in to the server as usual (`ssh root@…`).

**3. Server:** get the code.
```
git clone -b claude/sports-enrollment-remote-client-177af5 https://github.com/fumeia/sports.git /opt/vb
```

**4. Server:** look before changing anything (read-only). Paste the output to Claude.
```
bash /opt/vb/deploy/inspect.sh
```

**5. Server:** install. It shows what it will do and waits for `yes`. It asks
for your domain and a password for the page.
```
bash /opt/vb/deploy/setup.sh
```

**6. Phone:** open `https://vb.<your domain>` in Chrome, log in, then
⋮ menu → **Add to Home screen** (or **Install app**).

## Everyday

- **Update** (after Claude pushes changes), server:
  ```
  bash /opt/vb/deploy/update.sh
  ```
- **Change the page password**, server:
  ```
  VB_DATA_DIR=/var/lib/vb python3 /opt/vb/web/server.py set-password
  ```
- **Logs**, server: `journalctl -u vb-web -n 50 --no-pager` (the page). The
  Activity page shows the bot's own run log and free-spot watcher logs.
- **Remove vb**, server:
  ```
  bash /opt/vb/deploy/rollback.sh
  ```

## What setup changes on the server

- `/var/backups/vb/bot-<time>.tar.gz`: a copy of the bot folder (code, job
  list, login file; no venv or Chrome), mode 600.
- `/etc/systemd/system/vb-web.service`: the page, on `127.0.0.1:8710`,
  running as the bot folder's owner (it edits the bot's job list, like the
  text menu). If the bot's timer is a system unit and that owner isn't root,
  a sudo rule allows exactly start/enable/disable of that timer.
- `/var/lib/vb/`: page login, sessions, weekly plan.
- `/etc/caddy/vb.caddy` plus one `import` line in the Caddyfile (backed up).
  It runs `caddy validate` before the reload; if validation fails, the old
  config is put back.

## Try it without a server

```
python3 tests/devserver.py /tmp/vbtest 8710
```
Open http://127.0.0.1:8710, log in as `test` / `test-password-123`. It uses
made-up lessons, a stub `asvz_bot.py` that only writes `enrolled.log`, and a
stand-in scheduler with the same file handling as the server's
(`VB_TEST_SCHEDULER=/path/to/asvz_scheduler.py` uses a real one instead).
