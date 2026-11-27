"""Experiment 11: storm-surge ensemble forecast (see README.md, E11).

Four agents run 48 members of a shallow-water surge model plus a nature run, stop
and rerun members that go numerically unstable before the issue deadline, discard
non-conservative members and verify the ensemble. Plan: exp11_plan.py; truth and
grader: exp11_score.py; the model: wl_swm.py.
"""

import exp11_plan
import exp11_score
import pst_build

NAME = "e11_surge_ensemble"
TIMEOUT, ANSWER = exp11_plan.TIMEOUT, exp11_plan.ANSWER
plan, truth, grade = exp11_plan.plan, exp11_score.truth, exp11_score.grade


def owner(rec):
    m = int(rec.get("member") or 0)
    return "w1" if m == 0 else f"w{(m - 1) // exp11_plan.PER_AGENT + 1}"


def build(ws, seed, **kw):
    pl = plan(seed, **kw)
    imgs = {f"member{m}.img": p for m, p in pl["members"].items()}
    imgs["member0.img"] = pl["nature"]
    pst_build.install(ws, "swm", "wl_swm", imgs)
    pst_build.write(ws, "docs/FORECAST.md", exp11_plan.docs(pl))


TASK = """A storm-surge ensemble forecast must be run, checked and verified. Read docs/FORECAST.md first: it explains the model output, which runs are valid, the deadline and the products.

Run every member with its configured step, all twelve of yours at the same time and as soon as possible, watch the runs while they go and react in time. Ownership: w1 = members 1-12 and the nature run; w2 = 13-24; w3 = 25-36; w4 = 37-48. Runs are long, so never print a whole run.
Each agent writes answers/member_M.json for each of its members (format in the docs) and says in board/ where the output of its valid runs can be read.
w1 computes the products from all valid runs, waiting for the others, and writes answers/final.json: {"members": {"1": {...}, ..., "48": {...}}, "skill": {"lead_h": [12, 24, 36, 48, 60, 72], "rmse": [6 floats], "spread": [6 floats]}, "rank_histogram": [n + 1 integers], "q1": float, "q2": float, "q3": int, "q4": float}"""
