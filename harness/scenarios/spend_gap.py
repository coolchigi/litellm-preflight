"""Finding: a key can be blocked for budget while /key/info still shows it
under budget, and with proxy_batch_write_at: 60 the Redis spend counter can
expire before Postgres has the spend, which lets the key spend again
(public as https://github.com/BerriAI/litellm/issues/43732).

Enforcement reads a Redis counter (spend:key:<hash>, see
_virtual_key_max_budget_check in litellm/proxy/auth/auth_checks.py). /key/info
reads Postgres, which only gets spend when the batch writer runs every
proxy_batch_write_at seconds (10 by default). When the counter expires,
get_current_spend reseeds it from Postgres.

Variants (2 replicas):
  default          proxy_batch_write_at at its default
  batch-60         proxy_batch_write_at: 60

Spends a $0.01 key ($0.002 per call) until it's blocked, then keeps sending 1
request a second and watches /key/info.

Usage: python3 scenarios/spend_gap.py classic|split [variant ...]
"""

import hashlib
import time

from lib import Stack, log, save, stack_arg

MAX_BUDGET = 0.01
COST = 0.002
CATCHUP_TIMEOUT = 150

VARIANTS = {
    "default": {"config": "proxy.yaml"},
    "batch-60": {"config": "batch-write-60.yaml"},
}


def error_type(body):
    if isinstance(body, dict):
        err = body.get("error") or {}
        return err.get("type") or err.get("code") or str(err)[:80]
    return str(body)[:80]


def redis_counter(stack, key):
    key_hash = hashlib.sha256(key.encode()).hexdigest()
    redis = stack.containers("redis")[0]
    names = stack.exec(redis, "redis-cli", "--scan", "--pattern", f"*{key_hash}*").split()
    return {n: stack.exec(redis, "redis-cli", "GET", n).strip() for n in names if "spend" in n}


def measure(stack_name, variant):
    spec = VARIANTS[variant]
    stack = Stack(stack_name, replicas=2, config=spec["config"])
    stack.up()
    try:
        key = stack.new_key(max_budget=MAX_BUDGET, models=["mock-a"])["key"]
        t0 = time.time()
        calls = []
        for i in range(25):
            status, body, _ = stack.chat(key)
            calls.append({"t": round(time.time() - t0, 2), "status": status,
                          "error": None if status == 200 else error_type(body)})
            if status != 200:
                break
        t_block = calls[-1]["t"]
        allowed = sum(1 for c in calls if c["status"] == 200)
        at_block = stack.key_info(key)
        counter = redis_counter(stack, key)
        log(f"{stack_name}/{variant}: blocked after {allowed} calls (${allowed * COST:.3f}) with "
            f"{calls[-1]['status']} {calls[-1]['error']}. /key/info spend ${at_block.get('spend')}, Redis {counter}")

        timeline = []
        caught_up_at = None
        while time.time() - t0 < t_block + CATCHUP_TIMEOUT:
            info = stack.key_info(key)
            status, body, _ = stack.chat(key)
            now = round(time.time() - t0, 2)
            timeline.append({"t": now, "db_spend": info.get("spend"), "request_status": status})
            if caught_up_at is None and (info.get("spend") or 0) >= MAX_BUDGET - 1e-9:
                caught_up_at = now
                break
            time.sleep(1)
    finally:
        stack.down()

    admitted = [r["t"] for r in timeline if r["request_status"] == 200]
    result = {
        "finding": "spend-gap",
        "variant": variant,
        "settings": stack.settings,
        "max_budget": MAX_BUDGET,
        "cost_per_call": COST,
        "calls_allowed": allowed,
        "spent_by_allowed_calls": round(allowed * COST, 6),
        "blocked_with": {"status": calls[-1]["status"], "error": calls[-1]["error"]},
        "seconds_to_block": t_block,
        "db_spend_when_blocked": at_block.get("spend"),
        "redis_counter_when_blocked": counter,
        "seconds_blocked_while_db_under_budget": None if caught_up_at is None else round(caught_up_at - t_block, 1),
        "still_blocked_while_waiting": not admitted,
        "admitted_after_block_seconds": [round(t - t_block, 1) for t in admitted],
        "final_db_spend": timeline[-1]["db_spend"] if timeline else None,
        "calls": calls,
        "timeline": timeline,
    }
    log(f"{stack_name}/{variant}: /key/info caught up {result['seconds_blocked_while_db_under_budget']}s after "
        f"the block. {len(admitted)} requests admitted after the block, final DB spend ${result['final_db_spend']}")
    save("spend-gap", f"{stack_name}-{variant}", result)
    return result


if __name__ == "__main__":
    stack_name, variants = stack_arg()
    for variant in variants or VARIANTS:
        measure(stack_name, variant)
