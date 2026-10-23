#!/usr/bin/env python3
"""Fake systemctl for sandbox tests. Remembers whether the bot's timer is
on; "start <unit>.service" really runs the bot folder's scheduler ("run")
in the background and writes what it prints into a fake journal, in the
JSON format journalctl -o json uses."""
import json
import os
import subprocess
import sys
import time

ROOT = os.environ["VB_TEST_ROOT"]
TIMER_ON = os.path.join(ROOT, "timer-on")
RUNNING = os.path.join(ROOT, "run.pid")
JOURNAL = os.path.join(ROOT, "journal.jsonl")


def entry(msg, who="python3", pid=None, prio=6):
    with open(JOURNAL, "a") as f:
        f.write(json.dumps({"__REALTIME_TIMESTAMP": str(int(time.time() * 1e6)), "MESSAGE": msg,
                            "_PID": str(pid or os.getpid()), "SYSLOG_IDENTIFIER": who,
                            "PRIORITY": str(prio)}) + "\n")


def running():
    try:
        os.kill(int(open(RUNNING).read()), 0)
        return True
    except (OSError, ValueError):
        return False


def do_run():
    open(RUNNING, "w").write(str(os.getpid()))
    entry("Starting asvz-scheduler.service - ASVZ Enrollment Scheduler...", "systemd", 1)
    bot = os.environ["VB_TEST_BOT"]
    p = subprocess.Popen([sys.executable, os.path.join(bot, "asvz_scheduler.py"), "run"], cwd=bot,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    for line in p.stdout:
        entry(line.rstrip(), pid=p.pid, prio=3 if " ERROR " in line else 6)
    p.wait()
    entry("asvz-scheduler.service: Deactivated successfully.", "systemd", 1)
    entry("Finished asvz-scheduler.service - ASVZ Enrollment Scheduler.", "systemd", 1)
    os.remove(RUNNING)


def main():
    a = sys.argv[1:]
    while a and (a[0] in ("--user", "--no-block") or a[0] == "-M" or "@" in a[0]):
        a = a[2:] if a[0] == "-M" else a[1:]
    a = [x for x in a if x not in ("--no-block", "--now", "--timestamp=unix")]
    cmd = a[0] if a else ""
    if cmd == "is-active":
        on = os.path.exists(TIMER_ON) if a[1].endswith(".timer") else running()
        print("active" if on else "inactive")
        sys.exit(0 if on else 3)
    if cmd == "show":
        print(f"@{int(time.time()) + 120}" if os.path.exists(TIMER_ON) else "n/a")
    elif cmd == "enable":
        open(TIMER_ON, "w").close()
    elif cmd == "disable":
        if os.path.exists(TIMER_ON):
            os.remove(TIMER_ON)
    elif cmd == "start" and not running():
        subprocess.Popen([sys.executable, __file__, "_run"], start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    elif cmd == "_run":
        do_run()


if __name__ == "__main__":
    main()
