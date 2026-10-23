# vb — phone remote for the ASVZ bot

A web page (installable on the phone's home screen) that controls the ASVZ
enrollment bot **running on the server**. It has the desktop app's three
pages: **Discover**, **My plan** and **Activity**. Enrollment still happens on
the server, exactly as before; the phone only sends commands and shows status.

```
phone (home-screen app) ──HTTPS──▶ Caddy ──▶ vb-web (127.0.0.1:8710)
                                                 │ same files + commands as the desktop app
                                                 ▼
          bot folder: asvz_schedule.json · asvz_plan.json · asvz_watchers.json
                      asvz_scheduler.py run  ◀── vb-scheduler.timer (every 2 min)
```

- **Your university login stays on the server** (`.asvz-bot.json` in the bot
  folder, mode 600). The page never reads or sends the password; it only shows
  "user · organisation".
- **The page has its own login** (name + password you choose during setup,
  stored as a scrypt hash in `/var/lib/vb/auth.json`). After 5 wrong tries from
  one address, or 20 in total, login pauses for 15 minutes. A login lasts 30 days on
  the phone.
- **Every week**: tick the trainings once in Discover with *Every week* on.
  On every check (at most every 30 minutes) the bot queues each new date ASVZ
  publishes. No more adding them every Sunday.
- **The text menu still works.** `cd asvz-bot/src`, `source .venv/bin/activate`,
  `python3 asvz_scheduler.py` shows the same jobs as the phone.

## What's in this repo

| Path | What |
|---|---|
| `bot/asvz_scheduler.py`, `bot/asvz_plan.py` | Unchanged copies from the desktop app (release V1). `asvz_plan.py` does lesson search and weekly plans; this scheduler adds a run lock and weekly sync. Setup copies them into the bot folder. |
| `bot/keep_awake.py` | Server version of the laptop sleep helper: does nothing (servers don't sleep). |
| `web/server.py` | The API + page server (Python standard library only). |
| `web/static/` | The page (HTML/CSS/JS, PWA manifest, service worker, icons). |
| `deploy/inspect.sh` | **Read-only** look at how the bot runs on the server. |
| `deploy/setup.sh` | Install / re-apply. Backs up before changing anything. |
| `deploy/update.sh` | `git pull` + re-apply. |
| `deploy/rollback.sh` | Back to how it was before vb. |
| `deploy/asvz-login.sh` | Save/change the university login on the server. |
| `tests/` | Fake bot + fake ASVZ data, to try the page without a server. |

`asvz_bot.py` (the Selenium enrollment code) is **not** in this repo and is
never touched. Setup only continues if the server's copy is the known version.

## Install

Check each command's prompt: **Termux** means your phone, **server** means
`root@…`. Run them one at a time.

**1. DNS (Spaceship website, in the phone browser).** Add an A record: host
`vb`, value = your server's IP. Do it first so the HTTPS certificate works
right away.

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
If it finds something unexpected (bot folder layout, changed files), it stops
before changing anything and tells you what to send.

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
- **Change the university login**, server:
  ```
  bash /opt/vb/deploy/asvz-login.sh
  ```
- **Logs**, server: `journalctl -u vb-scheduler -n 50 --no-pager` (sign-ups),
  `journalctl -u vb-web -n 50 --no-pager` (page). The Activity page shows the
  first one too, plus the free-spot watcher logs.
- **Undo everything**, server:
  ```
  bash /opt/vb/deploy/rollback.sh
  ```

## What setup changes on the server

All of it is backed up first to `/var/backups/vb/` (bot folder code + data as
`bot-<time>.tar.gz`, Caddyfile, unit files). Nothing is deleted.

1. Bot folder:
   - `asvz_scheduler.py` is replaced by the desktop version; the old one stays next to it as `asvz_scheduler.py.before-vb-<time>`.
   - `asvz_plan.py` and `keep_awake.py` are added.
   - `.asvz-bot.json` is set to mode 600.
2. `vb-scheduler.service` + `.timer` (system units) run
   `asvz_scheduler.py run` every 2 minutes as the bot folder's owner. This is
   the same command the old per-user `asvz-scheduler.timer` ran. That old
   timer is switched off (its files stay), so there aren't two of them.
3. `vb-web.service` on `127.0.0.1:8710`, running as the bot folder's owner,
   because it edits the bot's files, just like the desktop app does. If that
   owner is not root, a sudo rule allows exactly three `systemctl` commands for
   the vb units and nothing else.
4. `/etc/caddy/vb.caddy` plus one `import` line in the Caddyfile. It runs
   `caddy validate` before the reload; if validation fails, the old config is
   put back.

## Try it without a server

```
python3 tests/devserver.py /tmp/vbtest 8710
```
Open http://127.0.0.1:8710, log in as `test` / `test-password-123`. It uses
made-up lessons and a stub `asvz_bot.py` that only writes `enrolled.log`.
