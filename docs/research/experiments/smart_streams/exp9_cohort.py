"""Experiment 9: sequencing cohort. Plan (simulated population), truth and grader."""

import os
import random

import exp9_text
import ss_build
import wl_seq

NAME = "e9_sequencing_cohort"
SAMPLES, PER_AGENT = 48, 12
READS, DURATION, DURATION2, TIMEOUT = 15000, 300.0, 200.0, 1200
N_FP, N_REPORT, ARRAY_ERR = 96, 16, 0.003
CONT, LOW, N_LOW, THRESH = (0.06, 0.18), (0.002, 0.01), 6, 0.03
ONSET = (0.35, 0.65)
REACT_S, PENALTY = 60.0, 0.5
ONSET_NEAR, ONSET_FAR, AF_NEAR, AF_FAR = 150, 600, 0.011, 0.032
WEIGHTS = {"calls": .25, "identity": .15, "contamination": .15, "degraded": .05,
           "relatives": .15, "frequencies": .10, "reactions": .15}
RELATIONS = ("duplicate", "parent_offspring", "sibling")
ANSWER = "answers/final.json"
TASK = exp9_text.TASK


def owner(rec):
    return f"w{(int(rec['sample']) - 1) // PER_AGENT + 1}"


def site_id(k):
    return f"p{k + 1:04d}"


def _people(rng, afs):
    """Persons (pairs of allele strings), relations among them, sequenced slots."""
    persons, rel = [], {}

    def founder():
        persons.append(["".join("1" if rng.random() < p else "0" for p in afs) for _ in "mp"])
        return len(persons) - 1

    def child(m, f):
        persons.append(["".join(persons[x][rng.random() < .5][k] for k in range(len(afs)))
                        for x in (m, f)])
        return len(persons) - 1
    slots = []                                            # (person, role)
    m, f = founder(), founder()
    c = child(m, f)
    rel[m, c] = rel[f, c] = "parent_offspring"
    slots += [(m, ""), (f, ""), (c, "")]
    for _ in range(2):
        par = founder()
        kid = child(par, founder())
        rel[par, kid] = "parent_offspring"
        slots += [(par, ""), (kid, "")]
    for _ in range(2):
        m, f = founder(), founder()
        a, b = child(m, f), child(m, f)
        rel[a, b] = "sibling"
        slots += [(a, ""), (b, "")]
    x = founder()
    slots += [(x, "twin"), (x, "dup_tube"), (founder(), "foreign")]
    while len(slots) < SAMPLES:
        slots.append((founder(), "free"))
    labels = {"dup_tube": founder(), "foreign": founder()}  # persons the two tubes are labelled with
    return persons, rel, slots, labels


def _dup_sites(pseed):
    """Sites in the segmental duplication that reads can still place: paralogous
    difference on both sides within 50 bp."""
    ref, sites, (a, b, dl) = wl_seq.panel(pseed)
    psv = [k for k in range(dl) if ref[a + k] != ref[b + k]]
    out = []
    for i, (p, *_) in enumerate(sites):
        for base in (a, b):
            if base <= p < base + dl:
                o = p - base
                if sum(o - 50 <= x < o for x in psv) >= 1 and sum(o < x <= o + 50 for x in psv) >= 1:
                    out.append(i)
    return out


def plan(seed, reads=READS, duration=DURATION, duration2=DURATION2):
    rng = random.Random(f"plan9:{seed}")
    pseed = rng.randrange(2**40)
    ref, sites, _ = wl_seq.panel(pseed)
    persons, rel, slots, labels = _people(rng, [s[3] for s in sites])
    while True:
        rng.shuffle(slots)
        role = {i + 1: r for i, (_, r) in enumerate(slots)}
        free = [s for s in role if role[s] == "free"]
        swaps = rng.sample(free, 4)
        ok = [s for s in role if role[s] in ("", "free") and s not in swaps]
        groups = [[s for s in ok if (s - 1) // PER_AGENT == g] for g in range(4)]
        if min(map(len, groups)) >= 5:
            break
    dna = {i + 1: p for i, (p, _) in enumerate(slots)}
    label = {s: labels.get(role[s], dna[s]) for s in dna}  # person the tube is labelled with
    for a, b in (swaps[:2], swaps[2:]):
        dna[a], dna[b] = dna[b], dna[a]
    ids, used = {}, set()
    for p in sorted(set(label.values())):
        while True:
            x = f"CB-{rng.randrange(1000, 10000)}"
            if x not in used:
                used.add(x)
                ids[p] = x
                break
    related = {frozenset(k) for k in rel}
    cont, onset, source = {}, {}, {}
    for g in groups:
        picks = rng.sample(g, 3)
        cont.update({s: round(rng.uniform(*CONT), 4) for s in picks[:2]})
        onset[picks[2]] = int(reads * rng.uniform(*ONSET))
    qc = set(cont) | set(onset)
    for s in sorted(cont):
        pool = [t for t in dna if t not in qc and role[t] in ("", "free") and t not in source.values()
                and frozenset((dna[s], dna[t])) not in related]
        source[s] = rng.choice(pool)
    for s in rng.sample([t for t in dna if t not in qc], N_LOW):
        cont[s] = round(rng.uniform(*LOW), 4)
        source[s] = rng.choice([t for t in dna if t != s and dna[t] != dna[s]
                                and frozenset((dna[s], dna[t])) not in related])
    fp = sorted(rng.sample(range(len(sites)), N_FP))
    dup = _dup_sites(pseed)
    rep = rng.sample(dup, min(2, len(dup)))
    rep = sorted(rep + rng.sample([k for k in range(len(sites)) if k not in rep
                                   and 150 <= sites[k][0] < len(ref) - 150], N_REPORT - len(rep)))
    arr = {}
    for p in sorted(set(label.values())):
        row = []
        for k in fp:
            g = sum(int(h[k]) for h in persons[p])
            row.append(rng.choice([x for x in (0, 1, 2) if x != g]) if rng.random() < ARRAY_ERR else g)
        arr[ids[p]] = row
    images = {}
    for s in dna:
        images[s] = {"panel": pseed, "seed": rng.randrange(2**40), "sample": s, "reads": reads,
                     "duration": duration * rng.uniform(.97, 1.02), "duration2": duration2,
                     "hap": persons[dna[s]], "cont": cont.get(s, 0.0),
                     "chap": persons[dna[source[s]]] if s in source else [],
                     "onset": onset.get(s, 0)}
    return {"sites": sites, "persons": persons, "rel": rel, "dna": dna, "role": role,
            "manifest": {s: ids[label[s]] for s in dna}, "ids": ids, "array": arr, "fp": fp,
            "report": rep, "source": source, "cont": cont, "onset": onset, "images": images,
            "reads": reads, "swaps": swaps}


def truth(seed, **kw):
    pl = plan(seed, **kw)
    dna, persons, ids, rep = pl["dna"], pl["persons"], pl["ids"], pl["report"]

    def gt(p, k):
        return sum(int(h[k]) for h in persons[p])
    samples = {}
    for s, p in dna.items():
        c = pl["cont"].get(s, 0.0)
        samples[str(s)] = {"individual": ids.get(p), "contamination": c,
                           "contaminant": pl["source"][s] if c >= THRESH else None,
                           "degraded_from": pl["onset"].get(s),
                           "calls": {site_id(k): ("0/0", "0/1", "1/1")[gt(p, k)] for k in rep}}
    rel = {frozenset(k): v for k, v in pl["rel"].items()}
    pairs = {}
    for s in dna:
        for t in dna:
            if s < t:
                r = "duplicate" if dna[s] == dna[t] else rel.get(frozenset((dna[s], dna[t])))
                if r:
                    pairs[f"{s}-{t}"] = r
    distinct = sorted(set(dna.values()))
    freq = {site_id(k): sum(gt(p, k) for p in distinct) / (2 * len(distinct)) for k in rep}
    return {"samples": samples, "relatives": pairs, "frequencies": freq, "reads": pl["reads"],
            "contaminated": sorted(s for s, c in pl["cont"].items() if c >= THRESH),
            "degraded": {str(s): k for s, k in pl["onset"].items()}}


def build(ws, seed, **kw):
    pl = plan(seed, **kw)
    ss_build.install(ws, "seqrun", "wl_seq", {f"sample{s}.img": p for s, p in pl["images"].items()})
    ref, sites, _ = wl_seq.panel(pl["images"][1]["panel"])
    fa = "".join(ref[i:i + 60] + "\n" for i in range(0, len(ref), 60))
    ss_build.write(ws, "ref/panel.fa", ">panel targeted SNP panel (captured regions, concatenated)\n" + fa)
    ss_build.write(ws, "ref/sites.tsv", "id\tpos\tref\talt\tpanel_af\n" + "".join(
        f"{site_id(k)}\t{p + 1}\t{r}\t{a}\t{f}\n" for k, (p, r, a, f) in enumerate(sites)))
    ss_build.write(ws, "manifest.tsv", "sample\tindividual\n" + "".join(
        f"{s}\t{pl['manifest'][s]}\n" for s in sorted(pl["dna"])))
    ss_build.write(ws, "array/fingerprints.tsv", "individual\t" + "\t".join(
        site_id(k) for k in pl["fp"]) + "\n" + "".join(
        f"{i}\t" + "\t".join(map(str, row)) + "\n" for i, row in sorted(pl["array"].items())))
    ss_build.write(ws, "docs/SEQUENCING.md", exp9_text.docs(pl["reads"], [site_id(k) for k in pl["report"]]))


# ── grading ──────────────────────────────────────────────────────────────────

def _num(x):
    try:
        x = float(x)
        return x if x == x and abs(x) != float("inf") else None
    except (TypeError, ValueError):
        return None


def _gt(x):
    x = str(x).strip().replace("|", "/")
    return {"0/0": "0/0", "0/1": "0/1", "1/0": "0/1", "1/1": "1/1"}.get(x)


def _sample_answers(ws, ans):
    final = ans.get("samples") if isinstance(ans, dict) else None
    final = final if isinstance(final, dict) else {}
    out = {}
    for s in range(1, SAMPLES + 1):
        a = final.get(str(s))
        if not isinstance(a, dict):
            a = ss_build.read_json(os.path.join(ws, f"answers/sample_{s}.json"))
        out[str(s)] = a if isinstance(a, dict) else {}
    return out


def _sid(x):
    try:
        return int(str(x).strip().lstrip("Ss"))
    except ValueError:
        return None


def score_samples(tr, got):
    """(calls, identity, contamination, degraded) credits in 0..1."""
    calls = ident = 0.0
    c_est = c_src = c_clean = 0.0
    d_credit, d_false = 0.0, 0
    for s, t in tr["samples"].items():
        a = got.get(s, {})
        ac = a.get("calls") if isinstance(a.get("calls"), dict) else {}
        calls += sum(_gt(ac.get(k)) == v for k, v in t["calls"].items()) / len(t["calls"])
        ind = a.get("individual")
        ident += (ind if ind not in ("", "null") else None) == t["individual"]
        est = _num(a.get("contamination"))
        if t["contaminant"] is not None:
            tol = max(0.02, 0.2 * t["contamination"])
            err = abs(est - t["contamination"]) if est is not None else 1.0
            c_est += 1.0 if err <= tol else .5 if err <= 2 * tol else 0.0
            c_src += _sid(a.get("contaminant")) == t["contaminant"]
        else:
            c_clean += (est is None or est < THRESH) and a.get("contaminant") in (None, "", "null")
        k = _num(a.get("degraded_from"))
        if t["degraded_from"] is not None:
            err = abs(k - t["degraded_from"]) if k is not None else 1e9
            d_credit += 1.0 if err <= ONSET_NEAR else .5 if err <= ONSET_FAR else 0.0
        elif k is not None:
            d_false += 1
    n, nc, nd = len(tr["samples"]), len(tr["contaminated"]), len(tr["degraded"])
    cont = .4 * c_est / nc + .4 * c_src / nc + .2 * c_clean / (n - nc)
    return calls / n, ident / n, cont, d_credit / (nd + d_false)


def score_relatives(tr, claims):
    seen = {}
    for x in claims if isinstance(claims, list) else []:
        x = x if isinstance(x, dict) else {}
        ss = x.get("samples")
        ss = [_sid(y) for y in ss] if isinstance(ss, list) and len(ss) == 2 else [None, None]
        if None in ss or ss[0] == ss[1]:
            seen[f"bad{len(seen)}"] = None
            continue
        seen.setdefault("-".join(map(str, sorted(ss))), str(x.get("relation")).strip().lower())
    want = tr["relatives"]
    credit = sum((1.0 if r == want[k] else .5) for k, r in seen.items() if k in want)
    return 2 * credit / (len(want) + len(seen)) if want or seen else 1.0


def score_frequencies(tr, got):
    got = got if isinstance(got, dict) else {}
    out = 0.0
    for k, v in tr["frequencies"].items():
        x = _num(got.get(k))
        err = abs(x - v) if x is not None else 1.0
        out += 1.0 if err <= AF_NEAR else .5 if err <= AF_FAR else 0.0
    return out / len(tr["frequencies"])


def score_reactions(ws, tr, meta):
    """Per QC sample: stopped in time .6 + rerun started .4; minus PENALTY per
    healthy run stopped and per unneeded library-2 or flowcell-2 run."""
    end = (meta or {}).get("run_end", float("inf")) - 2.0
    runs = {}
    for r in ss_build.launches(ws):
        runs.setdefault((str(r.get("sample")), r.get("lib"), r.get("fc")), []).append(r)
    cont, degr, total = set(map(str, tr["contaminated"])), tr["degraded"], tr["reads"]
    good, bad, detail = 0.0, 0, {}
    for s in tr["samples"]:
        first = (runs.get((s, 1, 1)) or [None])[0]
        stopped = bool(first) and not first["done"] and first["t_last"] < end
        lib2, fc2 = bool(runs.get((s, 2, 1))), bool(runs.get((s, 1, 2)))
        if s in cont:
            ok = stopped and first.get("reads", total) < total / 2
            good += .6 * ok + .4 * lib2
            bad += fc2
        elif s in degr:
            t = first.get("onset_t") if first else None
            ok = stopped and t is not None and first["t_last"] - t <= REACT_S
            good += .6 * ok + .4 * fc2
            bad += lib2
        else:
            ok = None
            bad += stopped + lib2 + fc2
        if ok is not None or stopped or lib2 or fc2:
            detail[s] = {"in_time": ok, "stopped": stopped, "lib2": lib2, "fc2": fc2}
    n = len(cont) + len(degr)
    return max(0.0, good - PENALTY * bad) / n, bad, detail


def grade(ws, tr, meta=None):
    ans = ss_build.read_json(os.path.join(ws, ANSWER))
    got = _sample_answers(ws, ans)
    calls, ident, cont, degr = score_samples(tr, got)
    rel = score_relatives(tr, ans.get("relatives") if isinstance(ans, dict) else None)
    freq = score_frequencies(tr, ans.get("allele_frequencies") if isinstance(ans, dict) else None)
    react, bad, detail = score_reactions(ws, tr, meta)
    parts = {"calls": calls, "identity": ident, "contamination": cont, "degraded": degr,
             "relatives": rel, "frequencies": freq, "reactions": react}
    return {"score": round(sum(WEIGHTS[k] * v for k, v in parts.items()), 4),
            "parts": {k: round(v, 4) for k, v in parts.items()},
            "process": {"unneeded_actions": bad, "qc": detail},
            "final_valid": isinstance(ans, dict)}
