# litellm-preflight

[![CI](https://github.com/coolchigi/litellm-preflight/actions/workflows/ci.yml/badge.svg)](https://github.com/coolchigi/litellm-preflight/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/litellm-preflight)](https://pypi.org/project/litellm-preflight/)
[![Python](https://img.shields.io/pypi/pyversions/litellm-preflight)](https://pypi.org/project/litellm-preflight/)

Reads your LiteLLM proxy config and flags settings that cost money or break once you run more than one process. It runs offline. It never connects to your proxy or sends anything anywhere.

Every check matches how LiteLLM v1.104.0 actually behaves, and was reproduced against real LiteLLM containers in a local [harness](https://github.com/coolchigi/litellm-preflight/tree/main/harness) before it shipped. Today there's 1 check, `health-checks`.

litellm-preflight is an independent project. It isn't affiliated with BerriAI or the LiteLLM project.

## Install

```bash
pip install litellm-preflight
```

Or run it without installing, with `uvx litellm-preflight` or `pipx run litellm-preflight`. It needs Python 3.9 or newer, and its only dependency is PyYAML.

## health-checks: what background health checks really cost

With `background_health_checks: true`, LiteLLM probes every deployment in your `model_list` once per `health_check_interval`. It does that in **every process** that runs the proxy: each replica, each uvicorn worker inside it, the collector sidecar if you run one, and on the split `litellm-gateway`/`litellm-backend` images, each backend pod too. 3 replicas with 2 workers make 6x the calls you'd expect, and each probe is a real request to your provider, billed like any other.

For a config with 20 deployments and `background_health_checks: true`, on 3 replicas with 2 workers each:

```bash
litellm-preflight health-checks config.yaml --replicas 3 --workers 2 --cost-per-probe 0.0003
```

```text
Config: config.yaml

Background health checks: on, every 300s, 20 deployments probed
Processes running the loop: 6 (3 replicas x 2 workers). Each one probes on its own.
Probes per day: up to 34,560
1 loop would make 5,760. You're making 6x that.
Cost at $0.0003 per probe: $10.37 a day, $311.04 per 30 days
Fix: set general_settings.use_shared_health_check: true, with a Redis every process can reach (general_settings.coordination_redis, a Redis litellm_settings.cache, or REDIS_HOST). Then 1 process probes for all.
```

It also catches checks that are on but never run. LiteLLM only starts the loop when `health_check_interval` is a whole number of seconds above 0, so a quoted `"300"`, a `30.5` or an `os.environ/` reference silently turns health checks off.

### What to pass it

- **Your `config.yaml`**, the file the proxy loads with `--config`. Files pulled in with `include:` are merged the same way LiteLLM merges them.
- **A Helm values file** for the [`litellm-helm`](https://github.com/BerriAI/litellm/tree/main/helm/litellm-helm) or split [`litellm`](https://github.com/BerriAI/litellm/tree/main/helm/litellm) chart. It reads the proxy config from it, and replicas, workers, autoscaling minimums, the backend and the collector sidecar too, so you can usually skip the flags.
- **A Kubernetes ConfigMap**, from `kubectl get configmap <name> -n <namespace> -o yaml > cm.yaml`.

Flags always win over what a values file says.

### Finding the numbers on Kubernetes

- `--replicas`: the pod count of the proxy (or gateway) Deployment, from `kubectl get deploy -n <namespace>`. With an HPA it moves during the day, so pass your usual count (`kubectl get hpa -n <namespace>`).
- `--workers`: the container's `--num_workers` argument or `NUM_WORKERS` env var. In the charts that's `numWorkers` or `gateway.numWorkers`. Unset means 1.
- `--backend-replicas`: the pod count of the backend Deployment, on the split chart.

### Working out --cost-per-probe

A chat probe sends a short fixed prompt ("Hey how's it going?" or "What's 1 + 1?") with `max_tokens` set to 16 by default. So 1 probe costs about 20 input tokens plus up to 16 output tokens. At $3 and $15 per million tokens, that's about $0.0003.

That's a floor for some deployments:

- Reasoning models can get a bigger `max_tokens` (`health_check_max_tokens_reasoning`), and wildcard routes like `openai/*` get no cap.
- `image_generation`, `video_generation` and `audio_speech` deployments generate real media on every probe. The report calls them out.
- A failing probe can be retried, so it can cost up to 3 requests.

### Options

| Flag | Default | What it's for |
|---|---|---|
| `--replicas N` | 1 | Proxy pods, or gateway pods with `--split` |
| `--workers N` | 1 | Uvicorn workers per pod (`--num_workers` or `NUM_WORKERS`) |
| `--split` | off | You run the `litellm-gateway` and `litellm-backend` images |
| `--backend-replicas N` | 1 | Backend pods, with `--split` |
| `--backend-workers N` | 1 | Uvicorn workers per backend pod, with `--split` |
| `--collector` | off | Each proxy pod runs the collector sidecar, which runs its own loop |
| `--db-deployments N` | 0 | Deployments added through the API or UI (`store_model_in_db`), added to the config's count |
| `--redis-env` | off | The proxy gets Redis from `REDIS_HOST` or `REDIS_URL`, which a config can't show |
| `--shared-ttl SECONDS` | 300 | `DEFAULT_SHARED_HEALTH_CHECK_TTL`, if you set that env var on the proxy |
| `--restarts-per-day N` | 0 | Process starts a day (deploys, autoscaling, `MAX_REQUESTS_BEFORE_RESTART`). Each start probes right away |
| `--cost-per-probe USD` | none | What 1 probe costs, to print a cost |

### Shared health checks

`use_shared_health_check: true` lets 1 process probe while the others reuse its results. Two catches, both reported:

- **It needs Redis.** Without one, LiteLLM quietly falls back to every process probing on its own.
- **Results are reused for 300s** (`DEFAULT_SHARED_HEALTH_CHECK_TTL`). An interval shorter than that becomes about 300s. With an interval longer than that, each process still wakes on its own schedule, so the count lands in a range that depends on how their start times line up.

### Limits

- **Counts are upper bounds for steady state.** LiteLLM sleeps the full interval after each round finishes, so slow probes stretch the real period.
- **The proxy's environment is invisible.** `os.environ/` references are resolved from your shell, and the report says when one isn't set there. `DEFAULT_HEALTH_CHECK_INTERVAL` on the proxy changes the default interval of 300s, and the tool assumes 300.
- **Shared rounds over 60s.** If a shared round takes longer than 60s (`DEFAULT_SHARED_HEALTH_CHECK_LOCK_TTL`), for example because a deployment hangs until its timeout, the other processes stop waiting and probe too.
- **Some deployments the router changes aren't modeled.** An allowlist that names an alias or routing group, deployments dropped by `supported_environments`, and invalid deployments the proxy skips at startup.
- **Hypercorn runs 1 process** whatever `--num_workers` says. Pass `--workers 1` if you use it.
- **1 price for every probe.**

## How it was validated

The [harness](https://github.com/coolchigi/litellm-preflight/tree/main/harness) runs LiteLLM v1.104.0 in Docker, both the classic image and the split images, against a mock model server that counts every call by container. With 3 deployments, a 20s interval and a 120s window it measured:

| Setup | Calls in 120s |
|---|---|
| 3 replicas | 54 |
| 1 replica x 2 workers | 36 |
| 3 split gateways + 1 backend | 72 |
| Shared, with Redis | a round of 3 every ~300s |
| Shared, with the TTL set to 20s | 18 |

The tests check the calculator against those numbers. [FINDINGS.md](https://github.com/coolchigi/litellm-preflight/blob/main/FINDINGS.md) has the full write-up, including the other LiteLLM behaviors the harness reproduced.

## Exit codes

`0` means the report printed. `2` means bad input: a file it can't read, a config LiteLLM would reject, or a bad flag. It doesn't fail on findings, so it's safe to run in CI.

The command line is the supported interface. Python imports may change before 1.0.

## Contributing

Seen LiteLLM break or cost money at scale? Open an [issue](https://github.com/coolchigi/litellm-preflight/issues) with your LiteLLM version, how you deploy it and what happened. A check gets added once it reproduces in the harness.

To report a security problem in litellm-preflight, see [SECURITY.md](https://github.com/coolchigi/litellm-preflight/blob/main/SECURITY.md).

## License

MIT
