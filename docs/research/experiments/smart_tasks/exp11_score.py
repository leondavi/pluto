"""Experiment 11: log parsing, ensemble products, truth and grader.

Truth is recomputed by integrating every member again (wl_swm.trace) and reading
the printed values, exactly as an agent reads them; it never uses the plan's
own integrations.
"""

import math
import os
import statistics

import exp11_plan as e11
import pst_build
import wl_swm

W = {"members": .25, "operations": .20, "skill": .20, "rank": .10, "questions": .25}


def _num(x, typ=float):
    try:
        v = typ(x)
    except (TypeError, ValueError):
        return None
    return v if isinstance(v, int) or math.isfinite(v) else None


def parse(lines, field_t=None, stop_at_onset=False):
    """Facts of one run from its printed lines."""
    out = {"onset_t": None, "mass0": None, "drift_max": 0.0, "drift_last": None,
           "complete": False, "obs": {}, "field": None}
    for x in lines:
        if x.startswith("step "):
            kv = dict(t.split("=", 1) for t in x.split()[2:])
            if out["onset_t"] is None and not float(kv["cfl"]) <= 1.0:
                out["onset_t"] = float(kv["t"])
                if stop_at_onset:
                    break
            m = float(kv["mass"])
            if out["mass0"] is None:
                out["mass0"] = m
            d = abs(m / out["mass0"] - 1)
            out["drift_last"] = d
            out["drift_max"] = d if not d <= out["drift_max"] else out["drift_max"]
        elif x.startswith("obs "):
            t, v = x[4:].split(" ", 1)
            out["obs"][float(t[2:])] = [float(y) for y in v[4:].split(",")]
        elif x.startswith("field ") and field_t is not None:
            t, eta, _ = x[6:].split(" ", 2)
            if float(t[2:]) == field_t:
                out["field"] = [float(y) for y in eta[4:].split(",")]
        elif x.startswith("# end"):
            out["complete"] = True
    return out


def run_lines(p, substeps=1):
    return (x for _, x in wl_swm.trace(p, substeps) if isinstance(x, str))


def hourly(t_end):
    return [3600.0 * k for k in range(1, int(t_end // 3600) + 1)]


def products(valid, nature, questions, t_end):
    """Ensemble products of {member: parsed run} against the parsed nature run."""
    ms, nat = sorted(valid), nature["obs"]
    n, hrs, out = len(ms), hourly(t_end), {}
    k = len(nat[0.0])
    rmse, spread = [], []
    for lead in e11.LEADS:
        t = lead * 3600.0
        cols = [[valid[m]["obs"][t][j] for m in ms] for j in range(k)]
        rmse.append(math.sqrt(sum((sum(c) / n - nat[t][j]) ** 2 for j, c in enumerate(cols)) / k))
        spread.append(math.sqrt(sum(statistics.variance(c) for c in cols) / k))
    out["skill"] = {"lead_h": list(e11.LEADS), "rmse": rmse, "spread": spread}
    hist = [0] * (n + 1)
    for t in hrs:
        for j in range(k):
            hist[sum(valid[m]["obs"][t][j] < nat[t][j] for m in ms)] += 1
    out["rank_histogram"] = hist
    st = questions["stations"]
    q = questions["q1"]
    j = st.index(q["station"])
    win = [t for t in nature["obs"] if q["from_h"] * 3600 <= t <= q["to_h"] * 3600]
    out["q1"] = sum(any(valid[m]["obs"][t][j] > q["threshold"] for t in win) for m in ms) / n
    j = st.index(questions["q2"]["station"])
    times = sorted(nat)
    out["q2"] = statistics.median(
        max(times, key=lambda t: (valid[m]["obs"][t][j], -t)) / 3600 for m in ms)
    err = {m: member_rmse(valid[m], nature, t_end) for m in ms}
    out["q3"] = min(ms, key=lambda m: (err[m], m))
    out["member_rmse"] = err
    q = questions["q4"]
    out["q4"] = sum(sum(x > q["threshold"] for x in valid[m]["field"]) for m in ms) / n
    return out


def member_rmse(run, nature, t_end):
    sq = [(a - b) ** 2 for t in hourly(t_end) for a, b in zip(run["obs"][t], nature["obs"][t])]
    return math.sqrt(sum(sq) / len(sq))


def truth(seed, **kw):
    """{"members": {m: report}, "roles", "products", "n_valid"} (slow: integrates all)."""
    pl = e11.plan(seed, **kw)
    qs = dict(pl["questions"], stations=pl["nature"]["stations"])
    lead = qs["q4"]["lead_h"] * 3600
    t_end = pl["nature"]["t_end"]
    members, valid = {}, {}
    for m, p in pl["members"].items():
        first = parse(run_lines(p), lead, stop_at_onset=True)
        if first["onset_t"] is not None:
            members[str(m)] = {"status": "unstable", "onset_h": first["onset_t"] / 3600,
                               "drift": None}
            valid[m] = parse(run_lines(p, 2), lead)
            assert valid[m]["onset_t"] is None and valid[m]["drift_max"] < 1e-10
        elif first["drift_max"] > 1e-9:
            members[str(m)] = {"status": "nonconservative", "onset_h": None,
                               "drift": first["drift_last"]}
        else:
            members[str(m)] = {"status": "ok", "onset_h": None, "drift": None}
            valid[m] = first
    nature = parse(run_lines(pl["nature"]), lead)
    return {"members": members, "roles": {str(m): r for m, r in pl["roles"].items()},
            "products": products(valid, nature, qs, t_end), "n_valid": len(valid),
            "t_end": t_end}


def _close(got, want, full, half):
    """1, .5 or 0 by relative error (absolute below 1e-6)."""
    g = _num(got)
    if g is None:
        return 0.0
    err = abs(g - want) / max(abs(want), 1e-6)
    return 1.0 if err <= full else .5 if err <= half else 0.0


def _within(got, want, full, half):
    g = _num(got)
    return 0.0 if g is None else 1.0 if abs(g - want) <= full else .5 if abs(g - want) <= half else 0.0


def member_score(g, want):
    g = g if isinstance(g, dict) else {}
    st = str(g.get("status", "")).strip().lower()
    if want["status"] == "ok":
        return float(st == "ok")
    x = .7 * (st == want["status"])
    if want["status"] == "unstable":
        o = _num(g.get("onset_h"))
        return x + .3 * (o is not None and abs(o - want["onset_h"]) <= .01)
    return x + .3 * _close(g.get("drift"), want["drift"], .05, .05)


def operations(ws, tr, meta):
    """Per member: a valid run complete by the issue time; for unstable members
    half for stopping the configured-step run in time, half for the rerun.
    Every unneeded launch costs .02 of this part."""
    issue = meta.get("phase1_start", 0.0) + e11.ISSUE_S
    by = {}
    for r in pst_build.launches(ws):
        by.setdefault(str(r.get("member")), []).append(r)
    total, extra, timely = 0.0, 0, 0
    for m, want in tr["members"].items():
        rs = by.get(m, [])
        k1 = [r for r in rs if r.get("substeps") == 1]
        k2 = [r for r in rs if (r.get("substeps") or 1) >= 2]
        if want["status"] != "unstable":
            total += any(r["done"] and r["t_last"] <= issue for r in k1)
            extra += max(0, len(k1) - 1) + len(k2)
            continue
        hit = [r for r in k1 if "onset" in r]
        ok = bool(hit) and all(not r["done"] and r["t_last"] - r["onset"] <= e11.REACT_S
                               for r in hit)
        timely += ok
        total += .5 * ok + .5 * any(r["done"] and r["t_last"] <= issue for r in k2)
        extra += max(0, len(k1) - 1) + max(0, len(k2) - 1)
    score = max(0.0, total / len(tr["members"]) - .02 * extra)
    return score, {"unneeded_launches": extra, "stopped_in_time": timely}


def grade(ws, tr, meta=None):
    """members .25, operations .20, skill .20, rank histogram .10, questions .25."""
    meta = meta or {}
    ans = pst_build.read_json(os.path.join(ws, e11.ANSWER))
    a = ans if isinstance(ans, dict) else {}
    got_m = a.get("members") if isinstance(a.get("members"), dict) else {}
    ms = {}
    for m, want in tr["members"].items():
        g = got_m.get(m)
        if not isinstance(g, dict):
            g = pst_build.read_json(os.path.join(ws, f"answers/member_{m}.json"))
        ms[m] = round(member_score(g, want), 3)
    ops, proc = operations(ws, tr, meta)
    want = tr["products"]
    sk = a.get("skill") if isinstance(a.get("skill"), dict) else {}
    cells = []
    for key in ("rmse", "spread"):
        g = sk.get(key) if isinstance(sk.get(key), list) else []
        cells += [_close(g[i] if i < len(g) else None, w, .01, .05)
                  for i, w in enumerate(want["skill"][key])]
    skill = sum(cells) / len(cells)
    h = a.get("rank_histogram")
    h = [(_num(x, int) or 0) for x in h] if isinstance(h, list) else []
    wh = want["rank_histogram"]
    size = max(len(h), len(wh))
    l1 = sum(abs((h[i] if i < len(h) else 0) - (wh[i] if i < len(wh) else 0))
             for i in range(size))
    rank = max(0.0, 1 - l1 / sum(wh)) if h else 0.0
    n = tr["n_valid"]
    q = {"q1": _within(a.get("q1"), want["q1"], .5 / n, 2.5 / n),
         "q2": _within(a.get("q2"), want["q2"], .01, 1.0),
         "q4": _close(a.get("q4"), want["q4"], .005, .05)}
    g3 = str(_num(a.get("q3"), int))
    err = want["member_rmse"]
    q["q3"] = 1.0 if g3 == str(want["q3"]) else (
        .5 if g3.isdigit() and int(g3) in err and err[int(g3)] <= 1.02 * err[want["q3"]] else 0.0)
    parts = {"members": sum(ms.values()) / len(ms), "operations": ops, "skill": skill,
             "rank": rank, "questions": sum(q.values()) / 4}
    score = sum(W[k] * v for k, v in parts.items())
    return {"score": round(score, 4), "parts": {k: round(v, 4) for k, v in parts.items()},
            "questions": q, "members": ms, "process": proc, "final_valid": isinstance(ans, dict)}
