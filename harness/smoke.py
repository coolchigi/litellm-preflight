"""End-to-end check that a harness stack works. Run it through ./harness smoke <stack>.

Proves each piece a repro depends on:
  1. the proxy answers through the load balancer
  2. a completion reaches the mock and the mock counts it
  3. a virtual key can be created and used
  4. spend lands in Postgres and shows up on /key/info (pricing works)
  5. /metrics serves LiteLLM metrics
  6. with replicas > 1, traffic spreads across every replica and the mock
     can tell the replicas apart (needed for per-pod counts)

Stdlib only. Exits 1 if any check fails.
"""

import argparse
import hashlib
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

PRICE_IN = 0.000001  # matches config/proxy.yaml
PRICE_OUT = 0.000002

failures = []


def read_env(path):
    env = {}
    for line in Path(path).read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def request(method, url, key=None, body=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
            status = resp.status
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        status = e.code
    except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
        return 0, str(e)
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, raw


def check(name, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))
    if not ok:
        failures.append(name)
    return ok


def chat(base, key, model="mock-a"):
    return request("POST", f"{base}/v1/chat/completions", key, {
        "model": model,
        "messages": [{"role": "user", "content": "harness smoke"}],
    })


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--stack", required=True)
    p.add_argument("--inference", required=True)
    p.add_argument("--admin", required=True)
    p.add_argument("--mock", required=True)
    p.add_argument("--replicas", type=int, default=1)
    p.add_argument("--spend-timeout", type=int, default=60)
    a = p.parse_args()

    master = read_env(Path(__file__).with_name(".env"))["LITELLM_MASTER_KEY"]
    print(f"smoke: {a.stack} stack, inference {a.inference}, admin {a.admin}, replicas {a.replicas}")

    status, body = request("GET", f"{a.inference}/health/readiness")
    check("proxy ready through the load balancer", status == 200, f"HTTP {status}")
    if a.admin != a.inference:
        status, _ = request("GET", f"{a.admin}/health/readiness")
        check("backend ready", status == 200, f"HTTP {status}")

    request("POST", f"{a.mock}/stats/reset")
    status, body = chat(a.inference, master)
    reply = body.get("choices", [{}])[0].get("message", {}).get("content") if isinstance(body, dict) else None
    check("completion with the master key", status == 200 and reply == "ok", f"HTTP {status}, reply {reply!r}")
    _, stats = request("GET", f"{a.mock}/stats")
    user_calls = stats.get("by_kind", {}).get("user", 0) if isinstance(stats, dict) else 0
    check("mock counted the call", user_calls == 1, f"user calls {user_calls}")

    status, body = request("POST", f"{a.admin}/key/generate", master, {
        "key_alias": f"harness-smoke-{int(time.time())}",
        "max_budget": 1.0,
        "models": ["mock-a"],
    })
    vkey = body.get("key") if isinstance(body, dict) else None
    if not check("virtual key created on the admin API", status == 200 and bool(vkey), f"HTTP {status}"):
        print(f"        response: {str(body)[:300]}")
        return finish()

    status, _ = chat(a.inference, vkey)
    check("completion with the virtual key", status == 200, f"HTTP {status}")

    expected = (stats.get("usage_per_call", {}).get("prompt_tokens", 1000) * PRICE_IN
                + stats.get("usage_per_call", {}).get("completion_tokens", 500) * PRICE_OUT)
    key_hash = hashlib.sha256(vkey.encode()).hexdigest()
    spend, waited, start = 0.0, 0, time.time()
    while time.time() - start < a.spend_timeout:
        status, body = request("GET", f"{a.admin}/key/info?key={key_hash}", master)
        spend = (body.get("info") or {}).get("spend") or 0.0 if isinstance(body, dict) else 0.0
        if spend > 0:
            break
        time.sleep(2)
    waited = round(time.time() - start)
    check("spend written to Postgres and visible on /key/info",
          abs(spend - expected) < 1e-9, f"spend ${spend:.6f}, expected ${expected:.6f}, took ~{waited}s")

    # /metrics needs a key by default (require_auth_for_metrics_endpoint, see
    # litellm/proxy/middleware/prometheus_auth_middleware.py), so a scraper does too.
    status, body = request("GET", f"{a.inference}/metrics/", master)
    has_metrics = status == 200 and isinstance(body, str) and "litellm_" in body
    check("/metrics serves LiteLLM metrics", has_metrics, f"HTTP {status}")

    if a.replicas > 1:
        request("POST", f"{a.mock}/stats/reset")
        for _ in range(a.replicas * 6):
            chat(a.inference, master)
        _, stats = request("GET", f"{a.mock}/stats")
        callers = {c: k.get("user", 0) for c, k in (stats.get("by_caller_kind") or {}).items() if k.get("user")}
        check(f"traffic reached all {a.replicas} replicas, mock tells them apart",
              len(callers) == a.replicas, json.dumps(callers))

    request("POST", f"{a.admin}/key/delete", master, {"keys": [vkey]})
    return finish()


def finish():
    if failures:
        print(f"smoke: {len(failures)} check(s) failed")
        return 1
    print("smoke: all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
