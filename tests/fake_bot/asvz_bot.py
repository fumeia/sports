# coding: utf-8
"""Stand-in for the real asvz_bot.py in sandbox tests: same names the
scheduler imports, but no Chrome and no ASVZ login. "Enrolling" appends
a line to enrolled.log. Also points asvz_plan at the fake ASVZ API."""
import json
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.environ.get("VB_TEST_DIR", ""))
try:
    import fake_asvz, asvz_plan
    fake_asvz.patch(asvz_plan)
except ImportError:
    pass

LESSON_BASE_URL = "https://schalter.asvz.ch"
CREDENTIALS_FILENAME = ".asvz-bot.json"


class AsvzBotException(Exception):
    pass


class CredentialsManager:
    def __init__(self, org, uname, password, save_credentials):
        with open(CREDENTIALS_FILENAME) as f:
            self.credentials = json.load(f)

    def get(self):
        return self.credentials


class _Driver:
    def get(self, url): pass
    def implicitly_wait(self, s): pass
    def quit(self): pass


class AsvzEnroller:
    def __init__(self, lesson_url, creds, chromedriver_path=None, proxy_url=None, **kw):
        self.lesson_url = lesson_url

    @staticmethod
    def get_driver(chromedriver_path=None, proxy_url=None):
        return _Driver()

    @staticmethod
    def _AsvzEnroller__get_enrollment_and_start_time(driver):
        start = datetime.now().replace(second=0, microsecond=0) + timedelta(days=2)
        return start - timedelta(days=1), start

    def enroll(self):
        with open("enrolled.log", "a") as f:
            f.write(f"{datetime.now():%H:%M:%S} enrolled {self.lesson_url}\n")
