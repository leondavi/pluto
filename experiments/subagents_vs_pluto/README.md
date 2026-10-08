# Experiment: Claude Code subagents vs Pluto

This harness compares two ways of coordinating a team of four LLM workers:

- **Native subagents.** One `claude -p` orchestrator drives four subagents (`w1`..`w4`) through the Agent tool.
- **Pluto.** Five independent `claude -p` processes (`orch` and `w1`..`w4`), each with its own Pluto MCP identity.

Every run uses the model and the shared storage CLI. The write-up is in [`docs/research/subagents-vs-pluto-communication.md`](../../docs/research/subagents-vs-pluto-communication.md).

## Files

| File | Role |
|---|---|
| `scenarios.py` | Holds the prompts for the four scenarios (S1 ring relay, S2 ledger contention, S3 stale lease holder / fencing, S4 dynamic fan-out) and the correctness validators. |
| `ledger.py` | The shared storage every agent calls. It logs a timestamp for every operation to `events.jsonl`. `append` deliberately does a racy read-modify-write. `put` rejects a stale `--token`. |
| `conditions.py` | Builds the `claude -p` command lines for both conditions. |
| `pluto_sandbox.py` | Starts a private Pluto server on spare ports 9300/9302 for each run. It never touches the live server on 9200/9202. |
| `metrics.py` | Parses the stream-json transcripts into token, cost and tool counts. |
| `run_experiment.py` | The runner. It appends one row per run to `results.jsonl`. |
| `analyze.py` | Turns `results.jsonl` into `summary.md` and `summary.csv`, with mean ± sd and a 95% CI. |

## Reproduce

```bash
./PlutoServer.sh --build                        # once: builds beams into /tmp/pluto/build
python3 experiments/subagents_vs_pluto/run_experiment.py --scenario all --condition all --reps 5
python3 experiments/subagents_vs_pluto/analyze.py
```

Requirements:
- the `claude` CLI, logged in
- `erl` (OTP 27+)
- the Pluto MCP friend venv at `/tmp/pluto/.venv`, which `PlutoMCPFriend.sh` creates (override the path with `PLUTO_FRIEND_PYTHON`)

To pin the server and adapter to a given build (as in the write-up, where v0.5.0 and v0.5.1 are compared), set `PLUTO_EBIN` to that build's `_build/default/lib/pluto/ebin` and `PLUTO_FRIEND_SCRIPT` to its `src_py/agent_mcp_friend/pluto_mcp_friend.py`. Each result row records the `pluto_version` the sandbox's `/health` reported.

`trace.py RUN_DIR/<agent>.jsonl` prints one agent's timeline of tool calls and results.

Settings:
- Change the sandbox ports with `EXP_PLUTO_TCP_PORT` and `EXP_PLUTO_HTTP_PORT`.
- Each process has a $3 budget cap (`--budget`).
- Each run has a 600 s cap (`--timeout`).

Output goes to `docs/research/data/subagents-vs-pluto/` by default, with one directory per run holding its transcripts and `events.jsonl`.

The offline tests need no server and no LLM: `pytest tests/test_experiment_subagents_vs_pluto.py`.
