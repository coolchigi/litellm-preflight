"""How many proxy processes a deployment runs. Every check that depends on scale uses this."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Topology:
    """Replicas and workers. On the split images, replicas and workers describe the gateway.

    Every process that runs proxy_server's startup runs its own background jobs: each
    replica, each uvicorn worker in it, the collector sidecar when it's enabled, and
    on the split images each backend replica too (backend/main.py reuses proxy_server's
    app and startup).
    """

    replicas: int = 1
    workers: int = 1
    split: bool = False
    backend_replicas: int = 1
    backend_workers: int = 1
    collector: bool = False

    def proxy_processes(self) -> int:
        serving = self.replicas * self.workers
        collectors = self.replicas if self.collector else 0
        backend = self.backend_replicas * self.backend_workers if self.split else 0
        return serving + collectors + backend

    def describe(self) -> str:
        parts = [f"{_plural(self.replicas, 'replica')} x {_plural(self.workers, 'worker')}"]
        if self.collector:
            parts.append(_plural(self.replicas, "collector sidecar"))
        if self.split and self.backend_replicas:
            backend = _plural(self.backend_replicas, "backend replica")
            if self.backend_workers > 1:
                backend += f" x {_plural(self.backend_workers, 'worker')}"
            parts.append(backend)
        return " + ".join(parts)


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"
