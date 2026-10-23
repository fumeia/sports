#!/usr/bin/env python3
# coding: utf-8
"""
Runs the web service against a throw-away bot folder, for trying the page
without a server or network:  python3 tests/devserver.py /tmp/vbtest [port]
Page login: test / test-password-123

Fakes: ASVZ's lesson search (fake_asvz.py), asvz_bot.py (only writes
enrolled.log), a stand-in scheduler with the server's file handling (or
the real one: VB_TEST_SCHEDULER=/path/to/asvz_scheduler.py), systemctl /
journalctl (fake_systemctl.py, fake_journalctl.py). The bot's timer is
simulated: while it's on, a scheduler run starts every VB_TEST_TICK
seconds (default 20). The helper (deploy/vb-bot-helper) is the real one,
run directly instead of through sudo.
"""
import json
import os
import shutil
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
root = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else "/tmp/vbtest")
port = sys.argv[2] if len(sys.argv) > 2 else "8710"
bot, data = os.path.join(root, "bot"), os.path.join(root, "data")
os.makedirs(bot, exist_ok=True)
os.makedirs(data, exist_ok=True)
shutil.copy(os.path.join(HERE, "fake_bot", "asvz_bot.py"), bot)
shutil.copy(os.environ.get("VB_TEST_SCHEDULER") or os.path.join(HERE, "fake_bot", "asvz_scheduler.py"),
            os.path.join(bot, "asvz_scheduler.py"))
open(os.path.join(bot, "location.py"), "w").write("def fetch_lesson_info(url):\n    return {}\n")
if not os.path.exists(os.path.join(bot, ".asvz-bot.json")):
    json.dump({"organisation": "SWITCH edu-ID", "username": "dummy.user@example.com",
               "password": "dummy"}, open(os.path.join(bot, ".asvz-bot.json"), "w"))

conf = os.path.join(root, "helper.conf")
with open(conf, "w") as f:
    f.write(f"BOT_DIR={bot}\nBOT_PYTHON={sys.executable}\nSCHEDULER_SCRIPT=asvz_scheduler.py\n"
            "TIMER_UNIT=asvz-scheduler\nTIMER_SCOPE=system\n")
systemctl = [sys.executable, os.path.join(HERE, "fake_systemctl.py")]
os.environ.update(
    VB_BOT_DIR=bot, VB_DATA_DIR=data, VB_PORT=port, VB_INSECURE_COOKIE="1",
    VB_HELPER_CMD=f"{sys.executable} {os.path.join(REPO, 'deploy', 'vb-bot-helper')}",
    VB_HELPER_TEST="1", VB_HELPER_CONF=conf, VB_TEST_ROOT=root, VB_TEST_BOT=bot,
    VB_SYSTEMCTL=os.path.join(HERE, "fake_systemctl.py"),
    VB_JOURNALCTL=os.path.join(HERE, "fake_journalctl.py"))


def fake_timer():
    tick = float(os.environ.get("VB_TEST_TICK", "20"))
    while True:
        time.sleep(tick)
        if os.path.exists(os.path.join(root, "timer-on")):
            subprocess.run(systemctl + ["start", "asvz-scheduler.service"])


threading.Thread(target=fake_timer, daemon=True).start()
sys.path.insert(0, os.path.join(REPO, "web"))
sys.path.insert(0, HERE)
import server  # noqa: E402
import fake_asvz  # noqa: E402

fake_asvz.patch(server.plan)
salt = os.urandom(16)
server.write_private(server.AUTH_FILE, {"user": "test", "salt": salt.hex(),
                                        "hash": server._scrypt("test-password-123", salt).hex()})
sys.argv = sys.argv[:1]
server.main()
