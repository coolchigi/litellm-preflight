# LiteLLM repro harness

Runs LiteLLM against a fake model server, so you can reproduce production behavior on your laptop without paying for a single token. There are 2 stacks:

- **classic**: the single `litellm` proxy image
- **split**: the `litellm-gateway`, `litellm-backend` and `litellm-migrations` images that LiteLLM's Terraform modules deploy

Both get Postgres, Redis, a load balancer and the mock. Everything is pinned to LiteLLM v1.104.0.

## Run it

You need Docker and Python 3.9+. The first pull is big (the split images are about 4 GB each).

```bash
./harness up classic
./harness smoke classic
./harness down classic
```

Swap `classic` for `split` to run the other one. They use different ports, so both can run at once.

| | classic | split |
|---|---|---|
| inference | 127.0.0.1:4000 | 127.0.0.1:4100 |
| admin (keys, teams, spend) | 127.0.0.1:4000 | 127.0.0.1:4101 |
| mock stats | 127.0.0.1:9000/stats | 127.0.0.1:9100/stats |

The master key lives in `.env`, which gets generated on first run. Keep that file. A new salt key makes encrypted values already in Postgres unreadable.

## Knobs

Env vars read by `./harness up`:

| Variable | Default | What it does |
|---|---|---|
| `LITELLM_VERSION` | `v1.104.0` | tag for every LiteLLM image |
| `PROXY_REPLICAS` | `1` | proxy (or gateway) containers behind the load balancer |
| `NUM_WORKERS` | `1` | uvicorn workers per container |
| `LITELLM_CONFIG` | `proxy.yaml` | which file in `config/` to load |
| `MOCK_PROMPT_TOKENS`, `MOCK_COMPLETION_TOKENS` | `1000`, `500` | usage the mock reports per call |
| `MOCK_LATENCY_MS` | `0` | delay before the mock answers |

Pass the same `PROXY_REPLICAS` to `./harness smoke` so it checks every replica gets traffic.

## The mock

`mock/server.py` answers every chat completion with "ok" and fixed usage. With the base config's pricing, each call costs exactly $0.002.

It counts each call 3 ways: by model, by kind, and by caller. Kind is `health` when the prompt is one of the messages LiteLLM's health checks send and `user` otherwise. Caller is the container that made the call, which is how you count traffic per replica.

```bash
./harness stats classic        # counts so far
./harness events classic 20    # last 20 calls, with timestamps
./harness reset classic        # back to zero
```

## Scenarios

One script per finding in `scenarios/`. Each brings the stack up per variant, measures, tears it down, and writes raw results to `results/<finding>/<stack>-<variant>.json`. What they found is in [FINDINGS.md](../FINDINGS.md).

```bash
python3 scenarios/health_checks.py classic   # ~15 min, the shared variant waits out a 300s cache
python3 scenarios/multiproc.py classic       # ~10 min
python3 scenarios/spend_gap.py classic       # ~5 min
python3 scenarios/auth_cache.py classic      # ~8 min
python3 scenarios/budget_reset.py classic    # ~15 min, waits for the budget reset job
```

Swap in `split` for the other stack. Classic and split can run at the same time, but 2 scenarios on the same stack can't. Pass variant names after the stack to run just those.

## Adding a scenario

Change one thing per scenario. Put a copy of `config/proxy.yaml` with your change in `config/` and point `LITELLM_CONFIG` at it. For env vars, memory limits or extra services, add a compose override with `-f`:

```bash
LITELLM_CONFIG=health-checks.yaml PROXY_REPLICAS=3 ./harness up classic -f scenarios/health-checks/compose.yaml
```

## Things the harness already tripped over

- v1.104.0 won't start with a publicly known master key like `sk-1234`. That's why `.env` gets generated.
- `/metrics` needs a key by default (`require_auth_for_metrics_endpoint`), so your scraper needs one too.
- `/metrics` redirects to `/metrics/`. If a proxy in front drops the port from the Host header, the redirect points at the wrong port. nginx's `$host` does this, so the load balancer here uses `$http_host`.
