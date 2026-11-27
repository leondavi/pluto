"""Cross-agent job links in finished runs: a job whose `follows` or
`after_jobs` names a job opened by another agent. Prints one line per run
with any; prints nothing for clean runs. Jobs handed over (ownership log) are
counted by opener, so a deliberate transfer is not flagged twice.

usage: ss_crossjobs.py STUDY_DIR [RUN_ID ...]
"""
import glob
import json
import os
import sys

study = sys.argv[1]
runs = sys.argv[2:] or [os.path.basename(p) for p in glob.glob(f"{study}/runs/*")]
for rid in runs:
    pk = {}
    for f in glob.glob(f"{study}/runs/{rid}/pluto/home/jobs/*/package.json"):
        try:
            d = json.load(open(f))
        except (OSError, ValueError):
            continue
        pk[d["id"]] = d
    bad = []
    for d in pk.values():
        for key in ("follows",):
            t = d.get(key)
            if t in pk and pk[t]["opener"] != d["opener"]:
                bad.append(f"{d['opener'][-2:]} {key} {pk[t]['opener'][-2:]}")
        for t in d.get("after_jobs") or []:
            if t in pk and pk[t]["opener"] != d["opener"]:
                bad.append(f"{d['opener'][-2:]} after {pk[t]['opener'][-2:]}")
    if bad:
        from collections import Counter
        print(rid, len(pk), "jobs; cross-agent:", dict(Counter(bad)))
