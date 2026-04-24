"""
Canonical pytest suite for the logistics toolkit.

Grader-supplied: the demo harness copies this file verbatim into every
team/solo workspace as `tests/test_logistics_toolkit.py` immediately
before running pytest. Every setup is graded against the IDENTICAL
suite, so pass/fail counts are comparable.

The suite tests only the documented public surface defined in the
TASK_SPEC of test_logistics_multiagent_vs_solo.py.

Modules under test:
  - src.logistics.routing       (CVRPTW data model + heuristics)
  - src.logistics.scheduling    (JSSP data model + heuristics)
  - src.logistics.graph_paths   (shortest path + constrained shortest path)
  - src.logistics.integration   (end-to-end planner)
  - src.logistics.api           (run_demo_scenario facade)
"""

from __future__ import annotations

import math

import pytest

from src.logistics.routing import (
    Location, Vehicle, Customer,
    compute_distance_matrix,
    check_routing_feasibility,
    routing_cost,
    build_initial_routes,
    improve_routes,
    routing_objectives,
)
from src.logistics.scheduling import (
    Job, Operation, Machine,
    check_schedule_feasibility,
    schedule_makespan,
    build_initial_schedule,
    improve_schedule,
    scheduling_objectives,
)
from src.logistics.graph_paths import (
    Graph,
    shortest_path_unconstrained,
    check_path_feasibility,
    find_constrained_path,
)
from src.logistics.integration import (
    plan_end_to_end,
    plan_cost_optimized,
    plan_service_optimized,
)
from src.logistics.api import run_demo_scenario


# ─────────────────────────── ROUTING ─────────────────────────────────────────


def _depot():
    return Location(name="depot", x=0.0, y=0.0)


def _small_customers():
    return [
        Customer(id="c1", location=Location("c1", 1.0, 0.0),
                 demand=2, time_window=(0.0, 100.0)),
        Customer(id="c2", location=Location("c2", 0.0, 2.0),
                 demand=3, time_window=(0.0, 100.0)),
        Customer(id="c3", location=Location("c3", 2.0, 2.0),
                 demand=1, time_window=(0.0, 100.0)),
    ]


def _vehicles(capacity=10, max_time=100.0, count=2):
    return [Vehicle(id=f"v{i}", capacity=capacity, max_time=max_time)
            for i in range(1, count + 1)]


def test_routing_distance_matrix_symmetric():
    locs = [Location("a", 0, 0), Location("b", 3, 4)]
    d = compute_distance_matrix(locs)
    # Distances are 5.0 in both directions; self-distance is 0.
    assert d[("a", "b")] == pytest.approx(5.0)
    assert d[("b", "a")] == pytest.approx(5.0)
    assert d[("a", "a")] == pytest.approx(0.0)


def test_routing_capacity_feasibility():
    depot = _depot()
    customers = _small_customers()
    # Tight capacity: a single vehicle of capacity 4 cannot serve all
    # (total demand = 6).
    vehicles = [Vehicle(id="v1", capacity=4, max_time=100.0)]
    plan = build_initial_routes(vehicles=vehicles, customers=customers,
                                depot=depot)
    # Either the heuristic refuses to overload (returns infeasible plan)
    # or it produces a feasible plan that simply skips a customer; we
    # just require the feasibility check itself to be honest.
    fb = check_routing_feasibility(plan=plan, vehicles=vehicles,
                                   customers=customers, depot=depot)
    assert isinstance(fb, bool)
    # If the plan claims to serve all 3 customers with one capacity-4
    # vehicle, it MUST be reported infeasible.
    served = {cid for r in plan.routes for cid in r.customer_ids}
    if served == {"c1", "c2", "c3"}:
        assert fb is False


def test_routing_cost_nonnegative_and_finite():
    depot = _depot()
    customers = _small_customers()
    vehicles = _vehicles(capacity=10, max_time=100.0, count=2)
    plan = build_initial_routes(vehicles=vehicles, customers=customers,
                                depot=depot)
    cost = routing_cost(plan=plan, vehicles=vehicles, customers=customers,
                        depot=depot)
    assert cost >= 0.0
    assert math.isfinite(cost)


def test_routing_improve_does_not_worsen_cost():
    depot = _depot()
    customers = _small_customers()
    vehicles = _vehicles(capacity=10, max_time=100.0, count=2)
    initial = build_initial_routes(vehicles=vehicles, customers=customers,
                                   depot=depot)
    improved = improve_routes(plan=initial, vehicles=vehicles,
                              customers=customers, depot=depot)
    c0 = routing_cost(initial, vehicles=vehicles,
                      customers=customers, depot=depot)
    c1 = routing_cost(improved, vehicles=vehicles,
                      customers=customers, depot=depot)
    # Local search may keep cost unchanged; it MUST NOT make it worse.
    assert c1 <= c0 + 1e-9


# ─────────────────────────── SCHEDULING ──────────────────────────────────────


def _two_job_three_machine():
    machines = [Machine(id=f"m{i}") for i in (1, 2, 3)]
    j1 = Job(id="j1", operations=[
        Operation(job_id="j1", machine_id="m1", duration=3, order_index=0),
        Operation(job_id="j1", machine_id="m2", duration=2, order_index=1),
    ])
    j2 = Job(id="j2", operations=[
        Operation(job_id="j2", machine_id="m2", duration=4, order_index=0),
        Operation(job_id="j2", machine_id="m3", duration=1, order_index=1),
    ])
    return [j1, j2], machines


def test_scheduling_no_machine_overlap_and_precedence():
    jobs, machines = _two_job_three_machine()
    sched = build_initial_schedule(jobs=jobs, machines=machines)
    assert check_schedule_feasibility(schedule=sched, jobs=jobs,
                                      machines=machines) is True


def test_scheduling_makespan_positive():
    jobs, machines = _two_job_three_machine()
    sched = build_initial_schedule(jobs=jobs, machines=machines)
    ms = schedule_makespan(sched)
    # Two-job, three-machine instance with positive durations -> makespan > 0.
    assert ms > 0
    assert math.isfinite(ms)


def test_scheduling_improve_keeps_feasible_and_does_not_worsen():
    jobs, machines = _two_job_three_machine()
    initial = build_initial_schedule(jobs=jobs, machines=machines)
    improved = improve_schedule(schedule=initial, jobs=jobs,
                                machines=machines)
    assert check_schedule_feasibility(schedule=improved, jobs=jobs,
                                      machines=machines) is True
    assert schedule_makespan(improved) <= schedule_makespan(initial) + 1e-9


# ─────────────────────────── GRAPH PATHS ─────────────────────────────────────


def _diamond_graph():
    """
    A <─cost=1,risk=10─> B
    A <─cost=5,risk=1──> C
    B <─cost=1,risk=10─> D
    C <─cost=5,risk=1──> D
    """
    g = Graph(nodes={"A", "B", "C", "D"}, edges={})
    g.edges[("A", "B")] = {"cost": 1.0, "risk": 10.0}
    g.edges[("B", "A")] = {"cost": 1.0, "risk": 10.0}
    g.edges[("A", "C")] = {"cost": 5.0, "risk": 1.0}
    g.edges[("C", "A")] = {"cost": 5.0, "risk": 1.0}
    g.edges[("B", "D")] = {"cost": 1.0, "risk": 10.0}
    g.edges[("D", "B")] = {"cost": 1.0, "risk": 10.0}
    g.edges[("C", "D")] = {"cost": 5.0, "risk": 1.0}
    g.edges[("D", "C")] = {"cost": 5.0, "risk": 1.0}
    return g


def test_unconstrained_shortest_path_picks_low_cost():
    g = _diamond_graph()
    p = shortest_path_unconstrained(graph=g, source="A", target="D")
    assert p is not None
    # Cheap path is A->B->D with cost 1+1 = 2.
    assert p.nodes[0] == "A" and p.nodes[-1] == "D"
    assert p.total_cost == pytest.approx(2.0)


def test_constrained_path_returns_none_when_infeasible():
    g = _diamond_graph()
    # No path from A to D has total risk <= 0.
    p = find_constrained_path(graph=g, source="A", target="D", max_risk=0.0)
    assert p is None


def test_constrained_path_feasible_when_loose():
    g = _diamond_graph()
    # max_risk=2 forces the safe-but-costly route A->C->D (risk = 2).
    p = find_constrained_path(graph=g, source="A", target="D", max_risk=2.0)
    assert p is not None
    assert check_path_feasibility(path=p, max_risk=2.0) is True
    # The cheap (risk=20) path must NOT be selected.
    assert p.total_risk <= 2.0 + 1e-9


# ─────────────────────────── INTEGRATION ─────────────────────────────────────


def test_integration_returns_combined_plan():
    """plan_end_to_end runs all three engines on a small built-in scenario."""
    result = plan_end_to_end()
    assert isinstance(result, dict)
    # Required top-level keys (per spec).
    for k in ("routing_plan", "schedule", "paths", "metrics"):
        assert k in result, f"missing key {k!r} in plan_end_to_end() result"
    metrics = result["metrics"]
    assert isinstance(metrics, dict)
    # Metrics must include numeric routing_cost and makespan.
    assert isinstance(metrics.get("routing_cost"), (int, float))
    assert isinstance(metrics.get("makespan"), (int, float))


def test_integration_plan_is_internally_feasible():
    """The integrated planner must produce an internally-feasible plan."""
    result = plan_end_to_end()
    metrics = result["metrics"]
    # The planner reports feasibility flags; both must be True.
    assert metrics.get("routing_feasible") is True
    assert metrics.get("schedule_feasible") is True


# ─────────────────────────── API ─────────────────────────────────────────────


def test_api_run_demo_scenario_summary():
    summary = run_demo_scenario()
    assert isinstance(summary, dict)
    for k in ("routing_cost", "makespan", "violations"):
        assert k in summary, f"missing key {k!r} in run_demo_scenario() summary"
    # No constraint violations on the built-in toy scenario.
    assert summary["violations"] == [] or summary["violations"] == 0


# ────────────────────── MULTI-OBJECTIVE / TRADE-OFF ──────────────────────────


def test_routing_objectives_breakdown():
    """routing_objectives must return distance + co2 + lateness components."""
    depot = _depot()
    customers = _small_customers()
    vehicles = _vehicles(capacity=10, max_time=100.0, count=2)
    plan = build_initial_routes(vehicles=vehicles, customers=customers,
                                depot=depot)
    obj = routing_objectives(plan=plan, vehicles=vehicles,
                             customers=customers, depot=depot)
    assert isinstance(obj, dict)
    for k in ("distance", "co2", "lateness_penalty"):
        assert k in obj, f"routing_objectives missing key {k!r}"
        assert isinstance(obj[k], (int, float))
        assert math.isfinite(float(obj[k]))
        assert obj[k] >= 0.0
    # Distance component must equal routing_cost (same plan, same scenario).
    assert obj["distance"] == pytest.approx(
        routing_cost(plan, vehicles=vehicles,
                     customers=customers, depot=depot),
        rel=1e-6, abs=1e-6,
    )


def test_scheduling_objectives_breakdown():
    """scheduling_objectives must return makespan + energy + overtime."""
    jobs, machines = _two_job_three_machine()
    sched = build_initial_schedule(jobs=jobs, machines=machines)
    obj = scheduling_objectives(schedule=sched, jobs=jobs, machines=machines)
    assert isinstance(obj, dict)
    for k in ("makespan", "energy", "overtime"):
        assert k in obj, f"scheduling_objectives missing key {k!r}"
        assert isinstance(obj[k], (int, float))
        assert math.isfinite(float(obj[k]))
        assert obj[k] >= 0.0
    # Makespan component must agree with schedule_makespan().
    assert obj["makespan"] == pytest.approx(
        schedule_makespan(sched), rel=1e-6, abs=1e-6,
    )


def test_integration_exposes_alternatives_and_choice():
    """plan_end_to_end must surface both candidate proposals + a choice."""
    result = plan_end_to_end()
    metrics = result["metrics"]
    # tradeoff_components has all six required float keys.
    tc = metrics.get("tradeoff_components")
    assert isinstance(tc, dict), "metrics.tradeoff_components missing"
    for k in ("distance", "co2", "lateness_penalty",
              "makespan", "energy", "overtime"):
        assert k in tc, f"tradeoff_components missing key {k!r}"
        assert isinstance(tc[k], (int, float))
    # alternatives is a list of >=2 entries with the same key set.
    alts = metrics.get("alternatives")
    assert isinstance(alts, list) and len(alts) >= 2, \
        "metrics.alternatives must have >=2 entries (cost vs service)"
    names = {a.get("name") for a in alts}
    assert "cost_optimized" in names and "service_optimized" in names, \
        f"alternatives must include both cost_optimized and " \
        f"service_optimized (got {names!r})"
    for a in alts:
        obj = a.get("objectives")
        assert isinstance(obj, dict)
        for k in ("distance", "co2", "lateness_penalty",
                  "makespan", "energy", "overtime"):
            assert k in obj, \
                f"alternative {a.get('name')!r} missing objective key {k!r}"
        assert isinstance(a.get("rationale"), str) and a["rationale"], \
            f"alternative {a.get('name')!r} missing rationale"
    # chosen + rationale required.
    assert metrics.get("chosen"), "metrics.chosen missing"
    assert isinstance(metrics.get("rationale"), str), \
        "metrics.rationale must be a string"
    # The two candidates must differ on at least one objective component
    # by more than a small epsilon. Identical vectors (or vectors that
    # only differ by floating-point noise) mean no real trade-off was
    # modelled — typically because the built-in scenario has so much
    # slack that cost-leaning and service-leaning plans collapse.
    cost_obj = next(a["objectives"] for a in alts
                    if a["name"] == "cost_optimized")
    svc_obj = next(a["objectives"] for a in alts
                   if a["name"] == "service_optimized")
    eps = 1e-6
    keys = ("distance", "co2", "lateness_penalty",
            "makespan", "energy", "overtime")
    diffs = {k: abs(float(cost_obj[k]) - float(svc_obj[k])) for k in keys}
    assert any(d > eps for d in diffs.values()), (
        "cost_optimized and service_optimized must differ on at least one "
        "objective component by more than {eps} — got identical vectors "
        "(diffs={diffs!r}). The built-in scenario likely has too much "
        "slack; tighten time windows or shift_end so candidates diverge."
        .format(eps=eps, diffs=diffs)
    )
    # And the candidates must also be available as standalone callables.
    c = plan_cost_optimized()
    s = plan_service_optimized()
    assert "tradeoff_components" in c["metrics"]
    assert "tradeoff_components" in s["metrics"]


def test_api_tradeoff_summary_present():
    """run_demo_scenario must expose a tradeoff_summary mirror."""
    summary = run_demo_scenario()
    ts = summary.get("tradeoff_summary")
    assert isinstance(ts, dict), "summary.tradeoff_summary missing"
    assert "components" in ts and isinstance(ts["components"], dict)
    assert "alternatives" in ts and isinstance(ts["alternatives"], list)
    assert len(ts["alternatives"]) >= 2
    assert ts.get("chosen")
    assert isinstance(ts.get("rationale"), str)
