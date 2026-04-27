# Pluto Role Library

A team of narrow, single-responsibility roles that collaborate via Pluto
(locks, messages, agent presence). Every role file is a **system prompt**
you can paste into an agent config — or load via
`pluto_agent_friend --role <name>`.

All roles speak the shared protocol in [`../protocol.md`](../protocol.md).

| Role                | File                                      | Owns                                                     |
|---------------------|-------------------------------------------|----------------------------------------------------------|
| Orchestrator        | [orchestrator.md](./orchestrator.md)      | Decomposition, task list, assignments                    |
| Specialist (Code)   | [specialist.md](./specialist.md)          | Implementing assigned code changes                       |
| Reviewer            | [reviewer.md](./reviewer.md)              | Static review, design/ML sanity, `review` verdicts       |
| QA / Tester         | [qa.md](./qa.md)                          | Running tests, `qa_result`                               |
| Data Steward        | [data-steward.md](./data-steward.md)      | Dataset versioning & schemas                             |
| Experiment Runner   | [experiment-runner.md](./experiment-runner.md) | Reproducible runs, metrics, artifacts                |
| Evaluator           | [evaluator.md](./evaluator.md)            | Baseline vs. candidate comparisons                       |
| Deployer            | [deployer.md](./deployer.md)              | Staging → canary → prod promotion with rollback          |
| SSH Bridge          | [ssh-bridge.md](./ssh-bridge.md)          | Safely executing remote commands                         |

## Loading a role into an agent

```bash
./PlutoAgentFriend.sh --agent-id orchestrator-1 \
  --framework copilot --model claude-sonnet-4.5 \
  --role orchestrator
```

`--role <name>` resolves to `library/roles/<name>.md` (or pass a full path).

## Design invariants (do not weaken)

1. **Single responsibility, single toolset.** If a role feels overloaded,
   split it.
2. **Pluto is the only coordination substrate.** No side-channel state.
3. **Ambiguity is a first-class error.** See protocol §6.
4. **Locks before writes.** Always.
5. **Every task has a `verification_hint` another role can run.**
