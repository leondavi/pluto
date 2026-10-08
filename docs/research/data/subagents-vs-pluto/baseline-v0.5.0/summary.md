| Scenario | Condition | n | Success | Wall (s) | Task span (s) | Tokens (k) | Output tok | Cost (USD) | Coord. calls |
|---|---|---|---|---|---|---|---|---|---|
| s2_contention | pluto | 1 | 1/1 | 314.1 ± 0.0 | 276.1 ± 0.0 | 3071 ± 0 | 14221 ± 0 | 0.926 ± 0.000 | 103.0 ± 0.0 |
| s3_fencing | pluto | 1 | 1/1 | 223.3 ± 0.0 | 155.2 ± 0.0 | 1441 ± 0 | 8943 ± 0 | 0.490 ± 0.000 | 54.0 ± 0.0 |

| Scenario | Condition | Mean hop (s) | Lost updates | Stale write exercised | Stale write rejected | Corrupted | Duplicates | Makespan (s) |
|---|---|---|---|---|---|---|---|---|
| s2_contention | pluto | – | 0.0 ± 0.0 | – | – | – | – | – |
| s3_fencing | pluto | – | – | 1/1 | 1/1 | 0/1 | – | – |
