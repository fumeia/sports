# coding: utf-8
"""Stand-in for the real asvz_bot.py in sandbox tests: same names the
scheduler imports, but no Chrome and no ASVZ login. "Enrolling" appends
a line to enrolled.log."""
import json
import os
from datetime import datetime, timedelta

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
        import logging
        logging.info("Your place number is: 5")
        with open("enrolled.log", "a") as f:
            f.write(f"{datetime.now():%H:%M:%S} enrolled {self.lesson_url}\n")
