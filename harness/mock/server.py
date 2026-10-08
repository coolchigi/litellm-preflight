"""Fake OpenAI-compatible model server for the repro harness.

Answers /v1/chat/completions with a fixed reply and fixed token usage, so every
call has a known cost, and counts each call by model, kind and caller:

- kind is "health" when the prompt is one LiteLLM's health checks send, else "user"
- caller is the container that made the call (reverse DNS on the Docker network),
  which is how per-replica health check traffic gets counted

Endpoints:
  POST /v1/chat/completions   (stream and non-stream)
  GET  /v1/models
  GET  /stats                 counts so far
  GET  /events?limit=N        most recent calls, newest last
  POST /stats/reset           zero everything
  GET  /health

Stdlib only. Settings come from env vars, see the constants below.
"""

import json
import os
import socket
import threading
import time
from collections import Counter, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PORT = int(os.environ.get("MOCK_PORT", "8080"))
PROMPT_TOKENS = int(os.environ.get("MOCK_PROMPT_TOKENS", "1000"))
COMPLETION_TOKENS = int(os.environ.get("MOCK_COMPLETION_TOKENS", "500"))
LATENCY_MS = int(os.environ.get("MOCK_LATENCY_MS", "0"))
# Background health checks send one of these at random (_get_random_llm_message in
# litellm/proxy/health_check.py). DEFAULT_HEALTH_CHECK_PROMPT in litellm/constants.py
# is used by other health check paths.
HEALTH_PROMPTS = {"Hey how's it going?", "What's 1 + 1?", "test from litellm"}
REPLY = "ok"

_lock = threading.Lock()
_events = deque(maxlen=5000)
_counts = Counter()
_by_model = Counter()
_by_kind = Counter()
_by_caller = Counter()
_by_caller_kind = Counter()
_dns_cache = {}
_started = time.time()


def caller_name(ip):
    """Container name for an IP on the compose network, or the IP itself."""
    if ip not in _dns_cache:
        try:
            host = socket.gethostbyaddr(ip)[0]
            # "project-proxy-1.project_default" -> "project-proxy-1"
            _dns_cache[ip] = host.split(".")[0]
        except OSError:
            _dns_cache[ip] = ip
    return _dns_cache[ip]


def classify(body):
    for message in body.get("messages") or []:
        content = message.get("content")
        if isinstance(content, list):
            content = " ".join(part.get("text", "") for part in content if isinstance(part, dict))
        if isinstance(content, str) and content.strip() in HEALTH_PROMPTS:
            return "health"
    return "user"


def record(path, model, kind, caller):
    event = {"t": round(time.time(), 3), "path": path, "model": model, "kind": kind, "caller": caller}
    with _lock:
        _events.append(event)
        _counts["total"] += 1
        _by_model[model] += 1
        _by_kind[kind] += 1
        _by_caller[caller] += 1
        _by_caller_kind[f"{caller}|{kind}"] += 1


def snapshot():
    with _lock:
        by_caller_kind = {}
        for key, n in _by_caller_kind.items():
            caller, kind = key.split("|", 1)
            by_caller_kind.setdefault(caller, {})[kind] = n
        return {
            "since": round(_started, 3),
            "total": _counts["total"],
            "by_model": dict(_by_model),
            "by_kind": dict(_by_kind),
            "by_caller": dict(_by_caller),
            "by_caller_kind": by_caller_kind,
            "usage_per_call": {"prompt_tokens": PROMPT_TOKENS, "completion_tokens": COMPLETION_TOKENS},
        }


def reset():
    global _started
    with _lock:
        for c in (_counts, _by_model, _by_kind, _by_caller, _by_caller_kind):
            c.clear()
        _events.clear()
        _started = time.time()


def completion(model, n):
    usage = {
        "prompt_tokens": PROMPT_TOKENS,
        "completion_tokens": COMPLETION_TOKENS,
        "total_tokens": PROMPT_TOKENS + COMPLETION_TOKENS,
    }
    return {
        "id": f"chatcmpl-mock-{n}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": REPLY}, "finish_reason": "stop"}],
        "usage": usage,
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _json(self, status, payload):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length))
        except ValueError:
            return {}

    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/health":
            self._json(200, {"status": "ok"})
        elif url.path in ("/v1/models", "/models"):
            self._json(200, {"object": "list", "data": [{"id": m, "object": "model"} for m in snapshot()["by_model"]]})
        elif url.path == "/stats":
            self._json(200, snapshot())
        elif url.path == "/events":
            limit = int(parse_qs(url.query).get("limit", ["200"])[0])
            with _lock:
                events = list(_events)[-limit:]
            self._json(200, {"events": events})
        else:
            self._json(404, {"error": f"no route for GET {url.path}"})

    def do_POST(self):
        url = urlparse(self.path)
        if url.path == "/stats/reset":
            reset()
            self._json(200, {"status": "reset"})
            return
        if url.path not in ("/v1/chat/completions", "/chat/completions"):
            self._json(404, {"error": f"no route for POST {url.path}"})
            return

        body = self._body()
        model = body.get("model") or "unknown"
        record(url.path, model, classify(body), caller_name(self.client_address[0]))
        if LATENCY_MS:
            time.sleep(LATENCY_MS / 1000)

        with _lock:
            n = _counts["total"]
        payload = completion(model, n)
        if not body.get("stream"):
            self._json(200, payload)
            return

        include_usage = bool((body.get("stream_options") or {}).get("include_usage"))
        chunks = [
            {"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None},
            {"index": 0, "delta": {"content": REPLY}, "finish_reason": None},
            {"index": 0, "delta": {}, "finish_reason": "stop"},
        ]
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        base = {"id": payload["id"], "object": "chat.completion.chunk", "created": payload["created"], "model": model}
        for choice in chunks:
            self.wfile.write(f"data: {json.dumps({**base, 'choices': [choice]})}\n\n".encode())
        if include_usage:
            self.wfile.write(f"data: {json.dumps({**base, 'choices': [], 'usage': payload['usage']})}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()
        self.close_connection = True


if __name__ == "__main__":
    print(f"mock-llm listening on :{PORT} (usage per call: {PROMPT_TOKENS} in / {COMPLETION_TOKENS} out)", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
