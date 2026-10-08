"""Finding: with enable_redis_auth_cache, a deleted virtual key could keep
authenticating (seen on v1.89.3).

The key object is cached in memory per worker (60s TTL) and, with
enable_redis_auth_cache, in Redis with litellm.default_redis_ttl, which is
None by default. /key/delete evicts both and broadcasts the eviction
(_delete_cache_key_object in litellm/proxy/auth/auth_checks.py).

Variants (2 replicas):
  redis         enable_redis_auth_cache: true, Redis from coordination_redis
  in-memory     the default, as a control
  redis-legacy  enable_redis_auth_cache with Redis from litellm_settings.cache,
                for older releases. Not run by default:
                LITELLM_VERSION=v1.89.3 python3 scenarios/auth_cache.py classic redis-legacy

Warms the key on both replicas, deletes it, then sends 2 requests every 5s for
3 minutes and records which ones still get through.

Usage: python3 scenarios/auth_cache.py classic|split [variant ...]
"""

import hashlib
import os
import time

from lib import Stack, log, save, stack_arg

CONFIGS = {
    "redis": "redis-auth-cache.yaml",
    "in-memory": "proxy.yaml",
    # Not run by default: Redis shared through litellm_settings.cache, the only
    # way older releases (v1.89.3) attach it. Use with LITELLM_VERSION to compare.
    "redis-legacy": "redis-auth-cache-legacy.yaml",
}
DEFAULT_VARIANTS = ("redis", "in-memory")
VERSION = os.environ.get("LITELLM_VERSION", "v1.104.0")
WATCH = 180


def redis_entries(stack, key):
    key_hash = hashlib.sha256(key.encode()).hexdigest()
    redis = stack.containers("redis")[0]
    names = stack.exec(redis, "redis-cli", "--scan", "--pattern", f"*{key_hash}*").split()
    return {n: stack.exec(redis, "redis-cli", "TTL", n).strip() for n in names}


def measure(stack_name, variant):
    stack = Stack(stack_name, replicas=2, config=CONFIGS[variant])
    stack.up()
    try:
        key = stack.new_key(models=["mock-a"])["key"]
        warm = [stack.chat(key)[0] for _ in range(6)]
        before = redis_entries(stack, key)
        status, body, _ = stack.delete_key(key)
        deleted_at = time.time()
        after = redis_entries(stack, key)
        log(f"{stack_name}/{variant}: warm-up {warm}, delete HTTP {status}. "
            f"Redis entries (name: TTL) before {before}, after {after}")

        probes = []
        while time.time() - deleted_at < WATCH:
            t = round(time.time() - deleted_at, 1)
            probes.append({"t": t, "statuses": [stack.chat(key)[0], stack.chat(key)[0]]})
            time.sleep(5)
        late = redis_entries(stack, key)
    finally:
        stack.down()

    ok_times = [p["t"] for p in probes if 200 in p["statuses"]]
    # Each probe hits both replicas. Once a probe is rejected everywhere, the key
    # is gone from every cache, so a success after that means it came back.
    all_rejected = next((p["t"] for p in probes if 200 not in p["statuses"]), None)
    resurrected = [t for t in ok_times if all_rejected is not None and t > all_rejected]
    result = {
        "finding": "auth-cache",
        "variant": variant,
        "litellm_version": VERSION,
        "settings": stack.settings,
        "delete_status": status,
        "redis_entries_before_delete": before,
        "redis_entries_after_delete": after,
        "redis_entries_at_end": late,
        "last_success_seconds_after_delete": max(ok_times) if ok_times else None,
        "successes_after_delete": sum(p["statuses"].count(200) for p in probes),
        "first_rejected_everywhere_seconds": all_rejected,
        "success_after_rejected_everywhere_seconds": resurrected,
        "probes": probes,
    }
    log(f"{stack_name}/{variant}: {result['successes_after_delete']} requests got through after delete, "
        f"last at {result['last_success_seconds_after_delete']}s, resurrected at {resurrected}")
    suffix = "" if VERSION == "v1.104.0" else f"-{VERSION}"
    save("auth-cache", f"{stack_name}-{variant}{suffix}", result)
    return result


if __name__ == "__main__":
    stack_name, variants = stack_arg()
    for variant in variants or DEFAULT_VARIANTS:
        measure(stack_name, variant)
