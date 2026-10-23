# coding: utf-8
"""
keep_awake.py — server version.

The desktop app keeps a laptop awake around sign-ups (systemd-inhibit,
rtcwake, closed-lid mode). A server never sleeps, so on the server every
function here does nothing. asvz_scheduler.py imports this module and
calls refresh() after each run; with this file in place it stays quiet.
"""

MIN_BATTERY_PERCENT = 25
HOT_CELSIUS = 85


def supported():
    return False


def load_state():
    return {}


def refresh(jobs, watchers_alive, in_process=False):
    return ""


def release():
    pass


def enable(allow_battery=False):
    return False, "Sleep mode is not needed on a server."


def disable():
    return True, "Sleep mode is not used on a server."


def set_allow_battery(allow):
    pass


def status_text():
    return "  Sleep mode: not used on a server (it never sleeps)."
