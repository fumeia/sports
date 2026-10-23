# coding: utf-8
"""
week.py — favourites, sign-up results, the week view and notifications.

Results: the bot's scheduler doesn't keep any record of what happened to
a job; it only logs it. The monitor (a thread in server.py) reads the
scheduler unit's journal and the free-spot watcher logs through the
helper every MONITOR_SECONDS and turns lines like "Job #3 finished
successfully" into a per-lesson result (history.json), and sends a phone
notification (ntfy) for each new one.

Also here: pausing a week, the overlap check, the Sunday summary, the
calendar feed.

Everything here lives in the data dir; nothing touches the bot folder.
"""

import hashlib
import json
import logging
import os
import re
import secrets
import threading
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

log = logging.getLogger("vb")
ZURICH = ZoneInfo("Europe/Zurich")
WEEKS_SHOWN = 8
HEALTH_LATE = timedelta(minutes=15)   # no run for this long while the timer is on -> alert


# ----------------------------------------------------------------------
# small json store
# ----------------------------------------------------------------------

class Store:
    def __init__(self, path, default):
        self.path, self.default = path, default
        self.lock = threading.Lock()

    def load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, ValueError):
            return json.loads(json.dumps(self.default))

    def save(self, data):
        tmp = self.path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    def update(self, fn):
        with self.lock:
            data = self.load()
            result = fn(data)
            self.save(data)
            return result


def setup(data_dir):
    global FAVS, HISTORY, NOTIFY, MONITOR, PAUSES, CALENDAR
    FAVS = Store(os.path.join(data_dir, "favorites.json"), {"sports": {}, "offers": {}})
    HISTORY = Store(os.path.join(data_dir, "history.json"), {"lessons": {}})
    NOTIFY = Store(os.path.join(data_dir, "notify.json"), {})
    MONITOR = Store(os.path.join(data_dir, "monitor.json"), {})
    PAUSES = Store(os.path.join(data_dir, "pauses.json"), {"weeks": {}})
    CALENDAR = Store(os.path.join(data_dir, "calendar.json"), {})


# ----------------------------------------------------------------------
# time helpers
# ----------------------------------------------------------------------

def aware(naive_or_aware_iso):
    """Scheduler times are naive local time of this machine; ASVZ times
    come with an offset. Returns an aware datetime (or None)."""
    if not naive_or_aware_iso:
        return None
    try:
        dt = datetime.fromisoformat(str(naive_or_aware_iso))
    except ValueError:
        return None
    return dt.astimezone() if dt.tzinfo is None else dt


def short(dt):
    """'Tue 27.10. 18:15' in Zurich time, like the desktop app."""
    z = dt.astimezone(ZURICH)
    return f"{['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'][z.weekday()]} {z:%d.%m. %H:%M}"


def ms(dt):
    return int(dt.timestamp() * 1000) if dt else None


# ----------------------------------------------------------------------
# favourites
# ----------------------------------------------------------------------

OFFER_FIELDS = ("key", "sport_id", "sport_name", "title", "weekday", "start", "end",
                "location", "niveau", "instructor")


def set_fav(kind, ident, on, meta=None):
    def go(f):
        bucket = f["sports" if kind == "sport" else "offers"]
        if on:
            bucket[ident] = meta or {}
        else:
            bucket.pop(ident, None)
        return f
    return FAVS.update(go)


# ----------------------------------------------------------------------
# results from the bot's log
# ----------------------------------------------------------------------

RX = {
    "start": re.compile(r"Starting job #(\d+)"),
    "exec": re.compile(r"Executing job #(\d+) for lesson (\d+)"),
    "place": re.compile(r"place number is:\s*(\S+)", re.I),
    "ok": re.compile(r"Job #(\d+) finished successfully"),
    "fail": re.compile(r"Job #(\d+) failed with error:\s*(.*?)(?:\.\s*Will retry.*)?$"),
    "missed": re.compile(r"Job #(\d+) missed the window"),
    "watch": re.compile(r"Handing job #(\d+) off to a detached watcher"),
    "spawnfail": re.compile(r"Failed to spawn watcher for job\s*#(\d+)"),
}


def events_from_run(run, jobs_seen):
    """[{type, job_id, lesson_id?, place?, error?}] for one scheduler run."""
    events, cur, lesson_of, place_of = [], None, {}, {}
    for line in run["lines"]:
        msg = line["msg"]
        if m := RX["start"].search(msg):
            cur = m.group(1)
        if m := RX["exec"].search(msg):
            cur = m.group(1)
            lesson_of[cur] = m.group(2)
        if (m := RX["place"].search(msg)) and cur:
            place_of[cur] = m.group(1).strip(".,")
        for kind in ("ok", "fail", "missed", "watch", "spawnfail"):
            if m := RX[kind].search(msg):
                jid = m.group(1)
                job = jobs_seen.get(jid, {})
                events.append({
                    "type": {"ok": "signed", "fail": "failed", "missed": "missed",
                             "watch": "watching", "spawnfail": "error"}[kind],
                    "job_id": jid, "t": line["t"],
                    "lesson_id": str(lesson_of.get(jid) or job.get("lesson_id") or ""),
                    "place": place_of.get(jid),
                    "error": m.group(2)[:200] if kind == "fail" else None,
                    "job": job,
                })
    return events


def last_session(text):
    """A watcher log is appended to by every watcher of that job id; each
    one starts with a "=====" header. Only the newest session counts."""
    parts = re.split(r"^=+\s*$", text, flags=re.M)
    return parts[-1] if len(parts) > 1 else text


def watcher_outcome(text):
    text = last_session(text)
    if re.search(r"\[watch\] Enrollment succeeded|Successfully enrolled", text):
        m = RX["place"].findall(text)
        return "watch_ok", (m[-1].strip(".,") if m else None)
    if re.search(r"giving up|Lesson already started", text, re.I):
        return "watch_gave_up", None
    return None, None


def record(event):
    """Stores the result for its lesson; returns True if it's new."""
    lid = event.get("lesson_id")
    if not lid:
        return False
    job = event.get("job") or {}

    def go(h):
        old = h["lessons"].get(lid, {})
        if old.get("status") == event["type"] and old.get("t") == event["t"]:
            return False
        h["lessons"][lid] = {
            "status": event["type"], "t": event["t"], "place": event.get("place") or old.get("place"),
            "error": event.get("error"),
            "title": job.get("title") or old.get("title"),
            "location": job.get("location") or old.get("location"),
            "lesson_start": job.get("lesson_start") or old.get("lesson_start"),
            "lesson_end": job.get("lesson_end") or old.get("lesson_end"),
            "lesson_url": job.get("lesson_url") or old.get("lesson_url"),
            "offer_key": job.get("offer_key") or old.get("offer_key"),
        }
        # forget results of lessons long past
        cutoff = datetime.now().astimezone() - timedelta(days=60)
        for k in [k for k, v in h["lessons"].items()
                  if (aware(v.get("lesson_start")) or cutoff) < cutoff]:
            del h["lessons"][k]
        return True
    return HISTORY.update(go)


# ----------------------------------------------------------------------
# notifications (ntfy)
# ----------------------------------------------------------------------

EVENT_TEXT = {  # type -> (title, tags, priority)
    "signed": ("Signed up", "white_check_mark", 4),
    "watch_ok": ("Got a freed-up spot", "white_check_mark", 4),
    "watching": ("Full - watching for a free spot", "eyes", 3),
    "watch_gave_up": ("No spot freed up", "x", 3),
    "missed": ("Missed a sign-up", "warning", 4),
    "failed": ("Sign-up attempt failed (will retry)", "warning", 3),
    "error": ("Bot error", "rotating_light", 4),
    "health_bad": ("The bot isn't running", "rotating_light", 5),
    "health_ok": ("The bot runs again", "white_check_mark", 3),
    "summary": ("Your week ahead", "calendar", 3),
}
SUMMARY_HOUR = 19          # Sunday, Zurich time
DEFAULT_EVENTS = {k: True for k in EVENT_TEXT}


def notify_settings():
    n = NOTIFY.load()
    changed = False
    if not n.get("topic"):
        n["topic"] = "asvz-" + secrets.token_urlsafe(18).replace("_", "").replace("-", "")[:22].lower()
        changed = True
    n.setdefault("server", "https://ntfy.sh")
    n.setdefault("enabled", False)
    n.setdefault("events", dict(DEFAULT_EVENTS))
    if changed:
        NOTIFY.save(n)
    return n


def send(title, message, tags="", priority=3, click=None, force=False):
    n = notify_settings()
    if not n["enabled"] and not force:
        return False
    body = {"topic": n["topic"], "title": title, "message": message, "priority": priority}
    if tags:
        body["tags"] = [tags]
    if click:
        body["click"] = click
    req = urllib.request.Request(n["server"].rstrip("/") + "/", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return 200 <= r.status < 300
    except Exception as e:
        log.warning(f"Notification failed: {e}")
        return False


def notify_event(ev, public_url=None):
    n = notify_settings()
    if not n["enabled"] or not n["events"].get(ev["type"], True):
        return
    title, tags, prio = EVENT_TEXT[ev["type"]]
    if ev.get("message"):  # events that bring their own text (summary)
        send(title, ev["message"], tags, prio, click=public_url)
        return
    h = HISTORY.load()["lessons"].get(ev.get("lesson_id") or "", {})
    what = h.get("title") or (f"Lesson {ev['lesson_id']}" if ev.get("lesson_id") else "")
    when = aware(h.get("lesson_start"))
    parts = [p for p in (what, short(when) if when else None, h.get("location"),
                         f"place {ev['place']}" if ev.get("place") else None, ev.get("error"),
                         ev.get("text")) if p]
    send(title, " · ".join(parts) or title, tags, prio, click=public_url)


# ----------------------------------------------------------------------
# the monitor: journal + watcher logs -> results, notifications, health
# ----------------------------------------------------------------------

JOB_FIELDS = ("lesson_id", "title", "location", "lesson_start", "lesson_end", "lesson_url", "offer_key")


def remember_jobs(jobs):
    """Keep what we know about queued jobs: once a job has run, the bot
    removes it from its list and only its id appears in the log."""
    def go(state):
        seen = state.setdefault("jobs_seen", {})
        for j in jobs:
            seen[str(j["id"])] = {k: j.get(k) for k in JOB_FIELDS}
    MONITOR.update(go)


def monitor_once(read, read_log, runs_from_journal, timer_status, public_url=None,
                 week_fn=None):
    """read(): {"jobs", "watchers"}; read_log(): {"journal", "watchers"};
    week_fn(): build_week() result."""
    data = read()
    remember_jobs(data["jobs"])
    state = MONITOR.load()
    first = "last_run" not in state
    jobs_seen = state.get("jobs_seen", {})
    for jid, w in data["watchers"].items():
        jobs_seen.setdefault(str(jid), {"lesson_id": w.get("lesson_id"),
                                        "lesson_url": w.get("lesson_url")})
    lg = read_log()
    runs = runs_from_journal(lg["journal"])          # newest first
    new_events = []
    for run in reversed(runs):
        if run["end"] and run["start"] > state.get("last_run", 0):
            for ev in events_from_run(run, jobs_seen):
                if record(ev):
                    new_events.append(ev)
            state["last_run"] = run["start"]
    seen = state.setdefault("watch_seen", {})
    for w in lg["watchers"]:
        outcome, place = watcher_outcome(w["text"])
        jid = re.sub(r"\D", "", w["name"])
        session = hashlib.sha1(last_session(w["text"])[:300].encode()).hexdigest()[:12]
        if outcome and seen.get(w["name"]) != f"{outcome}:{session}":
            seen[w["name"]] = f"{outcome}:{session}"
            ev = {"type": outcome, "job_id": jid, "t": w["modified"], "place": place,
                  "lesson_id": str((jobs_seen.get(jid) or {}).get("lesson_id") or ""),
                  "job": jobs_seen.get(jid) or {}}
            if record(ev):
                new_events.append(ev)
    # health: the timer is on, but nothing ran for a while
    t = timer_status()
    last = max((r["start"] for r in runs), default=0)
    late = bool(t.get("active")) and last and \
        time.time() * 1000 - last > HEALTH_LATE.total_seconds() * 1000
    if late and not state.get("health_bad"):
        state["health_bad"] = True
        new_events.append({"type": "health_bad", "t": int(time.time() * 1000),
                           "text": f"no run since {short(datetime.fromtimestamp(last / 1000).astimezone())}"})
    elif not late and state.get("health_bad"):
        state["health_bad"] = False
        new_events.append({"type": "health_ok", "t": int(time.time() * 1000)})
    if week_fn:
        ev = sunday_summary(state, week_fn)
        if ev:
            new_events.append(ev)
    for k in ("radar", "radar_at"):  # state of the removed free-spot radar
        state.pop(k, None)
    state.setdefault("last_run", 0)   # marks the quiet first pass as done
    with MONITOR.lock:   # merge: remember_jobs() may have added jobs meanwhile
        merged = {**jobs_seen, **MONITOR.load().get("jobs_seen", {})}
        state["jobs_seen"] = dict(list(merged.items())[-400:])
        MONITOR.save(state)
    if not first:  # the very first pass only fills the history, quietly
        for ev in new_events:
            notify_event(ev, public_url)
    return new_events


# ----------------------------------------------------------------------
# the week view
# ----------------------------------------------------------------------

def monday(now=None):
    z = (now or datetime.now(ZURICH)).astimezone(ZURICH)
    return datetime(z.year, z.month, z.day, tzinfo=ZURICH) - timedelta(days=z.weekday())


def build_week(jobs, watchers, plan_entries, starred_lessons, offer_key, alive,
               planned_lessons=()):
    """Items for WEEKS_SHOWN weeks from this Monday: queued jobs, running
    watchers, results, and starred courses you're not signed up for."""
    start = monday()
    end = start + timedelta(weeks=WEEKS_SHOWN)
    now = datetime.now().astimezone()
    hist = HISTORY.load()["lessons"]
    favs = FAVS.load()
    entries = {e["key"]: e for e in plan_entries}
    items, have = [], set()

    # Trainer per date from ASVZ (substitutes included), else the course's usual one
    lesson_by_id = {str(l["id"]): l for l in [*planned_lessons, *starred_lessons]}

    def add(lesson_id, s, e, kind, **kw):
        if not s or not (start <= s < end):
            return
        lid = str(lesson_id)
        have.add(lid)
        okey = kw.get("offer_key")
        l = lesson_by_id.get(lid)
        kw.setdefault("instructor", ", ".join(l["instructors"]) if l and l.get("instructors")
                      else entries.get(okey, {}).get("instructor") or None)
        if l and not kw.get("niveau"):
            kw["niveau"] = l.get("niveau") or None
        items.append({
            "lesson_id": lid, "start": s.isoformat(), "end": (e or s + timedelta(hours=1)).isoformat(),
            "kind": kind, "weekly": bool(entries.get(okey, {}).get("weekly")) if okey else False,
            "starred": bool(okey and okey in favs["offers"]),
            "url": kw.get("url") or f"https://schalter.asvz.ch/tn/lessons/{lid}", **kw})

    watching = {str(w.get("lesson_id")): (jid, w) for jid, w in watchers.items() if alive(w.get("pid"))}
    for j in jobs:
        lid = str(j.get("lesson_id"))
        h = hist.get(lid, {})
        kind = "queued"
        if h.get("status") == "failed":
            kind = "retrying"
        elif (aware(j.get("run_at")) or now) <= now:
            kind = "enrolling"
        add(lid, aware(j.get("lesson_start")), aware(j.get("lesson_end")), kind,
            job_id=j["id"], title=j.get("title"), location=j.get("location"),
            niveau=j.get("niveau"), offer_key=j.get("offer_key"), url=j.get("lesson_url"),
            enroll_start=aware(j.get("enrollment_start")).isoformat()
            if aware(j.get("enrollment_start")) else None,
            watch=bool(j.get("watch_free_slot")), error=h.get("error"))
    for lid, (jid, w) in watching.items():
        if lid in have:
            continue
        h = hist.get(lid, {})
        add(lid, aware(h.get("lesson_start")), aware(h.get("lesson_end")), "watching",
            watcher_id=int(jid), title=h.get("title"), location=h.get("location"),
            offer_key=h.get("offer_key"), url=h.get("lesson_url"))
    for lid, h in hist.items():
        if lid in have:
            continue
        kind = {"signed": "signed", "watch_ok": "signed", "missed": "missed", "error": "failed",
                "watch_gave_up": "missed", "failed": "failed", "watching": "missed"}.get(h["status"])
        if h["status"] == "watching":  # watcher not alive anymore and no success seen
            kind = "missed" if (aware(h.get("lesson_start")) or now) < now else "watching"
        add(lid, aware(h.get("lesson_start")), aware(h.get("lesson_end")), kind,
            title=h.get("title"), location=h.get("location"), place=h.get("place"),
            offer_key=h.get("offer_key"), url=h.get("lesson_url"), error=h.get("error"))
    # weekly courses: dates not queued yet (queued_ids also holds skipped
    # dates and ones that already ran, which mustn't show up as planned)
    paused_weeks = PAUSES.load()["weeks"]
    for l in planned_lessons:
        key = offer_key(l)
        e = entries.get(key)
        if (not e or not e.get("weekly") or str(l["id"]) in have or l["cancelled"]
                or l["id"] in e.get("queued_ids", []) or l["start"] < now
                or week_key(l["start"]) in paused_weeks):
            continue
        add(l["id"], l["start"], l["end"], "planned", title=f"{l['sport_name']}: {l['title']}",
            location=l["location"], niveau=l["niveau"], offer_key=key, url=l["url"],
            sport_id=l["sport_id"], enroll_start=l["oe_from"].isoformat() if l["oe_from"] else None,
            watch=bool(e.get("watch")))
    for l in starred_lessons:
        if str(l["id"]) in have or l["cancelled"]:
            continue
        key = offer_key(l)
        add(l["id"], l["start"], l["end"], "starred", title=f"{l['sport_name']}: {l['title']}",
            location=l["location"], niveau=l["niveau"], offer_key=key, url=l["url"],
            sport_id=l["sport_id"], free=l["free"],
            enroll_start=l["oe_from"].isoformat() if l["oe_from"] else None,
            bookable=bool(l["oe_enabled"] and (l["oe_to"] or l["start"]) > now))
    items.sort(key=lambda i: i["start"])
    mark_overlaps(items)
    paused = PAUSES.load()["weeks"]
    for i in items:
        i["paused"] = week_key(aware(i["start"])) in paused

    upcoming = [i for i in items if i["kind"] in ("queued", "enrolling", "retrying")
                and i.get("enroll_start")]
    nxt = next((i for i in upcoming if aware(i["enroll_start"]) > now), None)
    active = next((i for i in upcoming if aware(i["enroll_start"]) <= now), None)
    return {"week_start": start.isoformat(), "weeks": WEEKS_SHOWN, "items": items,
            "next_up": active or nxt, "paused_weeks": sorted(paused)}


# ----------------------------------------------------------------------
# pausing a week
# ----------------------------------------------------------------------

def week_key(dt):
    """Monday of dt's week (Zurich), as 'YYYY-MM-DD'."""
    return monday(dt).date().isoformat()


def is_paused(dt):
    return week_key(dt) in PAUSES.load()["weeks"]


# ----------------------------------------------------------------------
# overlaps
# ----------------------------------------------------------------------

ACTIVE = ("queued", "enrolling", "retrying", "signed", "watching", "planned")


def mark_overlaps(items):
    """item["overlaps"] = titles of other sign-ups at the same time."""
    act = [i for i in items if i["kind"] in ACTIVE]
    for a in act:
        a_s, a_e = aware(a["start"]), aware(a["end"])
        others = [b for b in act if b is not a and aware(b["start"]) < a_e and a_s < aware(b["end"])]
        if others:
            a["overlaps"] = [b.get("title") or f"Lesson {b['lesson_id']}" for b in others]


def overlap_messages(items, offer_keys=(), lesson_ids=()):
    """Readable warnings for sign-ups of the given courses/dates that clash."""
    out = []
    for i in items:
        if i.get("overlaps") and (i.get("offer_key") in offer_keys or i["lesson_id"] in lesson_ids):
            out.append(f"Overlap: {i.get('title') or 'Lesson ' + i['lesson_id']} "
                       f"({short(aware(i['start']))}) clashes with {', '.join(i['overlaps'])}")
    return out[:5]


# ----------------------------------------------------------------------
# Sunday summary
# ----------------------------------------------------------------------

def summary_text(result):
    nxt = monday() + timedelta(weeks=1)
    key = nxt.date().isoformat()
    if key in result.get("paused_weeks", []):
        return "Next week is paused - nothing gets signed up."
    lines = []
    for i in result["items"]:
        s = aware(i["start"])
        if not (nxt <= s < nxt + timedelta(weeks=1)) or i["kind"] not in ACTIVE:
            continue
        mark = {"signed": " (signed up)", "watching": " (waiting for a spot)"}.get(i["kind"], "")
        lines.append(f"{short(s)} {i.get('title') or 'Lesson ' + i['lesson_id']}{mark}"
                     + (" - overlaps!" if i.get("overlaps") else ""))
    if not lines:
        return "Nothing queued for next week. Star or sign up for courses in the app."
    return f"{len(lines)} sign-up{'s' if len(lines) != 1 else ''} next week:\n" + "\n".join(lines)


def sunday_summary(state, week_fn):
    z = datetime.now(ZURICH)
    today = z.date().isoformat()
    if z.weekday() != 6 or z.hour < SUMMARY_HOUR or state.get("summary_sent") == today:
        return None
    state["summary_sent"] = today
    return {"type": "summary", "t": int(time.time() * 1000), "message": summary_text(week_fn())}



# ----------------------------------------------------------------------
# calendar feed (subscribed by Google Calendar & co. through a secret link)
# ----------------------------------------------------------------------

def calendar_settings():
    c = CALENDAR.load()
    if not c.get("token"):
        c["token"] = secrets.token_urlsafe(24)
        c.setdefault("enabled", False)
        CALENDAR.save(c)
    return c


def calendar_ics(result, plan):
    """Your actual sign-ups (queued, signed up, waiting for a spot) as an
    .ics feed; skipped and paused dates simply aren't in it."""
    label = {"signed": "Signed up", "watching": "Full - waiting for a spot",
             "enrolling": "Signing up now", "retrying": "Retrying",
             "planned": "Every week - the bot queues it about a week before"}
    out = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//ASVZ Bot//EN", "CALSCALE:GREGORIAN",
           "METHOD:PUBLISH", "X-WR-CALNAME:ASVZ", "X-WR-TIMEZONE:Europe/Zurich",
           "REFRESH-INTERVAL;VALUE=DURATION:PT1H", "X-PUBLISHED-TTL:PT1H",
           *plan._VTIMEZONE.split("\n")]
    for i in result["items"]:
        if i["kind"] not in ACTIVE:
            continue
        title = i.get("title") or f"Lesson {i['lesson_id']}"
        status = label.get(i["kind"], "Queued - the bot signs you up")
        if i["kind"] == "signed" and i.get("place"):
            status += f", place {i['place']}"
        mark = "✓ " if i["kind"] == "signed" else ""
        description = "\n".join([status] + [f"{k}: {i[f]}" for k, f in
                                             (("Trainer", "instructor"), ("Level", "niveau")) if i.get(f)])
        out += plan._event(f"lesson-{i['lesson_id']}@asvz-bot", f"{mark}ASVZ {title}",
                           plan._ics_dt(i["start"]), plan._ics_dt(i["end"]),
                           i.get("location") or "", description, i["url"])
    out.append("END:VCALENDAR")
    return "\r\n".join(plan._fold(line) for line in out) + "\r\n"
