# coding: utf-8
"""Made-up ASVZ search API answers for sandbox tests (no network needed).
Shape copied from what asvz_plan._lesson_from_api reads."""
import json
import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ZURICH = ZoneInfo("Europe/Zurich")
SPORTS = [("245229", "Volleyball"), ("122920", "Yoga"), ("45743", "Badminton"),
          ("45645", "Fitness"), ("45737", "Unihockey")]
TEMPLATES = [  # sport, title, weekday, start, minutes, place, level, trainer, free
    ("245229", "Volleyball Mixed", 1, "18:15", 105, "Sport Center Polyterrasse", "Mittlere", "Anna Muster", 0),
    ("245229", "Volleyball Mixed", 3, "20:00", 105, "Sport Center Irchel", "Fortgeschrittene", "Beat Beispiel", 4),
    ("245229", "Volleyball Beginners", 5, "10:00", 90, "Sport Center Hönggerberg", "Alle", "Carla Test", 12),
    ("122920", "Yoga Flow", 0, "07:10", 60, "Sport Center Polyterrasse", "Alle", "Dana Om", 3),
    ("122920", "Yin Yoga", 2, "19:30", 75, "ASVZ Online", "Alle", "Eva Ruhe", None),
    ("45743", "Badminton Free Play", 4, "12:15", 90, "Sport Center Irchel", "Mittlere", "Fritz Feder", 20),
    ("45645", "Kraft & Cardio", 1, "06:45", 60, "Sport Center Hönggerberg", "Alle", "Gina Gym", 8),
    ("45737", "Unihockey", 6, "16:00", 120, "Sport Center Fluntern", "Fortgeschrittene", "Hans Stock", 1),
    ("122920", "Power Yoga", 1, "19:00", 60, "ASVZ Online", "Alle", "Ina Flex", 6),  # overlaps Volleyball Mixed
]
FREE_FILE = os.environ.get("VB_TEST_FREE")  # {"<title>": free} to change places while running
WEEKS = 7
_next_id = [700000]


def _lessons():
    now = datetime.now(ZURICH)
    today = now.date()
    out = []
    override = {}
    if FREE_FILE and os.path.exists(FREE_FILE):
        override = json.load(open(FREE_FILE))
    lid = 700000
    for t in TEMPLATES:
        sport_id, title, wd, start, mins, place, level, trainer, free = t
        free = override.get(title, free)
        h, m = map(int, start.split(":"))
        first = today + timedelta(days=(wd - today.weekday()) % 7)
        for w in range(WEEKS):
            lid += 1
            d = first + timedelta(weeks=w)
            st = datetime(d.year, d.month, d.day, h, m, tzinfo=ZURICH)
            if st < now:
                continue
            en = st + timedelta(minutes=mins)
            out.append({
                "url": f"https://schalter.asvz.ch/tn/lessons/{lid}",
                "from_date": st.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                "to_date": en.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                "oe_from_date": (st - timedelta(days=1)).astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                "oe_to_date": (st - timedelta(minutes=30)).astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                "oe_enabled": True, "cancelled": w == 3 and wd == 1,
                "sport": [sport_id], "sport_name": dict(SPORTS)[sport_id], "title": title,
                "places_free": free, "places_max": 24, "location": place,
                "niveau_name": level, "instructor_name": [trainer],
            })
    out.sort(key=lambda r: r["from_date"])
    return out


def api(params):
    lessons = _lessons()
    filters = [v for k, v in params.items() if k.startswith("f[")]
    sport = next((f.split(":", 1)[1] for f in filters if f.startswith("sport:")), None)
    if sport:
        lessons = [l for l in lessons if l["sport"][0] == sport]
    limit, offset = int(params.get("limit", 10)), int(params.get("offset", 0))
    counts = {}
    for l in _lessons():
        counts[l["sport"][0]] = counts.get(l["sport"][0], 0) + 1
    return {
        "results": lessons[offset:offset + limit],
        "count": {"total": len(lessons)},
        "facets": [{"id": "sport", "terms": [{"tid": sid, "label": name, "count": counts.get(sid, 0)}
                                             for sid, name in SPORTS]}],
    }


def patch(plan_module):
    plan_module._api = api
