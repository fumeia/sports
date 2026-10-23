#!/usr/bin/env python3
# coding: utf-8
"""
Runs the web service against a throw-away bot folder with fake ASVZ data,
fake systemctl, a stub asvz_bot.py and a stand-in scheduler — for trying the page without a
server or network:  python3 tests/devserver.py /tmp/vbtest [port]
Page login: test / test-password-123
"""
import json
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
root = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else "/tmp/vbtest")
port = sys.argv[2] if len(sys.argv) > 2 else "8710"
bot, data = os.path.join(root, "bot"), os.path.join(root, "data")
os.makedirs(bot, exist_ok=True)
os.makedirs(data, exist_ok=True)
# The bot folder: stub asvz_bot.py + a scheduler with the server's file
# handling (or the real server scheduler: VB_TEST_SCHEDULER=/path/to/it).
shutil.copy(os.path.join(HERE, "fake_bot", "asvz_bot.py"), bot)
shutil.copy(os.environ.get("VB_TEST_SCHEDULER") or os.path.join(HERE, "fake_bot", "asvz_scheduler.py"),
            os.path.join(bot, "asvz_scheduler.py"))
open(os.path.join(bot, "location.py"), "w").write("def fetch_lesson_info(url):\n    return {}\n")
if not os.path.exists(os.path.join(bot, ".asvz-bot.json")):
    json.dump({"organisation": "SWITCH edu-ID", "username": "dummy.user@example.com",
               "password": "dummy"}, open(os.path.join(bot, ".asvz-bot.json"), "w"))

os.environ.update(VB_BOT_DIR=bot, VB_DATA_DIR=data, VB_PORT=port, VB_INSECURE_COOKIE="1",
                  VB_SYSTEMCTL=os.path.join(HERE, "fake_bin_systemctl"),
                  VB_JOURNALCTL=os.path.join(HERE, "fake_bin_journalctl"),
                  VB_TEST_STATE=os.path.join(root, "timer-on"), VB_TEST_DIR=HERE,
                  VB_BOT_PYTHON=sys.executable, VB_TIMER_SCOPE="user", VB_TIMER_USER="test")
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
