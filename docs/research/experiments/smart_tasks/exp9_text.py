"""Experiment 9: the task prompt and the workspace's docs/SEQUENCING.md."""

TASK = """A sequencing core runs a cohort of 48 samples on a targeted SNP panel, and your team is its analysis group. Read docs/SEQUENCING.md first: it explains the program, its output, the reference files, the quality-control rules that apply while the runs are going and the questions to answer.

Run `bin/seqrun --sample S` for S = 1..48, each once and all twelve of yours at the same time. Watch the quality of your runs while they go and act as the rules say (stop, re-sequence), then answer the questions. Runs are long, so never print a whole run.
Ownership: w1 = samples 1-12; w2 = 13-24; w3 = 25-36; w4 = 37-48. The cohort questions need every sample's genotypes: share what the others need on board/.
Each agent writes answers/sample_S.json for each of its samples: {"individual": str|null, "contamination": float, "contaminant": int|null, "degraded_from": int|null, "calls": {"<site id>": "0/0"|"0/1"|"1/1", ...}}.
w1 combines everything, waiting for the others, into answers/final.json: {"samples": {"1": {...}, ..., "48": {...}}, "relatives": [{"samples": [a, b], "relation": "duplicate"|"parent_offspring"|"sibling"}, ...], "allele_frequencies": {"<site id>": float, ...}}"""

DOCS = """# Sequencing campaign

48 samples (S = 1..48) of one plate are sequenced on a targeted panel of 480 SNPs.
Each sample's DNA was made into a sequencing library (library 1); a backup tube of
every sample allows one second library (library 2), made with fresh reagents.

## The program

`bin/seqrun --sample S` sequences library 1 of sample S on flowcell 1 and aligns
every read to `ref/panel.fa` as soon as it is read. It prints SAM text to standard
output for about 300 s: {reads} reads, about 1 MB. A rerun prints the same lines.

* `bin/seqrun --sample S --library 2` sequences library 2 instead (about 200 s, {reads} reads).
* `bin/seqrun --sample S --flowcell 2 --from K` re-sequences reads K..{reads} of library 1
  on a fresh flowcell. The molecules are the same, so read indices keep their meaning; it takes
  time in proportion to the number of reads.

## Output

Lines starting with `@` are the SAM header; lines starting with `#` are instrument
messages. Every other line is one SAM record with tab-separated fields QNAME FLAG RNAME
POS MAPQ CIGAR RNEXT PNEXT TLEN SEQ QUAL and tags. SEQ and QUAL are not stored (`*`).

* QNAME ends with `:index`: the read index, 1..{reads}, in the order of sequencing.
* FLAG bits: 4 unmapped, 16 reverse strand, 1024 duplicate (PCR or optical).
* POS: 1-based reference position of the first aligned base. The aligner is ungapped:
  CIGAR has only S (soft clip, not aligned) and one M block, which covers the reference
  bases POS .. POS+M-1.
* MAPQ: 0 means the read fits another place equally well; the panel contains a segmental
  duplication.
* `NM:i:` number of mismatching aligned bases. `XM:Z:` comma-separated `POS:BASE:QUAL` for
  each of them (reference position, read base on the reference strand, Phred quality), or
  `-`. Every aligned base not listed matches the reference.

## Files

* `ref/panel.fa` the panel reference; `ref/sites.tsv`: id, pos (1-based), ref, alt and
  panel_af, the alt allele frequency in the large population that the cohort comes from.
  The sites lie far apart in the genome (unlinked).
* `manifest.tsv`: sample and individual, the person whose name is on the tube.
* `array/fingerprints.tsv`: an earlier genotyping array of every manifest individual at 96 panel
  sites (alt allele counts 0, 1, 2; about 99.7% of them correct).

## Analysis standard

Use a read only if it is mapped, not a duplicate, and has MAPQ >= 20, and a base only if
its quality is >= 20 (an aligned base that matches the reference counts as a reference
allele). People are diploid: a genotype is 0/0, 0/1 or 1/1 (number of alt alleles).

## Quality control while the runs go

1. **Contamination** (DNA of another person in a library). A library-1 run whose
   contamination is 3% or more must be stopped before it has printed half of its reads
   (read index {half}); then sequence library 2 of that sample and use it instead.
2. **Flowcell degradation.** From some read on, a degrading flowcell miscalls bases at full
   reported quality. The rate of mismatches with quality >= 20 at aligned positions that are
   not panel sites, normally about 0.1% of such bases, then rises far above that. Reads from
   the first degraded read on are unusable. Stop such a run within 60 s after its first
   degraded read was printed, and re-sequence from that read on with `--flowcell 2 --from K`.
3. Do not stop healthy runs and do not start library 2 or flowcell 2 runs without cause:
   each such action counts against the team.

Stopping means terminating the program (any signal). A run that is still going when time is
up does not count as stopped.

## Questions per sample (answers/sample_S.json)

* `individual`: the manifest individual whose DNA the sample contains, judged by genotype,
  or null if it matches nobody on the array. Tubes may have been swapped or mislabelled.
* `contamination`: fraction of library-1 reads that come from another person (0 if none;
  any value below 0.03 counts as 0). Within 0.02 or 20% of the truth is exact, twice that
  half right.
* `contaminant`: if contamination is 3% or more, the sample (1..48) whose DNA it is
  (every contamination of 3% or more in this cohort comes from another sample of the plate);
  otherwise null.
* `degraded_from`: index of the first degraded read of library 1 on flowcell 1, or null.
  Within 150 reads is exact, within 600 half right.
* `calls`: genotypes, from the usable data, of the person whose DNA the sample contains at
  the reportable sites {report}.

## Cohort questions (answers/final.json)

* `relatives`: every pair of samples whose DNA comes from the same person (`duplicate`), from
  parent and child (`parent_offspring`) or from full siblings (`sibling`). All other pairs are
  unrelated (the cohort has no second-degree relatives). Wrong pairs reduce the score; a pair
  with the wrong relation is half right.
* `allele_frequencies`: alt allele frequency at each reportable site among the distinct persons
  sequenced (a person sequenced twice counts once; include samples that match nobody on the
  array). Within 0.011 is exact, within 0.032 half right.
"""


def docs(reads, report):
    return DOCS.format(reads=reads, half=reads // 2, report=", ".join(report))
