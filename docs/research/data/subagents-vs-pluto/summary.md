| Scenario | Condition | n | Success | Wall (s) | Task span (s) | Tokens (k) | Output tok | Cost (USD) | Coord. calls |
|---|---|---|---|---|---|---|---|---|---|
| s1_ring | subagents | 5 | 5/5 | 104.4 ± 27.0 | 80.2 ± 19.8 | 616 ± 166 | 6518 ± 602 | 0.296 ± 0.058 | 9.0 ± 0.0 |
| s1_ring | pluto | 5 | 4/5 | 78.6 ± 3.2 | 46.3 ± 4.4 | 1010 ± 38 | 6163 ± 127 | 0.374 ± 0.038 | 36.8 ± 1.1 |
| s2_contention | subagents | 5 | 5/5 | 91.0 ± 23.4 | 36.2 ± 27.5 | 397 ± 113 | 10511 ± 2437 | 0.276 ± 0.053 | 4.0 ± 0.0 |
| s2_contention | pluto | 5 | 5/5 | 237.3 ± 92.6 | 130.1 ± 20.6 | 2802 ± 348 | 11004 ± 715 | 0.817 ± 0.090 | 96.8 ± 13.0 |
| s3_fencing | subagents | 5 | 5/5 | 236.4 ± 41.1 | 53.5 ± 10.2 | 572 ± 101 | 19967 ± 5660 | 0.472 ± 0.101 | 4.0 ± 0.0 |
| s3_fencing | pluto | 5 | 5/5 | 134.2 ± 35.0 | 50.8 ± 5.4 | 1172 ± 173 | 8956 ± 2253 | 0.429 ± 0.080 | 43.2 ± 6.5 |
| s4_fanout | subagents | 5 | 5/5 | 105.3 ± 30.6 | 50.9 ± 33.2 | 572 ± 523 | 9402 ± 4967 | 0.321 ± 0.232 | 6.4 ± 5.4 |
| s4_fanout | pluto | 5 | 5/5 | 89.0 ± 41.5 | 48.3 ± 6.5 | 1493 ± 254 | 9422 ± 1458 | 0.516 ± 0.078 | 63.4 ± 3.6 |

| Scenario | Condition | Mean hop (s) | Lost updates | Stale write exercised | Stale write rejected | Corrupted | Duplicates | Makespan (s) |
|---|---|---|---|---|---|---|---|---|
| s1_ring | subagents | 10.0 ± 2.5 | – | – | – | – | – | – |
| s1_ring | pluto | 6.0 ± 0.7 | – | – | – | – | – | – |
| s2_contention | subagents | – | 0.0 ± 0.0 | – | – | – | – | – |
| s2_contention | pluto | – | 0.0 ± 0.0 | – | – | – | – | – |
| s3_fencing | subagents | – | – | 5/5 | 5/5 | 0/5 | – | – |
| s3_fencing | pluto | – | – | 5/5 | 5/5 | 0/5 | – | – |
| s4_fanout | subagents | – | – | – | – | – | 0.0 ± 0.0 | 50.9 ± 33.2 |
| s4_fanout | pluto | – | – | – | – | – | 0.0 ± 0.0 | 48.3 ± 6.5 |
