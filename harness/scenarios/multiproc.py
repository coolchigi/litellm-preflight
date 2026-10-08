"""Finding: with PROMETHEUS_MULTIPROC_DIR set and workers recycled, every dead
worker leaves its .db files behind, so the directory (and the work /metrics
does to read it) grows with every restart.

prometheus_client's mark_process_dead only deletes "live" gauge files, so
counter and histogram files from dead workers stay by design. LiteLLM wipes
the directory at startup (prometheus_cleanup.wipe_directory, and
docker/component_entrypoint.sh for the split images), not while running.

Variants (2 workers each):
  control   multiproc dir, no recycling
  recycle   multiproc dir, workers recycled every 50 requests

Sends traffic in rounds and records after each round: files in the dir, its
size, how many worker pids have written files, /metrics latency and size,
and container memory.

Usage: python3 scenarios/multiproc.py classic|split [variant ...]
"""

import json
import statistics
import time
from concurrent.futures import ThreadPoolExecutor

from lib import Stack, log, save, stack_arg

MULTIPROC_DIR = "/tmp/litellm_prometheus_multiproc"
ROUNDS = 12
PER_ROUND = 250
CONCURRENCY = 4

FILES = {
    ("classic", "control"): [],
    ("classic", "recycle"): ["scenarios/multiproc/classic-recycle.yaml"],
    ("split", "control"): ["scenarios/multiproc/split-control.yaml"],
    ("split", "recycle"): ["scenarios/multiproc/split-recycle.yaml"],
}

DIR_PROBE = f"""
import json, os, re
d = {MULTIPROC_DIR!r}
names = os.listdir(d) if os.path.isdir(d) else []
pids = {{m.group(1) for n in names if (m := re.search(r'_(\\d+)\\.db$', n))}}
kinds = {{}}
for n in names:
    k = n.rsplit('_', 1)[0]
    kinds[k] = kinds.get(k, 0) + 1
print(json.dumps({{"exists": os.path.isdir(d), "files": len(names),
    "bytes": sum(os.path.getsize(os.path.join(d, n)) for n in names),
    "pids": len(pids), "by_kind": kinds}}))
"""


def probe(stack, container):
    out = stack.exec(container, "python", "-c", DIR_PROBE)
    return json.loads(out.strip().splitlines()[-1])


def scrape(stack, n=3):
    samples = []
    for _ in range(n):
        status, body, seconds = stack.metrics()
        samples.append((status, len(body) if isinstance(body, str) else 0, seconds))
    return {
        "status": [s[0] for s in samples],
        "bytes": samples[-1][1],
        "median_ms": round(statistics.median(s[2] for s in samples) * 1000, 1),
    }


def measure(stack_name, variant):
    stack = Stack(stack_name, replicas=1, workers=2, files=FILES[(stack_name, variant)])
    stack.up()
    rounds = []
    try:
        container = stack.containers(stack.serving)[0]
        start = probe(stack, container)
        log(f"{stack_name}/{variant}: multiproc dir at start {start}")
        rounds.append({"round": 0, "requests_sent": 0, "errors": 0, "dir": start,
                       "metrics": scrape(stack), "memory_mib": stack.memory_mib().get(container)})
        sent = errors = 0
        with ThreadPoolExecutor(CONCURRENCY) as pool:
            for r in range(1, ROUNDS + 1):
                statuses = list(pool.map(lambda _: stack.chat()[0], range(PER_ROUND)))
                sent += len(statuses)
                errors += sum(1 for s in statuses if s != 200)
                time.sleep(2)
                row = {"round": r, "requests_sent": sent, "errors": errors, "dir": probe(stack, container),
                       "metrics": scrape(stack), "memory_mib": stack.memory_mib().get(container)}
                rounds.append(row)
                log(f"{stack_name}/{variant}: round {r}: {row['dir']['files']} files, "
                    f"{row['dir']['bytes'] // 1024} KiB, {row['dir']['pids']} pids, "
                    f"/metrics {row['metrics']['median_ms']} ms, mem {row['memory_mib']} MiB, errors {errors}")
    finally:
        stack.down()

    first, last = rounds[0], rounds[-1]
    result = {
        "finding": "multiproc",
        "variant": variant,
        "settings": stack.settings,
        "multiproc_dir": MULTIPROC_DIR,
        "summary": {
            "requests": last["requests_sent"],
            "errors": last["errors"],
            "files": [first["dir"]["files"], last["dir"]["files"]],
            "kib": [first["dir"]["bytes"] // 1024, last["dir"]["bytes"] // 1024],
            "worker_pids_seen": last["dir"]["pids"],
            "metrics_ms": [first["metrics"]["median_ms"], last["metrics"]["median_ms"]],
            "memory_mib": [first["memory_mib"], last["memory_mib"]],
        },
        "rounds": rounds,
    }
    log(f"{stack_name}/{variant}: {result['summary']}")
    save("multiproc", f"{stack_name}-{variant}", result)
    return result


if __name__ == "__main__":
    stack_name, variants = stack_arg()
    for variant in variants or ("control", "recycle"):
        measure(stack_name, variant)
