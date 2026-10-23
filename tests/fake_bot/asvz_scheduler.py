# coding: utf-8
"""Stand-in for the server's asvz_scheduler.py in sandbox tests. Same
file handling as the real one: "run" loads asvz_schedule.json at the
start, works (VB_TEST_RUN_SECONDS), and saves what's left at the end —
no lock, so changes made meanwhile are overwritten. "add" and
"stop-watch" behave like the real commands."""
import argparse
import json
import logging
import os
import signal
import time
from datetime import datetime, timedelta

from asvz_bot import AsvzEnroller


def load():
    try:
        return json.load(open("asvz_schedule.json")).get("jobs", [])
    except (FileNotFoundError, ValueError):
        return []


def save(jobs):
    json.dump({"jobs": jobs}, open("asvz_schedule.json", "w"), indent=2)


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="command")
    a = sub.add_parser("add")
    a.add_argument("lesson_id")
    a.add_argument("--watch-free-slot", action="store_true")
    a.add_argument("--watch-poll-interval", type=int, default=30)
    sub.add_parser("run")
    s = sub.add_parser("stop-watch")
    s.add_argument("job_id", type=int)
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-8s %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S")
    logging.info("Loaded credentials from local store")

    if args.command == "add":
        enroll, lesson = AsvzEnroller._AsvzEnroller__get_enrollment_and_start_time(None)
        jobs = load()
        jobs.append({"id": max((j["id"] for j in jobs), default=0) + 1, "type": "lesson",
                     "lesson_id": args.lesson_id,
                     "lesson_url": f"https://schalter.asvz.ch/tn/lessons/{args.lesson_id}",
                     "enrollment_start": enroll.isoformat(timespec="minutes"),
                     "lesson_start": lesson.isoformat(timespec="minutes"),
                     "run_at": (enroll - timedelta(minutes=17)).isoformat(timespec="minutes"),
                     "watch_free_slot": args.watch_free_slot,
                     "watch_poll_interval": args.watch_poll_interval})
        save(jobs)
        print(f"Added job {jobs[-1]['id']}: lesson {args.lesson_id}")
    elif args.command == "run":
        jobs, now, remaining = load(), datetime.now(), []
        if not jobs:
            logging.info("No scheduled jobs to run.")
        time.sleep(float(os.environ.get("VB_TEST_RUN_SECONDS", "0")))
        for j in jobs:
            if now < datetime.fromisoformat(j["run_at"]):
                remaining.append(j)
                continue
            cutoff = (datetime.fromisoformat(j["lesson_start"]) if j.get("watch_free_slot")
                      else datetime.fromisoformat(j["enrollment_start"]) + timedelta(minutes=5))
            if now > cutoff:
                logging.info(f"[schedule run] Job #{j['id']} missed the window, removing.")
                continue
            logging.info(f"[schedule run] Executing job #{j['id']} for lesson {j['lesson_id']}")
            AsvzEnroller(j["lesson_url"], {}).enroll()
            logging.info(f"[schedule run] Job #{j['id']} finished successfully.")
        save(remaining)
    elif args.command == "stop-watch":
        w = json.load(open("asvz_watchers.json"))
        e = w.pop(str(args.job_id), None)
        if e:
            try:
                os.kill(e["pid"], signal.SIGTERM)
            except ProcessLookupError:
                pass
            json.dump(w, open("asvz_watchers.json", "w"))
        print("Watcher stopped" if e else "No running watcher")


if __name__ == "__main__":
    main()
