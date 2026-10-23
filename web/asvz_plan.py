# coding: utf-8
"""
asvz_plan.py

Everything the app needs to go from "I want to do Yoga on Tuesday
evenings" to queued enrollment jobs, without looking up lesson numbers:

  - ASVZ's public search API (the one asvz.ch's own sport search uses):
    list of sports, and every upcoming lesson of a sport
  - grouping single lessons into weekly *offers* (same sport, title,
    weekday, time, place and level)
  - the user's plan (asvz_plan.json): chosen offers, either "next date
    only" or "every week"
  - sync: turns plan entries into jobs in asvz_schedule.json. Weekly
    entries pick up newly published dates on every scheduler run
    (throttled to SYNC_INTERVAL)
  - an .ics calendar export for Google Calendar & co.

No browser involved: enrollment and lesson times come straight from the
API, so adding a lesson takes a second instead of a Chrome page load.

The same file is used by the Linux, macOS and Windows versions.
"""

import hashlib
import json
import logging
import os
import ssl
import time
import urllib.parse
import urllib.request
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None
    import msvcrt

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PLAN_FILENAME = os.path.join(BASE_DIR, "asvz_plan.json")
SCHEDULE_FILENAME = os.path.join(BASE_DIR, "asvz_schedule.json")
SCHEDULE_LOCK = os.path.join(BASE_DIR, ".schedule.lock")

API_URL = "https://asvz.ch/asvz_api/event_search"
LESSON_TYPE_ID = "3997"          # "Lektion" — the things schalter.asvz.ch/tn/lessons/ enrolls
PAGE_SIZE = 500
ZURICH = ZoneInfo("Europe/Zurich")

SYNC_INTERVAL = timedelta(minutes=30)

# Same lead as asvz_scheduler.SCHEDULER_LEAD (15 min login prep + 2 min margin).
SCHEDULER_LEAD = timedelta(minutes=17)
# A normal (non-watch) job keeps retrying this long after enrollment opens
# (or after it was added, if enrollment was already open by then).
RETRY_WINDOW = timedelta(minutes=10)

WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
WEEKDAYS_SHORT = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


# ----------------------------------------------------------------------
# API
# ----------------------------------------------------------------------

def _ssl_context():
    # python.org's macOS Python has no CA certificates of its own until
    # "Install Certificates.command" has run; certifi (a requirement) has.
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


_SSL = _ssl_context()


def _api(params):
    url = API_URL + "?" + urllib.parse.urlencode(params, doseq=True)
    req = urllib.request.Request(url, headers={
        "Accept": "application/json",
        "User-Agent": "asvz-bot (personal enrollment helper)",
    })
    with urllib.request.urlopen(req, timeout=30, context=_SSL) as r:
        return json.load(r)


def fetch_sports():
    """[{id, name, count}] of all sports that have upcoming lessons, A–Z."""
    data = _api({"_format": "json", "limit": 1, "f[0]": f"type:{LESSON_TYPE_ID}"})
    for facet in data.get("facets", []):
        if facet.get("id") == "sport":
            sports = [{"id": t["tid"], "name": t["label"], "count": t.get("count", 0)}
                      for t in facet.get("terms", [])]
            return sorted(sports, key=lambda s: s["name"].casefold())
    return []


def _parse_utc(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def _lesson_from_api(r):
    url = r.get("url") or ""
    if "/tn/lessons/" not in url:
        return None
    start = _parse_utc(r.get("from_date"))
    end = _parse_utc(r.get("to_date"))
    if not start:
        return None
    return {
        "id": int(url.rstrip("/").rsplit("/", 1)[-1]),
        "url": url,
        "sport_id": (r.get("sport") or [""])[0],
        "sport_name": r.get("sport_name") or "",
        "title": (r.get("title") or "").strip(),
        "start": start,
        "end": end or start + timedelta(hours=1),
        "oe_from": _parse_utc(r.get("oe_from_date")),
        "oe_to": _parse_utc(r.get("oe_to_date")),
        "oe_enabled": bool(r.get("oe_enabled", True)),
        "cancelled": bool(r.get("cancelled")),
        "free": r.get("places_free"),
        "max": r.get("places_max"),
        "location": r.get("location") or ", ".join(r.get("facility_name") or []),
        "niveau": r.get("niveau_name") or "",
        "instructors": r.get("instructor_name") or [],
    }


def fetch_lessons(sport_id):
    """All published upcoming lessons of one sport (typically ~7 weeks ahead)."""
    lessons, offset = [], 0
    while True:
        data = _api({
            "_format": "json", "limit": PAGE_SIZE, "offset": offset,
            "f[0]": f"sport:{sport_id}", "f[1]": f"type:{LESSON_TYPE_ID}",
        })
        results = data.get("results", [])
        lessons += [l for l in map(_lesson_from_api, results) if l]
        total = (data.get("count") or {}).get("total", 0)
        offset += PAGE_SIZE
        if not results or offset >= total:
            return lessons


TIMETABLE_DAYS = 8
MAX_TIMETABLE_PAGES = 30


def fetch_upcoming(days=TIMETABLE_DAYS):
    """Every sport's lessons in the next `days` days, like asvz.ch's
    Sportfahrplan. The API returns lessons sorted by date, so paging stops
    as soon as it passes the end of the window (~2500 lessons a week)."""
    until = datetime.now(timezone.utc) + timedelta(days=days)
    lessons = []
    for page in range(MAX_TIMETABLE_PAGES):
        data = _api({"_format": "json", "limit": PAGE_SIZE, "offset": page * PAGE_SIZE,
                     "f[0]": f"type:{LESSON_TYPE_ID}"})
        results = data.get("results", [])
        lessons += [l for l in map(_lesson_from_api, results) if l and l["start"] < until]
        total = (data.get("count") or {}).get("total", 0)
        last = _parse_utc(results[-1].get("from_date")) if results else None
        if not results or (page + 1) * PAGE_SIZE >= total or (last and last >= until):
            break
    return lessons


def timetable_offers(days=TIMETABLE_DAYS):
    """Weekly offers of all sports, from the next `days` days only. They
    carry partial=True: complete_offers() fills in their later dates."""
    offers = group_offers(fetch_upcoming(days))
    for o in offers:
        o["partial"] = True
    return offers


def complete_offers(offers):
    """Replaces partial offers (see timetable_offers) by the same offers
    with every published date, loaded per sport. Needed before signing up
    "every week"; other offers are passed through unchanged."""
    out = [o for o in offers if not o.get("partial")]
    partial = [o for o in offers if o.get("partial")]
    for sport_id in dict.fromkeys(o["sport_id"] for o in partial):
        full = {o["key"]: o for o in group_offers(fetch_lessons(sport_id))}
        out += [full.get(o["key"], o) for o in partial if o["sport_id"] == sport_id]
    return out


# ----------------------------------------------------------------------
# Offers = the same lesson every week
# ----------------------------------------------------------------------

def offer_key(lesson):
    local = lesson["start"].astimezone(ZURICH)
    return "|".join([str(lesson["sport_id"]), lesson["title"], str(local.weekday()),
                     local.strftime("%H:%M"), lesson["location"], lesson["niveau"]])


def group_offers(lessons):
    """Groups lessons into weekly offers, sorted by weekday and time."""
    groups = {}
    for l in lessons:
        groups.setdefault(offer_key(l), []).append(l)

    offers = []
    for key, ls in groups.items():
        ls.sort(key=lambda l: l["start"])
        first = ls[0]
        local = first["start"].astimezone(ZURICH)
        trainers = Counter(t for l in ls for t in l["instructors"])
        offers.append({
            "key": key,
            "sport_id": first["sport_id"],
            "sport_name": first["sport_name"],
            "title": first["title"],
            "weekday": local.weekday(),
            "start": local.strftime("%H:%M"),
            "end": first["end"].astimezone(ZURICH).strftime("%H:%M"),
            "location": first["location"],
            "niveau": first["niveau"],
            "instructor": ", ".join(t for t, _ in trainers.most_common(2)),
            "lessons": ls,
        })
    offers.sort(key=lambda o: (o["weekday"], o["start"], o["title"], o["location"]))
    return offers


def bookable(lessons, now=None):
    """Lessons whose enrollment hasn't closed yet (and that aren't cancelled)."""
    now = now or datetime.now(timezone.utc)
    return [l for l in lessons
            if not l["cancelled"] and l["oe_enabled"]
            and (l["oe_to"] or l["start"]) > now]


def next_bookable(offer, now=None):
    b = bookable(offer["lessons"], now)
    return b[0] if b else None


# ----------------------------------------------------------------------
# Files
# ----------------------------------------------------------------------

def lock_file(f, blocking=True):
    """Exclusive lock on the open file `f` (fcntl on Linux/macOS, msvcrt on
    Windows). Non-blocking: returns False if someone else holds it."""
    if fcntl:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            return True
        except OSError:
            if blocking:
                raise
            return False
    while True:
        try:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            if not blocking:
                return False
            time.sleep(0.05)


def unlock_file(f):
    if fcntl:
        fcntl.flock(f, fcntl.LOCK_UN)
    else:
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)


@contextmanager
def schedule_lock():
    """Serialises read-modify-write of asvz_schedule.json between the app
    and the background scheduler."""
    # "a+": never truncate a file another process holds a lock on (Windows).
    with open(SCHEDULE_LOCK, "a+") as f:
        lock_file(f)
        try:
            yield
        finally:
            unlock_file(f)


def _write_atomic(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    for attempt in range(40):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            # Windows: the target is open in another process for a moment.
            if attempt == 39:
                raise
            time.sleep(0.05)


def load_schedule():
    try:
        with open(SCHEDULE_FILENAME, "r", encoding="utf-8") as f:
            return json.load(f).get("jobs", [])
    except (FileNotFoundError, ValueError):
        return []


def save_schedule(jobs):
    _write_atomic(SCHEDULE_FILENAME, json.dumps({"jobs": jobs}, indent=2))


def load_plan():
    try:
        with open(PLAN_FILENAME, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, ValueError):
        data = {}
    data.setdefault("entries", [])
    return data


def save_plan(plan):
    _write_atomic(PLAN_FILENAME, json.dumps(plan, indent=2, ensure_ascii=False))


# ----------------------------------------------------------------------
# Plan entries -> jobs
# ----------------------------------------------------------------------

def _local_naive(dt):
    """API time (UTC) -> this machine's local time, naive — the format the
    scheduler and enroll() use everywhere."""
    return dt.astimezone().replace(tzinfo=None)


def _job_for(lesson, entry, job_id, now_local):
    enrollment_start = _local_naive(lesson["oe_from"] or lesson["start"] - timedelta(days=1))
    lesson_start = _local_naive(lesson["start"])
    run_at = max(now_local, enrollment_start - SCHEDULER_LEAD)
    enroll_until = max(enrollment_start, now_local) + RETRY_WINDOW
    if lesson["oe_to"]:
        enroll_until = min(enroll_until, _local_naive(lesson["oe_to"]))
    return {
        "id": job_id,
        "type": "lesson",
        "lesson_id": lesson["id"],
        "lesson_url": lesson["url"],
        "enrollment_start": enrollment_start.isoformat(timespec="minutes"),
        "lesson_start": lesson_start.isoformat(timespec="minutes"),
        "lesson_end": _local_naive(lesson["end"]).isoformat(timespec="minutes"),
        "run_at": run_at.isoformat(timespec="minutes"),
        "enroll_until": enroll_until.isoformat(timespec="minutes"),
        "created_at": now_local.isoformat(timespec="minutes"),
        "watch_free_slot": bool(entry.get("watch")),
        "watch_poll_interval": entry.get("watch_poll_interval", 30),
        "title": f"{entry['sport_name']}: {entry['title']}",
        "location": entry["location"],
        "niveau": entry["niveau"],
        "offer_key": entry["key"],
        "source": "app",
    }


def _queue_lessons(entry, lessons):
    """Adds jobs for `lessons` that this entry hasn't queued before.
    Returns the new jobs. Caller holds schedule_lock()."""
    queued = set(entry.setdefault("queued_ids", []))
    jobs = load_schedule()
    in_schedule = {j["lesson_id"] for j in jobs}
    next_id = max((j["id"] for j in jobs), default=0) + 1
    now_local = datetime.now()
    new = []
    for l in lessons:
        if l["id"] in queued or l["id"] in in_schedule:
            continue
        job = _job_for(l, entry, next_id, now_local)
        next_id += 1
        jobs.append(job)
        new.append(job)
        entry["queued_ids"].append(l["id"])
        entry.setdefault("dates", []).append(_date_record(l))
    if new:
        save_schedule(jobs)
    return new


def _date_record(lesson):
    return {
        "lesson_id": lesson["id"],
        "start": lesson["start"].astimezone(ZURICH).isoformat(timespec="minutes"),
        "end": lesson["end"].astimezone(ZURICH).isoformat(timespec="minutes"),
    }


def _entry_from_offer(offer, weekly, watch, watch_poll_interval=30):
    fields = ("key", "sport_id", "sport_name", "title", "weekday", "start", "end",
              "location", "niveau", "instructor")
    entry = {k: offer[k] for k in fields}
    entry.update(weekly=bool(weekly), watch=bool(watch),
                 watch_poll_interval=watch_poll_interval,
                 added_at=datetime.now().isoformat(timespec="minutes"),
                 queued_ids=[], dates=[], cancelled=[])
    return entry


def add_offers(offers, weekly, watch, watch_poll_interval=30):
    """
    Puts the chosen offers into the plan and queues their jobs right away:
    every bookable date if weekly, otherwise just the next one.
    Returns (number_of_new_jobs, [messages]).
    """
    offers = complete_offers(offers)  # network: outside the lock
    messages, total = [], 0
    with schedule_lock():
        plan = load_plan()
        by_key = {e["key"]: e for e in plan["entries"]}
        for offer in offers:
            entry = by_key.get(offer["key"])
            if entry is None:
                entry = _entry_from_offer(offer, weekly, watch, watch_poll_interval)
                plan["entries"].append(entry)
                by_key[entry["key"]] = entry
            else:
                entry["weekly"] = entry["weekly"] or bool(weekly)
                entry["watch"] = bool(watch)
            entry["cancelled"] = _cancelled_starts(offer["lessons"])

            candidates = bookable(offer["lessons"])
            if not weekly:
                candidates = candidates[:1]
            if not candidates:
                messages.append(f"{offer['title']}: no date open for enrollment right now"
                                + (" — will be added once ASVZ publishes the next one."
                                   if weekly else "."))
                continue
            new = _queue_lessons(entry, candidates)
            total += len(new)
        save_plan(plan)
    return total, messages


def _cancelled_starts(lessons):
    return [l["start"].astimezone(ZURICH).isoformat(timespec="minutes")
            for l in lessons if l["cancelled"]]


def set_weekly(key, weekly):
    with schedule_lock():
        plan = load_plan()
        for e in plan["entries"]:
            if e["key"] == key:
                e["weekly"] = bool(weekly)
        plan.pop("last_sync", None)  # pick up new dates on the next sync
        save_plan(plan)


def remove_entry(key):
    """Removes an offer from the plan together with its jobs that haven't
    run yet. Returns the number of removed jobs."""
    with schedule_lock():
        plan = load_plan()
        plan["entries"] = [e for e in plan["entries"] if e["key"] != key]
        save_plan(plan)
        jobs = load_schedule()
        keep = [j for j in jobs if j.get("offer_key") != key]
        save_schedule(keep)
    return len(jobs) - len(keep)


def remove_job(job_id):
    """Removes one queued date. For plan entries the date also leaves the
    calendar export, and a one-off entry with no dates left disappears."""
    with schedule_lock():
        jobs = load_schedule()
        job = next((j for j in jobs if j["id"] == job_id), None)
        save_schedule([j for j in jobs if j["id"] != job_id])
        if not job or not job.get("offer_key"):
            return
        plan = load_plan()
        for e in plan["entries"]:
            if e["key"] == job["offer_key"]:
                e["dates"] = [d for d in e.get("dates", []) if d["lesson_id"] != job["lesson_id"]]
        plan["entries"] = [e for e in plan["entries"] if e["weekly"] or e.get("dates")]
        save_plan(plan)


def sync(force=False):
    """
    Called by every scheduler run: for "every week" entries, queue the
    dates ASVZ has published since the last sync. Network errors are
    logged and retried on the next run. Returns the number of new jobs.
    """
    plan = load_plan()
    weekly = [e for e in plan["entries"] if e.get("weekly")]
    if not weekly:
        return 0
    last = plan.get("last_sync")
    if not force and last and datetime.now() - datetime.fromisoformat(last) < SYNC_INTERVAL:
        return 0

    by_sport = {}
    for sport_id in {e["sport_id"] for e in weekly}:
        try:
            by_sport[sport_id] = fetch_lessons(sport_id)
        except Exception as e:
            logging.warning(f"[plan sync] Could not load lessons for sport {sport_id}: {e}")

    added = 0
    with schedule_lock():
        plan = load_plan()  # re-read under the lock
        for entry in plan["entries"]:
            if not entry.get("weekly") or entry["sport_id"] not in by_sport:
                continue
            mine = [l for l in by_sport[entry["sport_id"]] if offer_key(l) == entry["key"]]
            entry["cancelled"] = _cancelled_starts(mine)
            new = _queue_lessons(entry, bookable(mine))
            for job in new:
                logging.info(f"[plan sync] Queued {job['title']} on "
                             f"{job['lesson_start'].replace('T', ' ')} (lesson {job['lesson_id']}).")
            added += len(new)
        if len(by_sport) == len({e["sport_id"] for e in weekly}):
            plan["last_sync"] = datetime.now().isoformat(timespec="seconds")
        save_plan(plan)
    return added


# ----------------------------------------------------------------------
# Calendar export (.ics)
# ----------------------------------------------------------------------

# Europe/Zurich rules (EU DST), so calendars place weekly events
# correctly across the summer/winter time switch.
_VTIMEZONE = """BEGIN:VTIMEZONE
TZID:Europe/Zurich
X-LIC-LOCATION:Europe/Zurich
BEGIN:DAYLIGHT
TZOFFSETFROM:+0100
TZOFFSETTO:+0200
TZNAME:CEST
DTSTART:19700329T020000
RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU
END:DAYLIGHT
BEGIN:STANDARD
TZOFFSETFROM:+0200
TZOFFSETTO:+0100
TZNAME:CET
DTSTART:19701025T030000
RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU
END:STANDARD
END:VTIMEZONE"""


def _esc(text):
    return (str(text).replace("\\", "\\\\").replace(";", "\\;")
            .replace(",", "\\,").replace("\n", "\\n"))


def _fold(line):
    """RFC 5545: lines longer than 75 octets continue on the next line."""
    out, raw = [], line.encode("utf-8")
    while len(raw) > 75:
        cut = 75 if not out else 74
        while (raw[cut] & 0xC0) == 0x80:  # don't split a UTF-8 character
            cut -= 1
        out.append(raw[:cut].decode("utf-8"))
        raw = raw[cut:]
    out.append(raw.decode("utf-8"))
    return "\r\n ".join(out)


def _ics_dt(iso_zurich):
    return datetime.fromisoformat(iso_zurich).astimezone(ZURICH).strftime("%Y%m%dT%H%M%S")


def _uid(*parts):
    return hashlib.sha1("|".join(map(str, parts)).encode()).hexdigest()[:20] + "@asvz-bot"


def _event(uid, summary, start, end, location, description, url, extra=()):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{stamp}",
        f"DTSTART;TZID=Europe/Zurich:{start}",
        f"DTEND;TZID=Europe/Zurich:{end}",
        *extra,
        f"SUMMARY:{_esc(summary)}",
        f"LOCATION:{_esc(location)}",
        f"DESCRIPTION:{_esc(description)}",
        f"URL:{url}",
        "END:VEVENT",
    ]
    return lines


def _description(entry):
    parts = [f"{entry['sport_name']}: {entry['title']}"]
    if entry.get("niveau"):
        parts.append(f"Level: {entry['niveau']}")
    if entry.get("instructor"):
        parts.append(f"Trainer: {entry['instructor']}")
    parts.append("Enrollment is handled by the ASVZ bot." if entry.get("weekly")
                 else "Signed up via the ASVZ bot.")
    return "\n".join(parts)


def _first_occurrence(entry):
    """The first date of a weekly entry, from its queued dates or, failing
    that, the next matching weekday."""
    dates = sorted(entry.get("dates", []), key=lambda d: d["start"])
    if dates:
        return dates[0]["start"], dates[0]["end"]
    today = datetime.now(ZURICH).date()
    day = today + timedelta(days=(entry["weekday"] - today.weekday()) % 7)
    h, m = map(int, entry["start"].split(":"))
    eh, em = map(int, entry["end"].split(":"))
    start = datetime(day.year, day.month, day.day, h, m, tzinfo=ZURICH)
    end = datetime(day.year, day.month, day.day, eh, em, tzinfo=ZURICH)
    return start.isoformat(timespec="minutes"), end.isoformat(timespec="minutes")


def build_ics(entries, name="ASVZ"):
    """
    Calendar with one event series per weekly entry (RRULE:FREQ=WEEKLY,
    known cancellations left out) and one event per date for the others.
    """
    out = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//ASVZ Bot//EN",
           "CALSCALE:GREGORIAN", "METHOD:PUBLISH", f"X-WR-CALNAME:{_esc(name)}",
           "X-WR-TIMEZONE:Europe/Zurich", *_VTIMEZONE.split("\n")]

    for e in entries:
        summary = f"ASVZ {e['sport_name']}" + (f" – {e['title']}" if e["title"] != e["sport_name"] else "")
        url = (f"https://schalter.asvz.ch/tn/lessons/{e['dates'][0]['lesson_id']}"
               if e.get("dates") else "https://asvz.ch")
        if e.get("weekly"):
            start, end = _first_occurrence(e)
            extra = ["RRULE:FREQ=WEEKLY"]
            for c in e.get("cancelled", []):
                if c >= start:
                    extra.append(f"EXDATE;TZID=Europe/Zurich:{_ics_dt(c)}")
            out += _event(_uid("weekly", e["key"]), summary, _ics_dt(start), _ics_dt(end),
                          e["location"], _description(e), url, extra)
        else:
            for d in e.get("dates", []):
                out += _event(_uid("lesson", d["lesson_id"]), summary, _ics_dt(d["start"]),
                              _ics_dt(d["end"]), e["location"], _description(e),
                              f"https://schalter.asvz.ch/tn/lessons/{d['lesson_id']}")
    out.append("END:VCALENDAR")
    return "\r\n".join(_fold(l) for l in out) + "\r\n"


def preview_entries(offers, weekly):
    """Plan-entry-shaped dicts for offers that aren't in the plan (yet), so
    the Discover page can export a calendar straight from a selection."""
    entries = []
    for o in offers:
        e = _entry_from_offer(o, weekly, False)
        b = bookable(o["lessons"]) or o["lessons"]
        chosen = b if weekly else b[:1]
        e["dates"] = [_date_record(l) for l in chosen]
        e["cancelled"] = _cancelled_starts(o["lessons"])
        entries.append(e)
    return entries
