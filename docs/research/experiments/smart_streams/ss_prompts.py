"""Prompt assembly. The core is identical in both conditions; only TOOLS differs."""

import exp1_shards
import exp2_sssp
import exp3_relay

EXPS = {"1": exp1_shards, "2": exp2_sssp, "3": exp3_relay}

CORE = """You are agent {me} in a team of four equal agents: {team}. {phase_note}Every agent works in the same directory: {ws}
Agents coordinate only through files there: use board/ for notes (one file per author, so nobody overwrites another) and answers/ for results. Write a JSON answer to a temporary name and rename it when complete.

{task}

Rules:
- The programs in bin/ are opaque black boxes. Run them; do not read, decompile, modify or copy them or their image files, and do not read .runs/. Work only inside the workspace and with your own jobs. Your own analysis scripts are welcome (put them in scratch_{me}/).
- There is no human to ask. Your session ends for good when you give your final message, so do not end your turn while work you are responsible for is unfinished.
- You have at most {limit} s of wall time and a fixed token budget: keep program output out of your context.
- A correct answer file is worth more than a perfect one written too late. Write partial answers early.

Tools. {tools}
"""

TOOLS = {
    "native": """Use your normal Claude Code tools. Start long programs with Bash run_in_background or with nohup, with stdout and stderr redirected to a log file in the workspace, and inspect the logs with grep, awk, python, tail, Grep or bounded Read (shell loops, xargs or one Bash call can start, check or search many at once); Monitor and background-task output can tell you when something happens. A process started by run_in_background ends when your session ends; a detached one (nohup ... &, with a small wrapper that saves the exit code to a file) survives it. Do not use any other agent or scheduling tool.""",
    "smartstreams": """Besides your normal Claude Code tools you have the smartstreams MCP tools, which run programs as durable jobs owned by a server: the job and its complete output survive your session, and any agent can read them by job id. Start the long programs with job_start (cwd = the workspace; a command may be a shell pipeline), follow them with job_wait, job_status and job_list, read bounded pieces with job_grep, job_window and job_next_chunk (job_wait and job_grep accept a job_ids list for many jobs at once), stop them with job_control, and hand them over with job_transfer (only the owner may control a job; agent ids: {ids}). Use your ordinary tools for everything else. Do not use any other agent or scheduling tool.""",
}


def roster(exp):
    """[(name, phase)] - always four agents."""
    if exp == "3":
        return [("p1", 1), ("p2", 1), ("q1", 2), ("q2", 2)]
    return [(f"w{i}", 1) for i in range(1, 5)]


def agent_prompt(exp, condition, me, ws, ids, limit, cutoff=None):
    mod = EXPS[exp]
    names = [n for n, _ in roster(exp)]
    note, task = "", mod.TASK
    if exp == "3":
        cutoff = cutoff or exp3_relay.PHASE1_CUTOFF
        shift = (exp3_relay.SHIFT1.format(me=me, soft=int(cutoff * 0.8)) if me[0] == "p"
                 else exp3_relay.SHIFT2)
        task = task.format(cutoff=cutoff, shift=shift)
        note = "The team works in two shifts of two agents. "
        limit = cutoff if me[0] == "p" else limit
    tools = TOOLS[condition]
    if condition == "smartstreams":
        tools = tools.format(ids=", ".join(f"{n}={ids[n]}" for n in names))
    return CORE.format(me=me, team=", ".join(names), phase_note=note, ws=ws, task=task,
                       limit=int(limit), tools=tools)
