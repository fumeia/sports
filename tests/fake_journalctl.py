#!/usr/bin/env python3
"""Fake journalctl for sandbox tests: prints the last -n lines of the
fake journal fake_systemctl.py writes (already in -o json format)."""
import os
import sys

n = int(sys.argv[sys.argv.index("-n") + 1]) if "-n" in sys.argv else 100
try:
    lines = open(os.path.join(os.environ["VB_TEST_ROOT"], "journal.jsonl")).read().splitlines()
except FileNotFoundError:
    lines = []
print("\n".join(lines[-n:]))
