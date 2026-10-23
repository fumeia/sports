#!/usr/bin/env python3
"""Who may use the shared vault database (CouchDB _security).

    security.py ADMIN_PW DB                 make sure only listed users get in
    security.py ADMIN_PW DB add NAME        let NAME read and write the vault
    security.py ADMIN_PW DB remove NAME     take NAME out again

Only "admin" (you) administers the database; collaborators are members:
they can read and write notes, not change the database's settings. An
empty members list would let *every* CouchDB user in, so "admin" always
stays in it."""
import base64
import json
import sys
import urllib.request

pw, db = sys.argv[1], sys.argv[2]
op, name = (sys.argv[3], sys.argv[4]) if len(sys.argv) == 5 else (None, None)
url = f"http://127.0.0.1:5984/{db}/_security"
auth = {"Authorization": "Basic " + base64.b64encode(f"admin:{pw}".encode()).decode()}


def call(method, body=None):
    req = urllib.request.Request(url, method=method, headers={**auth, "Content-Type": "application/json"},
                                 data=json.dumps(body).encode() if body is not None else None)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


sec = call("GET")
members = set(sec.get("members", {}).get("names", [])) | {"admin"}
if op == "add":
    members.add(name)
elif op == "remove":
    members.discard(name)
sec = {"admins": {"names": ["admin"], "roles": []},
       "members": {"names": sorted(members), "roles": []}}
call("PUT", sec)
print(json.dumps(sec))
