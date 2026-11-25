"""Experiment 5: unpredictable output. Plan, truth and grader."""

import os
import random

import ss_build
import wl_probe

NAME = "e5_unpredictable_output"
STREAMS, PER_AGENT = 48, 12
RECORDS, DURATION, TIMEOUT = 16000, 300.0, 1200
TTL_S = 90.0
NEAR, FAR = 8, 60                                  # lines: full and half location credit
N_CAUSES, N_ASKS, N_CLEAN = 6, 28, 6
W_FIND, W_ACK, W_CAUSE = .75, .15, .10
ANSWER = "answers/final.json"
KINDS = sorted(wl_probe.KIND.values()) + ["GAP", "RESTART", "DEGRADED"]


def owner(rec):
    return f"w{(rec['stream'] - 1) // PER_AGENT + 1}"


def plan(seed, records=RECORDS, duration=DURATION):
    rng = random.Random(f"plan5:{seed}")
    lanes = rng.sample(range(1, 25), N_CAUSES)
    clean, ids = set(), list(range(1, STREAMS + 1))
    for g in range(4):                              # the same load for every agent
        clean |= set(rng.sample(ids[g * PER_AGENT:(g + 1) * PER_AGENT], N_CLEAN // 4 + (g < 2)))
    asks = {s for g in range(4) for s in rng.sample(ids[g * 12:g * 12 + 12], N_ASKS // 4)}
    out, engines = {}, list(wl_probe.COLS)

    def code():
        return "".join(rng.choice("BCDFGHJKLMNPQRSTVWXZ") for _ in range(3)) + \
            f"-{rng.randrange(16**4):04x}"
    for s in ids:
        half, writers, extra = records // 2, [], []
        for tag, eng in zip(rng.sample("ABCDEFGH", 2), rng.sample(engines, 2)):
            reinit = int(half * rng.uniform(.3, .6)) if rng.random() < .4 else -1
            todo = []
            for _ in range(0 if s in clean else rng.choice((1, 1, 2, 2))):
                kind = wl_probe.KIND[eng] if rng.random() < .68 else rng.choice(["GAP", "RESTART"])
                at = half * rng.uniform(.06, .88)
                while any(abs(at - t[0]) < half * .02 for t in todo) or abs(at - reinit) < half * .02:
                    at = half * rng.uniform(.06, .88)
                c = rng.randrange(N_CAUSES)
                todo.append([int(at), kind, lanes[c], c])
            writers.append({"tag": tag, "engine": eng, "plan": todo, "reinit": reinit,
                            "drift": int(half * rng.uniform(.35, .65))})
        notes = rng.sample(wl_probe.GOOD_NOTES, rng.randint(6, 10))
        bad = [] if s in clean else rng.sample(wl_probe.BAD_NOTES, rng.choice((0, 0, 1, 1, 2)))
        for text, kind in [(t, None) for t in notes] + [(t, "DEGRADED") for t in bad]:
            extra.append([rng.uniform(.05, .95), "note",
                          text.replace("{n}", str(rng.randrange(2, 900))), kind])
        for t in rng.sample(wl_probe.DECOYS, rng.randint(1, 2)):
            extra.append([rng.uniform(.05, .9), "decoy", t.replace("{c}", code()), None])
        if s in asks:
            c = code()
            extra.append([rng.uniform(.25, .65), "ask",
                          rng.choice(wl_probe.ASKS).replace("{c}", c), c])
        out[s] = {"seed": rng.randrange(2**40), "stream": s, "records": records,
                  "duration": duration * rng.uniform(.96, 1.02), "writers": writers,
                  "extra": extra, "noise": rng.uniform(.12, .3)}
    return out


def truth(seed, **kw):
    """{stream: {"anomalies": [...], "ask": code or None, "lines": n}} (slow: renders all)."""
    out = {}
    for s, p in plan(seed, **kw).items():
        notes = {"anomalies": []}
        n = sum(1 for _ in wl_probe.trace(p, notes))
        out[str(s)] = {"anomalies": notes["anomalies"], "lines": n,
                       "ask": next((e[3] for e in p["extra"] if e[1] == "ask"), None)}
    return out


def build(ws, seed, **kw):
    ss_build.install(ws, "probe", "wl_probe",
                     {f"stream{s}.img": p for s, p in plan(seed, **kw).items()})
    os.makedirs(os.path.join(ws, "control"), exist_ok=True)
    ss_build.write(ws, "docs/PROBE.md", PROBE)


def claims_of(ans):
    """[(line, kind, cause)] of one stream's answer; malformed entries count as claims."""
    xs = ans.get("anomalies") if isinstance(ans, dict) else None
    out = []
    for x in xs if isinstance(xs, list) else []:
        x = x if isinstance(x, dict) else {}
        try:
            line = int(x.get("line"))
        except (TypeError, ValueError):
            line = -10**9
        cause = x.get("cause")
        out.append((line, str(x.get("kind")).strip().upper(),
                    None if cause in (None, "", "null") else str(cause).strip().lower()))
    return out


def match(claims, want):
    """Greedy one-to-one matching, equal kinds first, then nearest line.
    Returns [(claim index, truth index, credit)]: location 0.5 (0.25 if farther
    than NEAR) plus 0.5 for the kind."""
    cand = []
    for i, (line, kind, _) in enumerate(claims):
        for j, w in enumerate(want):
            d = abs(line - w["line"])
            if d <= FAR:
                cand.append((kind != w["kind"], d, i, j))
    used_i, used_j, out = set(), set(), []
    for other, d, i, j in sorted(cand):
        if i not in used_i and j not in used_j:
            used_i.add(i)
            used_j.add(j)
            out.append((i, j, (.5 if d <= NEAR else .25) + .5 * (not other)))
    return out


def grade(ws, tr, meta=None):
    """0.75 micro F1 of anomalies over all streams (partial credit), 0.15 timely
    answers minus wrong codes, 0.10 pairwise F1 of the root-cause groups."""
    ans = ss_build.read_json(os.path.join(ws, ANSWER))
    final = ans.get("streams") if isinstance(ans, dict) else None
    final = final if isinstance(final, dict) else {}
    credit, n_claims, n_true, n_found, by_kind = 0.0, 0, 0, 0, {}
    label = {}
    for s, t in tr.items():
        a = final.get(s)
        if not isinstance(a, dict):
            a = ss_build.read_json(os.path.join(ws, f"answers/stream_{s}.json"))
        claims = claims_of(a)
        n_claims, n_true = n_claims + len(claims), n_true + len(t["anomalies"])
        for w in t["anomalies"]:
            by_kind.setdefault(w["kind"], [0.0, 0])[1] += 1
        for i, j, c in match(claims, t["anomalies"]):
            credit, n_found = credit + c, n_found + 1
            by_kind[t["anomalies"][j]["kind"]][0] += c
            if claims[i][2] is not None:
                label[s, j] = claims[i][2]
    f1 = 2 * credit / (n_claims + n_true) if n_claims + n_true else 0.0

    def pairs(m):
        ks = sorted(m)
        return {(a, b) for i, a in enumerate(ks) for b in ks[i + 1:] if m[a] == m[b]}
    want = pairs({(s, j): w["cause"] for s, t in tr.items()
                  for j, w in enumerate(t["anomalies"]) if w["cause"] is not None})
    got = pairs(label)
    cause = 2 * len(want & got) / (len(want) + len(got)) if got else 0.0
    first, wrong = {}, 0
    for r in ss_build.launches(ws):
        first.setdefault(str(r.get("stream")), r)        # a rerun's answers do not count
    for r in first.values():
        wrong += len(r.get("wrong", []))
    asks = {s: t["ask"] for s, t in tr.items() if t["ask"]}
    hits = sum(first.get(s, {}).get("acks", {}).get(c, 1e9) <= TTL_S for s, c in asks.items())
    react = max(0, hits - wrong) / len(asks)
    parts = {"anomalies": round(f1, 4), "answers": round(react, 4), "causes": round(cause, 4)}
    return {"score": round(W_FIND * f1 + W_ACK * react + W_CAUSE * cause, 4), "parts": parts,
            "process": {"claims": n_claims, "true": n_true, "found": n_found,
                        "precision": round(credit / n_claims, 4) if n_claims else 0.0,
                        "recall": round(credit / n_true, 4), "answers_in_time": hits,
                        "answers_asked": len(asks), "wrong_codes": wrong},
            "by_kind": {k: [round(a, 2), n] for k, (a, n) in sorted(by_kind.items())},
            "final_valid": isinstance(ans, dict)}


PROBE = """# Probe streams

`bin/probe --stream S` (S = 1..48) prints 1 to 2 MB in about 300 s (all output goes
to standard output). A rerun prints the same lines. The output format is NOT
documented: it differs between streams, changes during a stream, and several
programs write into it (records of two writers, log messages, progress displays,
dumps). A line ends at a newline character (\\n) only; a carriage return (\\r, used by progress
displays) does not end a line, so reading a log with universal newlines miscounts.
Line numbers below start at 1.

Each writer (a capital letter) operates one data structure for the whole run and
prints one record per operation, possibly over several lines. A record names, in
some form, the writer, the writer's record counter (plus 1 from record to record), the
lane (1..24) that processed it, the operation and its values. Names, order and
layout of these differ. Which structure a writer operates must be inferred.

## Anomalies

Report every record that breaks a rule of its structure, once (not its echoes):

| Kind | Structure | Rule |
|---|---|---|
| ORDER | min-priority queue | an extraction returns the smallest key that is in the queue |
| COUNT | hash table | after storing a new key the size is 1 more than in the writer's previous record, after removing a present key 1 less, otherwise unchanged (the record says whether the key was present) |
| HEIGHT | AVL tree | height <= 1.44 * log2(nodes + 2) |
| COMPONENTS | union-find | the number of sets falls by exactly 1 if the two were separate and were merged, and otherwise stays |
| MERGE | sorted merge | length of result = left + right - equal elements dropped |
| LEVEL | breadth-first search | within a traversal, levels never go down from one visit to the next |
| ACCOUNTING | LRU cache (running totals) | hits + misses = lookups |
| MEMBERSHIP | Bloom filter | a key that was added is never reported absent (a wrong "present" is legal) |
| GAP | any writer | the record counter skips numbers |
| RESTART | any writer | the record counter starts again at 1 without an announcement (the structure is not emptied) |
| DEGRADED | message lines | a message says that data was lost or dropped, a check was skipped, or a guarantee was given up from now on |

An announced re-initialisation, in the stream's own words, empties the writer's
structure and restarts its counter; that is no anomaly. Structures start empty
(a union-find starts with 400 separate elements). A value that is wrong from some record on is
reported at the first such record only; compare counts with the previous record.
Words like error, warning or failure mean nothing: most such messages are harmless.
Messages about work that was retried, finished, rolled back or repaired are harmless.
Some streams have no anomaly. Give the line of the record (its first line). A location
within 8 lines is exact, within 60 lines half right. Reports of things that are
not anomalies reduce the score.

## Root causes

Six faulty components cause all anomalies except DEGRADED. Each component damages
records on one lane only, the same one in all streams. Give anomalies of one
component the same `cause` label (any text; null for DEGRADED). Labels are
compared across all 48 streams.

## Questions

A stream may ask its operator for an answer, in its own words, and quote a code
(three capital letters, a hyphen, four hex digits), at most once per stream. Then
write the code to `control/stream_S.ack` within 90 s while the first run of the
stream is active. Nothing confirms it. Codes also occur in other contexts (old tickets,
quotations, replays, labels); answering such a code costs as much as a missed question.
"""

TASK = """Forty-eight programs print output whose format nobody documented. Find what is wrong in it. Read docs/PROBE.md first: it states the rules that the data must obey, the anomaly kinds and the questions that streams may ask.

Run `bin/probe --stream S` for S = 1..48, each once and all twelve of yours at the same time. Answer questions in time while the streams run, then (or meanwhile) find every anomaly.
Ownership: w1 = streams 1-12; w2 = 13-24; w3 = 25-36; w4 = 37-48. Streams are long, so never print a whole stream.
Each agent writes answers/stream_S.json for each of its streams: {"anomalies": [{"line": int, "kind": "ORDER|COUNT|HEIGHT|COMPONENTS|MERGE|LEVEL|ACCOUNTING|MEMBERSHIP|GAP|RESTART|DEGRADED", "cause": str|null}, ...]} ([] if none). Use consistent cause labels across agents (board/).
w1 combines everything, waiting for the others, into answers/final.json: {"streams": {"1": {"anomalies": [...]}, ..., "48": {...}}}"""
