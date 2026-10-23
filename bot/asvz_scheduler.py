#!/usr/bin/env python3
# coding: utf-8

import argparse
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta
from location import fetch_lesson_info
import re

import asvz_plan



from asvz_bot import (
    AsvzEnroller,
    CredentialsManager,
    LESSON_BASE_URL,
    AsvzBotException,
)

SCHEDULE_FILENAME = "asvz_schedule.json"
WATCHER_LOG_DIR = "watcher_logs"
WATCHERS_FILENAME = "asvz_watchers.json"

# Wie früh vor enrollment_start der Bot gestartet werden soll.
# Annahme:
#   - dein enroll() macht intern 15-Minuten-Prep
#   - Login + Redirect dauern ~100s → wir nehmen 120s Puffer.
LOGIN_AND_BOOT_MARGIN = timedelta(seconds=120)
INTERNAL_PREP_LEAD = timedelta(minutes=15)
SCHEDULER_LEAD = INTERNAL_PREP_LEAD + LOGIN_AND_BOOT_MARGIN  # ~17 Minuten

# Wie lange nach enrollment_start wir es noch versuchen
GRACE_AFTER_ENROLLMENT = timedelta(minutes=20)

# Watch-for-free-slot jobs use a single, persistent browser session that
# stays logged in across every poll (see AsvzEnroller.watch_and_enroll()).
# That means it CANNOT run inside a periodic scheduler tick — this script
# processes all due jobs in one short-lived pass and exits, but a
# persistent watch needs to keep one process/session alive for however
# long it takes (potentially hours). So instead of running it inline, the
# scheduler hands the job off to a detached background process the moment
# it's due, and forgets about it (the background process owns success,
# failure, and retries from then on — it is NOT retried by this scheduler
# again). Credentials are passed via environment variables, never as CLI
# args, so they don't show up in `ps aux`.



def _load_schedule():
    if not os.path.isfile(SCHEDULE_FILENAME):
        return []
    try:
        with open(SCHEDULE_FILENAME, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("jobs", [])
    except Exception:
        logging.error("Failed to load schedule file, starting with empty schedule.")
        return []


def _save_schedule(jobs):
    data = {"jobs": jobs}
    with open(SCHEDULE_FILENAME, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def _load_watchers():
    """
    Registry of detached watch_and_enroll() background processes, keyed by
    job id as a string. Separate from asvz_schedule.json because a job is
    fully removed from the schedule the moment it's handed off (see
    _spawn_watcher docstring) — this file is the ONLY remaining record of
    that process once it's running.
    """
    if not os.path.isfile(WATCHERS_FILENAME):
        return {}
    try:
        with open(WATCHERS_FILENAME, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        logging.error("Failed to load watcher registry, starting with empty registry.")
        return {}


def _save_watchers(watchers):
    with open(WATCHERS_FILENAME, "w", encoding="utf-8") as f:
        json.dump(watchers, f, indent=2)


def _pid_is_alive(pid: int) -> bool:
    try:
        # Signal 0 sends nothing, just checks whether we could signal the
        # process — raises if it doesn't exist or we lack permission.
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists, but owned by someone else — still "alive" from our POV.
        return True
    except Exception:
        return False


def _running_under_systemd() -> bool:
    """
    Best-effort: are we being run as a systemd service (as opposed to a
    manual foreground run)? systemd sets INVOCATION_ID for units it
    starts. Used only to decide whether escaping the cgroup is worth it.
    """
    return bool(os.environ.get("INVOCATION_ID"))

def _wrap_in_systemd_scope(cmd, scope_name):
    """
    Wrap `cmd` so it runs in its OWN transient systemd scope (a separate
    cgroup), instead of inheriting the scheduler service's cgroup.

    Environment variables for Chrome/ChromeDriver are passed explicitly
    via --setenv so they survive the scope boundary.
    """
    systemd_run = shutil.which("systemd-run")
    if not systemd_run or not _running_under_systemd():
        return cmd

    wrapper = [
        systemd_run,
        "--scope",
        "--collect",
        "--unit", scope_name,
        "--quiet",
    ]
    if os.geteuid() != 0:
        wrapper.append("--user")

    # Pass Chrome paths through the scope boundary.
    # systemd-run --scope does NOT inherit the caller's environment,
    # so without these, the child process can't find Chrome/ChromeDriver.
    for var in ("CHROME_BIN", "CHROMEDRIVER_BIN", "PYTHONUNBUFFERED"):
        val = os.environ.get(var)
        if val:
            wrapper.extend(["--setenv", f"{var}={val}"])

    return wrapper + cmd

def _spawn_watcher(job, creds, org_key, proxy_url):
    """
    Hands a booked-out lesson off to a detached background process running
    AsvzEnroller.watch_and_enroll() (a single persistent login session that
    polls every job['watch_poll_interval'] seconds and only re-logs in if
    the session actually drops). This process outlives the current
    scheduler invocation and is not managed by it afterwards.

    Credentials go through environment variables only, so they never show
    up in `ps aux` output.

    org_key must be the RAW organisation key (e.g. "ethz"), i.e. whatever
    was passed to --organisation, NOT creds[CREDENTIALS_ORG] — that field
    holds the already-resolved display value (e.g. "ETH Zürich"), and
    CredentialsManager looks its `org` argument up in ORGANISATIONS, so
    passing the resolved value back in would KeyError.
    """
    os.makedirs(WATCHER_LOG_DIR, exist_ok=True)
    log_path = os.path.join(WATCHER_LOG_DIR, f"job_{job['id']}.log")

    script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "asvz_bot.py")

    # IMPORTANT: asvz_bot.py's argparse puts --watch-free-slot,
    # --watch-poll-interval and -x/--proxy on the TOP-LEVEL parser, and
    # the enrollment type ('lesson') is a SUBCOMMAND with the lesson id as
    # a positional. So all global flags must come BEFORE 'lesson', and the
    # id comes AFTER it as a bare positional (there is no --lesson-id).
    # Getting this order wrong makes argparse exit(2) instantly, which is
    # exactly why the detached watcher used to die the moment it spawned.
    #
    # -u forces unbuffered stdio: without it, a child that crashes hard
    # (or gets killed) loses everything still sitting in Python's output
    # buffer, which is exactly why job_<id>.log came back EMPTY even though
    # the process had clearly started and then died. Belt-and-braces, we
    # also set PYTHONUNBUFFERED=1 in the env below.
    py_cmd = [sys.executable, "-u", script_path]
    if proxy_url:
        py_cmd += ["-x", proxy_url]
    py_cmd += [
        "--watch-free-slot",
        "--watch-poll-interval", str(job.get("watch_poll_interval", 30)),
        "lesson", str(job["lesson_id"]),
    ]

    # ------------------------------------------------------------------
    # Escape the scheduler's systemd cgroup.
    #
    # The scheduler runs as a systemd service that deactivates the instant
    # its run finishes. With the default KillMode=control-group, systemd
    # SIGTERMs the WHOLE service cgroup on deactivation - and our detached
    # child is still in that cgroup (start_new_session=True changes the
    # session/process group but does NOT move the process out of the
    # cgroup). asvz_bot's graceful-stop handler then catches that SIGTERM
    # and the watcher shuts itself down ~1s after spawning. That is the
    # 'GracefulStop' traceback that showed up in job_<id>.log.
    #
    # Running the watcher inside its own transient 'systemd-run --scope'
    # puts it in a separate cgroup/unit, so stopping the scheduler service
    # no longer takes the watcher down with it. If systemd-run isn't there
    # (non-systemd host, or a manual foreground run), fall back to plain
    # detached Popen.
    # ------------------------------------------------------------------
    scope_name = f"asvz-watcher-job{job['id']}-{int(time.time())}"
    cmd = _wrap_in_systemd_scope(py_cmd, scope_name)

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    if org_key:
        env["ASVZ_ORGANIZATION"] = org_key
    env["ASVZ_USERNAME"] = creds.get("username", "")
    env["ASVZ_PASSWORD"] = creds.get("password", "")

    logging.info(
        f"[schedule run] Handing job #{job['id']} off to a detached watcher "
        f"process (log: {log_path})."
    )

    with open(log_path, "a") as log_file:
        # Write a header + the exact command into the log BEFORE exec, so
        # even if the child dies before producing a single line of its own
        # output, the log still tells us what was launched and with which
        # interpreter. (Credentials are in env, never in this printed cmd.)
        log_file.write(
            "\n==================================================\n"
            f"[watcher spawn] {datetime.now().isoformat(timespec='seconds')}\n"
            f"[watcher spawn] interpreter: {sys.executable}\n"
            f"[watcher spawn] cwd: {os.path.dirname(script_path)}\n"
            f"[watcher spawn] cmd: {' '.join(cmd)}\n"
            "==================================================\n"
        )
        log_file.flush()
        log_file.write(
            f"[watcher spawn] CHROME_BIN={env.get('CHROME_BIN', 'NOT SET')}\n"
            f"[watcher spawn] CHROMEDRIVER_BIN={env.get('CHROMEDRIVER_BIN', 'NOT SET')}\n"
        )
        proc = subprocess.Popen(
            cmd,
            env=env,
            cwd=os.path.dirname(script_path),
            stdout=log_file,
            stderr=log_file,
            stdin=subprocess.DEVNULL,
            start_new_session=True,  # detach from this process's session/group
        )

    # Give it a moment, then check it didn't instantly die. This turns a
    # silent "spawned then vanished" (the old symptom) into an explicit
    # error with the child's exit code, and avoids recording a dead PID in
    # the registry.
    time.sleep(1.0)
    early_rc = proc.poll()
    if early_rc is not None:
        logging.error(
            f"[schedule run] Watcher for job #{job['id']} exited immediately "
            f"(rc={early_rc}). See {log_path} for the traceback. "
            "Not recording it as a running watcher."
        )
        raise AsvzBotException(
            f"Watcher for job #{job['id']} died on startup (rc={early_rc}); "
            f"see {log_path}."
        )

    used_scope = cmd is not py_cmd  # True if we wrapped in systemd-run

    watchers = _load_watchers()
    watchers[str(job["id"])] = {
        "pid": proc.pid,
        "lesson_id": job["lesson_id"],
        "lesson_url": job["lesson_url"],
        "title": job.get("title"),
        "log_path": log_path,
        "spawned_at": datetime.now().isoformat(timespec="minutes"),
        "scope": scope_name if used_scope else None,
    }
    _save_watchers(watchers)

    if used_scope:
        logging.info(
            f"[schedule run] Watcher for job #{job['id']} running as PID "
            f"{proc.pid} in its own systemd scope '{scope_name}' "
            f"(survives the scheduler service stopping)."
        )
    else:
        logging.info(
            f"[schedule run] Watcher for job #{job['id']} running as PID "
            f"{proc.pid} (plain detached process; systemd-run not used)."
        )


def _schedule_add_lesson(args, creds):
    """
    Add a lesson to the schedule.

    Ablauf:
      - Lesson-Seite einmal (headless) laden,
      - Login + Redirect,
      - enrollment_start und lesson_start mit derselben Helper-Funktion
        wie in enroll() bestimmen,
      - run_at = enrollment_start - SCHEDULER_LEAD berechnen,
      - Job in asvz_schedule.json speichern.
    """
    lesson_id = args.lesson_id
    proxy_url = args.proxy

    lesson_url = "{}/tn/lessons/{}".format(LESSON_BASE_URL, lesson_id)

    #logging.info(f"[schedule add] Preparing job for lesson {lesson_id} ({lesson_url})")

    # Enroller-Objekt, damit wir __organisation_login und _redirect_back_to_lesson nutzen können
    #enroller = AsvzEnroller(chromedriver_path, lesson_url, creds, proxy_url) # (NEW EDIT)
    driver = AsvzEnroller.get_driver(proxy_url=proxy_url)

    try:
        driver.get(lesson_url)
        driver.implicitly_wait(3)

        logging.info("[schedule add] Reading times from public lesson page")
        driver.get(lesson_url)
        time.sleep(1.0)

        # genau derselbe Parser wie in enroll()
        enrollment_start, lesson_start = AsvzEnroller._AsvzEnroller__get_enrollment_and_start_time(
            driver
        )
#        title, location = enroller._AsvzEnroller__parse_basic_public_info(driver)
#        logging.info(f"[schedule add] Course title: {title}")
#        logging.info(f"[schedule add] Location: {location}")
    finally:
        try:
            driver.quit()
        except Exception:
            pass

    # run_at berechnen
    run_at = enrollment_start - SCHEDULER_LEAD
    now = datetime.now()
    if run_at < now:
        logging.warning(
            "[schedule add] Computed run_at is in the past "
            f"({run_at.isoformat(timespec='minutes')}), using 'now' instead."
        )
        run_at = now

    jobs = _load_schedule()
    next_id = max((job["id"] for job in jobs), default=0) + 1

    job = {
        "id": next_id,
        "type": "lesson",
        "lesson_id": lesson_id,
        "lesson_url": lesson_url,
        "enrollment_start": enrollment_start.isoformat(timespec="minutes"),
        "lesson_start": lesson_start.isoformat(timespec="minutes"),
        "run_at": run_at.isoformat(timespec="minutes"),
        "created_at": now.isoformat(timespec="minutes"),
        "watch_free_slot": bool(args.watch_free_slot),
        "watch_poll_interval": args.watch_poll_interval,
        #"title": title,
        #"location": location,
    }

    with asvz_plan.schedule_lock():
        jobs = _load_schedule()  # re-read: the app/timer may have changed it meanwhile
        job["id"] = next_id = max((j["id"] for j in jobs), default=0) + 1
        jobs.append(job)
        _save_schedule(jobs)

    weekday = enrollment_start.strftime("%a")
    logging.info(
        f"[schedule add] Added job {next_id}: lesson {lesson_id} on "
        f"{weekday}, {enrollment_start.strftime('%Y-%m-%d %H:%M')} "
        f"(run_at={run_at.strftime('%Y-%m-%d %H:%M')})"
        + (f" [watch-free-slot every {args.watch_poll_interval}s]" if args.watch_free_slot else "")
    )


def _schedule_remove(job_id: int):
    if not any(j["id"] == job_id for j in _load_schedule()):
        logging.warning(f"No scheduled job with id={job_id} found.")
        return
    # Also keeps the app's plan (asvz_plan.json) in step.
    asvz_plan.remove_job(job_id)
    logging.info(f"Removed scheduled job #{job_id}.")


def _stop_watch(job_id: int, wait_sec: float = 5.0):
    """
    Sends SIGTERM to a running detached watch_and_enroll() process.
    asvz_bot.py installs a graceful-stop signal handler, so this exits
    cleanly (driver.quit() runs) rather than being hard-killed mid-cycle.
    """
    watchers = _load_watchers()
    key = str(job_id)
    entry = watchers.get(key)

    if entry is None:
        logging.warning(
            f"No running watcher found for job #{job_id}. "
            "Use 'watchers' to see what's currently tracked."
        )
        return

    pid = entry["pid"]

    if not _pid_is_alive(pid):
        logging.info(
            f"Watcher for job #{job_id} (PID {pid}) is not running anymore "
            "(already finished or crashed) — removing from registry."
        )
        del watchers[key]
        _save_watchers(watchers)
        return

    logging.info(f"Sending SIGTERM to watcher for job #{job_id} (PID {pid})...")
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        logging.info("Process already gone.")
        del watchers[key]
        _save_watchers(watchers)
        return

    deadline = time.time() + wait_sec
    while time.time() < deadline:
        if not _pid_is_alive(pid):
            logging.info(f"Watcher for job #{job_id} stopped cleanly.")
            del watchers[key]
            _save_watchers(watchers)
            return
        time.sleep(0.5)

    logging.warning(
        f"Watcher for job #{job_id} (PID {pid}) did not exit within "
        f"{wait_sec}s of SIGTERM. It may be mid-login or stuck. If it's "
        f"still needed, check {entry.get('log_path')} first. To force it: "
        f"kill -9 {pid}"
    )


def _list_watchers():
    """
    Lists currently tracked detached watchers, pruning any whose process
    has already exited (finished, crashed, or was stopped outside of
    stop-watch, e.g. via a raw kill or a server reboot).
    """
    watchers = _load_watchers()
    if not watchers:
        print("No active watchers.")
        return

    changed = False
    print("Active watchers:")
    for job_id, entry in list(watchers.items()):
        alive = _pid_is_alive(entry["pid"])
        if not alive:
            del watchers[job_id]
            changed = True
            continue
        scope = entry.get("scope")
        scope_line = f"\n       scope: {scope}" if scope else ""
        print(
            f"  job #{job_id}: lesson {entry['lesson_id']} "
            f"(PID {entry['pid']}, since {entry['spawned_at']})\n"
            f"       log: {entry['log_path']}{scope_line}"
        )

    if changed:
        _save_watchers(watchers)

    if not any(_pid_is_alive(e["pid"]) for e in watchers.values()):
        if not watchers:
            print("  (none — all previously tracked watchers had already exited)")


def _schedule_list():
    jobs = _load_schedule()
    if not jobs:
        print("No scheduled jobs.")
        return

    print("Scheduled jobs:")
    for job in jobs:
        enroll_dt = datetime.fromisoformat(job["enrollment_start"])
        lesson_dt = datetime.fromisoformat(job["lesson_start"])
        run_dt = datetime.fromisoformat(job["run_at"])
        weekday = enroll_dt.strftime("%a")
        url = f"https://schalter.asvz.ch/tn/lessons/{job['lesson_id']}"
        info = job if job.get("title") else fetch_lesson_info(url)
        title    = info.get("title")
        location = info.get("location")
        niveau   = info.get("niveau")

        #title = job.get("title", "Unknown Title")
        #location = job.get("location", "Unknown Location")

        watch_line = ""
        if job.get("watch_free_slot"):
            watch_line = f"       watch:      on, every {job.get('watch_poll_interval', 30)}s\n"

        print(
            f"  {job['id']}: {job['lesson_id']}\n"
            f"       {title}\n"
            f"       {location}\n"
            f"       {niveau}\n"
            f"       enrollment: {weekday}, {enroll_dt.strftime('%Y-%m-%d %H:%M')}\n"
            f"       lesson:     {lesson_dt.strftime('%Y-%m-%d %H:%M')}\n"
            f"       run_at:     {run_dt.strftime('%Y-%m-%d %H:%M')}\n"
            f"{watch_line}"
        )


RUN_LOCK_FILENAME = ".scheduler.lock"


def _schedule_run(args, creds):
    """
    One scheduler pass, never two at once (the timer and the app's
    "Run now" could otherwise start the same enrollment twice). Weekly
    plans from the app are synced first, so newly published dates get
    queued.
    """
    import fcntl
    with open(RUN_LOCK_FILENAME, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            logging.info("Another scheduler run is already in progress — skipping.")
            return
        # First (a wake-up may have just happened): stay awake for as long
        # as this run takes if a sign-up is near.
        _update_keep_awake(in_process=True)
        try:
            asvz_plan.sync()
        except Exception as e:
            logging.warning(f"[plan sync] Failed: {e}")
        return _schedule_run_locked(args, creds)


def _update_keep_awake(in_process=False):
    """Keep the laptop awake around sign-ups and while watchers run; in
    sleep mode also wake it up beforehand and let it run with the lid
    closed (see keep_awake.py)."""
    try:
        import keep_awake
        alive = sum(1 for e in _load_watchers().values() if _pid_is_alive(e["pid"]))
        keep_awake.refresh(_load_schedule(), alive, in_process=in_process)
    except Exception as e:
        logging.warning(f"[keep-awake] {e}")


def _save_run_result(original_jobs, remaining):
    """
    Saves what's left after a run without clobbering changes made while
    it ran (it can sit in enroll() for ~17 minutes): jobs added meanwhile
    are kept, jobs removed meanwhile stay removed.
    """
    with asvz_plan.schedule_lock():
        original_ids = {j["id"] for j in original_jobs}
        current = _load_schedule()
        current_ids = {j["id"] for j in current}
        merged = [j for j in remaining if j["id"] in current_ids]
        merged += [j for j in current if j["id"] not in original_ids]
        _save_schedule(merged)


def _schedule_run_locked(args, creds):
    """
    Run all due scheduled jobs einmal.

    - Jobs mit now < run_at → übersprungen.
    - Jobs mit now > enrollment_start + GRACE → als „expired“ entfernt.
    - Jobs, deren enroll() Exception wirft → bleiben für den nächsten run.
    """
    proxy_url = args.proxy
    jobs = _load_schedule()
    if not jobs:
        logging.info("No scheduled jobs to run.")
        return

    now = datetime.now()
    remaining = []

    for job in jobs:
        if job.get("type") != "lesson":
            logging.warning(f"Unsupported job type: {job.get('type')}, keeping it.")
            remaining.append(job)
            continue

        lesson_id = job["lesson_id"]
        lesson_url = job.get("lesson_url") or "{}/tn/lessons/{}".format(
            LESSON_BASE_URL, lesson_id
        )

        enrollment_start = datetime.fromisoformat(job["enrollment_start"])
        lesson_start = datetime.fromisoformat(job["lesson_start"])
        run_at = datetime.fromisoformat(job["run_at"])
        watch_free_slot = bool(job.get("watch_free_slot"))

        # ---- NEW START-WINDOW LOGIC ----

        # A) Noch nicht dran → warten
        if now < run_at:
            remaining.append(job)
            continue

        # B) Zu spät → Job verpasst.
        # Watch-mode jobs get a much longer leash: the whole point is to
        # keep checking for a freed-up slot for as long as the lesson
        # hasn't started yet, potentially hours after enrollment opened.
        missed_cutoff = lesson_start if watch_free_slot else (
            # Jobs from the app carry their own deadline (they may be added
            # while enrollment is already open).
            datetime.fromisoformat(job["enroll_until"]) if job.get("enroll_until")
            else enrollment_start + timedelta(minutes=5)
        )
        if now > missed_cutoff:
            logging.info(
                f"[schedule run] Job #{job['id']} missed the window "
                f"(now={now}, cutoff={missed_cutoff}), removing."
            )
            continue

        # C) Genau im Startfenster → ausführen
        # run_at ≤ now < enrollment_start
        logging.info(
            f"[schedule run] Starting job #{job['id']} "
            f"(now={now}, run_at={run_at}, enrollment_start={enrollment_start})"
        )

        if watch_free_slot:
            # Persistent-session watching can't run inline in this
            # short-lived scheduler tick — hand it off and forget it.
            # The background process owns success/failure/retries from
            # here on; it will not be retried by this scheduler again.
            try:
                _spawn_watcher(job, creds, args.organisation, proxy_url)
            except Exception as e:
                logging.error(
                    f"[schedule run] Failed to spawn watcher for job "
                    f"#{job['id']}: {e}. Will retry spawning on next run."
                )
                remaining.append(job)
            # else: don't re-add to remaining → handed off successfully
            continue

        # Im Fenster → enroll() starten
        logging.info(
            f"[schedule run] Executing job #{job['id']} for lesson {lesson_id} "
            f"(run_at={run_at}, enrollment_start={enrollment_start})"
        )

        try:
            enroller = AsvzEnroller(lesson_url, creds, proxy_url=proxy_url)
            enroller.enroll()
            logging.info(f"[schedule run] Job #{job['id']} finished successfully.")
            # nicht zu remaining hinzufügen → Job erledigt
        except Exception as e:
            logging.error(
                f"[schedule run] Job #{job['id']} failed with error: {e}. "
                "Will retry on next scheduler run."
            )
            remaining.append(job)

    _save_run_result(jobs, remaining)


# ----------------------------------------------------------------------
# Presentation helpers (banner + colours)
# ----------------------------------------------------------------------

# Colours are only emitted when stdout is an interactive terminal, so
# they never pollute systemd/journal logs or piped output.
_USE_COLOR = sys.stdout.isatty()

RESET = "\033[0m"


def _sgr(text, *codes):
    if not _USE_COLOR:
        return text
    return "\033[" + ";".join(codes) + "m" + text + RESET


# --- basic styles ---
def _bold(t):   return _sgr(t, "1")
def _dim(t):    return _sgr(t, "2")
def _grey(t):   return _sgr(t, "90")

# --- 256-colour foreground helpers (fg 38;5;N) ---
def _fg(t, n):  return _sgr(t, "38", "5", str(n))

# --- themed accent colours (one per menu option) ---
THEME = {
    "add":     46,   # green
    "list":    45,   # cyan
    "remove":  208,  # orange
    "run":     226,  # yellow
    "watchers":39,   # blue
    "stop":    201,  # magenta
    "logs":    141,  # violet
    "quit":    244,  # grey
    "title":   51,   # bright cyan
    "accent":  213,  # pink
}


def _theme(t, key):
    return _fg(_bold(t), THEME.get(key, 255))


# Banner drawn inside a coloured box (background fill on each line) so it
# reads as a proper header rather than plain ASCII text.
_BANNER_LINES = [
    "    _    ______     _______   ____        _   ",
    "   / \\  / ___\\ \\   / /__  /  | __ )  ___ | |_ ",
    "  / _ \\ \\___ \\\\ \\ / /  / /   |  _ \\ / _ \\| __|",
    " / ___ \\ ___) |\\ V /  / /_   | |_) | (_) | |_ ",
    "/_/   \\_\\____/  \\_/  /____|  |____/ \\___/ \\__|",
]

# kept for the argparse --help description (plain text there)
BANNER = "\n" + "\n".join(_BANNER_LINES) + "\n"


def banner():
    """Prints a colour-boxed ASCII banner and a short overview."""
    if _USE_COLOR:
        # dark-blue background (bg 48;5;17), bright-cyan bold text
        for line in _BANNER_LINES:
            padded = line.ljust(48)
            print("  " + _sgr(padded, "1", "38", "5", "51", "48", "5", "17"))
    else:
        print(BANNER)
    print()
    print("  " + _theme("ASVZ Enrollment Scheduler", "title")
          + _grey("  ·  automatic sign-ups for ASVZ sport lessons"))
    print()
    print("  Queue up ASVZ lessons and this bot signs you in the instant")
    print("  enrollment opens. If a lesson is already booked out, it can")
    print("  keep watching in the background and grab a freed-up slot for you.")
    print()
    print("  " + _bold("What you can do:"))
    print(f"    {_theme('add', 'add')}         Queue a lesson by its ID (times read automatically)")
    print(f"    {_theme('list', 'list')}        Show everything currently queued")
    print(f"    {_theme('remove', 'remove')}      Remove a queued lesson")
    print(f"    {_theme('run', 'run')}         Process all due jobs now (used by the systemd timer)")
    print(f"    {_theme('watchers', 'watchers')}    Show background slot-watchers still running")
    print(f"    {_theme('stop-watch', 'stop')}  Stop one background slot-watcher cleanly")
    print(f"    {_theme('logs', 'logs')}        View the service journal and watcher logs")
    print()
    print(_grey("  Tip: run with no arguments for this menu, or"))
    print(_grey("       'python3 asvz_scheduler.py <command> --help' for details."))
    print()


def _prompt(text, default=None):
    suffix = f" [{default}]" if default is not None else ""
    try:
        val = input(_bold(f"  {text}{suffix}: ")).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    if not val and default is not None:
        return str(default)
    return val


def _run_menu(args, creds_getter):
    """
    Simple interactive menu over the working commands. Deliberately does
    NOT expose the legacy/unverified paths — only add/list/remove/run/
    watchers/stop-watch, mirroring the CLI subcommands one-to-one.

    creds_getter is a zero-arg callable that lazily builds credentials
    only when an action actually needs them (add/run), so just browsing
    the menu never prompts for a password.
    """
    banner()
    while True:
        print(_bold("  Menu:"))
        print(f"    {_theme('1', 'add')}) {_fg('Add a lesson', THEME['add'])}")
        print(f"    {_theme('2', 'list')}) {_fg('List scheduled jobs', THEME['list'])}")
        print(f"    {_theme('3', 'remove')}) {_fg('Remove a job', THEME['remove'])}")
        print(f"    {_theme('4', 'run')}) {_fg('Run due jobs now', THEME['run'])}")
        print(f"    {_theme('5', 'watchers')}) {_fg('Show running watchers', THEME['watchers'])}")
        print(f"    {_theme('6', 'stop')}) {_fg('Stop a watcher', THEME['stop'])}")
        print(f"    {_theme('7', 'logs')}) {_fg('View logs / debug', THEME['logs'])}")
        print(f"    {_theme('0', 'quit')}) {_fg('Quit', THEME['quit'])}")
        print()
        choice = _prompt("Choose", default="0")
        print()

        if choice in (None, "0", "q", "quit", "exit"):
            print(_grey("  Bye!"))
            return

        try:
            if choice == "1":
                lesson_id = _prompt("Lesson ID")
                if not lesson_id:
                    print(_fg("  Cancelled.\n", THEME['remove'])); continue
                watch = (_prompt("Watch for a free slot if booked out? (y/N)", "N") or "").lower().startswith("y")
                interval = 30
                if watch:
                    interval = int(_prompt("Poll interval (seconds)", "30") or 30)
                add_args = argparse.Namespace(
                    lesson_id=int(lesson_id),
                    watch_free_slot=watch,
                    watch_poll_interval=interval,
                    proxy=args.proxy,
                )
                _schedule_add_lesson(add_args, creds_getter())
            elif choice == "2":
                _schedule_list()
            elif choice == "3":
                _schedule_list()
                jid = _prompt("Job id to remove")
                if jid:
                    _schedule_remove(int(jid))
            elif choice == "4":
                run_args = argparse.Namespace(proxy=args.proxy, organisation=args.organisation)
                _schedule_run(run_args, creds_getter())
            elif choice == "5":
                _list_watchers()
            elif choice == "6":
                _list_watchers()
                jid = _prompt("Job id to stop")
                if jid:
                    _stop_watch(int(jid))
            elif choice == "7":
                jid = _prompt("Job id for watcher log (blank = all)", default="")
                lines = _prompt("How many lines", default="40") or "40"
                _show_logs(job_id=int(jid) if jid else None, lines=int(lines))
            else:
                print(_fg("  Unknown choice.", THEME['remove']))
        except ValueError:
            print(_fg("  That wasn't a valid number.", THEME['remove']))
        except Exception as e:
            print(_fg(f"  Something went wrong: {e}", THEME['remove']))
        print()


def _show_logs(job_id=None, lines=40, service="asvz-scheduler.service"):
    """
    Debug helper: shows the systemd service journal and per-job watcher
    logs so you can see exactly why a watcher started/exited without
    hunting for journalctl invocations by hand.

    job_id=None shows every watcher log in WATCHER_LOG_DIR; otherwise just
    that job's log. Never raises — missing journalctl or missing log files
    are reported inline, since this is a diagnostics convenience.
    """
    print(_theme(f"== systemd journal ({service}, last {lines} lines) ==", "title"))
    try:
        out = subprocess.run(
            ["journalctl", "--user", "-u", service, "-n", str(lines), "--no-pager"],
            capture_output=True, text=True, timeout=15,
        )
        text = out.stdout.strip() or out.stderr.strip()
        print(text or _grey("  (no journal output — not run as root, or no such unit)"))
    except FileNotFoundError:
        print(_grey("  journalctl not found (not a systemd host?) — skipping."))
    except Exception as e:
        print(_grey(f"  Could not read journal: {e}"))

    print()
    print(_theme("== watcher logs ==", "title"))
    if not os.path.isdir(WATCHER_LOG_DIR):
        print(_grey(f"  No {WATCHER_LOG_DIR}/ directory yet — no watcher has run."))
        return

    if job_id is not None:
        targets = [os.path.join(WATCHER_LOG_DIR, f"job_{job_id}.log")]
    else:
        targets = sorted(
            os.path.join(WATCHER_LOG_DIR, f)
            for f in os.listdir(WATCHER_LOG_DIR)
            if f.endswith(".log")
        )

    if not targets:
        print(_grey("  No watcher log files found."))
        return

    for path in targets:
        print(_fg(f"  --- {path} ---", THEME["logs"]))
        if not os.path.isfile(path):
            print(_grey("    (no such file)"))
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                tail = f.readlines()[-lines:]
            if not tail:
                print(_grey("    (file is empty)"))
            else:
                for ln in tail:
                    print("    " + ln.rstrip())
        except Exception as e:
            print(_grey(f"    Could not read: {e}"))
        print()


def build_parser():
    parser = argparse.ArgumentParser(
        prog="asvz_scheduler.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            BANNER
            + "\nASVZ Enrollment Scheduler \u2014 automatic sign-ups for ASVZ sport lessons.\n\n"
            "Queue up ASVZ lessons and this bot signs you in the instant enrollment\n"
            "opens. If a lesson is already booked out, it can keep watching in the\n"
            "background and grab a freed-up slot for you.\n\n"
            "Run with no command for an interactive menu."
        ),
    )

    parser.add_argument(
        "-o",
        "--organisation",
        dest="organisation",
        help="Organisation (ethz, uzh, …)",
    )
    parser.add_argument("-u", "--username", dest="username", help="Organisation username")
    parser.add_argument(
        "-p", "--password", dest="password", help="Organisation password (optional)"
    )
    parser.add_argument(
        "--save-credentials",
        action="store_true",
        help="Save credentials in local store (same format as asvz_bot).",
    )
    parser.add_argument(
        "--proxy",
        dest="proxy",
        help="Proxy URL, if needed (e.g. http://user:pass@host:port)",
    )

    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="After the command runs, also dump the service journal and "
             "watcher logs (handy for debugging why a watcher exited).",
    )

    sub = parser.add_subparsers(dest="command", required=False)

    sub.add_parser(
        "menu",
        help="Launch the interactive menu (also the default with no command).",
    )

    p_logs = sub.add_parser(
        "logs",
        help="Show the systemd service journal and per-job watcher logs.",
    )
    p_logs.add_argument(
        "job_id", type=int, nargs="?", default=None,
        help="Only show this job's watcher log (default: all watcher logs).",
    )
    p_logs.add_argument(
        "-n", "--lines", type=int, default=40,
        help="Number of trailing lines to show (default: 40).",
    )

    p_add = sub.add_parser("add", help="Add a lesson to the schedule (auto-read times)")
    p_add.add_argument(
        "lesson_id",
        type=int,
        help="Lesson ID (from https://schalter.asvz.ch/tn/lessons/<id>)",
    )
    p_add.add_argument(
        "--watch-free-slot",
        action="store_true",
        help="If the lesson is already booked out when enrollment opens, "
             "keep checking periodically for a freed-up slot instead of giving up.",
    )
    p_add.add_argument(
        "--watch-poll-interval",
        type=int,
        default=30,
        help="Seconds between checks when --watch-free-slot is active (default: 30).",
    )

    p_remove = sub.add_parser("remove", help="Remove a scheduled job by id")
    p_remove.add_argument("job_id", type=int, help="Job id to remove")

    sub.add_parser("list", help="List scheduled jobs")

    sub.add_parser(
        "run",
        help="Run all due scheduled jobs (for systemd timer, cron, etc.)",
    )

    p_stop_watch = sub.add_parser(
        "stop-watch",
        help="Stop a running detached slot-watcher for a specific job "
             "(sends SIGTERM so it shuts down cleanly).",
    )
    p_stop_watch.add_argument(
        "job_id",
        type=int,
        help="Job id whose watcher should be stopped (see 'watchers').",
    )

    sub.add_parser(
        "watchers",
        help="List currently running detached slot-watchers (prunes any "
             "whose process has already exited).",
    )

    sub.add_parser("gui", help="Open the ASVZ Bot app window.")
    sub.add_parser(
        "plan-sync",
        help="Queue newly published dates for the app's 'every week' offers now.",
    )
    sub.add_parser("status", help="Show sleep-mode status.")
    p_lid = sub.add_parser(
        "closed-lid",
        help="Sleep mode: wake the laptop before each sign-up and keep it running "
             "with the lid closed until enrollment is done.",
    )
    p_lid.add_argument("action", choices=["enable", "disable", "status"])
    p_lid.add_argument("--allow-battery", action="store_true",
                       help="Also run lid-closed on battery (default: only when plugged in).")

    return parser


def main():
    # Data files are relative paths; keep them next to the scripts no
    # matter where we're started from (app launcher, another directory).
    os.chdir(os.path.dirname(os.path.abspath(__file__)))

    parser = build_parser()
    args = parser.parse_args()

    if args.command == "gui":
        import app
        sys.exit(app.main())
    if args.command == "plan-sync":
        logging.info(f"Queued {asvz_plan.sync(force=True)} new date(s).")
        _update_keep_awake()
        return
    if args.command in ("status", "closed-lid"):
        import keep_awake
        if args.command == "closed-lid" and args.action == "enable":
            print("  " + keep_awake.enable(allow_battery=args.allow_battery)[1])
            _update_keep_awake()
        elif args.command == "closed-lid" and args.action == "disable":
            print("  " + keep_awake.disable()[1])
        else:
            print(keep_awake.status_text())
        return

    # No command given -> launch the interactive menu instead of erroring.
    if args.command in (None, "menu"):
        def _lazy_creds():
            return CredentialsManager(
                args.organisation, args.username, args.password, args.save_credentials
            ).get()
        _run_menu(args, _lazy_creds)
        return

    # credentials werden nur für add/run benötigt
    creds = None
    if args.command in ("add", "run"):
        cm = CredentialsManager(
            args.organisation, args.username, args.password, args.save_credentials
        )
        creds = cm.get()

    if args.command == "add":
        _schedule_add_lesson(args, creds)
    elif args.command == "remove":
        _schedule_remove(args.job_id)
    elif args.command == "list":
        _schedule_list()
    elif args.command == "run":
        _schedule_run(args, creds)
    elif args.command == "stop-watch":
        _stop_watch(args.job_id)
    elif args.command == "watchers":
        _list_watchers()
    elif args.command == "logs":
        _show_logs(job_id=args.job_id, lines=args.lines)
    else:
        raise AsvzBotException(f"Unknown command: {args.command!r}")

    if args.command in ("add", "remove", "run", "stop-watch", "watchers"):
        _update_keep_awake()

    # --verbose: after any command, also dump journal + watcher logs.
    if getattr(args, "verbose", False) and args.command != "logs":
        print()
        _show_logs()


if __name__ == "__main__":
    main()
