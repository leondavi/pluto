#!/usr/bin/env python3
"""Run the SmartStreams vs native (with and without sub-agents) experiments (see README.md)."""

import argparse
import hashlib
import importlib.util
import json
import os
import random
import secrets
import shutil
import signal
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import ss_build  # noqa: E402
import ss_chaos  # noqa: E402
import ss_classify  # noqa: E402
import ss_metrics  # noqa: E402
import ss_procs  # noqa: E402
import ss_prompts  # noqa: E402

REPO = os.path.abspath(os.path.join(HERE, *[".."] * 4))
ADAPTER = os.path.join(REPO, "src_py", "agent_smart_streams", "smart_streams.py")
PROTOCOL_VERSION = 3
MIN_PEAK = 8
CONDITIONS = ("smartstreams", "native", "native_sub")      # the arms
VARIANTS = ("ss_mcp", "ss_cli")                            # only to choose the SS design
SS_MODES = ss_prompts.SS_MODES
SUBAGENT_TOOLS = ["Agent", "Task"]
SUBAGENT_DEPTH, SUBAGENT_PARALLEL = 1, 4
BUILTIN = ["Bash", "BashOutput", "KillShell", "TaskOutput", "TaskStop", "Monitor", "Read",
           "Grep", "Glob", "Write", "Edit", "TodoWrite", "ToolSearch"]
JOB_TOOLS = ["start", "window", "grep", "control", "list", "transfer", "wait", "analyze",
             "watch", "more"]
DENIED = ["Agent", "Task", "WebFetch", "WebSearch", "Skill", "Workflow", "SendMessage",
          "ScheduleWakeup", "CronCreate", "RemoteTrigger", "EnterWorktree", "Artifact",
          "PushNotification"]
# Nothing that identifies or can reach an enclosing Claude Code session may leak
# into the agents, the Pluto server or its jobs; only authentication is kept.
SESSION_PREFIXES = ("CLAUDECODE", "CLAUDE_CODE_", "CLAUDE_AGENT_", "CLAUDE_EFFORT", "CLAUDE_PID",
                    "CLAUDE_PROJECT_DIR")
KEEP_ENV = {"CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
            "CLAUDE_CODE_USE_FOUNDRY", "CLAUDE_CODE_SKIP_BEDROCK_AUTH",
            "CLAUDE_CODE_SKIP_VERTEX_AUTH", "CLAUDE_CODE_SKIP_FOUNDRY_AUTH",
            "CLAUDE_CODE_API_KEY_HELPER_TTL_MS", "CLAUDE_CODE_CLIENT_CERT",
            "CLAUDE_CODE_CLIENT_KEY"}


def allowed_tools(condition):
    if SS_MODES.get(condition, "none") != "none":
        return BUILTIN + [f"mcp__smartstreams__job_{t}" for t in JOB_TOOLS]
    return BUILTIN + (SUBAGENT_TOOLS if condition == "native_sub" else [])


def denied_tools(condition):
    return [t for t in DENIED if condition != "native_sub" or t not in SUBAGENT_TOOLS]


def mcp_config(condition, agent_id, args, port=None, home=None):
    if condition not in SS_MODES:
        return {"mcpServers": {}}
    srv = {"command": args.mcp_python,
           "args": [args.adapter, "--agent-id", agent_id, "--host", args.pluto_host,
                    "--http-port", str(port or args.http_port), "--framework", "claude", "--tools",
                    "none" if SS_MODES[condition] == "none" else "core"]}
    home = home or args.smartstreams_home
    if home:
        srv["env"] = {"PLUTO_SMARTSTREAMS_HOME": home}
    return {"mcpServers": {"smartstreams": srv}}


def claude_command(condition, cfg_path, args):
    """The prompt goes over stdin; it never appears in a process list."""
    return [args.claude, "-p", "--model", args.model, "--output-format", "stream-json",
            "--verbose", "--mcp-config", cfg_path, "--strict-mcp-config",
            "--permission-mode", "dontAsk", "--max-budget-usd", str(args.budget_usd),
            "--setting-sources", "project", "--disable-slash-commands",
            "--no-session-persistence",
            "--allowedTools", ",".join(allowed_tools(condition)),
            "--disallowedTools", ",".join(denied_tools(condition))] + (
                ["--forward-subagent-text"] if condition == "native_sub" else [])


def session_vars(env):
    return [k for k in env if k.startswith(SESSION_PREFIXES) and k not in KEEP_ENV]


def agent_env(condition="native", model="sonnet"):
    """Sub-agents run the parent's model, cannot nest, and stay in the workspace."""
    drop = set(session_vars(os.environ)) | {"PLUTO_SMARTSTREAMS_HOME"}
    env = {k: v for k, v in os.environ.items() if k not in drop}
    env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] = "1"
    if condition == "native_sub":
        env.update(CLAUDE_CODE_SUBAGENT_MODEL=model, CLAUDE_CODE_SUBAGENT_MODEL_FORCE="1",
                   CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH=str(SUBAGENT_DEPTH),
                   CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS=str(SUBAGENT_PARALLEL))
    return env


def ss_env(env, condition, agent_id, host, port, home):
    """pss for the variants that have it."""
    if SS_MODES.get(condition) not in ("both", "none"):
        return env
    return dict(env, PSS_AGENT=agent_id, PSS_HOST=host, PSS_PORT=str(port),
                PATH=os.path.join(REPO, "bin") + os.pathsep + env.get("PATH", ""),
                **({"PLUTO_SMARTSTREAMS_HOME": home} if home else {}))


def private_pluto(base):
    """A Pluto server for one run: own ports, own home (tests/smart_streams_server.py)."""
    spec = importlib.util.spec_from_file_location(
        "smart_streams_server", os.path.join(REPO, "tests", "smart_streams_server.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.Server(base, max_running=96, max_per_agent=32)


def stop_pluto(server):
    """cleanup() needs a live API; make sure the server dies even without one."""
    try:
        server.cleanup()
    except Exception as e:  # noqa: BLE001 - the run's result must survive this
        print(f"server cleanup: {e}", file=sys.stderr)
        if server.proc and server.proc.poll() is None:
            server.kill()
    finally:
        os.environ.pop("PLUTO_SMARTSTREAMS_HOME", None)


def run_seed(master, exp, rep):
    """Same workload for all conditions of one (exp, rep) block."""
    h = hashlib.sha256(f"{master}|{exp}|{rep}".encode()).digest()
    return int.from_bytes(h[:6], "big")


def schedule(exps, conditions, reps, master, rep_start=1):
    """[(exp, rep, [conditions in order])]: shuffled blocks, shuffled order inside."""
    out = []
    for rep in range(rep_start, rep_start + reps):
        rng = random.Random(f"order:{master}:{rep}")
        block = list(exps)
        rng.shuffle(block)
        for e in block:
            c = list(conditions)
            rng.shuffle(c)
            out.append((e, rep, c))
    return out


def preflight(args, conditions):
    if shutil.which(args.claude) is None:
        sys.exit(f"claude CLI not found: {args.claude}")
    probe = args.out
    if subprocess.run(["git", "-C", probe, "rev-parse", "--show-toplevel"],
                      capture_output=True).returncode == 0:
        sys.exit(f"--out must be outside any git repository: {args.out}")
    if args.http_port and "4" in args.exps and set(SS_MODES) & set(conditions):
        sys.exit("experiment 4 kills its server and needs private ones: drop --http-port")
    if set(SS_MODES) & set(conditions):
        if not os.path.isfile(args.adapter):
            sys.exit(f"adapter not found: {args.adapter}")
        if not args.http_port:
            ebin = os.path.join(REPO, "src_erl", "_build", "default", "lib", "pluto", "ebin")
            if not os.path.isfile(os.path.join(ebin, "pluto_job_mgr.beam")):
                sys.exit("build the server first: (cd src_erl && rebar3 compile)")
            if shutil.which("erl") is None:
                sys.exit("erl not found")
            return
        url = f"http://{args.pluto_host}:{args.http_port}/ping"
        try:
            urllib.request.urlopen(url, timeout=5).read()
        except OSError as e:
            sys.exit(f"Pluto server not reachable at {url}: {e}")


def run_one(exp, condition, rep, seed, args):
    mod = ss_prompts.EXPS[exp]
    run_id = secrets.token_hex(5)                  # unpredictable and condition-free
    run_dir = os.path.join(args.out, "runs", run_id)
    ws, logs = os.path.join(run_dir, "ws"), os.path.join(run_dir, "logs")
    os.makedirs(ws)
    os.makedirs(logs)
    mod.build(ws, seed)
    before = ss_build.tree_digest(ws)
    timeout = args.timeout or mod.TIMEOUT
    cutoff = getattr(mod, "PHASE1_CUTOFF", None)
    roster = ss_prompts.roster(exp)
    ids = {n: f"ss-{run_id}-{n}" for n, _ in roster}
    server = port = home = chaos = None
    tracker, t0 = ss_procs.Tracker(), time.time()
    status, meta, killed, env = {}, {}, [], agent_env(condition, args.model)
    sessions = [n for n, _ in roster]

    def launch(name, session, limit=None, note=""):
        cfg = os.path.join(logs, f"{session}.mcp.json")
        with open(cfg, "w") as f:
            json.dump(mcp_config(condition, ids[name], args, port, home), f)
        prompt = ss_prompts.agent_prompt(exp, condition, name, ws, ids, timeout, cutoff) + note
        return ss_procs.Agent(session, claude_command(condition, cfg, args), prompt, ws, logs,
                              ss_env(env, condition, ids[name], args.pluto_host,
                                     port or args.http_port, home), tracker, limit)

    def respawn(name, n):
        """Replacement of a crashed agent: same prompt and id, what is left of its time."""
        limit = max(60.0, t0 + timeout - time.time())
        sessions.append(f"{name}.r{n}")
        return launch(name, sessions[-1], limit, mod.REPLACEMENT.format(me=name)
                      + f" About {int(limit)} s of the time limit remain.\n")
    try:
        if condition in SS_MODES and not args.http_port:
            server = private_pluto(os.path.join(run_dir, "pluto"))
            server.start()
            port, home = server.port, server.home
        t0 = meta["phase1_start"] = time.time()         # server start is not agent time
        tracker.start()
        for phase in sorted({p for _, p in roster}):
            agents = []
            for name, p in roster:
                if p != phase:
                    continue
                agents.append(launch(name, name))
            if hasattr(mod, "chaos"):
                chaos = ss_chaos.Chaos(mod.chaos(seed), t0, respawn, mod.RESPAWN_S, server,
                                       mod.SERVER_DOWN_S)
            short = exp == "3" and phase == 1
            status.update(ss_procs.run_phase(agents, cutoff if short else timeout, short, chaos))
            meta[f"phase{phase}_end"] = time.time()
    finally:
        meta["run_end"] = time.time()
        if chaos:
            chaos.finish()
            meta["crashes"] = chaos.crashes()
        tracker.sample()
        if tracker.is_alive():
            tracker.stop()
        if server:
            stop_pluto(server)
        killed = ss_procs.cleanup(tracker, ws, t0)
    per, fails, leaks, cats = {}, {}, [], {}
    for name in sessions:
        ev = ss_metrics.read_jsonl(os.path.join(logs, f"{name}.jsonl"))
        per[name] = ss_metrics.parse_agent(ev)
        fails[name] = ss_metrics.failures(per[name], status.get(name, {}), condition)
        leaks += [dict(zip(("code", "tool", "excerpt"), h), agent=name)
                  for h in ss_metrics.find_leaks(ev, ws, args.out, denied_tools(condition))]
        cats[name] = ss_classify.by_category(ev, per[name])
    g = mod.grade(ws, mod.truth(seed), meta)
    tampered = ss_build.tree_digest(ws) != before
    team = ss_metrics.merge(per)
    hard = [f for fs in fails.values() for f in fs
            if f not in ("cutoff", "crash_injected")]
    recs = ss_build.launches(ws)
    peaks = ss_build.peak_streams(recs, [n for n, _ in roster], meta, mod.owner,
                                  getattr(mod, "owner2", None))
    cpu = tracker.cpu or [0.0]
    row = {"protocol_version": PROTOCOL_VERSION, "exp": exp, "name": mod.NAME,
           "condition": condition, "rep": rep, "run_id": run_id,
           "model": args.model, "seed": seed, "started_at": t0,
           "score": g["score"], "wall_s": round(meta["run_end"] - t0, 1),
           "grade": g, "team": team, "per_agent": per, "status": status,
           "protocol_failures": fails, "n_protocol_failures": len(hard) + int(not g["final_valid"]),
           "leaks": leaks, "tampered": tampered, "tainted": bool(leaks) or tampered,
           "leftovers_killed": killed, "categories": ss_classify.merge(cats),
           "chaos": chaos.log if chaos else [], "sessions": sessions,
           "timed_out": {n: any(bool(st.get("timed_out")) for s, st in status.items()
                                if s.split(".")[0] == n) for n, _ in roster},
           "peak_streams": peaks, "low_parallelism": min(peaks.values()) < MIN_PEAK,
           "peak_streams_total": ss_build.peak([(r["t0"], r["t_last"]) for r in recs]),
           "cpu_share_mean": round(sum(cpu) / len(cpu), 3), "cpu_share_max": max(cpu),
           "max_pacing_lag_s": max((r["lag"] for r in recs), default=0.0),
           "program_cpu_s": round(sum(r["cpu"] for r in recs), 1),
           "program_seconds": round(sum(r["t_last"] - r["t0"] for r in recs), 1),
           "program_launches": len(recs)}
    with open(os.path.join(run_dir, "result.json"), "w") as f:
        json.dump(row, f, indent=1)
    return row, run_dir


def run_block(exp, rep, conds, master, args):
    """One block, one run per arm. A finished run's directory is unreadable while
    the others run, and rows (with the seed) are written only afterwards."""
    rows, locked = [], []
    try:
        for c in conds:
            row, d = run_one(exp, c, rep, run_seed(master, exp, rep), args)
            rows.append(row)
            os.chmod(d, 0)
            locked.append(d)
    finally:
        for d in locked:
            os.chmod(d, 0o700)
        with open(os.path.join(args.out, "results.jsonl"), "a") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
                print(f"[exp{exp} rep{rep} {r['condition']}] score={r['score']:.3f} "
                      f"wall={r['wall_s']}s cost=${r['team']['cost_usd']:.2f} "
                      f"result_bytes={r['team']['tool_result_bytes']} "
                      f"failures={r['n_protocol_failures']} tainted={r['tainted']}", flush=True)
    return rows


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--exp", default="all", help="all or a comma list of 1,2,3,4,5")
    p.add_argument("--condition", "--conditions", default="all",
                   help="all or a comma list of " + ", ".join(CONDITIONS))
    p.add_argument("--reps", type=int, default=1)
    p.add_argument("--rep-start", type=int, default=1)
    p.add_argument("--model", default="sonnet")
    p.add_argument("--budget-usd", type=float, default=3.0, help="per agent")
    p.add_argument("--timeout", type=int, default=0, help="per agent, s (0 = experiment default)")
    p.add_argument("--out", required=True, help="directory outside any git repository")
    p.add_argument("--pluto-host", default="127.0.0.1")
    p.add_argument("--http-port", type=int, default=0,
                   help="use a running server; 0 = private server per run (default)")
    p.add_argument("--smartstreams-home", default="",
                   help="only with --http-port: that server's PLUTO_SMARTSTREAMS_HOME")
    p.add_argument("--mcp-python", default=os.environ.get("PLUTO_FRIEND_PYTHON", sys.executable))
    p.add_argument("--adapter", default=ADAPTER)
    p.add_argument("--claude", default="claude")
    p.add_argument("--seed", default=os.environ.get("SS_EXP_SEED"),
                   help="master seed; default random (a seed in argv is visible to ps)")
    p.add_argument("--dry-run", action="store_true", help="print commands and prompts only")
    a = p.parse_args(argv)
    a.out = os.path.realpath(a.out)
    a.exps = list(ss_prompts.EXPS) if a.exp == "all" else a.exp.split(",")
    if set(a.exps) - set(ss_prompts.EXPS):
        p.error("--exp takes 1 to 5")
    a.conditions = list(CONDITIONS) if a.condition == "all" else a.condition.split(",")
    if set(a.conditions) - set(CONDITIONS + VARIANTS) or len(set(a.conditions)) < len(a.conditions):
        p.error("--condition takes " + ", ".join(CONDITIONS))
    return a


def dry_run(plan, args):
    for exp, rep, conds in plan:
        print(f"== exp{exp} rep{rep}: order {' -> '.join(conds)}")
    for exp in args.exps:
        for c in args.conditions:
            name, _ = ss_prompts.roster(exp)[0]
            ids = {n: f"ss-RUN-{n}" for n, _ in ss_prompts.roster(exp)}
            mod = ss_prompts.EXPS[exp]
            print(f"\n--- exp{exp} {c} agent {name}")
            print(" ".join(claude_command(c, f"<logs>/{name}.mcp.json", args)))
            print(json.dumps(mcp_config(c, ids[name], args)))
            print(ss_prompts.agent_prompt(exp, c, name, "<WS>", ids, args.timeout or mod.TIMEOUT))


def main(argv=None):
    args = parse_args(argv)
    master = args.seed or secrets.token_hex(8)
    plan = schedule(args.exps, args.conditions, args.reps, master, args.rep_start)
    if args.dry_run:
        return dry_run(plan, args)
    for k in session_vars(os.environ):                   # the Pluto server inherits our env
        del os.environ[k]
    os.makedirs(os.path.join(args.out, "runs"), exist_ok=True)
    preflight(args, args.conditions)

    def on_term(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, on_term)
    for exp, rep, conds in plan:
        run_block(exp, rep, conds, master, args)


if __name__ == "__main__":
    main()
