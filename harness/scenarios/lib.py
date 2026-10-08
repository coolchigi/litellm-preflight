"""Helpers shared by the scenario scripts. Stdlib only."""

import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HARNESS = Path(__file__).resolve().parent.parent
RESULTS = HARNESS / "results"

URLS = {
    "classic": {"inference": "http://127.0.0.1:4000", "admin": "http://127.0.0.1:4000", "mock": "http://127.0.0.1:9000"},
    "split": {"inference": "http://127.0.0.1:4100", "admin": "http://127.0.0.1:4101", "mock": "http://127.0.0.1:9100"},
}
# The service that takes inference traffic in each stack
SERVING = {"classic": "proxy", "split": "gateway"}


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def utcnow():
    return datetime.now(timezone.utc)


def parse_ts(value):
    """LiteLLM timestamps come back as ISO strings, sometimes without a zone (meaning UTC)."""
    if not value:
        return None
    ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def master_key():
    for line in (HARNESS / ".env").read_text().splitlines():
        if line.startswith("LITELLM_MASTER_KEY="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("no LITELLM_MASTER_KEY in harness/.env, run ./harness once first")


def request(method, url, key=None, body=None, timeout=30):
    """Returns (status, parsed body or text, seconds). Status 0 means no HTTP response."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    start = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw, status = resp.read().decode(), resp.status
    except urllib.error.HTTPError as e:
        raw, status = e.read().decode(), e.code
    except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
        return 0, str(e), time.monotonic() - start
    elapsed = time.monotonic() - start
    try:
        return status, json.loads(raw), elapsed
    except ValueError:
        return status, raw, elapsed


def run(cmd, env=None, check=True, quiet=True):
    proc = subprocess.run(cmd, cwd=HARNESS, env={**os.environ, **(env or {})},
                          capture_output=quiet, text=True)
    if check and proc.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed ({proc.returncode}):\n{proc.stdout}\n{proc.stderr}")
    return proc


class Stack:
    def __init__(self, name, replicas=1, workers=1, config="proxy.yaml", files=(), env=None):
        assert name in URLS, name
        self.name = name
        self.urls = URLS[name]
        self.project = f"litellm-{name}"
        self.serving = SERVING[name]
        self.files = list(files)
        self.env = {
            "PROXY_REPLICAS": str(replicas),
            "NUM_WORKERS": str(workers),
            "LITELLM_CONFIG": config,
            **(env or {}),
        }
        self.key = master_key()
        self.settings = {"stack": name, "replicas": replicas, "workers": workers, "config": config,
                         "files": self.files, "env": env or {}}

    def _harness(self, *args, check=True):
        return run(["./harness", *args], env=self.env, check=check)

    def up(self):
        self.down()
        log(f"{self.name}: up {self.settings}")
        extra = [arg for f in self.files for arg in ("-f", f)]
        self._harness("up", self.name, *extra)
        self.started_at = utcnow()

    def down(self):
        self._harness("down", self.name, check=False)

    def containers(self, service):
        out = run(["docker", "ps", "--filter", f"label=com.docker.compose.project={self.project}",
                   "--filter", f"label=com.docker.compose.service={service}", "--format", "{{.Names}}"]).stdout
        return sorted(out.split())

    def exec(self, container, *cmd, check=True):
        return run(["docker", "exec", container, *cmd], check=check).stdout

    def memory_mib(self):
        """Memory per container from docker stats, in MiB."""
        out = run(["docker", "stats", "--no-stream", "--format", "{{.Name}}\t{{.MemUsage}}"]).stdout
        mem = {}
        for line in out.splitlines():
            name, usage = line.split("\t")
            if not name.startswith(self.project + "-"):
                continue
            used = usage.split("/")[0].strip()
            for unit, factor in (("GiB", 1024), ("MiB", 1), ("KiB", 1 / 1024), ("B", 1 / 1024 / 1024)):
                if used.endswith(unit):
                    mem[name] = round(float(used[: -len(unit)]) * factor, 1)
                    break
        return mem

    def logs(self, service):
        return run(["docker", "compose", "-p", self.project, "logs", "--no-color", service], check=False).stdout

    # --- LiteLLM API ---

    def chat(self, key=None, model="mock-a", timeout=30):
        return request("POST", f"{self.urls['inference']}/v1/chat/completions", key or self.key, {
            "model": model, "messages": [{"role": "user", "content": "scenario traffic"}],
        }, timeout=timeout)

    def new_key(self, **body):
        status, resp, _ = request("POST", f"{self.urls['admin']}/key/generate", self.key, body)
        if status != 200:
            raise RuntimeError(f"/key/generate HTTP {status}: {str(resp)[:300]}")
        return resp

    def key_info(self, key):
        key_hash = hashlib.sha256(key.encode()).hexdigest()
        status, resp, _ = request("GET", f"{self.urls['admin']}/key/info?key={key_hash}", self.key)
        if status != 200:
            return {"_status": status, "_body": str(resp)[:300]}
        return resp.get("info") or {}

    def delete_key(self, key):
        return request("POST", f"{self.urls['admin']}/key/delete", self.key, {"keys": [key]})

    def metrics(self):
        return request("GET", f"{self.urls['inference']}/metrics/", self.key, timeout=60)

    # --- mock ---

    def mock_reset(self):
        request("POST", f"{self.urls['mock']}/stats/reset")

    def mock_stats(self):
        return request("GET", f"{self.urls['mock']}/stats")[1]

    def mock_events(self, limit=5000):
        return request("GET", f"{self.urls['mock']}/events?limit={limit}")[1].get("events", [])


def save(scenario, name, data):
    path = RESULTS / scenario / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, default=str) + "\n")
    log(f"saved {path.relative_to(HARNESS)}")
    return path


def stack_arg():
    if len(sys.argv) < 2 or sys.argv[1] not in URLS:
        raise SystemExit(f"usage: {sys.argv[0]} classic|split [variant ...]")
    return sys.argv[1], sys.argv[2:]
