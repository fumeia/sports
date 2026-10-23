# vb — phone remote for the ASVZ bot

A web page (installable on the phone's home screen) that controls the ASVZ
enrollment bot **running on the server**. Enrollment still happens on the
server, by the bot's own scheduler and its own timer, exactly as before. The
phone only sends commands and shows status.

- **Week**:
  - The next sign-up with a live countdown, and whether the bot is healthy.
  - Your week as a timeline: queued / signing up now / signed up ✓ (with place
    number) / full, waiting for a spot / missed / failed, plus starred courses
    you're not signed up for, shown faded. Tap one to sign up, skip, switch
    every week on or off, star or unstar it.
  - Below the timeline: your courses.
- **Find**: ASVZ's lesson search (the desktop app's Discover), with ★ for
  sports and courses. Starred sports come first, and "Starred courses" lists
  all starred courses across sports.
- **Bot**: the bot's timer on/off, run now, phone notifications, every run of
  the bot (newest first, tap for its log), free-spot watchers, add a lesson by
  number.
- **Pause a week**: in the week view, "Pause this week" takes that week's
  sign-ups off the list, and weekly courses skip it. "Resume" puts them
  back, including the ones added in the text menu (if they're still ahead).
- **Overlap warning**: sign-ups at the same time are marked ⚠ in the week
  view, and you get a warning right when you sign up.
- **Calendar subscription**: a private link (Week → Calendar subscription)
  that Google Calendar or any calendar app subscribes to: your actual
  sign-ups (queued, signed up ✓, waiting for a spot), with skipped and
  paused dates left out. It needs no login, so the long random link is the
  key. You can switch it off or make a new one at any time. Google refreshes
  subscribed calendars only every few hours.
- **Sunday summary**: Sunday at 19:00, one notification with next week's
  sign-ups (or "paused").
- **Phone notifications** via the free ntfy app: signed up, full → watching,
  a freed-up spot taken, missed, failed attempt, and "the bot isn't running".
  The topic is a long random name and works like a password. Messages contain
  course names and times only. Results come from the bot's own log lines
  ("Job #3 finished successfully", "Your place number is: 12", the watcher's
  "Enrollment succeeded").

```
phone (home-screen app) ──HTTPS──▶ Caddy ──▶ vb-web (127.0.0.1:8710, user vb)
                                                 │ sudo, only this:
                                                 ▼
                                   /usr/local/sbin/vb-bot-helper (root)
                                                 │ job list · add · stop-watch · timer · log
                                                 ▼
   bot folder (unchanged): asvz_schedule.json · asvz_watchers.json · asvz_scheduler.py
                           asvz_scheduler.py run  ◀── the bot's existing timer
```

- **The bot is not changed.** Setup replaces, adds and chmods nothing in the
  bot folder, and the bot's timer stays the one that runs it. The page only
  touches what the text menu touches: it adds and removes jobs in
  `asvz_schedule.json`, and calls the scheduler's own `add`, `stop-watch` and
  (through the timer) `run`.
- **The page can't read your university login.** It runs as its own system
  user `vb`, which has no access to `/root` and therefore none to
  `.asvz-bot.json`. Everything that touches the bot goes through
  `/usr/local/sbin/vb-bot-helper` (the only thing `vb` may run as root, via
  sudo). It has a fixed list of actions: read and write the job list, the
  bot's own `add` / `stop-watch`, run now, timer on/off, read the bot's log.
  It never outputs the login file; it only says whether one exists. Before
  writing the job list it checks every job, so lesson links can only point
  to `https://schalter.asvz.ch/tn/lessons/<number>`.
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
| `web/week.py` | Favourites, results from the bot's log, the week view, ntfy notifications. |
| `deploy/vb-bot-helper` | The root helper (installed to `/usr/local/sbin`). |
| `web/static/` | The page (HTML/CSS/JS, PWA manifest, service worker, icons). |
| `deploy/inspect.sh` | **Read-only** look at how the bot runs on the server. |
| `deploy/setup.sh` | Install / re-apply (backs up the bot folder first, changes nothing in it). |
| `deploy/update.sh` | `git pull` + re-apply. |
| `deploy/rollback.sh` | Remove vb again (stop the page; tells you which Caddy line to remove). |
| `deploy/disable-unused.sh` | Turn off the unused torrent/media tools (`--undo` turns them back on). |
| `deploy/compare-scheduler.sh` | Read-only: the server scheduler vs. the release version. |
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

**6. Server:** add `import /etc/caddy/vb.caddy` to `/etc/caddy/Caddyfile`
yourself (next to your other import lines), then check and reload:
```
caddy validate --config /etc/caddy/Caddyfile
```
```
systemctl reload caddy
```

**7. Phone:** open `https://vb.<your domain>` in Chrome, log in, then
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
  Activity page shows each run of the bot's timer (newest first, refreshed
  every 10 s while open; tap a run for its log lines) and the free-spot
  watcher logs.
- **Remove vb**, server:
  ```
  bash /opt/vb/deploy/rollback.sh
  ```

## What setup changes on the server

- `/var/backups/vb/bot-<time>.tar.gz`: a copy of the bot folder (code, job
  list, login file; no venv or Chrome), mode 600.
- System user `vb` (no login shell, home `/var/lib/vb`).
- `/etc/systemd/system/vb-web.service`: the page, on `127.0.0.1:8710`, as
  user `vb`.
- `/usr/local/sbin/vb-bot-helper` + `/etc/vb/helper.conf` (both root-owned)
  and `/etc/sudoers.d/vb-web`: `vb` may run that helper as root, nothing
  else.
- `/var/lib/vb/`: page login, sessions, weekly plan, favourites, results, notification settings.
- `/etc/caddy/vb.caddy`: the site block for `vb.<domain>`. **Your Caddyfile
  is not edited.** At the end, setup prints the line to add yourself
  (`import /etc/caddy/vb.caddy`) and the `caddy validate` / `systemctl reload
  caddy` commands. Once that import exists, later updates validate before
  reloading, and put the old `vb.caddy` back if validation fails.

## Try it without a server

```
python3 tests/devserver.py /tmp/vbtest 8710
```
Open http://127.0.0.1:8710, log in as `test` / `test-password-123`. It uses
made-up lessons, a stub `asvz_bot.py` that only writes `enrolled.log`, and a
stand-in scheduler with the same file handling as the server's
(`VB_TEST_SCHEDULER=/path/to/asvz_scheduler.py` uses a real one instead).
