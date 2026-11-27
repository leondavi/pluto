"""Texts of experiment 7: the documentation in the workspace and the task prompt."""

ZETA = """# Scanning the critical line

`bin/zeta --job J [--from N] [--safe]` (J = 1..48) scans one window of the critical
line in about 300 s and prints about 1 MB. A new launch of a job prints the same
scan. A complete scan ends with `END` and exit code 0.

## Mathematics

Z(t) (Hardy's function) is real, and its real zeros are the zeros 1/2 + it of the zeta
function. The programs use the Riemann-Siegel formula. With a = sqrt(t / 2pi),
N = floor(a), p = a - N:

    Z(t) = 2 sum_{n=1..N} n^(-1/2) cos(theta(t) - t ln n) + (-1)^(N-1) a^(-1/2) C0(p) + small
    C0(p) = cos(2pi (p^2 - p - 1/16)) / cos(2pi p)
    theta(t) = (t/2) ln(t / 2pi) - t/2 - pi/8 + 1/(48 t) + ...

C0 is smooth; at p = 1/4 and 3/4 take the limit. Here |small| < 0.0005 (healthy programs
include more terms, so compare Z with a tolerance).

* Gram point g_n: theta(g_n) = n pi. It is *good* if (-1)^n Z(g_n) > 0, else *bad* (a failure of
  Gram's law). g_n and g_{n+1} bound Gram interval n.
* Gram block: [g_a, g_b) with g_a and g_b good and all Gram points between them bad. Its length is b - a
  (often 1). **Rosser's rule**, proved true for everything in this campaign: a
  Gram block of length b - a contains exactly b - a zeros.
* A zero shows as a sign change between two samples. Two zeros between neighbouring
  samples do not show. Counting by blocks (Turing's method) finds that: a block
  with fewer sign changes than its length has missed zeros (and their number is even).
* S(g_n) = N(g_n) - n - 1, where N(T) is the number of zeros with ordinate in (0, T]. S = 0 at
  good Gram points. For zeros gamma < gamma' that follow each other, the normalised
  spacing is delta = (gamma' - gamma) ln(gamma / 2pi) / 2pi.
* Zero spacings resemble eigenvalue spacings of random Hermitian matrices (GUE); the
  Wigner surmise has the distribution function
  F(s) = erf(2s / sqrt(pi)) - (4s / pi) exp(-4 s^2 / pi).

## Windows and bands

There are eight bands. A band is one unbroken range of Gram indices, cut into six
windows (part 1..6 in increasing height). Job J scans part (J-1) // 8 + 1 of band (J-1) % 8 + 1. Cuts fall
anywhere, also inside Gram blocks, and the end of one window is the start of the next. The first
line gives the window (`n=A..B`: Gram intervals A..B-1) and its `grid` K: samples lie at t = g_n + (j/K)(g_{n+1} - g_n).

## Output

| Line | Meaning |
|---|---|
| `G n=.. t=.. Z=..` | sample at Gram point g_n |
| `S t=.. Z=..` | sample inside the current Gram interval (K-1 per interval) |
| `R t=..` | zero between the last two samples, refined |
| `X t=.. Z=..` | extremum of Z between the last two `R` zeros |
| `B n=.. it=.. lo=.. hi=..` | one step of a bisection that is shown in detail; the scan waits meanwhile |
| `Y`, `Q` | samples and zeros of a requested refinement |
| `#` | remarks. Counts in them are the program's own, not verified |

## While a program runs

**Refinement.** If a block is short of zeros, append the line `refine A B F` to
`control/job_J.cmd` (A, B: the Gram indices of the block's ends; F = 2..64). The running
program prints samples of [g_A, g_B] on an F times finer grid
(`Y`) and the zeros they show (`Q`). It can do so only within 75 s after it printed `G n=B`; then it has dropped
its tables. If zeros are still missing, ask again with a larger F. A launch
accepts 60 requests and at most 16 Gram intervals in each; requests for blocks that are not short count against you.
Only blocks whose two ends lie in the window matter. Requests concern the launch that runs when they are written.

**Evaluator trouble.** In some jobs the evaluator goes wrong at some Gram index n = onset and stays wrong:

* `NOISE`: the phases t ln n of the main sum are rounded too coarsely
* `TRUNC`: the last terms of the main sum are dropped
* `REMAINDER`: the remainder (C0 and beyond) is left out
* `THETA`: a constant has been added to theta(t) in Z; the Gram points stay right
* `STUCK`: a bisection stops converging: its bracket no longer shrinks and never will. (Healthy
  bisections end after about 40 steps.) onset is the Gram index it shows.

Nothing announces it. From onset on, all values and zeros are useless (missing zeros there need no
request). Stop the program and launch the job again with `--safe --from N`, N a good Gram index just
before onset or equal to it, within 120 s of the onset's `G` line (for STUCK: of the first step).
The safe evaluator is always right. It is refused for indices that no
earlier launch has printed, must not be started for jobs without trouble, and one launch that repeats
more than 150 healthy Gram intervals counts half. The new launch prints the rest of the window on the
same grid; blocks in it can be refined as before. A job has trouble at most once. Launch every job
once without options; any further launch that is not such a repair counts against you, as does
stopping a healthy program.
"""

TASK = """The Riemann hypothesis is to be verified in eight bands of the critical line by 48 scanning programs, some of which go wrong. Read docs/ZETA.md first: it states the mathematics, the output format, and what must be done while the programs run.

Run `bin/zeta --job J` for J = 1..48, all twelve of yours at the same time; prepare before you start them, because requests and restarts have deadlines (75 s and 120 s) from the first minute on. The programs run about five minutes. Keep up with the output of each: request refinement of every Gram block that is short of zeros, and replace every evaluator that went wrong. Evaluating Z(t) yourself at some of the printed points is a way to notice that; Python 3 is available.
Ownership: w1 = jobs 1-12; w2 = 13-24; w3 = 25-36; w4 = 37-48. Output is long, so never print a whole stream.

Report per job, for its complete window A..B (Gram intervals A..B-1) after all repairs: {"zeros": number of zeros in [g_A, g_B), "missed": how many of them a healthy scan on the job's grid does not show as sign changes, "gram_failures": number of bad Gram points n with A <= n < B, "config": "OK|NOISE|TRUNC|REMAINDER|THETA|STUCK", "onset": int|null}. Each agent writes answers/job_J.json for each of its jobs.
Each band is spread over several agents, so exchange lists of zeros and Gram points through files. Per band, over its whole range (windows joined): {"closest": the three pairs of neighbouring zeros with the smallest normalised spacing, as [gamma, delta] with gamma the lower zero (to 0.001), "variance": population variance of delta, "longest_block": [a, length] of a longest Gram block inside the band, "S_plus": int, "S_minus": int (number of Gram points g_n of the band, both ends included, with S(g_n) > 0 and with S(g_n) < 0)}.
Campaign: {"ks": Kolmogorov-Smirnov distance between all deltas of all bands together (only pairs inside one band) and F, "closest": [gamma, delta] of the closest pair of all, "border": [gamma, delta] of the closest pair of neighbouring zeros that lie in different windows of one band}.
w1 combines everything, waiting for the others, into answers/final.json: {"jobs": {"1": {...}, ..., "48": {...}}, "bands": {"1": {...}, ..., "8": {...}}, "campaign": {...}}"""
