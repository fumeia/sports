#!/usr/bin/env python3
# coding: utf-8
"""
server.py — "vb", the phone remote for the ASVZ bot.

A small HTTP service (standard library only) that runs next to the
existing bot on the server and offers what the desktop app (app.py) does:

  Discover   ASVZ's public lesson search, weekly offers, "Sign me up"
  My plan    plan entries, queued sign-ups, free-spot watchers
  Activity   the bot's existing timer on/off, run now, log

It never enrolls by itself and never sees the university login. It
leaves the bot folder's code alone and only touches what the bot's own
text menu touches: it adds/removes jobs in asvz_schedule.json and calls
"<scheduler> add" / "stop-watch". Enrollment happens exactly as before,
in "<scheduler> run", started by the bot's existing timer.

The bot's scheduler saves the whole job list at the end of every run and
takes no lock, so a job written while a run is going could get lost.
Hence every change is applied only while no run is active (otherwise it
waits, see Pending), and for 15 minutes after a change it is checked
again and repaired if a run that started at the same moment undid it.

Weekly plans (asvz_plan.py, from the desktop app) live in the data dir,
not in the bot folder. New dates are queued by this service, only about
a week ahead (QUEUE_DAYS), so the job list stays as short as when you
added them by hand every Sunday.

Listens on 127.0.0.1 only; Caddy puts HTTPS in front. The page has its
own login (scrypt password hash in the data dir, 30-day session cookie).

    python3 server.py                 run (settings from VB_* env vars)
    python3 server.py set-password    set the page login (asks twice)
"""

import getpass
import hashlib
import hmac
import http.server
import json
import logging
import os
import secrets
import subprocess
import sys
import threading
import time
import urllib.parse
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(HERE, "static")

BOT_DIR = os.path.abspath(os.environ.get("VB_BOT_DIR", "."))
DATA_DIR = os.path.abspath(os.environ.get("VB_DATA_DIR", "/var/lib/vb"))
HOST = os.environ.get("VB_HOST", "127.0.0.1")
PORT = int(os.environ.get("VB_PORT", "8710"))
BOT_PYTHON = os.environ.get("VB_BOT_PYTHON") or sys.executable
SCHEDULER_NAME = os.environ.get("VB_SCHEDULER_SCRIPT", "asvz_scheduler.py")
SCHEDULER = os.path.join(BOT_DIR, SCHEDULER_NAME)
# The bot's existing timer: <unit>.timer starts <unit>.service.
TIMER_UNIT = os.environ.get("VB_TIMER_UNIT", "asvz-scheduler")
TIMER_SCOPE = os.environ.get("VB_TIMER_SCOPE", "user")        # "user" or "system"
TIMER_USER = os.environ.get("VB_TIMER_USER", "")              # owner of a user unit
QUEUE_DAYS = int(os.environ.get("VB_QUEUE_DAYS", "8"))
# Dev/test only: allow the session cookie over plain http://localhost.
INSECURE_COOKIE = os.environ.get("VB_INSECURE_COOKIE") == "1"
SYSTEMCTL = os.environ.get("VB_SYSTEMCTL", "/usr/bin/systemctl")
JOURNALCTL = os.environ.get("VB_JOURNALCTL", "/usr/bin/journalctl")

AUTH_FILE = os.path.join(DATA_DIR, "auth.json")
SESSIONS_FILE = os.path.join(DATA_DIR, "sessions.json")
SESSION_DAYS = 30
COOKIE = "vb_session"

CREDS_FILE = os.path.join(BOT_DIR, ".asvz-bot.json")
WATCHERS_FILE = os.path.join(BOT_DIR, "asvz_watchers.json")
WATCHER_LOG_DIR = os.path.join(BOT_DIR, "watcher_logs")

SPORTS_TTL = 3600
OFFERS_TTL = 600
LOOP_SECONDS = 15
REPAIR_WINDOW = 15 * 60

sys.path.insert(0, HERE)
import asvz_plan as plan  # noqa: E402  (copy of the desktop app's module)

# Plan + lock in our data dir; the job list is the bot's own file.
plan.PLAN_FILENAME = os.path.join(DATA_DIR, "asvz_plan.json")
plan.SCHEDULE_LOCK = os.path.join(DATA_DIR, ".schedule.lock")
plan.SCHEDULE_FILENAME = os.path.join(BOT_DIR, "asvz_schedule.json")

_bookable = plan.bookable


def _bookable_soon(lessons, now=None):
    """Like asvz_plan.bookable, but only dates whose enrollment opens
    within QUEUE_DAYS. Later dates get queued by a later sync."""
    horizon = (now or datetime.now().astimezone()) + timedelta(days=QUEUE_DAYS)
    return [l for l in _bookable(lessons, now)
            if (l["oe_from"] or l["start"] - timedelta(days=1)) <= horizon]


plan.bookable = _bookable_soon

log = logging.getLogger("vb")


# ----------------------------------------------------------------------
# small helpers
# ----------------------------------------------------------------------

def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return default


def write_private(path, data):
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (TypeError, ValueError):
        return False


def local_iso(naive_iso):
    """Scheduler times are naive local time of this machine (that's what
    enroll() compares against). Send them with an offset, so the page can
    show them in Zurich time whatever the server's time zone is."""
    if not naive_iso:
        return None
    try:
        return datetime.fromisoformat(naive_iso).astimezone().isoformat(timespec="minutes")
    except ValueError:
        return None


def mask(username):
    if not username:
        return ""
    name, at, domain = username.partition("@")
    return (name[:2] + "…" if len(name) > 2 else name) + at + domain


def run(cmd, timeout=30, env=None):
    try:
        r = subprocess.run(cmd, cwd=BOT_DIR, capture_output=True, text=True,
                           timeout=timeout, stdin=subprocess.DEVNULL, env=env)
        return r.returncode, (r.stdout + r.stderr).strip()
    except FileNotFoundError as e:
        return 127, str(e)
    except subprocess.TimeoutExpired:
        return 124, f"Timed out after {timeout}s"


# ----------------------------------------------------------------------
# the bot's existing timer
# ----------------------------------------------------------------------

def systemctl(*args, change=False):
    if TIMER_SCOPE == "user":
        if os.geteuid() == 0 and TIMER_USER:
            return run([SYSTEMCTL, "--user", "-M", f"{TIMER_USER}@", *args], timeout=30)
        return run([SYSTEMCTL, "--user", *args], timeout=30)
    # System unit, not running as root: deploy/setup.sh's sudo rule
    # allows exactly the changing commands used here.
    prefix = ["sudo", "-n"] if change and os.geteuid() != 0 else []
    return run(prefix + [SYSTEMCTL, *args], timeout=30)


def timer_active():
    return systemctl("is-active", f"{TIMER_UNIT}.timer")[1].strip() == "active"


def timer_next():
    out = systemctl("show", f"{TIMER_UNIT}.timer", "--property=NextElapseUSecRealtime",
                    "--value")[1].strip()
    return out if out and not out.startswith("n/a") and "\n" not in out else None


def journal(lines):
    unit = f"{TIMER_UNIT}.service"
    if TIMER_SCOPE == "user":
        cmd = ([JOURNALCTL, "--user-unit", unit] if os.geteuid() == 0
               else [JOURNALCTL, "--user", "-u", unit])
    else:
        cmd = [JOURNALCTL, "-u", unit]
    return run(cmd + ["-n", str(lines), "--no-pager", "-o", "short-iso"], timeout=15)[1]


def kick_scheduler():
    """Process due jobs now (like the desktop app after "Sign me up").
    Starting the unit while it already runs does nothing, so this never
    causes two runs at once."""
    if timer_active():
        systemctl("start", "--no-block", f"{TIMER_UNIT}.service", change=True)


def _boot_time():
    with open("/proc/stat") as f:
        for line in f:
            if line.startswith("btime"):
                return int(line.split()[1])
    return 0


def scheduler_runs():
    """[(pid, start_unix_time)] of running "<scheduler> run" processes —
    started by the timer, "Run now", or by hand. (Option 4 of the text
    menu runs inside the menu process and can't be told apart; it's the
    same situation as using the menu while the timer runs.)"""
    found = []
    tick = os.sysconf("SC_CLK_TCK")
    boot = None
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                args = [a.decode(errors="replace") for a in f.read().split(b"\0") if a]
        except OSError:
            continue
        for i, a in enumerate(args):
            if os.path.basename(a) == SCHEDULER_NAME and "run" in args[i + 1:]:
                try:
                    with open(f"/proc/{pid}/stat") as f:
                        ticks = int(f.read().rsplit(")", 1)[1].split()[19])
                    boot = boot if boot is not None else _boot_time()
                    found.append((int(pid), boot + ticks / tick))
                except (OSError, ValueError, IndexError):
                    found.append((int(pid), 0))
                break
    return found


# ----------------------------------------------------------------------
# changing the job list safely (see module docstring)
# ----------------------------------------------------------------------

WRITE_LOCK = threading.RLock()
PENDING = []      # [(label, fn)] waiting for the running scheduler pass to end
EXPECT = {}       # str(lesson_id) -> {"until", "present", "job"}  (repair window)


def _ids(jobs):
    return {str(j.get("lesson_id")): j for j in jobs}


def _after_change(before, started):
    """Bookkeeping after one of our changes to the job list:
    - drop jobs we just added for a lesson that was already queued
      (e.g. added by number in the text menu — ids there are strings),
    - jobs added while enrollment is already open: set enrollment_start
      to now, because the bot drops non-watch jobs 5 minutes after it,
    - remember what we changed, to repair it if a run undid it."""
    with plan.schedule_lock():
        jobs = plan.load_schedule()
        old = {j["id"] for j in before}
        old_lessons = {str(j.get("lesson_id")) for j in before}
        now = datetime.now()
        keep, changed = [], False
        for j in jobs:
            if j["id"] not in old and str(j.get("lesson_id")) in old_lessons:
                changed = True
                continue
            if (j["id"] not in old and not j.get("watch_free_slot")
                    and datetime.fromisoformat(j["enrollment_start"]) < now):
                j["enrollment_start"] = now.isoformat(timespec="minutes")
                changed = True
            keep.append(j)
        if changed:
            plan.save_schedule(keep)
            jobs = keep
    b, a = _ids(before), _ids(jobs)
    until = time.time() + REPAIR_WINDOW
    for lid in a.keys() - b.keys():
        EXPECT[lid] = {"until": until, "present": True, "job": a[lid]}
    for lid in b.keys() - a.keys():
        EXPECT[lid] = {"until": until, "present": False, "job": b[lid]}
    if any(t < started for _, t in scheduler_runs()):
        log.warning("A scheduler run started while we changed the job list — will re-check after it.")


def change_now(fn):
    """Runs fn (which edits the job list through asvz_plan) while no
    scheduler pass is active. Returns fn's result."""
    with WRITE_LOCK:
        before = plan.load_schedule()
        started = time.time()
        result = fn()
        _after_change(before, started)
        return result


def change(label, fn):
    """Apply now if the bot is idle, else queue it. Returns
    (applied, result)."""
    with WRITE_LOCK:
        if not scheduler_runs() and not PENDING:
            return True, change_now(fn)
        PENDING.append((label, fn))
        log.info(f"Bot is busy — queued: {label}")
        return False, None


def repair():
    """Re-apply our recent changes that a scheduler run overwrote."""
    now_ts, now = time.time(), datetime.now()
    for lid in [k for k, e in EXPECT.items() if e["until"] < now_ts]:
        del EXPECT[lid]
    if not EXPECT:
        return
    with plan.schedule_lock():
        jobs = plan.load_schedule()
        have = _ids(jobs)
        changed = False
        for lid, e in EXPECT.items():
            if e["present"] and lid not in have:
                job = e["job"]
                # Gone because it ran (enrolled / handed to a watcher) is fine;
                # only put back what wasn't due yet.
                if datetime.fromisoformat(job["run_at"]) > now + timedelta(minutes=1):
                    job = dict(job, id=max((j["id"] for j in jobs), default=0) + 1)
                    jobs.append(job)
                    e["job"] = job
                    changed = True
                    log.warning(f"Re-added lesson {lid}: a scheduler run had overwritten it.")
            elif not e["present"] and lid in have:
                jobs = [j for j in jobs if str(j.get("lesson_id")) != lid]
                changed = True
                log.warning(f"Removed lesson {lid} again: a scheduler run had put it back.")
        if changed:
            plan.save_schedule(jobs)


def background_loop():
    """Every LOOP_SECONDS, while the bot is idle: apply queued changes,
    repair, queue newly published weekly dates (throttled to 30 min
    inside asvz_plan.sync)."""
    while True:
        time.sleep(LOOP_SECONDS)
        try:
            with WRITE_LOCK:
                if scheduler_runs():
                    continue
                repair()
                while PENDING and not scheduler_runs():
                    label, fn = PENDING.pop(0)
                    try:
                        change_now(fn)
                        log.info(f"Applied queued change: {label}")
                    except Exception as e:
                        log.error(f"Queued change failed ({label}): {e}")
                if not scheduler_runs():
                    n = change_now(plan.sync)
                    if n:
                        log.info(f"Weekly plan: queued {n} new date(s).")
                        kick_scheduler()
        except Exception:
            log.exception("Background loop")


# ----------------------------------------------------------------------
# login
# ----------------------------------------------------------------------

def _scrypt(password, salt, n=2 ** 14, r=8, p=1):
    return hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, dklen=32)


def set_password_interactive():
    os.makedirs(DATA_DIR, exist_ok=True)
    current = load_json(AUTH_FILE, {})
    default = current.get("user") or "me"
    user = input(f"Login name for the page [{default}]: ").strip() or default
    while True:
        pw = getpass.getpass("New password (at least 10 characters): ")
        if len(pw) < 10:
            print("Too short.")
            continue
        if getpass.getpass("Repeat it: ") != pw:
            print("They don't match, try again.")
            continue
        break
    salt = secrets.token_bytes(16)
    write_private(AUTH_FILE, {"user": user, "salt": salt.hex(), "hash": _scrypt(pw, salt).hex(),
                              "n": 2 ** 14, "r": 8, "p": 1})
    # A new password logs out every phone/browser that was signed in.
    write_private(SESSIONS_FILE, {})
    print(f"Saved. Log in on the page as '{user}'.")


def check_password(user, password):
    a = load_json(AUTH_FILE, None)
    if not a or not a.get("hash"):
        return False
    try:
        got = _scrypt(password, bytes.fromhex(a["salt"]), a.get("n", 2 ** 14),
                      a.get("r", 8), a.get("p", 1))
    except (ValueError, MemoryError):
        return False
    return hmac.compare_digest(got.hex(), a["hash"]) and hmac.compare_digest(
        user.encode(), a.get("user", "").encode())


class Sessions:
    """Session tokens (only their SHA-256 is stored) with expiry; kept on
    disk so a service restart doesn't log the phone out."""

    def __init__(self):
        self.lock = threading.Lock()
        self.data = load_json(SESSIONS_FILE, {})

    @staticmethod
    def _h(token):
        return hashlib.sha256(token.encode()).hexdigest()

    def _save(self):
        try:
            write_private(SESSIONS_FILE, self.data)
        except OSError as e:
            log.warning(f"Could not save sessions: {e}")

    def create(self):
        token = secrets.token_urlsafe(32)
        with self.lock:
            now = time.time()
            self.data = {k: v for k, v in self.data.items() if v > now}
            self.data[self._h(token)] = now + SESSION_DAYS * 86400
            self._save()
        return token

    def valid(self, token):
        if not token:
            return False
        with self.lock:
            exp = self.data.get(self._h(token))
            return bool(exp and exp > time.time())

    def drop(self, token):
        with self.lock:
            if self.data.pop(self._h(token or ""), None):
                self._save()


class Throttle:
    """Slows down password guessing: at most 5 failed logins per address
    and 20 overall per 15 minutes."""
    WINDOW, PER_IP, TOTAL = 900, 5, 20

    def __init__(self):
        self.lock = threading.Lock()
        self.fails = []  # (time, ip)

    def blocked(self, ip):
        with self.lock:
            cutoff = time.time() - self.WINDOW
            self.fails = [(t, i) for t, i in self.fails if t > cutoff]
            return (len(self.fails) >= self.TOTAL
                    or sum(1 for _, i in self.fails if i == ip) >= self.PER_IP)

    def failed(self, ip):
        with self.lock:
            self.fails.append((time.time(), ip))


SESSIONS = Sessions()
THROTTLE = Throttle()


# ----------------------------------------------------------------------
# ASVZ data (cached, like the desktop app's in-memory cache)
# ----------------------------------------------------------------------

_cache = {}
_cache_lock = threading.Lock()


def cached(key, ttl, fetch, force=False):
    with _cache_lock:
        hit = _cache.get(key)
    if hit and not force and time.time() - hit[0] < ttl:
        return hit[1]
    value = fetch()
    with _cache_lock:
        _cache[key] = (time.time(), value)
    return value


def get_offers(sport, force=False):
    if sport == "all":
        return cached("offers:all", OFFERS_TTL, plan.timetable_offers, force)
    return cached(f"offers:{sport}", OFFERS_TTL,
                  lambda: plan.group_offers(plan.fetch_lessons(sport)), force)


def offer_json(o):
    nxt = next(iter(_bookable(o["lessons"])), None)
    return {
        "key": o["key"], "sport_id": o["sport_id"], "sport_name": o["sport_name"],
        "title": o["title"], "weekday": o["weekday"], "start": o["start"], "end": o["end"],
        "location": o["location"], "niveau": o["niveau"], "instructor": o["instructor"],
        "partial": bool(o.get("partial")),
        "next": {"start": nxt["start"].isoformat(), "free": nxt["free"], "max": nxt["max"]}
        if nxt else None,
    }


def offers_for_keys(sport, keys):
    by_key = {o["key"]: o for o in get_offers(sport)}
    missing = [k for k in keys if k not in by_key]
    if missing:  # cache expired meanwhile and ASVZ changed: reload once
        by_key = {o["key"]: o for o in get_offers(sport, force=True)}
    return [by_key[k] for k in keys if k in by_key]


# ----------------------------------------------------------------------
# plan / schedule / watchers
# ----------------------------------------------------------------------

def plan_state():
    p = plan.load_plan()
    jobs = plan.load_schedule()
    watchers = load_json(WATCHERS_FILE, {})
    out_jobs = []
    for j in sorted(jobs, key=lambda j: j.get("lesson_start", "")):
        out_jobs.append({
            "id": j["id"], "lesson_id": j.get("lesson_id"),
            "lesson_url": j.get("lesson_url") or
            f"https://schalter.asvz.ch/tn/lessons/{j.get('lesson_id')}",
            "title": j.get("title"), "location": j.get("location"), "niveau": j.get("niveau"),
            "lesson_start": local_iso(j.get("lesson_start")),
            "enrollment_start": local_iso(j.get("enrollment_start")),
            "run_at": local_iso(j.get("run_at")),
            "watch": bool(j.get("watch_free_slot")),
            "from_plan": bool(j.get("offer_key")),
        })
    out_watchers = []
    for job_id, w in sorted(watchers.items(), key=lambda kv: int(kv[0])):
        if not pid_alive(w.get("pid")):
            continue
        out_watchers.append({"id": int(job_id), "lesson_id": w.get("lesson_id"),
                             "title": w.get("title"), "since": local_iso(w.get("spawned_at"))})
    out_entries = [{k: e.get(k) for k in ("key", "sport_name", "title", "weekday", "start",
                                          "end", "location", "niveau", "instructor",
                                          "weekly", "watch")}
                   for e in sorted(p["entries"], key=lambda e: (e["weekday"], e["start"]))]
    return {"entries": out_entries, "jobs": out_jobs, "watchers": out_watchers,
            "pending": [label for label, _ in PENDING], "queue_days": QUEUE_DAYS,
            "last_sync": local_iso(p.get("last_sync"))}


def credentials_status():
    c = load_json(CREDS_FILE, {})
    ok = all(c.get(k) for k in ("organisation", "username", "password"))
    return {"set": ok, "organisation": c.get("organisation") if ok else None,
            "username": mask(c.get("username")) if ok else None}


def read_log(lines=200):
    parts = [f"── Bot runs ({TIMER_UNIT}.service) ──", journal(lines) or "(nothing yet)"]
    try:
        logs = sorted((os.path.join(WATCHER_LOG_DIR, f) for f in os.listdir(WATCHER_LOG_DIR)
                       if f.endswith(".log")), key=os.path.getmtime)[-3:]
    except FileNotFoundError:
        logs = []
    for path in logs:
        with open(path, encoding="utf-8", errors="replace") as f:
            tail = f.readlines()[-60:]
        parts += ["", f"── Free-spot watcher {os.path.basename(path)} ──", "".join(tail).rstrip()]
    return "\n".join(parts)


def bot_env():
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    for var in ("CHROME_BIN", "CHROMEDRIVER_BIN"):
        if os.environ.get("VB_" + var):
            env[var] = os.environ["VB_" + var]
    return env


BUSY_TEXT = ("The bot is busy with a run right now (up to ~20 minutes while it waits for an "
             "enrollment to open). Saved — it's applied as soon as the run is done.")


# ----------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------

STATIC_TYPES = {
    ".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8", ".svg": "image/svg+xml",
    ".png": "image/png", ".webmanifest": "application/manifest+json",
}

SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; "
                               "style-src 'self'; script-src 'self'; frame-ancestors 'none'; "
                               "base-uri 'none'; form-action 'self'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
}


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "vb"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        log.info("%s %s", self.client_ip(), fmt % args)

    # ---- plumbing
    def client_ip(self):
        ip = self.client_address[0]
        fwd = self.headers.get("X-Forwarded-For")
        if fwd and ip in ("127.0.0.1", "::1"):
            ip = fwd.split(",")[-1].strip()  # the address Caddy saw
        return ip

    def cookie(self, name):
        for part in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == name:
                return v
        return None

    def send(self, status, body, ctype="application/json", headers=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False, default=str)
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, v)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 64 * 1024:
            raise ApiError(413, "Request too large")
        try:
            data = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            raise ApiError(400, "Bad JSON")
        if not isinstance(data, dict):
            raise ApiError(400, "Bad JSON")
        return data

    def session_cookie(self, token, max_age):
        attrs = [f"{COOKIE}={token}", "Path=/", "HttpOnly", "SameSite=Strict",
                 f"Max-Age={max_age}"]
        if not INSECURE_COOKIE:
            attrs.append("Secure")
        return "; ".join(attrs)

    # ---- verbs
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def dispatch(self, method):
        url = urllib.parse.urlsplit(self.path)
        path = url.path
        query = dict(urllib.parse.parse_qsl(url.query))
        try:
            if not path.startswith("/api/"):
                if method != "GET":
                    raise ApiError(405, "Method not allowed")
                return self.static(path)
            if method == "POST" and self.headers.get("X-VB") != "1":
                # Custom header: a page on another site can't send it
                # without a CORS preflight, which we never answer.
                raise ApiError(403, "Missing X-VB header")
            name = method.lower() + "_" + path[5:].strip("/").replace("/", "_")
            handler = getattr(self, "api_" + name, None)
            if handler is None:
                raise ApiError(404, "Not found")
            if name not in ("post_login", "get_me") and not SESSIONS.valid(self.cookie(COOKIE)):
                raise ApiError(401, "Please log in")
            result = handler(self.body() if method == "POST" else query)
            if isinstance(result, tuple):  # (status, body, ctype, headers)
                self.send(*result)
            else:
                self.send(200, result if result is not None else {"ok": True})
        except ApiError as e:
            self.send(e.status, {"error": str(e)})
        except Exception as e:  # report, don't kill the thread silently
            log.exception("Request failed")
            self.send(500, {"error": f"{type(e).__name__}: {e}"})

    def static(self, path):
        if path in ("", "/"):
            path = "/index.html"
        full = os.path.realpath(os.path.join(STATIC_DIR, path.lstrip("/")))
        if not full.startswith(STATIC_DIR + os.sep) or not os.path.isfile(full):
            raise ApiError(404, "Not found")
        ext = os.path.splitext(full)[1]
        with open(full, "rb") as f:
            data = f.read()
        headers = {"Cache-Control": "no-cache"}
        if path == "/sw.js":
            headers["Service-Worker-Allowed"] = "/"
        self.send(200, data, STATIC_TYPES.get(ext, "application/octet-stream"), headers)

    # ---- login
    def api_get_me(self, q):
        a = load_json(AUTH_FILE, {})
        return {"logged_in": SESSIONS.valid(self.cookie(COOKIE)),
                "password_set": bool(a.get("hash"))}

    def api_post_login(self, b):
        ip = self.client_ip()
        if THROTTLE.blocked(ip):
            raise ApiError(429, "Too many failed attempts. Wait 15 minutes.")
        if not check_password(str(b.get("user", "")), str(b.get("password", ""))):
            THROTTLE.failed(ip)
            log.warning(f"Failed login from {ip}")
            time.sleep(1)
            raise ApiError(401, "Wrong login name or password")
        token = SESSIONS.create()
        return (200, {"ok": True}, "application/json",
                {"Set-Cookie": self.session_cookie(token, SESSION_DAYS * 86400)})

    def api_post_logout(self, b):
        SESSIONS.drop(self.cookie(COOKIE))
        return (200, {"ok": True}, "application/json", {"Set-Cookie": self.session_cookie("", 0)})

    # ---- status / activity
    def api_get_status(self, q):
        return {
            "server_time": datetime.now().astimezone().isoformat(timespec="seconds"),
            "timer": timer_active(), "timer_next": timer_next(),
            "timer_unit": f"{TIMER_UNIT}.timer", "running": bool(scheduler_runs()),
            "pending": len(PENDING), "credentials": credentials_status(), "bot_dir": BOT_DIR,
            "scheduler_ok": os.path.isfile(SCHEDULER),
        }

    def api_get_log(self, q):
        return {"text": read_log()}

    def api_post_timer(self, b):
        on = bool(b.get("on"))
        code, out = systemctl("enable" if on else "disable", "--now", f"{TIMER_UNIT}.timer",
                              change=True)
        if code:
            raise ApiError(500, out or "systemctl failed")
        return {"timer": timer_active(),
                "message": "Background checks on: due sign-ups are processed automatically again."
                if on else "Background checks off: the bot signs up for nothing until you turn them on."}

    def api_post_run(self, b):
        code, out = systemctl("start", "--no-block", f"{TIMER_UNIT}.service", change=True)
        if code:
            raise ApiError(500, out or "systemctl failed")
        return {"message": "Check started"}

    # ---- discover
    def api_get_sports(self, q):
        try:
            sports = cached("sports", SPORTS_TTL, plan.fetch_sports, q.get("force") == "1")
        except Exception as e:
            raise ApiError(502, f"Can't reach ASVZ: {e}")
        return {"sports": sports, "timetable_days": plan.TIMETABLE_DAYS}

    def api_get_offers(self, q):
        sport = q.get("sport") or "all"
        try:
            offers = get_offers(sport, q.get("force") == "1")
        except Exception as e:
            raise ApiError(502, f"Couldn't load offers: {e}")
        return {"offers": [offer_json(o) for o in offers]}

    def _chosen(self, b):
        keys = b.get("keys")
        if not isinstance(keys, list) or not keys or len(keys) > 200:
            raise ApiError(400, "Choose at least one offer")
        offers = offers_for_keys(str(b.get("sport") or "all"), [str(k) for k in keys])
        if not offers:
            raise ApiError(409, "These offers are gone from ASVZ — reload the list")
        return offers

    def api_post_signup(self, b):
        if not credentials_status()["set"]:
            raise ApiError(409, "The bot has no ASVZ login on the server yet. "
                                "Set it there first (see README).")
        offers = plan.complete_offers(self._chosen(b))  # network: before taking any lock
        weekly, watch = bool(b.get("weekly")), bool(b.get("watch"))
        applied, result = change(f"Sign up: {', '.join(o['title'] for o in offers)}",
                                 lambda: plan.add_offers(offers, weekly, watch))
        if not applied:
            return {"offers": len(offers), "queued": 0, "messages": [], "pending": True,
                    "message": BUSY_TEXT}
        count, messages = result
        kick_scheduler()
        return {"offers": len(offers), "queued": count, "messages": messages}

    def api_post_ics(self, b):
        if b.get("plan"):
            entries, name = plan.load_plan()["entries"], "asvz-plan.ics"
        else:
            entries = plan.preview_entries(self._chosen(b), bool(b.get("weekly")))
            name = "asvz-selection.ics"
        return (200, plan.build_ics(entries), "text/calendar; charset=utf-8",
                {"Content-Disposition": f'attachment; filename="{name}"'})

    # ---- my plan
    def api_get_plan(self, q):
        return plan_state()

    def _changed(self, applied, done_text):
        return {"pending": not applied, "message": done_text if applied else BUSY_TEXT}

    def api_post_plan_weekly(self, b):
        key, weekly = str(b.get("key", "")), bool(b.get("weekly"))

        def go():
            plan.set_weekly(key, weekly)
            return plan.sync(force=True) if weekly else 0
        try:
            applied, n = change("Every week " + ("on" if weekly else "off"), go)
        except Exception as e:
            return {"message": f"Saved, but couldn't reach ASVZ: {e}"}
        if applied and n:
            kick_scheduler()
        return self._changed(applied, f"{n} new date(s) queued" if n else "")

    def api_post_plan_remove(self, b):
        key = str(b.get("key", ""))
        applied, n = change("Remove offer", lambda: plan.remove_entry(key))
        return self._changed(applied, f"Removed" + (f" with {n} queued date(s)" if n else ""))

    def api_post_plan_sync(self, b):
        if scheduler_runs():
            return {"queued": 0, "message": "The bot is busy with a run; new dates are looked "
                                            "for automatically right after it."}
        try:
            n = change_now(lambda: plan.sync(force=True))
        except Exception as e:
            raise ApiError(502, f"Couldn't reach ASVZ: {e}")
        if n:
            kick_scheduler()
        return {"queued": n, "message": f"{n} new date(s) queued" if n else
                f"Up to date — dates are queued about {QUEUE_DAYS} days ahead"}

    def api_post_jobs_remove(self, b):
        try:
            job_id = int(b.get("id"))
        except (TypeError, ValueError):
            raise ApiError(400, "Bad job id")
        applied, _ = change("Skip a date", lambda: plan.remove_job(job_id))
        return self._changed(applied, "Date skipped")

    def api_post_jobs_add(self, b):
        """The text menu's "1) Add a lesson": by lesson number, through the
        bot's own scheduler (reads the lesson page with Chrome, ~10–30 s)."""
        lesson_id = str(b.get("lesson_id", "")).strip()
        if "/lessons/" in lesson_id:
            lesson_id = lesson_id.rstrip("/").rsplit("/", 1)[-1]
        if not lesson_id.isdigit() or len(lesson_id) > 10:
            raise ApiError(400, "Lesson number: digits only, e.g. 757666")
        if not credentials_status()["set"]:
            raise ApiError(409, "The bot has no ASVZ login on the server yet.")
        if lesson_id in _ids(plan.load_schedule()):
            raise ApiError(409, f"Lesson {lesson_id} is already queued.")
        cmd = [BOT_PYTHON, SCHEDULER, "add", lesson_id]
        if b.get("watch"):
            cmd += ["--watch-free-slot", "--watch-poll-interval", "30"]
        with WRITE_LOCK:
            if scheduler_runs():
                raise ApiError(409, "The bot is busy with a run right now. Try again in a few "
                                    "minutes (nothing was added).")
            out = change_now(lambda: run(cmd, timeout=240, env=bot_env()))
        code, text = out
        if code:
            raise ApiError(502, "Couldn't add the lesson:\n" + text[-1500:])
        kick_scheduler()
        return {"message": text.strip().splitlines()[-1] if text.strip() else "Added"}

    def api_post_watchers_stop(self, b):
        try:
            job_id = int(b.get("id"))
        except (TypeError, ValueError):
            raise ApiError(400, "Bad job id")
        code, out = run([BOT_PYTHON, SCHEDULER, "stop-watch", str(job_id)], timeout=60,
                        env=bot_env())
        return {"message": out.strip().splitlines()[-1] if out.strip() else "Watcher stopped"}


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if sys.argv[1:] == ["set-password"]:
        set_password_interactive()
        return
    if sys.argv[1:]:
        sys.exit(__doc__)
    os.makedirs(DATA_DIR, exist_ok=True)
    if not load_json(AUTH_FILE, {}).get("hash"):
        log.warning("No page password yet: run 'server.py set-password'. Nobody can log in.")
    if not os.path.isfile(SCHEDULER):
        log.error(f"Scheduler not found: {SCHEDULER}")
    threading.Thread(target=background_loop, daemon=True).start()
    log.info(f"vb listening on http://{HOST}:{PORT} (bot folder {BOT_DIR}, "
             f"timer {TIMER_UNIT}.timer [{TIMER_SCOPE}])")
    Server((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
