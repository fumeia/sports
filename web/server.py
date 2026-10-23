#!/usr/bin/env python3
# coding: utf-8
"""
server.py — "vb", the phone remote for the ASVZ bot.

A small HTTP service (standard library only) that runs next to the
existing bot on the server and does what the desktop app (app.py) does:

  Discover   ASVZ's public lesson search, weekly offers, "Sign me up"
  My plan    plan entries, queued sign-ups, free-spot watchers
  Activity   background checks (the vb-scheduler timer), run now, log

It never enrolls by itself and never sees the university login: it
writes the same files as the desktop app (asvz_plan.json,
asvz_schedule.json, through asvz_plan.py from the bot folder) and calls
the same scheduler commands. Enrollment happens in
"asvz_scheduler.py run", started by the vb-scheduler timer.

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
SCHED_UNIT = os.environ.get("VB_SCHEDULER_UNIT", "vb-scheduler")
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
SCHEDULER = os.path.join(BOT_DIR, "asvz_scheduler.py")

SPORTS_TTL = 3600
OFFERS_TTL = 600

sys.path.insert(0, BOT_DIR)
try:
    import asvz_plan as plan  # the bot's own module (installed by deploy/setup.sh)
except ImportError as e:  # reported by /api/status, the page shows it
    plan = None
    PLAN_IMPORT_ERROR = str(e)
else:
    PLAN_IMPORT_ERROR = None

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


def systemctl(*args):
    """Start/stop our own units. As root directly; otherwise through the
    sudo rule deploy/setup.sh installs for exactly these commands."""
    prefix = [] if os.geteuid() == 0 else ["sudo", "-n"]
    return run(prefix + [SYSTEMCTL, *args], timeout=30)


def timer_active():
    code, out = run([SYSTEMCTL, "is-active", f"{SCHED_UNIT}.timer"], timeout=10)
    return out.strip() == "active"


def timer_next():
    code, out = run([SYSTEMCTL, "show", f"{SCHED_UNIT}.timer",
                     "--property=NextElapseUSecRealtime", "--value"], timeout=10)
    return out.strip() or None


def scheduler_busy():
    code, out = run([SYSTEMCTL, "is-active", f"{SCHED_UNIT}.service"], timeout=10)
    return out.strip() in ("active", "activating")


def kick_scheduler():
    """Process due jobs now (like the desktop app after "Sign me up")."""
    if timer_active():
        systemctl("start", "--no-block", f"{SCHED_UNIT}.service")


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
    nxt = plan.next_bookable(o)
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
    entries = plan.load_plan()["entries"]
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
                   for e in sorted(entries, key=lambda e: (e["weekday"], e["start"]))]
    return {"entries": out_entries, "jobs": out_jobs, "watchers": out_watchers,
            "last_sync": local_iso(plan.load_plan().get("last_sync"))}


def credentials_status():
    c = load_json(CREDS_FILE, {})
    ok = all(c.get(k) for k in ("organisation", "username", "password"))
    return {"set": ok, "organisation": c.get("organisation") if ok else None,
            "username": mask(c.get("username")) if ok else None}


def read_log(lines=200):
    parts = [f"── Background checks (journalctl -u {SCHED_UNIT}) ──"]
    code, out = run([JOURNALCTL, "-u", f"{SCHED_UNIT}.service", "-n", str(lines),
                     "--no-pager", "-o", "short-iso"], timeout=15)
    parts.append(out or "(nothing yet)")
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
            if PLAN_IMPORT_ERROR and name not in ("post_login", "get_me", "post_logout",
                                                  "get_status", "get_log"):
                raise ApiError(503, f"asvz_plan.py missing in the bot folder: {PLAN_IMPORT_ERROR}")
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
            "timer": timer_active(), "timer_next": timer_next(), "running": scheduler_busy(),
            "credentials": credentials_status(), "bot_dir": BOT_DIR,
            "plan_error": PLAN_IMPORT_ERROR,
        }

    def api_get_log(self, q):
        return {"text": read_log()}

    def api_post_timer(self, b):
        on = bool(b.get("on"))
        code, out = systemctl("enable" if on else "disable", "--now", f"{SCHED_UNIT}.timer")
        if code:
            raise ApiError(500, out or "systemctl failed")
        return {"timer": timer_active(),
                "message": "Background checks on: due sign-ups are processed every 2 minutes."
                if on else "Background checks off: nothing gets signed up until you turn them on."}

    def api_post_run(self, b):
        code, out = systemctl("start", "--no-block", f"{SCHED_UNIT}.service")
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
        offers = self._chosen(b)
        weekly, watch = bool(b.get("weekly")), bool(b.get("watch"))
        count, messages = plan.add_offers(offers, weekly, watch)
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

    def api_post_plan_weekly(self, b):
        key, weekly = str(b.get("key", "")), bool(b.get("weekly"))
        plan.set_weekly(key, weekly)
        n = 0
        if weekly:
            try:
                n = plan.sync(force=True)
            except Exception as e:
                return {"queued": 0, "message": f"Saved, but couldn't reach ASVZ: {e}"}
            kick_scheduler()
        return {"queued": n}

    def api_post_plan_remove(self, b):
        return {"removed_jobs": plan.remove_entry(str(b.get("key", "")))}

    def api_post_plan_sync(self, b):
        try:
            n = plan.sync(force=True)
        except Exception as e:
            raise ApiError(502, f"Couldn't reach ASVZ: {e}")
        if n:
            kick_scheduler()
        return {"queued": n}

    def api_post_jobs_remove(self, b):
        try:
            job_id = int(b.get("id"))
        except (TypeError, ValueError):
            raise ApiError(400, "Bad job id")
        plan.remove_job(job_id)
        return {"ok": True}

    def api_post_jobs_add(self, b):
        """The text menu's "1) Add a lesson": by lesson number, read from
        the lesson page with Chrome (takes ~10–30 s)."""
        lesson_id = str(b.get("lesson_id", "")).strip()
        if "/lessons/" in lesson_id:
            lesson_id = lesson_id.rstrip("/").rsplit("/", 1)[-1]
        if not lesson_id.isdigit() or len(lesson_id) > 10:
            raise ApiError(400, "Lesson number: digits only, e.g. 757666")
        if not credentials_status()["set"]:
            raise ApiError(409, "The bot has no ASVZ login on the server yet.")
        cmd = [BOT_PYTHON, SCHEDULER, "add", lesson_id]
        if b.get("watch"):
            cmd += ["--watch-free-slot", "--watch-poll-interval", "30"]
        code, out = run(cmd, timeout=240, env=bot_env())
        if code:
            raise ApiError(502, "Couldn't add the lesson:\n" + out[-1500:])
        kick_scheduler()
        return {"message": out.strip().splitlines()[-1] if out.strip() else "Added"}

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
    if PLAN_IMPORT_ERROR:
        log.error(f"asvz_plan.py not found in {BOT_DIR}: {PLAN_IMPORT_ERROR}")
    log.info(f"vb listening on http://{HOST}:{PORT} (bot folder {BOT_DIR})")
    Server((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
