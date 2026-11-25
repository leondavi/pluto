"""Experiment 6: release trains - plan, workspace builder, ground truth and grader."""

import math
import os
import random

import ss_build
import wl_train

NAME = "e6_release_trains"
LANES, GROUP, BUILDS = 40, 10, 4
BAKE_S, TTL_S, DOWN_TTL_S, OUTAGE_RUN, TIMEOUT = 72.0, 70.0, 60.0, 25, 1200
BAD = {"ERRORS": 4, "LATENCY": 4, "MEMORY": 3, "CHECKSUM": 3, "OUTAGE": 4}     # per agent
COMPONENTS = ("auth", "billing", "cache", "codec", "gateway", "i18n", "ledger", "mailer",
              "queue", "render", "router", "store")
VERSIONS = 6
W_DECIDE, W_REPORT, W_CULPRIT = .50, .40, .10
ANSWER = "answers/final.json"


def owner(rec):
    return f"w{(rec['lane'] - 1) // GROUP + 1}"


def _policy(rng):
    return {"lat_ratio": rng.choice((1.15, 1.25, 1.35, 1.5)),
            "err_margin": rng.choice((1.5, 2.5, 3.5)), "mem_mb": rng.choice((60, 90, 130))}


def _params(rng, kind, pol):
    """Canary behaviour that gives ``kind`` under the policy in force."""
    err_s = rng.uniform(.008, .015)
    m = pol["err_margin"] / 100
    return {"err_s": err_s, "err_c": err_s + (m + rng.uniform(.02, .03) if kind == "ERRORS"
                                              else m * rng.uniform(0, .3)),
            "lat": pol["lat_ratio"] * rng.uniform(1.14, 1.25) if kind == "LATENCY"
            else rng.uniform(1.0, max(1.0, pol["lat_ratio"] * .9)),
            "grow": pol["mem_mb"] * (rng.uniform(1.5, 2.0) if kind == "MEMORY"
                                     else rng.uniform(0, .5)),
            "ck_c": rng.uniform(.004, .01) if kind == "CHECKSUM" else 0.0,
            "noise_err": rng.choice((.02, .15, .3)), "noise_lat": rng.choice((1.0, 1.6, 2.2)),
            "blips": sorted(rng.uniform(.2, .9) for _ in range(1))
            if pol["err_margin"] > 3 and rng.random() < .5 else []}


def plan(seed, bake_s=BAKE_S, ttl=TTL_S, down_ttl=DOWN_TTL_S, speed=1.0):
    """({lane: params}, {reason: culprit}). Every drawn build obeys the rules exactly."""
    rng = random.Random(f"plan6:{seed}")
    culprits = {k: f"{c}@{rng.randrange(1, VERSIONS + 1)}"
                for k, c in zip(BAD, rng.sample(COMPONENTS, len(BAD)))}
    guilty, lanes = set(culprits.values()), {}
    for g in range(LANES // GROUP):
        kinds = [k for k, n in BAD.items() for _ in range(n)]
        kinds += ["OK"] * (GROUP * BUILDS - len(kinds))
        rng.shuffle(kinds)
        for i in range(GROUP):
            lane, pol, builds = g * GROUP + i + 1, None, []
            for q in range(BUILDS):
                kind = kinds[i * BUILDS + q]
                at = 0 if q == 0 else rng.uniform(.3, .9) if rng.random() < .5 else None
                if at is not None:
                    pol = _policy(rng)
                if kind == "OUTAGE" and at:             # must not cut off a policy line
                    at = rng.uniform(.05, .1)
                parts = [rng.choice([x for x in (f"{c}@{v + 1}" for v in range(VERSIONS))
                                     if x not in guilty]) for c in COMPONENTS]
                if kind != "OK":
                    name = culprits[kind].split("@")[0]
                    parts[COMPONENTS.index(name)] = culprits[kind]
                b = {"id": f"{rng.getrandbits(24):06x}b", "seq": q + 1, "plan": kind,
                     "parts": parts, "policy": " ".join(f"{k}={v}" for k, v in pol.items()),
                     "policy_at": at, "bake_s": bake_s * rng.uniform(.94, 1.06),
                     "down_at": rng.uniform(.35, .8)}
                while True:
                    b.update(_params(rng, kind, pol), seed=rng.randrange(2**40))
                    got = evaluate([x for _, x in wl_train.bake(b)], pol)
                    if kind == "OUTAGE" and got["reason"] == "OK" or got["reason"] == kind:
                        break
                builds.append(b)
            lanes[lane] = {"lane": lane, "builds": builds, "ttl": ttl, "down_ttl": down_ttl,
                           "speed": speed}
    return lanes, culprits


def p95(xs):
    return sorted(xs)[math.ceil(.95 * len(xs)) - 1]


def evaluate(lines, pol):
    """Reference verdict of one bake by the documented rules."""
    ms, err, mem, bad, warm = {"canary": [], "stable": []}, {"canary": 0, "stable": 0}, [], 0, False
    for x in lines:
        t = x.split()
        f = dict(y.split("=", 1) for y in t[2:] if "=" in y)
        if t[1] == "BAKE" and t[2] == "warm":
            warm = True
        elif t[1] == "POLICY":
            pol = {k: float(v) for k, v in f.items()}
        elif t[1] == "MEM" and warm and f["group"] == "canary":
            mem.append(float(f["rss_mb"]))
        elif t[1] == "REQ" and warm and f["kind"] == "user" and f["retry"] == "0":
            ms[f["group"]].append(int(f["ms"]))
            err[f["group"]] += f["status"][0] == "5"
            bad += (f["group"] == "canary"
                    and int(f["ck"]) != wl_train.checksum(int(f["id"]), int(f["ms"])))
    rate = {g: 100 * err[g] / len(ms[g]) for g in ms}
    why = [k for k, hit in (
        ("ERRORS", rate["canary"] > rate["stable"] + pol["err_margin"]),
        ("LATENCY", p95(ms["canary"]) > pol["lat_ratio"] * p95(ms["stable"])),
        ("MEMORY", mem[-1] - mem[0] > pol["mem_mb"]), ("CHECKSUM", bad > 0)) if hit]
    return {"reason": "+".join(why) or "OK", "p95": p95(ms["canary"]), "errors": err["canary"]}


def truth(seed, **kw):
    lanes, culprits = plan(seed, **kw)
    out = {}
    for k, p in lanes.items():
        pol = None
        for b in p["builds"]:
            pol = {x: float(y) for x, y in (z.split("=") for z in b["policy"].split())}
            w = {"lane": k, "seq": b["seq"], "reason": b["plan"]}
            if b["plan"] != "OUTAGE":
                w.update(evaluate([x for _, x in wl_train.bake(b)], pol))
            out[b["id"]] = w
    return {"builds": out, "culprits": culprits}


def build(ws, seed, **kw):
    lanes, _ = plan(seed, **kw)
    ss_build.install(ws, "train", "wl_train", {f"lane{k}.img": p for k, p in lanes.items()})
    ss_build.write(ws, "docs/TRAIN.md", DOC)
    os.makedirs(os.path.join(ws, "control"), exist_ok=True)


def _d(x):
    return x if isinstance(x, dict) else {}


def _eq(a, b):
    try:
        return not isinstance(a, bool) and float(a) == float(b)
    except (TypeError, ValueError):
        return False


def report_score(got, want):
    """Reason 0.4, p95 0.3, errors 0.3; an outage has a reason only."""
    got = _d(got)
    hit = str(got.get("reason")).strip().upper() == want["reason"]
    if want["reason"] == "OUTAGE":
        return float(hit)
    return .4 * hit + .3 * _eq(got.get("p95"), want["p95"]) + .3 * _eq(
        got.get("errors"), want["errors"])


def decisions(ws, want):
    """{build: action} of the first launch of every lane."""
    first, out = {}, {}
    for r in ss_build.launches(ws):
        first.setdefault(r.get("lane"), r)
    for r in first.values():
        out.update({k: v[0] for k, v in _d(r.get("dec")).items()
                    if k in want and v[0] != "none"})
    return out


def grade(ws, tr, meta=None):
    """Decisions in time 0.50 (right +1, wrong -1, none 0; good and bad builds weigh the
    same, so a constant action on all builds earns nothing), build reports 0.40, culprits 0.10."""
    want = tr["builds"]
    ans = ss_build.read_json(os.path.join(ws, ANSWER))
    dec, marks = decisions(ws, want), {True: [], False: []}
    for k, w in want.items():
        good = w["reason"] == "OK"
        right = "promote" if good else "rollback"
        marks[good].append(0 if k not in dec else 1 if dec[k] == right else -1)
    x = max(0.0, sum(sum(m) / len(m) for m in marks.values()) / 2)
    got, ys = _d(_d(ans).get("builds")), []
    part = {n: _d(ss_build.read_json(os.path.join(ws, f"answers/lane_{n}.json")))
            for n in range(1, LANES + 1)}
    for k, w in want.items():
        g = got.get(k)
        if not isinstance(g, dict):
            g = part[w["lane"]].get(k)
        ys.append(report_score(g, w))
    y = sum(ys) / len(ys)
    c = _d(_d(ans).get("culprits"))
    z = sum(str(c.get(k)).strip() == v for k, v in tr["culprits"].items()) / len(tr["culprits"])
    out = [m for ms in marks.values() for m in ms]
    parts = {"decisions": round(x, 4), "reports": round(y, 4), "culprits": round(z, 4)}
    return {"score": round(W_DECIDE * x + W_REPORT * y + W_CULPRIT * z, 4), "parts": parts,
            "process": {"right": out.count(1), "wrong": out.count(-1), "missed": out.count(0)},
            "final_valid": isinstance(ans, dict)}


DOC = """# Release trains

`bin/train --lane K` (K = 1..40) releases four candidate builds, one after the other,
and prints 1 to 1.5 MB in about 5 to 9 minutes. Each build first bakes for about a
minute: live traffic is split between the old version (`group=stable`) and the
candidate (`group=canary`). Then the lane opens a gate and waits for a decision.
The bake lines of a lane are the same in every launch, but only decisions taken
by the first launch of a lane count.

| Line (the first field is the time in the bake, ms) | Meaning |
|---|---|
| `BAKE begin build seq` | a new build starts; `seq` is 1 to 4 |
| `MANIFEST build c@v c@v ...` | component versions in the build |
| `POLICY lat_ratio err_margin mem_mb` | limits; valid for the whole build in which the line appears (also for lines before it) and for the lane's later builds until the next POLICY |
| `BAKE warm build` | the warm-up of the build is over |
| `REQ group kind retry route status ms id ck span` | one request |
| `MEM group rss_mb` | memory sample |
| `GATE open build ttl` / `GATE closed build result` | the lane waits for a decision / goes on |
| `IDLE`, `INFO`, `WARN`, `ERROR`, `#` | operational noise, no influence on a decision |

## Rules

Only lines after `BAKE warm` of the build count. A REQ line counts only if kind=user
and retry=0. An error is a counted line with a status from 500 to 599. p95 of n counted `ms`
values is the value at position ceil(0.95 n), counted from 1, in ascending order.

| Reason | A build is bad if |
|---|---|
| ERRORS | canary error rate in percent > stable error rate in percent + err_margin |
| LATENCY | canary p95 > lat_ratio x stable p95 |
| MEMORY | last minus first canary rss_mb > mem_mb |
| CHECKSUM | a counted canary line has ck != (id*31 + ms*17) mod 4093 |
| OUTAGE | 25 counted canary lines in a row are errors (see below) |

A build is bad for at most one reason; otherwise it is OK. An OUTAGE build is not judged by the other rules.

## Decisions

A decision is a line `BUILD promote` or `BUILD rollback` (BUILD is the id of the build)
in `control/lane_K.txt`. Append one line per build; the first one that the lane accepts is
final. Promote good builds, roll back bad ones.

* At a gate: within 70 s after `GATE open`. Without a decision the lane goes on and the build
  counts as undecided. A decision written earlier is taken when the gate opens.
* Outage: from some moment of the bake on, all counted canary lines fail, the bake does not end and
  no gate opens. Roll the build back within 60 s of the first failing line of that run. Shorter runs of errors
  (up to 24 counted canary lines) are harmless blips and recover by themselves.

A lane continues as soon as it has a decision. A wrong decision costs as much as a right one earns.

## Culprits

Each of the five reasons is caused by exactly one component version (`name@version`): it is in
the manifest of every build, on all lanes, that is bad for this reason and of no other build.
"""

TASK = """Forty release lanes each ship four candidate builds under live traffic, and every build needs an operator's decision. Read docs/TRAIN.md first: it defines the line format, the rules for good and bad builds, how to decide and the time limits.

Run `bin/train --lane K` for K = 1..40, each once, and
1. decide on every build in time while the lanes run: promote good builds, roll back bad ones, and roll back outages early;
2. report for every build its reason (OK if good), the p95 of the canary group (int ms) and the number of canary errors, both over counted lines (null for OUTAGE builds);
3. find the component version that causes each reason. This needs the builds of all lanes.

Ownership: w1 = lanes 1-10; w2 = 11-20; w3 = 21-30; w4 = 31-40. Run all ten of your lanes at the same time. Each lane prints 1 to 1.5 MB, so never print a whole log. You may prepare before you launch.
Each agent writes answers/lane_K.json for each of its lanes: {"BUILD": {"reason": "OK|ERRORS|LATENCY|MEMORY|CHECKSUM|OUTAGE", "p95": int|null, "errors": int|null}, ... 4 builds}, and shares its evidence on culprits in board/.
w1 combines everything, waiting for the others, into answers/final.json: {"builds": {"BUILD": {...}, ... all 160}, "culprits": {"ERRORS": "name@version", "LATENCY": ..., "MEMORY": ..., "CHECKSUM": ..., "OUTAGE": ...}}"""
