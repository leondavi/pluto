"""Workload 9: one sequencing run of one sample on a targeted SNP panel.

The program builds the sample's two haplotypes on the panel reference, draws
library molecules (optionally mixed with another person's DNA, PCR duplicates,
short inserts with adapter read-through, junk), sequences them with a quality
model (optionally on a degrading flowcell), aligns every read with a seeded,
ungapped local aligner and prints SAM records. Every read is a pure function
of the image and its index, so a rerun prints the same lines.
"""

import argparse
import random

from wl_common import *  # noqa: F401,F403  (inlined by the builder)

BASES = "ACGT"
COMP = str.maketrans("ACGT", "TGCA")
LENGTH, N_SITES, DUP_LEN, PSV_RATE = 48000, 480, 1500, 0.02
READ, K, SEEDS = 100, 12, (0, 22, 44, 66, 88)
MATCH, MISS, CLIP, MIN_SCORE = 1, -4, 5, 30
ADAPTER = "AGATCGGAAGAGCACACGTCTGAACTCCAGTCACATCTCGTATGCCGTCTTCTGCTTG"
JUNK, SHORT = 0.02, 0.10
DUP_RATE = {1: 0.08, 2: 0.05}
LOWQ, LQ_ERR, HQ_ERR, ARTIFACT = 0.03, 0.25, 0.001, 0.25
MESSAGES = ["tile {t} refocused", "fluidics: bubble in lane {l} flushed",
            "warning: chiller at {c} C, within limits", "phasing {p}%, prephasing 0.0{l}%",
            "error: transient USB reset, run continues", "lane {l} cluster density {d} K/mm2",
            "image analysis caught up ({t})", "warning: tile {t} has low intensity, kept",
            "reagent cartridge temperature nominal", "flowcell clamp re-seated"]


def panel(seed, length=LENGTH, n_sites=N_SITES):
    """(reference, [(pos0, ref, alt, panel_af)], (dup source, dup copy, dup length))."""
    rng = random.Random(f"panel:{seed}")
    seq = rng.choices(BASES, (.29, .21, .21, .29), k=length)
    a = rng.randrange(length // 8, length // 3)
    b = rng.randrange(length // 2, length - DUP_LEN - length // 8)
    seq[b:b + DUP_LEN] = seq[a:a + DUP_LEN]
    psv = set()
    for k in range(DUP_LEN):
        if rng.random() < PSV_RATE:
            seq[b + k] = rng.choice([x for x in BASES if x != seq[b + k]])
            psv.update((a + k, b + k))
    step, sites = length // n_sites, []
    for t in range(n_sites):
        pos = t * step + rng.randrange(8, step - 8)
        while pos in psv:
            pos += 1
        alt = rng.choice([x for x in BASES if x != seq[pos]])
        sites.append((pos, seq[pos], alt, round(rng.uniform(.2, .8), 3)))
    return "".join(seq), sites, (a, b, DUP_LEN)


def haplotype(ref, sites, alleles):
    s = list(ref)
    for (pos, _, alt, _), x in zip(sites, alleles):
        if x == "1":
            s[pos] = alt
    return "".join(s)


def _other(b, rnd):
    return BASES[(BASES.index(b) + 1 + int(rnd() * 3)) % 4] if b in BASES else "A"


class Run:
    """Library ``lib`` of one sample sequenced on flowcell ``fc``."""

    def __init__(self, p, lib=1, fc=1):
        self.ref, self.sites, _ = panel(p["panel"])
        self.lib, self.fc, self.total = lib, fc, p["reads"]
        self.haps = [haplotype(self.ref, self.sites, h) for h in p["hap"]]
        self.c = p["cont"] if lib == 1 else 0.0
        self.chaps = [haplotype(self.ref, self.sites, h) for h in p["chap"]] if self.c else None
        self.onset = p["onset"] if (lib, fc) == (1, 1) else 0
        self.fseed = p["seed"] * 4 + lib                 # molecules of the library
        self.sseed = p["seed"] * 16 + 4 * lib + fc + 7    # the sequencing of them
        self.index = {}
        for k in range(len(self.ref) - K + 1):
            self.index.setdefault(self.ref[k:k + K], []).append(k)
        self.seen = set()
        tag = random.Random(f"fc:{p['seed']}:{lib}:{fc}")
        self.fcid = "".join(tag.choice("ABCDEFGHJKLMNPRSTVWXY23456789") for _ in range(5)) + "DSX3"

    def fragment(self, i):
        """(source sequence, start, length, reverse, junk) of molecule i."""
        r = random.Random(self.fseed * 1000003 + i)
        if r.random() < JUNK:
            return "".join(r.choices(BASES, k=READ)), 0, READ, r.random() < .5, True
        src = self.chaps if self.c and r.random() < self.c else self.haps
        hap = src[r.random() < .5]
        ln = READ if r.random() >= SHORT else r.randrange(50, READ)
        return hap, r.randrange(len(hap) - ln + 1), ln, r.random() < .5, False

    def read(self, i):
        """(bases on the reference strand, qualities, reverse, junk, rng) of read i."""
        rr = random.Random(self.sseed * 1000003 + i)
        j = i
        if i > 1 and rr.random() < DUP_RATE[self.lib]:
            j = max(1, i - 1 - rr.randrange(400))
        hap, start, ln, rev, junk = self.fragment(j)
        body = hap[start:start + ln]
        if ln < READ:
            ad = ADAPTER[:READ - ln]
            body = ad.translate(COMP)[::-1] + body if rev else body + ad
        out, quals, rnd = list(body), [0] * READ, rr.random
        degr = self.onset and i >= self.onset
        a_from, a_to = ("G", "T") if rev else ("C", "A")
        for k in range(READ):
            x = rnd()
            if x < LOWQ:
                quals[k] = 2 + int(x * (14 / LOWQ))
                if rnd() < LQ_ERR:
                    out[k] = _other(out[k], rnd)
            else:
                quals[k] = 25 + int((x - LOWQ) * (16 / (1 - LOWQ)))
                if degr and out[k] == a_from and rnd() < ARTIFACT:
                    out[k] = a_to
                elif rnd() < HQ_ERR:
                    out[k] = _other(out[k], rnd)
        return "".join(out), quals, rev, junk, rr

    def align(self, s, rr):
        """None or (score, diagonal, first aligned, end aligned, mapq)."""
        ref, n, diags = self.ref, len(self.ref), set()
        for off in SEEDS:
            for p in self.index.get(s[off:off + K], ()):
                diags.add(p - off)
        cands = []
        for d in sorted(diags):
            pre = [0]                                    # prefix scores
            for k in range(READ):
                q = d + k
                pre.append(pre[-1] + (MATCH if 0 <= q < n and ref[q] == s[k] else MISS))
            best, blo, bhi = -READ, 0, READ
            lo_val, lo_at = 0, 0                         # min of pre[lo] + clip penalty
            for hi in range(1, READ + 1):
                v = pre[hi] - (CLIP if hi < READ else 0) - lo_val
                if v > best:
                    best, blo, bhi = v, lo_at, hi
                if pre[hi] + CLIP < lo_val:
                    lo_val, lo_at = pre[hi] + CLIP, hi
            cands.append((best, d, blo, bhi))
        if not cands or max(cands)[0] < MIN_SCORE:
            return None
        top = max(cands)[0]
        ties = [c for c in cands if c[0] == top]
        pick = ties[int(rr.random() * len(ties))]
        second = max([c[0] for c in cands if c is not pick] + [0])
        mapq = 0 if second >= top else min(60, 6 * (top - second))
        return pick + (mapq,)

    def record(self, i):
        s, quals, rev, junk, rr = self.read(i)
        lane, tile = 1 + int(rr.random() * 4), 1101 + int(rr.random() * 24) * 10 + int(rr.random() * 9)
        name = f"{self.fcid}:{lane}:{tile}:{i}"
        hit = self.align(s, rr)
        if hit is None:
            return f"{name}\t4\t*\t0\t0\t*\t*\t0\t0\t*\t*"
        _, d, lo, hi, mapq = hit
        flag = 16 * rev
        key = (d, rev)
        if key in self.seen:
            flag |= 1024
        self.seen.add(key)
        cig = (f"{lo}S" if lo else "") + f"{hi - lo}M" + (f"{READ - hi}S" if hi < READ else "")
        mm = [f"{d + k + 1}:{s[k]}:{quals[k]}" for k in range(lo, hi) if self.ref[d + k] != s[k]]
        return (f"{name}\t{flag}\tpanel\t{d + lo + 1}\t{mapq}\t{cig}\t*\t0\t0\t*\t*"
                f"\tNM:i:{len(mm)}\tXM:Z:{','.join(mm) or '-'}")


def trace(p, lib=1, fc=1, start=1, led=None):
    """Yield (fraction, line) of one run, from read ``start`` on."""
    run = Run(p, lib, fc)
    total, s = run.total, p["sample"]
    n = total - start + 1
    rng = random.Random(f"msg:{p['seed']}:{lib}:{fc}")
    notes = {rng.randrange(start, total + 1): rng.choice(MESSAGES) for _ in range(rng.randint(4, 9))}
    yield 0.0, "@HD\tVN:1.6\tSO:unknown"
    yield 0.0, f"@SQ\tSN:panel\tLN:{len(run.ref)}"
    yield 0.0, (f"@RG\tID:{run.fcid}.{lib}\tSM:S{s:02d}\tLB:S{s:02d}-L{lib}\tPL:ILLUMINA"
                f"\tPU:{run.fcid}")
    yield 0.0, (f"@PG\tID:seqrun\tPN:seqrun\tVN:2.3\tCL:seqrun --sample {s} --library {lib}"
                f" --flowcell {fc}" + (f" --from {start}" if fc == 2 else ""))
    for i in range(start, total + 1):
        frac = (i - start) / n
        if led is not None:
            led.rec["reads"] = i
            if i == run.onset:
                led.mark(onset_t=time.time())
        if i in notes:
            yield frac, "# " + notes[i].format(t=1101 + rng.randrange(24) * 10, l=rng.randint(1, 4),
                                               c=round(rng.uniform(19.5, 20.5), 1),
                                               p=round(rng.uniform(.08, .2), 3),
                                               d=rng.randint(180, 290))
        if (i - start) % 1200 == 0 and i > start:
            yield frac, f"# progress {i - 1}/{total} reads, {rng.uniform(88, 95):.1f}% pass filter"
        yield frac, run.record(i)
    yield 1.0, f"# run complete: reads {start}-{total}"


def main():
    ap = argparse.ArgumentParser(description="Sequence and align one library of one sample.")
    ap.add_argument("--sample", type=int, required=True)
    ap.add_argument("--library", type=int, default=1, choices=(1, 2))
    ap.add_argument("--flowcell", type=int, default=1, choices=(1, 2))
    ap.add_argument("--from", dest="start", type=int, default=0)
    a = ap.parse_args()
    try:
        p = load_image(f"sample{a.sample}.img")
    except OSError:
        ap.error(f"no sample {a.sample}")
    if a.flowcell == 2 and (a.library != 1 or not 1 <= a.start <= p["reads"]):
        ap.error(f"--flowcell 2 needs library 1 and --from K with 1 <= K <= {p['reads']}")
    if a.flowcell == 1 and a.start:
        ap.error("--from is only for --flowcell 2")
    start = a.start or 1
    try:
        os.nice(5)
    except OSError:
        pass
    led = Ledger(f"s{a.sample}", sample=a.sample, lib=a.library, fc=a.flowcell, start=start,
                 total=p["reads"], reads=start - 1)
    dur = p["duration2"] if a.library == 2 else p["duration"] * (p["reads"] - start + 1) / p["reads"]
    run_paced(trace(p, a.library, a.flowcell, start, led), dur, led)
    led.beat(done=True)


if __name__ == "__main__":
    main()
