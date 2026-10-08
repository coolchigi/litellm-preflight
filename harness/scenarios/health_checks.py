"""Finding: background health checks run once per pod (and per worker), so the
model calls they cost multiply with your replica count.

Variants (health_check_interval 20s, 3 models):
  per-pod       3 replicas x 1 worker, background_health_checks on
  per-worker    1 replica x 2 workers
  shared        3 replicas, plus use_shared_health_check. Shared results are
                reused for DEFAULT_SHARED_HEALTH_CHECK_TTL (300s), so this one
                counts for 11 minutes from stack start
  shared-ttl20  shared, with DEFAULT_SHARED_HEALTH_CHECK_TTL=20

Each variant counts health check calls at the mock over a fixed window and
compares that with what one loop would make:
  models x (window / interval)

Usage: python3 scenarios/health_checks.py classic|split [variant ...]
"""

import time
from collections import defaultdict

from lib import Stack, log, save, stack_arg

MODELS = 3
INTERVAL = 20
SETTLE = 20

VARIANTS = {
    "per-pod": {"replicas": 3, "workers": 1, "config": "health-checks.yaml"},
    "per-worker": {"replicas": 1, "workers": 2, "config": "health-checks.yaml"},
    "shared": {"replicas": 3, "workers": 1, "config": "health-checks-shared.yaml",
               "window": 660, "from_start": True},
    "shared-ttl20": {"replicas": 3, "workers": 1, "config": "health-checks-shared.yaml",
                     "files": "scenarios/health-checks/{stack}-ttl20.yaml"},
}


def measure(stack_name, variant):
    spec = dict(VARIANTS[variant])
    window = spec.pop("window", 120)
    from_start = spec.pop("from_start", False)
    files = [spec.pop("files").format(stack=stack_name)] if "files" in spec else []
    stack = Stack(stack_name, files=files, **spec)
    stack.up()
    try:
        if from_start:
            # The mock started with the stack, so its counts already cover startup
            remaining = window - (time.time() - stack.started_at.timestamp())
        else:
            time.sleep(SETTLE)
            stack.mock_reset()
            remaining = window
        log(f"{stack_name}/{variant}: counting health checks for {window}s")
        time.sleep(max(0, remaining))
        stats = stack.mock_stats()
        events = [e for e in stack.mock_events() if e["kind"] == "health"]
    finally:
        stack.down()

    health_by_caller = {c: k.get("health", 0) for c, k in stats["by_caller_kind"].items() if k.get("health")}
    cycles = window / INTERVAL
    serving_processes = spec["replicas"] * spec["workers"]
    one_loop = MODELS * cycles
    times = defaultdict(list)
    for e in events:
        times[e["caller"]].append(e["t"])
    # Group calls into bursts (a gap over 5s starts a new one) to see the real cadence
    bursts = []
    for e in sorted(events, key=lambda e: e["t"]):
        if not bursts or e["t"] - bursts[-1]["end"] > 5:
            bursts.append({"start": e["t"], "end": e["t"], "calls": 0, "callers": set()})
        bursts[-1]["end"] = e["t"]
        bursts[-1]["calls"] += 1
        bursts[-1]["callers"].add(e["caller"])
    t0 = bursts[0]["start"] if bursts else 0
    result = {
        "finding": "health-checks",
        "variant": variant,
        "settings": stack.settings,
        "counted_from_stack_start": from_start,
        "bursts": [{"at": round(b["start"] - t0, 1), "calls": b["calls"], "callers": sorted(b["callers"])}
                   for b in bursts],
        "window_seconds": window,
        "health_check_interval": INTERVAL,
        "models": MODELS,
        "health_calls_total": stats["by_kind"].get("health", 0),
        "user_calls_total": stats["by_kind"].get("user", 0),
        "health_calls_by_caller": health_by_caller,
        "expected_if_one_loop_cluster_wide": one_loop,
        "expected_if_one_loop_per_serving_process": one_loop * serving_processes,
        "multiplier_vs_one_loop": round(stats["by_kind"].get("health", 0) / one_loop, 2),
        "first_last_call_by_caller": {c: [min(t), max(t)] for c, t in times.items()},
    }
    log(f"{stack_name}/{variant}: {result['health_calls_total']} health calls, "
        f"{result['multiplier_vs_one_loop']}x one loop, by caller {health_by_caller}")
    save("health-checks", f"{stack_name}-{variant}", result)
    return result


if __name__ == "__main__":
    stack_name, variants = stack_arg()
    for variant in variants or VARIANTS:
        measure(stack_name, variant)
