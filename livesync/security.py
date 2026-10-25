#!/usr/bin/env python3
"""Who may use the shared vault database (CouchDB _security).

    security.py ADMIN_PW DB                 make sure only listed users get in
    security.py ADMIN_PW DB add NAME        let NAME read and write the vault
    security.py ADMIN_PW DB remove NAME     take NAME out again

Only "admin" (you) administers the database; collaborators are members:
they can read and write notes, not change the database's settings.

Members are every login made with add-user.sh (CouchDB's _users list),
so the list can always be rebuilt — e.g. after the LiveSync plugin's
"Rebuild everything" deleted and recreated the database, which drops
its access list. "admin" always stays in it."""
import base64
import json
import sys
import urllib.parse
import urllib.request

pw, db = sys.argv[1], sys.argv[2]
op, name = (sys.argv[3], sys.argv[4]) if len(sys.argv) == 5 else (None, None)
base = "http://127.0.0.1:5984"
url = f"{base}/{db}/_security"
auth = {"Authorization": "Basic " + base64.b64encode(f"admin:{pw}".encode()).decode()}


def call(method, body=None, target=None):
    req = urllib.request.Request(target or url, method=method, headers={**auth, "Content-Type": "application/json"},
                                 data=json.dumps(body).encode() if body is not None else None)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


sec = call("GET")
q = urllib.parse.urlencode({"startkey": '"org.couchdb.user:"', "endkey": '"org.couchdb.user:\ufff0"'})
users = {r["id"].split(":", 1)[1] for r in call("GET", target=f"{base}/_users/_all_docs?{q}")["rows"]}
members = set(sec.get("members", {}).get("names", [])) | users | {"admin"}
if op == "add":
    members.add(name)
elif op == "remove":
    members.discard(name)
sec = {"admins": {"names": ["admin"], "roles": []},
       "members": {"names": sorted(members), "roles": []}}
call("PUT", sec)
print(json.dumps(sec))
