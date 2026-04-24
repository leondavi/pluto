"""
demo_logistics_multiagent_vs_solo — multi-agent teams vs solo models on
a logistics & network-planning toolkit with embedded NP-hard cores.

Five real setups, all graded against the same canonical pytest suite:

  Setup 1 — Haiku  team   (Planner + 2 Specialists + Reviewer, all Haiku)
  Setup 2 — Sonnet team   (same role structure, Sonnet)
  Setup 3 — Haiku  solo   (one Copilot call with full spec)
  Setup 4 — Sonnet solo   (one Copilot call with full spec)
  Setup 5 — Opus   solo   (one Copilot call with full spec)

Multi-agent teams use real Pluto coordination: registration over real
HTTP, real /agents/send, real /locks/acquire and /locks/release, real
long_poll. All agents are real `copilot -p ... --model <X>` subprocesses;
QA is a real `pytest` subprocess. There are NO mock servers, NO fake
clients, NO placeholder agents.

Output (under /tmp/pluto/demo/logistics_multiagent_vs_solo/):
  haiku_team/    sonnet_team/
  haiku_solo/    sonnet_solo/    opus_solo/
  trace.json
  logistics_multiagent_vs_solo_demo.md

Opt-in: PLUTO_RUN_DEMOS=1  (and `copilot` on PATH).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import unittest
from typing import Any, Callable

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(_REPO, "src_py"))
sys.path.insert(0, os.path.join(_REPO, "tests"))

from pluto_client import PlutoHttpClient  # noqa: E402
from pluto_test_server import (  # noqa: E402
    PlutoTestServer, PLUTO_HOST, PLUTO_HTTP_PORT,
)

ROLES_DIR = os.path.join(_REPO, "library", "roles")
PROTOCOL_PATH = os.path.join(_REPO, "library", "protocol.md")
CANONICAL_TESTS = os.path.join(
    os.path.dirname(__file__), "canonical_test_logistics_toolkit.py"
)

DEMO_NAME = "logistics_multiagent_vs_solo"
DEMO_ROOT = f"/tmp/pluto/demo/{DEMO_NAME}"
HAIKU_TEAM_WS = os.path.join(DEMO_ROOT, "haiku_team")
SONNET_TEAM_WS = os.path.join(DEMO_ROOT, "sonnet_team")
HAIKU_SOLO_WS = os.path.join(DEMO_ROOT, "haiku_solo")
SONNET_SOLO_WS = os.path.join(DEMO_ROOT, "sonnet_solo")
OPUS_SOLO_WS = os.path.join(DEMO_ROOT, "opus_solo")
REPORT_DIR = DEMO_ROOT
TRACE_DIR = DEMO_ROOT

HAIKU_MODEL = os.environ.get("PLUTO_HAIKU_MODEL", "claude-haiku-4.5")
SONNET_MODEL = os.environ.get("PLUTO_SONNET_MODEL", "claude-sonnet-4.6")
OPUS_MODEL = os.environ.get("PLUTO_OPUS_MODEL", "claude-opus-4.7")

RUN_DEMOS = os.environ.get("PLUTO_RUN_DEMOS") == "1"

# Per-run unique suffix for agent ids — prevents collisions with stale
# sessions left on a long-running Pluto server from prior aborted runs
# (root cause of the haiku-team specialists never receiving their
# task_assigned messages in the first run of this demo).
RUN_TAG = f"{os.getpid()}-{int(time.time())}"


def have_copilot() -> bool:
    return shutil.which("copilot") is not None


def _aid(prefix: str, role: str) -> str:
    """Compose a per-run-unique agent id like 'logh-r{TAG}-specialist-1'."""
    return f"{prefix}-r{RUN_TAG}-{role}"


# ── Trace ─────────────────────────────────────────────────────────────────────


class Trace:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.events: list[dict[str, Any]] = []
        self.t0 = time.time()

    def add(self, actor: str, kind: str, **detail: Any) -> None:
        with self._lock:
            self.events.append({
                "t": round(time.time() - self.t0, 3),
                "actor": actor, "kind": kind, **detail,
            })

    def dump(self) -> list[dict]:
        with self._lock:
            return list(self.events)


# ── Pluto-bound role agent ────────────────────────────────────────────────────


class RoleAgent(threading.Thread):
    def __init__(self, agent_id: str, host: str, http_port: int, trace: Trace):
        super().__init__(daemon=True, name=f"agent-{agent_id}")
        self.agent_id = agent_id
        self.trace = trace
        self.client = PlutoHttpClient(
            host=host, http_port=http_port, agent_id=agent_id,
        )
        self._handlers: dict[str, Callable[[dict], None]] = {}
        self._stop = threading.Event()
        self.ready = threading.Event()

    def on(self, msg_type: str, fn: Callable[[dict], None]) -> None:
        self._handlers[msg_type] = fn

    def send(self, to: str, payload: dict) -> None:
        self.trace.add(self.agent_id, "send", to=to,
                       payload_type=payload.get("type"))
        self.client.send(to, payload)

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        resp = self.client.register()
        if resp.get("status") != "ok":
            self.trace.add(self.agent_id, "note",
                           event="register_failed", resp=resp)
            return
        self.trace.add(self.agent_id, "note", event="registered")
        self.ready.set()
        try:
            while not self._stop.is_set():
                try:
                    msgs = self.client.long_poll(timeout=2)
                except Exception as exc:  # noqa: BLE001
                    self.trace.add(self.agent_id, "note",
                                   event="poll_error", err=repr(exc))
                    time.sleep(0.3)
                    continue
                for m in msgs:
                    p = m.get("payload", {}) or {}
                    pt = p.get("type")
                    if not pt:
                        continue
                    self.trace.add(self.agent_id, "recv",
                                   frm=m.get("from"), payload_type=pt)
                    h = self._handlers.get(pt)
                    if h:
                        try:
                            h(m)
                        except Exception as exc:  # noqa: BLE001
                            self.trace.add(self.agent_id, "note",
                                           event="handler_error",
                                           err=repr(exc))
        finally:
            try:
                self.client.unregister()
            except Exception:  # noqa: BLE001
                pass


# ── Real `copilot -p` wrapper ────────────────────────────────────────────────


def run_copilot(prompt: str, *, workdir: str, model: str,
                timeout: int = 1200) -> tuple[int, str, str, float]:
    cmd = ["copilot", "-p", prompt, "--model", model,
           "--allow-all-tools", "--allow-all-paths", "--add-dir", workdir,
           "--no-ask-user"]
    t0 = time.time()
    proc = subprocess.run(cmd, cwd=workdir, capture_output=True,
                          text=True, timeout=timeout)
    return proc.returncode, proc.stdout, proc.stderr, time.time() - t0


def load_role(name: str) -> str:
    with open(os.path.join(ROLES_DIR, f"{name}.md"), encoding="utf-8") as f:
        return f.read()


def load_protocol() -> str:
    with open(PROTOCOL_PATH, encoding="utf-8") as f:
        return f.read()


# ── The shared task spec (used verbatim for solo + team setups) ──────────────


TASK_SPEC = """\
Build a small LOGISTICS & NETWORK-PLANNING toolkit in pure-stdlib Python.
Five required modules under `src/logistics/`. The grader will run a fixed
pytest suite (`tests/test_logistics_toolkit.py`, NOT visible to you in
advance) against the EXACT public surface specified below. You MUST NOT
create or modify anything under `tests/`.

NP-HARDNESS DISCLOSURE — required:
  * Capacitated VRP with Time Windows (CVRPTW) generalises TSP and is
    NP-hard. Your routing solver MUST be a heuristic (no claim of
    polynomial-time exact global optimum).
  * Job-Shop Scheduling (JSSP) is NP-hard. Same disclosure: heuristic
    only.
  * Constrained Shortest Path (resource-constrained) is NP-hard in
    general. Heuristic only — you may ALSO ship an exact baseline for
    the unconstrained variant (Dijkstra), which IS polynomial.
Each module's top-level docstring MUST mention this honestly.

================================================================
(1) src/logistics/routing.py  — CVRPTW
================================================================
Dataclasses (use @dataclass; field order matters):
    @dataclass
    class Location:        name: str; x: float; y: float
    @dataclass
    class Vehicle:         id: str;  capacity: int;   max_time: float
    @dataclass
    class Customer:        id: str;  location: Location;
                           demand: int;
                           time_window: tuple[float, float] = (0.0, float('inf'))
    @dataclass
    class Route:           vehicle_id: str
                           customer_ids: list[str]
    @dataclass
    class Plan:            routes: list[Route]

Functions:
    compute_distance_matrix(locations: list[Location])
        -> dict[tuple[str, str], float]
        # Symmetric Euclidean distance keyed by (name_a, name_b);
        # self-distance == 0.0.
    check_routing_feasibility(plan: Plan, vehicles: list[Vehicle],
                              customers: list[Customer], depot: Location)
        -> bool
        # True iff every route's total demand <= its vehicle capacity AND
        # every route's total travel-time (Euclidean distance treated as
        # time, +0 service time) <= its vehicle.max_time AND every visited
        # customer's time_window can be met along the route.
    routing_cost(plan: Plan, vehicles: list[Vehicle],
                 customers: list[Customer], depot: Location) -> float
        # Sum of route distances, depot -> ... -> depot.
    build_initial_routes(vehicles: list[Vehicle],
                         customers: list[Customer], depot: Location) -> Plan
        # Heuristic (e.g. greedy nearest-neighbor or Clarke-Wright).
        # If a customer cannot be served by any vehicle, omit it.
    improve_routes(plan: Plan, vehicles: list[Vehicle],
                   customers: list[Customer], depot: Location) -> Plan
        # Local-search heuristic (2-opt or relocate). MUST NEVER worsen
        # the cost: routing_cost(improve_routes(p, ...)) <= routing_cost(p, ...).
    routing_objectives(plan: Plan, vehicles: list[Vehicle],
                       customers: list[Customer], depot: Location)
        -> dict[str, float]
        # MULTI-OBJECTIVE breakdown of the same plan. Returns a dict with
        # AT LEAST these float keys:
        #   "distance":         sum of route distances (same as routing_cost)
        #   "co2":              proportional to distance (e.g. 0.20 * distance)
        #   "lateness_penalty": sum over visited customers of
        #                       max(0, arrival_time - time_window_close);
        #                       0.0 if every customer is served within window.
        # These are SCALAR COMPONENTS, not a single weighted sum — callers
        # combine them with their own weights.

================================================================
(2) src/logistics/scheduling.py  — JSSP
================================================================
Dataclasses:
    @dataclass
    class Operation:  job_id: str; machine_id: str
                      duration: float; order_index: int
    @dataclass
    class Job:        id: str; operations: list[Operation]
    @dataclass
    class Machine:    id: str

Schedule representation: a dict mapping (job_id, order_index) ->
(start: float, end: float).

Functions:
    check_schedule_feasibility(schedule, jobs, machines) -> bool
        # True iff:
        #   - end == start + duration for every operation;
        #   - operations of the same job are ordered by order_index
        #     (start[k+1] >= end[k]);
        #   - no two operations on the same machine overlap.
    schedule_makespan(schedule) -> float
        # max end across all operations; 0.0 for the empty schedule.
    build_initial_schedule(jobs, machines) -> Schedule
        # Heuristic dispatch (e.g. earliest-start first or shortest-
        # processing-time). Must produce a feasible schedule.
    improve_schedule(schedule, jobs, machines) -> Schedule
        # Local improvements. MUST keep feasibility AND
        # schedule_makespan(improved) <= schedule_makespan(initial).
    scheduling_objectives(schedule, jobs, machines,
                          shift_end: float = 8.0) -> dict[str, float]
        # MULTI-OBJECTIVE breakdown. Returns a dict with AT LEAST these
        # float keys:
        #   "makespan":  max end across all operations
        #   "energy":    sum over machines of (machine busy time
        #                * energy_rate), where energy_rate=1.0 unless
        #                otherwise specified per machine. A simple sum
        #                of all operation durations is acceptable.
        #   "overtime":  sum over operations of max(0, end - shift_end)
        # Callers combine these with their own weights.

================================================================
(3) src/logistics/graph_paths.py  — shortest path + constrained variant
================================================================
Dataclasses:
    @dataclass
    class Graph:  nodes: set[str]
                  edges: dict[tuple[str, str], dict]
                  # each edge dict has at least 'cost': float and 'risk': float

    @dataclass
    class Path:   nodes: list[str]
                  total_cost: float
                  total_risk: float

Functions:
    shortest_path_unconstrained(graph: Graph, source: str, target: str)
        -> Path | None
        # Exact Dijkstra by 'cost'. None if unreachable.
    check_path_feasibility(path: Path, max_risk: float) -> bool
        # True iff path.total_risk <= max_risk.
    find_constrained_path(graph: Graph, source: str, target: str,
                          max_risk: float) -> Path | None
        # Heuristic: among feasible paths (total_risk <= max_risk), try
        # to minimise cost. NP-hard in general; ANY method that returns
        # a feasible path when one exists, and None when none exists,
        # is acceptable. (For small graphs a label-setting search or
        # bounded BFS is fine.)

================================================================
(4) src/logistics/integration.py  — TRADE-OFF AWARE END-TO-END PLANNER
================================================================
Defines a small built-in scenario (depot + 3 customers + 2 vehicles
+ 2 jobs + a 4-node graph) and a multi-objective planner.

The planner MUST balance CONFLICTING OBJECTIVES — a single scalar
"cost" is INSUFFICIENT. Routing trades distance vs CO2 vs lateness;
scheduling trades makespan vs energy vs overtime. A good plan
acknowledges these trade-offs explicitly instead of collapsing to one
number.

Two named candidate-builders MUST be exported:

    plan_cost_optimized() -> dict
        # Builds a plan that favours minimal operational cost
        # (distance, energy, machine time). May incur lateness or
        # overtime. Returns the same shape as plan_end_to_end().
    plan_service_optimized() -> dict
        # Builds a plan that favours service / reliability (on-time
        # delivery, slack, lower overtime). May cost more distance
        # or energy. Returns the same shape as plan_end_to_end().

And the reconciled top-level entry point:

    plan_end_to_end(weights: dict | None = None) -> dict
        # Reconciles the two candidate plans into a single integrated
        # plan using the supplied weights (or sensible defaults).
        # `weights` may include any of:
        #   "distance", "co2", "lateness_penalty",
        #   "makespan",  "energy", "overtime"
        # Missing weights default to 1.0 for cost-side components and
        # 1.0 for service-side components.
        #
        # Returns a dict with keys:
        #   "routing_plan":  Plan
        #   "schedule":      Schedule
        #   "paths":         dict[str, Path | None]
        #   "metrics":       dict[str, Any]   — see below
        #
        # metrics MUST include:
        #   "routing_cost":      float   (= distance, kept for back-compat)
        #   "makespan":          float
        #   "routing_feasible":  bool
        #   "schedule_feasible": bool
        #   "tradeoff_components": {
        #       "distance":         float,
        #       "co2":              float,
        #       "lateness_penalty": float,
        #       "makespan":         float,
        #       "energy":           float,
        #       "overtime":         float,
        #   }
        #   "alternatives": [
        #       {"name": "cost_optimized",
        #        "objectives": {...same 6 keys as tradeoff_components...},
        #        "rationale": "<one short sentence>"},
        #       {"name": "service_optimized",
        #        "objectives": {...},
        #        "rationale": "<one short sentence>"},
        #   ]
        #   "chosen": "cost_optimized" | "service_optimized" | "reconciled"
        #   "rationale": "<one short sentence describing the trade-off>"
        #
        # The built-in scenario MUST yield routing_feasible=True and
        # schedule_feasible=True for the returned plan.
        #
        # ---- BUILT-IN SCENARIO: BINDING-CONSTRAINT REQUIREMENT ----
        # The scenario data you ship in this module MUST be calibrated so
        # that at least ONE objective component differs between
        # plan_cost_optimized() and plan_service_optimized(). A toy
        # scenario with so much slack that both candidates collapse to
        # the same plan is NOT acceptable; the canonical grader checks
        # that the two candidates' objective vectors differ on at least
        # one component (epsilon 1e-6).
        #
        # Concrete data guidance to guarantee divergence:
        #   * Routing: include at least one customer whose time_window
        #     close is tight enough that a cost-leaning ordering arrives
        #     LATE (lateness_penalty > 0) while a service-leaning
        #     ordering arrives ON TIME (lateness_penalty = 0) but at
        #     extra distance / co2.
        #   * Scheduling: include enough operations and set shift_end
        #     so that the cost-leaning (densely-packed) schedule incurs
        #     overtime > 0 while a service-leaning schedule (with slack
        #     or smarter ordering) reduces overtime, even if makespan
        #     or energy is slightly higher.
        # The goal is a SMALL, REALISTIC scenario where the trade-off is
        # actually visible — not a degenerate one where it is hidden.

================================================================
(5) src/logistics/api.py  — TRADE-OFF AWARE FACADE
================================================================
A thin facade:
    run_demo_scenario(weights: dict | None = None) -> dict
Calls plan_end_to_end(weights) and returns a flat summary dict with keys:
    "routing_cost":     float
    "makespan":         float
    "paths_feasible":   bool
    "violations":       list   (MUST be [] for the built-in scenario)
    "tradeoff_summary": {
        "components": {... same 6-key dict as metrics.tradeoff_components},
        "alternatives": [...same as metrics.alternatives],
        "chosen": "...",
        "rationale": "..."
    }


================================================================
General rules
================================================================
  - Pure Python standard library only. No third-party imports.
  - Do NOT create or modify anything under tests/.
  - Do NOT add __main__ blocks unless requested by an individual file.
  - Make `src/logistics/__init__.py` and `src/__init__.py` empty.
  - Prefer dataclasses where specified; the test suite imports them by
    those exact names.
"""


# ── Per-module specifications used by the team Planner / dispatcher ──────────


def build_team_task_list(workdir: str) -> list[dict]:
    base = os.path.join(workdir, "src", "logistics")
    return [
        {
            "task_id": "t-001",
            "title": "Implement routing.py (CVRPTW core + heuristics)",
            "type": "code",
            "owner": "specialist",
            "files": [f"file:{os.path.join(base, 'routing.py')}"],
            "dependencies": [],
            "definition_of_done": (
                "src/logistics/routing.py contains the dataclasses Location,"
                " Vehicle, Customer, Route, Plan and the functions"
                " compute_distance_matrix, check_routing_feasibility,"
                " routing_cost, build_initial_routes, improve_routes per the"
                " full TASK_SPEC. Heuristic-only; module docstring states"
                " NP-hardness."
            ),
            "verification_hint":
                "python -c 'from src.logistics.routing import "
                "Location, Vehicle, Customer, build_initial_routes; "
                "p=build_initial_routes([Vehicle(\"v1\",10,100.0)], "
                "[Customer(\"c1\",Location(\"c1\",1,0),1)], "
                "Location(\"d\",0,0)); assert p.routes'",
            "full_spec": TASK_SPEC,
        },
        {
            "task_id": "t-002",
            "title": "Implement scheduling.py (JSSP core + heuristics)",
            "type": "code",
            "owner": "specialist",
            "files": [f"file:{os.path.join(base, 'scheduling.py')}"],
            "dependencies": [],   # parallel-eligible with t-001 and t-003
            "definition_of_done": (
                "src/logistics/scheduling.py contains the dataclasses Job,"
                " Operation, Machine and the functions"
                " check_schedule_feasibility, schedule_makespan,"
                " build_initial_schedule, improve_schedule per the full"
                " TASK_SPEC. Heuristic-only; module docstring states"
                " NP-hardness."
            ),
            "verification_hint":
                "python -c 'from src.logistics.scheduling import "
                "Job, Operation, Machine, build_initial_schedule; "
                "j=Job(\"j1\",[Operation(\"j1\",\"m1\",2,0)]); "
                "s=build_initial_schedule([j],[Machine(\"m1\")]); "
                "assert s'",
            "full_spec": TASK_SPEC,
        },
        {
            "task_id": "t-003",
            "title": "Implement graph_paths.py (shortest + constrained path)",
            "type": "code",
            "owner": "specialist",
            "files": [f"file:{os.path.join(base, 'graph_paths.py')}"],
            "dependencies": [],   # parallel-eligible with t-001 and t-002
            "definition_of_done": (
                "src/logistics/graph_paths.py contains the dataclasses Graph"
                " and Path and the functions shortest_path_unconstrained"
                " (exact Dijkstra), check_path_feasibility, and"
                " find_constrained_path (heuristic). Module docstring"
                " states constrained variant is NP-hard."
            ),
            "verification_hint":
                "python -c 'from src.logistics.graph_paths import "
                "Graph, shortest_path_unconstrained; "
                "g=Graph({\"a\",\"b\"},{(\"a\",\"b\"):{\"cost\":1.0,\"risk\":0.0}}); "
                "p=shortest_path_unconstrained(g,\"a\",\"b\"); "
                "assert p is not None and p.total_cost==1.0'",
            "full_spec": TASK_SPEC,
        },
        {
            "task_id": "t-004a",
            "title": "Implement plan_cost_optimized() (CostOptimizer proposal)",
            "type": "code",
            "owner": "cost_optimizer",
            "files": [f"file:{os.path.join(base, 'integration.py')}"],
            "dependencies": ["t-001", "t-002", "t-003"],
            "definition_of_done": (
                "src/logistics/integration.py defines the built-in scenario"
                " (depot + customers + vehicles + jobs + 4-node graph) AND a"
                " function plan_cost_optimized() that returns the same dict"
                " shape required for plan_end_to_end (routing_plan, schedule,"
                " paths, metrics). The CostOptimizer favours minimal"
                " operational cost (distance, energy, machine time); it may"
                " incur lateness or overtime. metrics.tradeoff_components"
                " MUST include all six float keys. The function is a"
                " PROPOSAL ONLY — do NOT yet write plan_end_to_end()."
            ),
            "verification_hint":
                "python -c 'from src.logistics.integration import "
                "plan_cost_optimized; r=plan_cost_optimized(); "
                "assert \"tradeoff_components\" in r[\"metrics\"]'",
            "full_spec": TASK_SPEC,
            "role_hint": "cost_optimizer",
        },
        {
            "task_id": "t-004b",
            "title": "Implement plan_service_optimized() (ServiceReliability proposal)",
            "type": "code",
            "owner": "service_reliability",
            "files": [f"file:{os.path.join(base, 'integration.py')}"],
            "dependencies": ["t-004a"],   # serialised: same file
            "definition_of_done": (
                "src/logistics/integration.py gains a function"
                " plan_service_optimized() that returns the same dict shape"
                " as plan_cost_optimized. The ServiceReliability agent"
                " favours on-time delivery, slack, and lower overtime; it"
                " may cost more distance or energy."
                " metrics.tradeoff_components MUST include all six float"
                " keys. PRESERVE plan_cost_optimized() and the built-in"
                " scenario from t-004a."
            ),
            "verification_hint":
                "python -c 'from src.logistics.integration import "
                "plan_service_optimized, plan_cost_optimized; "
                "assert plan_service_optimized()[\"metrics\"][\"tradeoff_components\"]'",
            "full_spec": TASK_SPEC,
            "role_hint": "service_reliability",
        },
        {
            "task_id": "t-004",
            "title": "Reconcile proposals into plan_end_to_end() (MetaPlanner)",
            "type": "code",
            "owner": "meta_planner",
            "files": [f"file:{os.path.join(base, 'integration.py')}"],
            "dependencies": ["t-004a", "t-004b"],
            "definition_of_done": (
                "src/logistics/integration.py gains plan_end_to_end(weights"
                "=None) that calls plan_cost_optimized() and"
                " plan_service_optimized(), reconciles them into a single"
                " integrated plan using the supplied weights (or sensible"
                " defaults), and returns a dict whose metrics include"
                " tradeoff_components AND alternatives (a list of BOTH"
                " candidate proposals with their full 6-key objective"
                " vectors and one-sentence rationales) AND chosen AND"
                " rationale. routing_feasible and schedule_feasible MUST"
                " be True for the built-in scenario."
            ),
            "verification_hint":
                "python -c 'from src.logistics.integration import "
                "plan_end_to_end; r=plan_end_to_end(); m=r[\"metrics\"]; "
                "assert m[\"routing_feasible\"] is True; "
                "assert len(m[\"alternatives\"])>=2; "
                "assert m[\"chosen\"]'",
            "full_spec": TASK_SPEC,
            "role_hint": "meta_planner",
        },
        {
            "task_id": "t-005",
            "title": "Implement api.py (run_demo_scenario facade)",
            "type": "code",
            "owner": "specialist",
            "files": [f"file:{os.path.join(base, 'api.py')}"],
            "dependencies": ["t-004"],
            "definition_of_done": (
                "src/logistics/api.py exposes run_demo_scenario(weights=None)"
                " returning a flat dict with floats routing_cost, makespan,"
                " bool paths_feasible, list violations (empty for the"
                " built-in scenario), AND a tradeoff_summary dict with keys"
                " components, alternatives, chosen, rationale (mirroring"
                " the metrics fields populated by plan_end_to_end)."
            ),
            "verification_hint":
                "python -c 'from src.logistics.api import run_demo_scenario; "
                "s=run_demo_scenario(); assert s[\"violations\"] in ([],0); "
                "assert \"tradeoff_summary\" in s'",
            "full_spec": TASK_SPEC,
        },
    ]


# ── Planner: real LLM call producing a complexity contract ───────────────────


PLANNER_PROMPT_TMPL = """\
You are the Planner / Complexity Analyst for a multi-agent Pluto team
about to build a logistics toolkit.

Your single job in this turn: write a short COMPLEXITY CONTRACT (plain
markdown, max ~40 lines) that the Specialists and Reviewer will follow.
The contract must explicitly identify which subproblems are NP-hard
(VRP-TW, JSSP, constrained shortest path) and which are polynomial
(Dijkstra unconstrained, feasibility checks). State that all NP-hard
subproblems will be solved heuristically, with no claim of global
optimum.

Write your contract to the file `COMPLEXITY_CONTRACT.md` in the
workspace root. Then respond with exactly one line:
PLAN_DONE <one-sentence summary>

=== ROLE ===
{role}

=== PROTOCOL ===
{protocol}

=== TOOLKIT SPEC (for context) ===
{spec}
"""


def run_planner(workdir: str, role_text: str, protocol_text: str,
                trace: Trace, metrics: dict, model: str,
                actor: str) -> dict:
    prompt = PLANNER_PROMPT_TMPL.format(
        role=role_text, protocol=protocol_text, spec=TASK_SPEC,
    )
    trace.add(actor, "shell", op="copilot_start",
              task_id="planner", model=model)
    rc, out, err, dur = run_copilot(prompt, workdir=workdir,
                                    model=model, timeout=600)
    metrics.setdefault("calls", []).append({
        "actor": actor, "task_id": "planner",
        "model": model, "rc": rc, "duration_s": round(dur, 2),
        "stdout_chars": len(out), "stderr_chars": len(err),
    })
    trace.add(actor, "shell", op="copilot_done",
              task_id="planner", rc=rc, duration_s=round(dur, 2))
    contract_path = os.path.join(workdir, "COMPLEXITY_CONTRACT.md")
    contract = ""
    if os.path.isfile(contract_path):
        try:
            contract = open(contract_path).read()
        except OSError:
            pass
    return {"rc": rc, "duration_s": round(dur, 2),
            "contract_path": contract_path,
            "contract_chars": len(contract)}


# ── Specialist (real LLM call per task) ──────────────────────────────────────


SPECIALIST_PROMPT_TMPL = """\
You are a Pluto Code Specialist on a multi-agent team. Your role and the
shared coordination protocol are inlined below — do NOT attempt to read
them from disk.

=== ROLE: SPECIALIST ===
{role}

=== ROLE FOCUS (specific to this task) ===
{role_focus}

=== PROTOCOL ===
{protocol}

=== COMPLEXITY CONTRACT (from the team Planner) ===
{contract}

=== FULL TOOLKIT SPEC (shared across all sub-tasks) ===
{spec}

=== TASK ASSIGNED TO YOU NOW ===
{task_assigned}

=== HARD CONSTRAINTS ===
- Workspace root: {workdir}
- Touch ONLY the file(s) listed in task.files (path after `file:` is absolute).
- Pure Python standard library only.
- Top-level module docstring MUST mention NP-hardness if the module is
  routing.py, scheduling.py, or graph_paths.py.
- Do NOT invoke pytest yourself; QA will run it.
- After implementing, respond with exactly one line:
  TASK_DONE <one-sentence summary>
"""


ROLE_FOCUS_TEXT = {
    "specialist": (
        "Generic specialist: implement the task to the letter of the spec."
    ),
    "cost_optimizer": (
        "You are the COST OPTIMIZER on this team. Your bias is toward "
        "MINIMAL OPERATIONAL COST: fewer kilometres, lower CO2, lower "
        "machine energy, lower machine time. You may incur some lateness "
        "or overtime if doing so reduces operational cost. Build a "
        "candidate plan that explicitly leans this way and report its "
        "objective vector honestly (do NOT hide lateness/overtime). The "
        "MetaPlanner will reconcile your proposal with the "
        "ServiceReliability proposal — you are NOT the final word."
    ),
    "service_reliability": (
        "You are the SERVICE / RELIABILITY agent on this team. Your bias "
        "is toward ON-TIME DELIVERY, slack against time windows, and low "
        "overtime — even if that means more distance, CO2, or machine "
        "energy. Build a candidate plan that explicitly leans this way "
        "and report its objective vector honestly (do NOT hide the extra "
        "distance / energy). The MetaPlanner will reconcile your proposal "
        "with the CostOptimizer proposal — you are NOT the final word."
    ),
    "meta_planner": (
        "You are the META-PLANNER. The CostOptimizer and "
        "ServiceReliability agents have each produced a candidate plan in "
        "this same file (plan_cost_optimized() and "
        "plan_service_optimized()). Your job is to write "
        "plan_end_to_end(weights=None) that calls BOTH candidates, reads "
        "their full objective vectors, and reconciles them into a single "
        "integrated plan using the supplied weights (or sensible "
        "defaults that balance cost and service). The returned dict's "
        "metrics MUST include alternatives (a list with both candidates' "
        "full objective vectors and rationales), a chosen field naming "
        "the selected option, and a one-sentence rationale describing the "
        "trade-off. Do NOT delete the two candidate functions."
    ),
}


def specialist_handler(agent: RoleAgent, msg: dict, *,
                       orch_id: str,
                       role_text: str, protocol_text: str,
                       contract_text: str,
                       workdir: str, model: str,
                       metrics: dict) -> None:
    payload = msg["payload"]
    task = payload["task"]
    task_id = task["task_id"]

    granted_refs: list[str] = []
    for resource in task.get("files", []):
        resp = agent.client.acquire(resource, mode="write", ttl_ms=600_000)
        agent.trace.add(agent.agent_id, "lock", op="acquire",
                        resource=resource, status=resp.get("status"))
        if resp.get("status") == "ok":
            granted_refs.append(resp["lock_ref"])

    for resource in task.get("files", []):
        path = resource.removeprefix("file:")
        os.makedirs(os.path.dirname(path), exist_ok=True)

    prompt = SPECIALIST_PROMPT_TMPL.format(
        role=role_text, protocol=protocol_text,
        role_focus=ROLE_FOCUS_TEXT.get(
            task.get("role_hint") or task.get("owner") or "specialist",
            ROLE_FOCUS_TEXT["specialist"]),
        contract=contract_text or "(no contract written)",
        spec=task.get("full_spec", ""),
        task_assigned=json.dumps(payload, indent=2, default=str),
        workdir=workdir,
    )
    agent.trace.add(agent.agent_id, "shell", op="copilot_start",
                    task_id=task_id, model=model)
    rc, out, err, dur = run_copilot(prompt, workdir=workdir,
                                    model=model, timeout=900)
    metrics.setdefault("calls", []).append({
        "actor": agent.agent_id, "task_id": task_id,
        "model": model, "rc": rc, "duration_s": round(dur, 2),
        "stdout_chars": len(out), "stderr_chars": len(err),
    })
    agent.trace.add(agent.agent_id, "shell", op="copilot_done",
                    task_id=task_id, rc=rc, duration_s=round(dur, 2))

    for ref in granted_refs:
        try:
            agent.client.release(ref)
            agent.trace.add(agent.agent_id, "lock", op="release",
                            lock_ref=ref)
        except Exception:  # noqa: BLE001
            pass

    files_changed = [r for r in task.get("files", [])
                     if os.path.isfile(r.removeprefix("file:"))]
    status = "done" if rc == 0 and files_changed else "error"
    agent.send(orch_id, {
        "type": "task_result", "task_id": task_id, "status": status,
        "summary": (out.splitlines() or ["<no output>"])[-1][:160],
        "details": {"files_changed": files_changed, "copilot_rc": rc,
                    "duration_s": round(dur, 2)},
        "notes": [],
    })


# ── Reviewer (one real LLM call over all 5 modules) ──────────────────────────


REVIEWER_PROMPT_TMPL = """\
You are a Pluto Reviewer for a multi-agent team. Your role and the
protocol are inlined below — do NOT read them from disk.

=== ROLE: REVIEWER ===
{role}

=== PROTOCOL ===
{protocol}

=== COMPLEXITY CONTRACT ===
{contract}

=== TOOLKIT SPEC ===
{spec}

=== FILES UNDER REVIEW ===
{files_block}

Check that:
  * each NP-hard module (routing, scheduling, graph_paths) acknowledges
    NP-hardness in its docstring and uses heuristics rather than claiming
    polynomial-time exact global optimisation;
  * documented public surface matches the spec (names + signatures);
  * routing.routing_objectives and scheduling.scheduling_objectives expose
    the full multi-objective component breakdowns required by the spec;
  * integration.py exports BOTH plan_cost_optimized() and
    plan_service_optimized() AND a reconciling plan_end_to_end(weights);
    the returned dict must include metrics.tradeoff_components,
    metrics.alternatives (>=2 entries), metrics.chosen and
    metrics.rationale;
  * api.run_demo_scenario surfaces a tradeoff_summary mirroring those
    fields;
  * the reconciliation logic is HONEST — alternatives' objective vectors
    must reflect a real cost-vs-service trade-off, not two identical plans;
  * obvious bugs / missing constraint checks.

Respond with EXACTLY ONE JSON object (no markdown fence):
{{
  "verdict": "approved" | "needs_changes",
  "findings":   ["short bullet ...", ...],
  "suggestions":["short bullet ...", ...]
}}
"""


def run_reviewer(workdir: str, role_text: str, protocol_text: str,
                 contract_text: str, trace: Trace, metrics: dict,
                 model: str, actor: str) -> dict:
    files = [
        os.path.join(workdir, "src", "logistics", m)
        for m in ("routing.py", "scheduling.py", "graph_paths.py",
                  "integration.py", "api.py")
    ]
    # Fail-fast guard: if any required module is missing, do NOT invoke
    # the Reviewer LLM. With --allow-all-tools the Reviewer will happily
    # implement the missing modules itself, masking team failures behind
    # an "approved" verdict (this exact bug occurred in the v1 run of
    # this demo when the haiku-team specialists silently dropped their
    # task_assigned messages).
    missing = [os.path.basename(p) for p in files if not os.path.isfile(p)]
    if missing:
        verdict = {
            "verdict": "needs_changes",
            "findings": [f"missing module: {m}" for m in missing],
            "suggestions": ["the team failed to produce these modules; "
                            "no LLM review was performed"],
            "skipped": True, "missing_modules": missing,
        }
        trace.add(actor, "note", event="review_skipped",
                  reason="missing_modules", missing=missing)
        return verdict
    blocks = []
    for fp in files:
        try:
            content = open(fp).read()
        except OSError:
            content = "<missing>"
        blocks.append(f"--- {fp} ---\n{content}\n")
    prompt = REVIEWER_PROMPT_TMPL.format(
        role=role_text, protocol=protocol_text,
        contract=contract_text or "(no contract written)",
        spec=TASK_SPEC, files_block="\n".join(blocks),
    )
    trace.add(actor, "shell", op="copilot_start",
              task_id="review", model=model)
    rc, out, err, dur = run_copilot(prompt, workdir=workdir,
                                    model=model, timeout=900)
    metrics.setdefault("calls", []).append({
        "actor": actor, "task_id": "review",
        "model": model, "rc": rc, "duration_s": round(dur, 2),
        "stdout_chars": len(out), "stderr_chars": len(err),
    })
    trace.add(actor, "shell", op="copilot_done",
              task_id="review", rc=rc, duration_s=round(dur, 2))
    parsed: dict = {"verdict": "unknown", "raw": out[-500:]}
    try:
        s, e = out.find("{"), out.rfind("}")
        if s != -1 and e > s:
            parsed = json.loads(out[s:e + 1])
    except Exception:  # noqa: BLE001
        pass
    if parsed.get("verdict") not in ("approved", "needs_changes"):
        parsed["verdict"] = parsed.get("verdict", "unknown")
    # Self-diagnosis signal: did the Reviewer flag the trade-off-collapse
    # bug pytest test #17 was designed to catch?
    blob = " ".join(str(x).lower()
                    for x in (parsed.get("findings") or [])
                            + (parsed.get("suggestions") or []))
    keys = ("trade-off", "tradeoff", "collapse", "identical",
            "same plan", "no real trade", "no trade-off",
            "alternatives are identical", "alternatives identical",
            "cost_optimized and service_optimized")
    parsed["tradeoff_bug_flagged"] = any(k in blob for k in keys)
    return parsed


# ── Solo run (one real LLM call with the whole spec) ─────────────────────────


SOLO_PROMPT_TMPL = """\
You are a senior Python engineer working solo on a non-trivial task. No
orchestrator, no reviewer, no other agents — just you. Implement the
entire spec end-to-end in this workspace.

Workspace root: {workdir}

=== TASK ===
{spec}

When you are done, respond with exactly one line:
TASK_DONE <one-sentence summary>
"""


def run_solo(workdir: str, trace: Trace, metrics: dict, *,
             model: str, actor: str) -> dict:
    prompt = SOLO_PROMPT_TMPL.format(workdir=workdir, spec=TASK_SPEC)
    trace.add(actor, "shell", op="copilot_start", model=model)
    rc, out, err, dur = run_copilot(prompt, workdir=workdir,
                                    model=model, timeout=1500)
    metrics.setdefault("calls", []).append({
        "actor": actor, "task_id": "solo",
        "model": model, "rc": rc, "duration_s": round(dur, 2),
        "stdout_chars": len(out), "stderr_chars": len(err),
    })
    trace.add(actor, "shell", op="copilot_done",
              rc=rc, duration_s=round(dur, 2))
    return {"rc": rc, "duration_s": round(dur, 2),
            "summary": (out.splitlines() or ["<no output>"])[-1][:160]}


# ── QA (real pytest, identical for every setup) ──────────────────────────────


def install_canonical_tests(workdir: str) -> None:
    dst = os.path.join(workdir, "tests", "test_logistics_toolkit.py")
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copyfile(CANONICAL_TESTS, dst)


def qa_pytest(workdir: str, trace: Trace, actor: str) -> dict:
    install_canonical_tests(workdir)
    test_path = os.path.join(workdir, "tests", "test_logistics_toolkit.py")
    required = [
        os.path.join(workdir, "src", "logistics", m)
        for m in ("routing.py", "scheduling.py", "graph_paths.py",
                  "integration.py", "api.py")
    ]
    missing = [p for p in required if not os.path.isfile(p)]
    if missing:
        return {"status": "fail", "rc": -1,
                "reason": f"missing modules: {[os.path.basename(p) for p in missing]}",
                "passed": 0, "failed": 0, "duration_s": 0.0,
                "stdout_tail": "", "stderr_tail": ""}
    t0 = time.time()
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", test_path, "-q",
         "--rootdir", workdir, "--tb=short"],
        cwd=workdir, capture_output=True, text=True, timeout=240,
    )
    dur = time.time() - t0
    trace.add(actor, "shell", op="pytest_done",
              rc=proc.returncode, duration_s=round(dur, 2))
    passed = failed = 0
    for line in (proc.stdout + proc.stderr).splitlines():
        if "passed" in line or "failed" in line:
            parts = line.replace(",", " ").split()
            for i, tok in enumerate(parts):
                try:
                    n = int(tok)
                except ValueError:
                    continue
                if i + 1 < len(parts):
                    nxt = parts[i + 1].rstrip(".,")
                    if nxt.startswith("passed"):
                        passed = max(passed, n)
                    elif nxt.startswith("failed"):
                        failed = max(failed, n)
    return {
        "status": "pass" if proc.returncode == 0 else "fail",
        "rc": proc.returncode, "passed": passed, "failed": failed,
        "duration_s": round(dur, 2),
        "stdout_tail": proc.stdout[-2000:],
        "stderr_tail": proc.stderr[-500:],
    }


# ── Team driver (parameterised by prefix + model + workspace) ────────────────


def drive_team(prefix: str, model: str, workspace: str,
               trace: Trace) -> dict:
    """Run a full multi-agent team (Planner + 2 Specialists + Reviewer)."""
    metrics: dict[str, Any] = {"calls": []}
    spec_role = load_role("specialist")
    rev_role = load_role("reviewer")
    orch_role = load_role("orchestrator")
    protocol_text = load_protocol()

    orch_id = _aid(prefix, "orchestrator-1")
    spec1_id = _aid(prefix, "specialist-1")
    spec2_id = _aid(prefix, "specialist-2")
    cost_id = _aid(prefix, "cost-optimizer-1")
    svc_id = _aid(prefix, "service-reliability-1")
    meta_id = _aid(prefix, "meta-planner-1")
    rev_id = _aid(prefix, "reviewer-1")
    qa_id = _aid(prefix, "qa-1")

    # Step 1: Planner LLM call (also a real Pluto-registered actor for
    # event symmetry, but the planner work is a single shot).
    planner = RoleAgent(orch_id, PLUTO_HOST, PLUTO_HTTP_PORT, trace)
    # We only need the orchestrator agent for the broadcast + result loop;
    # the planner call itself runs synchronously here.
    planner.start()
    assert planner.ready.wait(timeout=15), \
        f"{orch_id} did not register"
    planner_result = run_planner(workspace, orch_role, protocol_text,
                                 trace, metrics, model=model,
                                 actor=orch_id)
    contract_text = ""
    if os.path.isfile(planner_result["contract_path"]):
        try:
            contract_text = open(planner_result["contract_path"]).read()
        except OSError:
            pass

    # Step 2: register specialists + role agents + reviewer; run dependency-aware DAG.
    spec1 = RoleAgent(spec1_id, PLUTO_HOST, PLUTO_HTTP_PORT, trace)
    spec2 = RoleAgent(spec2_id, PLUTO_HOST, PLUTO_HTTP_PORT, trace)
    cost_agent = RoleAgent(cost_id, PLUTO_HOST, PLUTO_HTTP_PORT, trace)
    svc_agent = RoleAgent(svc_id, PLUTO_HOST, PLUTO_HTTP_PORT, trace)
    meta_agent = RoleAgent(meta_id, PLUTO_HOST, PLUTO_HTTP_PORT, trace)

    def make_handler(agent: RoleAgent):
        def handler(m: dict) -> None:
            def work():
                specialist_handler(
                    agent, m, orch_id=orch_id,
                    role_text=spec_role, protocol_text=protocol_text,
                    contract_text=contract_text,
                    workdir=workspace, model=model, metrics=metrics,
                )
            threading.Thread(target=work, daemon=True,
                             name=f"work-{agent.agent_id}").start()
        return handler

    for a in (spec1, spec2, cost_agent, svc_agent, meta_agent):
        a.on("task_assigned", make_handler(a))

    states: dict[str, str] = {}
    results: dict[str, dict] = {}
    cv = threading.Condition()

    def on_task_result(m: dict) -> None:
        p = m["payload"]
        results[p["task_id"]] = p
        with cv:
            states[p["task_id"]] = ("completed" if p["status"] == "done"
                                    else "failed")
            cv.notify_all()

    planner.on("task_result", on_task_result)

    for a in (spec1, spec2, cost_agent, svc_agent, meta_agent):
        a.start()
    for a in (spec1, spec2, cost_agent, svc_agent, meta_agent):
        assert a.ready.wait(timeout=15), f"{a.agent_id} did not register"

    tasks = build_team_task_list(workspace)
    for t in tasks:
        states[t["task_id"]] = "pending"

    specialists = [spec1_id, spec2_id]
    rr = {"i": 0}

    def next_spec() -> str:
        s = specialists[rr["i"] % len(specialists)]
        rr["i"] += 1
        return s

    OWNER_TO_ID = {
        "cost_optimizer": cost_id,
        "service_reliability": svc_id,
        "meta_planner": meta_id,
    }

    def target_for(t: dict) -> str:
        owner = t.get("owner") or "specialist"
        if owner in OWNER_TO_ID:
            return OWNER_TO_ID[owner]
        return next_spec()

    impl_t0 = time.time()
    deadline = impl_t0 + 2400  # 40 minutes per team

    # Broadcast task list (observability event).
    planner.client.broadcast({
        "type": "task_list", "version": 1, "updated_by": orch_id,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "tasks": tasks,
    })
    trace.add(orch_id, "note", event="task_list_broadcast",
              count=len(tasks))

    while time.time() < deadline:
        with cv:
            for t in tasks:
                tid = t["task_id"]
                deps_ok = all(states.get(d) == "completed"
                              for d in t.get("dependencies", []))
                if states[tid] == "pending" and deps_ok:
                    states[tid] = "in_progress"
                    target = target_for(t)
                    planner.send(target, {
                        "type": "task_assigned", "task": t,
                        "constraints": ["stdlib-only",
                                        "heuristic for NP-hard subproblems"],
                        "acceptance_criteria": [t["definition_of_done"]],
                        "verification_hints": [t["verification_hint"]],
                    })
            cv.wait(timeout=1.0)
        if all(s in ("completed", "failed") for s in states.values()):
            break
    impl_duration = time.time() - impl_t0

    # Step 3: Reviewer pass (real LLM call over all 5 modules).
    review = run_reviewer(workspace, rev_role, protocol_text,
                          contract_text, trace, metrics,
                          model=model, actor=rev_id)

    for a in (planner, spec1, spec2, cost_agent, svc_agent, meta_agent):
        a.stop()
    for a in (planner, spec1, spec2, cost_agent, svc_agent, meta_agent):
        a.join(timeout=5)

    qa = qa_pytest(workspace, trace, actor=qa_id)

    return {
        "prefix": prefix, "model": model, "workspace": workspace,
        "states": states, "results": results,
        "planner": planner_result, "review": review,
        "qa": qa, "metrics": metrics,
        "impl_duration_s": round(impl_duration, 2),
    }


# ── Solo driver ──────────────────────────────────────────────────────────────


def drive_solo(prefix: str, model: str, workspace: str,
               trace: Trace) -> dict:
    metrics: dict[str, Any] = {"calls": []}
    impl = run_solo(workspace, trace, metrics,
                    model=model, actor=_aid(prefix, "monolith-1"))
    qa = qa_pytest(workspace, trace, actor=_aid(prefix, "qa-1"))
    return {"prefix": prefix, "model": model, "workspace": workspace,
            "impl": impl, "qa": qa, "metrics": metrics}


# ── Workspace setup ──────────────────────────────────────────────────────────


def reset_workspace(root: str) -> None:
    if os.path.exists(root):
        shutil.rmtree(root)
    for sub in ("src/logistics", "tests"):
        os.makedirs(os.path.join(root, sub), exist_ok=True)
    open(os.path.join(root, "src", "__init__.py"), "w").close()
    open(os.path.join(root, "src", "logistics", "__init__.py"), "w").close()
    open(os.path.join(root, "tests", "__init__.py"), "w").close()


# ── Test class ───────────────────────────────────────────────────────────────


@unittest.skipUnless(RUN_DEMOS, "set PLUTO_RUN_DEMOS=1 to run real-server demos")
@unittest.skipUnless(have_copilot(), "copilot CLI not available on PATH")
class LogisticsMultiAgentVsSoloDemo(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = PlutoTestServer()
        cls.server.start()
        os.makedirs(DEMO_ROOT, exist_ok=True)
        for ws in (HAIKU_TEAM_WS, SONNET_TEAM_WS,
                   HAIKU_SOLO_WS, SONNET_SOLO_WS, OPUS_SOLO_WS):
            reset_workspace(ws)

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()

    def test_compare(self):
        trace = Trace()

        # Two team setups.
        haiku_team = drive_team("logh", HAIKU_MODEL, HAIKU_TEAM_WS, trace)
        sonnet_team = drive_team("logs", SONNET_MODEL, SONNET_TEAM_WS, trace)

        # Three solo setups.
        haiku_solo = drive_solo("logh-solo", HAIKU_MODEL,
                                HAIKU_SOLO_WS, trace)
        sonnet_solo = drive_solo("logs-solo", SONNET_MODEL,
                                 SONNET_SOLO_WS, trace)
        opus_solo = drive_solo("logo-solo", OPUS_MODEL,
                               OPUS_SOLO_WS, trace)

        out_json = os.path.join(TRACE_DIR, "trace.json")
        with open(out_json, "w") as f:
            json.dump({
                "trace": trace.dump(),
                "haiku_team": haiku_team,
                "sonnet_team": sonnet_team,
                "haiku_solo": haiku_solo,
                "sonnet_solo": sonnet_solo,
                "opus_solo": opus_solo,
                "pluto_http_port": PLUTO_HTTP_PORT,
                "models": {"haiku": HAIKU_MODEL,
                           "sonnet": SONNET_MODEL,
                           "opus": OPUS_MODEL},
            }, f, indent=2, default=str)

        from tests.demo_logistics_multiagent_vs_solo.write_report import render
        render(out_json, os.path.join(REPORT_DIR, f"{DEMO_NAME}_demo.md"))

        # Demo passes if every setup made >=1 LLM call (infra sanity).
        all_made_calls = (
            len(haiku_team["metrics"]["calls"]) > 0
            and len(sonnet_team["metrics"]["calls"]) > 0
            and len(haiku_solo["metrics"]["calls"]) > 0
            and len(sonnet_solo["metrics"]["calls"]) > 0
            and len(opus_solo["metrics"]["calls"]) > 0
        )
        self.assertTrue(all_made_calls, "infra problem: a setup made 0 LLM calls")


if __name__ == "__main__":
    unittest.main()
