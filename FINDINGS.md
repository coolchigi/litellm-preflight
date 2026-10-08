# What still reproduces on LiteLLM v1.104.0

Tested 2026-10-06 with the repro harness on both stacks: **classic** (the `litellm` image) and **split** (`litellm-gateway`, `litellm-backend`, `litellm-migrations`). Each variant ran once per stack. Raw results are in `harness/results/`, and source references are to the v1.104.0 tag.

| Finding | Classic | Split |
|---|---|---|
| Health checks run in every process | Reproduces: 3 replicas make 3x the calls | Worse: the backend runs its own loop, so 3 gateways make 4x |
| Multiproc files pile up with worker recycling | Reproduces. No memory growth at this scale | Same |
| Key blocked while `/key/info` shows it under budget | Reproduces, 25s | Reproduces, 13s |
| `proxy_batch_write_at: 60` lets a key overspend ([#43732](https://github.com/BerriAI/litellm/issues/43732)) | $0.016 recorded on a $0.01 budget | $0.020 on $0.01 |
| Budget windows are clock slots, reset waits for the job | Reproduces, blocked 616s past reset | Reproduces, 525s |
| Deleted keys come back with `enable_redis_auth_cache` | Fixed (still reproduces on v1.89.3) | Fixed |

## Health checks run in every process

With `background_health_checks: true`, every process starts its own loop at startup (`_run_background_health_check` in `litellm/proxy/proxy_server.py`). Counted at the mock over 120s with 3 models and `health_check_interval: 20`, where 1 loop makes 18 calls:

| Setup | Classic | Split |
|---|---|---|
| 3 replicas, 1 worker each | 54 (3x) | 72 (4x) |
| 1 replica, 2 workers | 36 (2x) | 54 (3x) |

The split backend made 18 calls on its own in both rows. `backend/main.py` reuses proxy_server's app and lifespan, so it starts the same loop even though it never serves inference.

`use_shared_health_check: true` stops the multiplying: 1 pod takes a Redis lock and probes for everyone. But results are reused for `DEFAULT_SHARED_HEALTH_CHECK_TTL` (300s in `litellm/constants.py`) before anyone probes again. With a 20s interval, the mock saw a round of probes at 0s, 300s and 600s, and nothing in between. Setting the TTL to 20 gave 18 calls in 120s, exactly 1x.

**Doctor rule:** probes per day = processes × models × 86,400 / interval, where processes counts every replica, every worker, and the backend on split. In shared mode, flag an interval below the TTL, because the TTL wins.

## Multiproc files pile up with worker recycling

2 workers, 3,000 requests. The recycle variant restarts each worker every 50 requests (`MAX_REQUESTS_BEFORE_RESTART` on classic, uvicorn's `--limit-max-requests` on split). Classic creates `PROMETHEUS_MULTIPROC_DIR` on its own when workers > 1 and prometheus is a callback (`_maybe_setup_prometheus_multiproc_dir` in `litellm/proxy/proxy_cli.py`). Split gets it from the env var, the way LiteLLM's Terraform module sets it.

| | Control (classic) | Recycle, classic | Recycle, split |
|---|---|---|---|
| Worker pids that wrote files | 2 | 56 | 55 |
| Files in the dir | 8 | 169 | 166 |
| Dir size | 512 KiB | 10.6 MiB | 10.4 MiB |
| `/metrics` latency, first to last round | 4 to 8 ms | 5 to 37 ms | 4 to 24 ms |
| `/metrics` body | 50 KB | 112 KB | 111 KB |
| Container memory, first to last round | 1541 to 1657 MiB | 1546 to 1457 MiB | 1428 to 1408 MiB |

Every dead worker leaves its counter, histogram and gauge files behind. LiteLLM wipes the directory only at startup (`wipe_directory` in `litellm/proxy/prometheus_cleanup.py`, plus `docker/component_entrypoint.sh` on split). `mark_process_dead` in prometheus_client 0.20.0 deletes live gauge files only, so nothing removes the rest while the container runs.

The body grows because gauges in `all` mode carry a `pid` label, so a dead worker's last values stay in `/metrics`. After 600 requests on classic, 11 gauges were exported once for each of 12 pids, 132 series. One of them is `litellm_remaining_api_key_budget_metric`, so stale budget values from dead workers sit next to live ones. Every restart adds series downstream too.

Memory didn't grow here. 55 restarts with 1 key and 1 model write small files. The production OOMs had far more restarts and far more label combinations, and this harness hasn't run a soak long enough to show the memory side. 13 (classic) and 25 (split) of the 3,000 requests failed during recycling, and I haven't looked into why.

**Doctor rule:** flag a multiproc dir (set, or created by workers > 1 with the prometheus callback) combined with worker recycling.

## A key is blocked while /key/info shows it under budget

2 replicas, a $0.01 key, $0.002 per call. Both stacks blocked it after exactly 5 calls with HTTP **422** `budget_exceeded`. v1.82.3 returns 400 for the same thing (checked on the classic stack). At that moment `/key/info` said $0.00 and the Redis counter said $0.01.

Enforcement reads the Redis counter first (`_virtual_key_max_budget_check` in `litellm/proxy/auth/auth_checks.py`, which calls `get_current_spend`). `/key/info` reads Postgres, which gets spend from the batch writer every `proxy_batch_write_at` seconds (10 by default). `/key/info` caught up after 25s on classic and 13s on split.

**Doctor rule:** explain the gap, and match budget alerts on 422 as well as 400.

## proxy_batch_write_at: 60 lets a key overspend

Same test with `proxy_batch_write_at: 60`. About 60s after the key was blocked, requests started getting through again. The probe sent 1 request a second, and 5 got through before it blocked again. By the time the test stopped watching, Postgres had recorded $0.016 on classic and $0.020 on split, for a $0.01 budget.

Why, from the code: the Redis spend counter had a 60s TTL in these runs (`redis-cli TTL`). When it expires before the batch write lands, `get_current_spend` in `litellm/proxy/proxy_server.py` reseeds it from Postgres (step 3 of the fallback chain in its docstring). Postgres still says $0, so the key gets a fresh budget. 5 calls at $0.002 is exactly 1 more budget, and then the counter was back at $0.01 and the key blocked again. With the default of 10s, the write landed before the counter expired in both runs.

60 is the value LiteLLM's Best Practices for Production page recommends for `proxy_batch_write_at`.

This is public as [BerriAI/litellm#43732](https://github.com/BerriAI/litellm/issues/43732), opened 29 Sep 2026 and reproduced by 2 others.

**Doctor rule:** flag `proxy_batch_write_at` of 60 or more, since the counter can expire before spend reaches Postgres.

## Budget windows are clock slots

`budget_reset_at` is set when the key is created, before any spend, and snaps to a clock boundary (`get_next_standardized_reset_time` in `litellm/litellm_core_utils/duration_parser.py`). Keys created at 07:02:12 UTC on Tuesday 6 Oct:

| budget_duration | Resets at (UTC) | First window |
|---|---|---|
| 2m | 07:04 | 1m 48s |
| 5m | 07:05 | 2m 48s |
| 1h | 08:00 | 58m |
| 1d | midnight | 17h |
| 7d | Monday 12 Oct, 00:00 | 5.7 days |
| 30d | 1 Nov, 00:00 | 25.7 days |
| 1mo | 1 Nov, 00:00 | 25.7 days |

`7d` and `30d` are special cases in `_handle_day_reset`: weekly on Monday, monthly on the 1st. Any other `Nd` counts N days from today's midnight. The function's docstring says N > 1 resets "every N days from now", which is wrong for both.

A key that hits its budget stays blocked past `budget_reset_at` until the reset job runs, every 597 to 605s (`PROXY_BUDGET_RESCHEDULER_MIN_TIME` and `MAX_TIME`). Measured: blocked 616s past reset on classic and 525s on split. `/key/info` reset in the same 5s probe both times, which points at the job. 616s is a bit over one interval, and I haven't pinned down why.

**Doctor rule:** show each key's next reset and the worst-case lag of one job interval. Warn on `7d` and `30d`.

## Deleted keys coming back with enable_redis_auth_cache

Fixed on v1.104.0, and the harness still catches it on v1.89.3. Same config on both (Redis through `litellm_settings.cache`, the only way v1.89.3 shares the auth cache), 2 replicas, deleted key probed with 2 requests every 5s for 3 minutes:

| | v1.89.3 | v1.104.0 |
|---|---|---|
| Requests through after delete | 72 of 72 | 12 of 72 |
| Last one through | 176s, the end of the test | 56s |
| Key object in Redis at the end | Back, with a fresh TTL | Gone |

On v1.89.3 the key object left Redis on delete and then reappeared, so a worker holding a stale copy wrote it back. I haven't bisected which release fixed it.

What still happens on v1.104.0, with or without the Redis auth cache: pods that didn't serve the delete keep accepting the key until their in-memory copy expires (60s, `user_api_key_cache_ttl`). On classic that was 1 of 2 replicas. On split it was every gateway, because deletes go to the backend. The production docs mention the 60s window. The split part is the one worth a rule.

**Doctor rule:** on split, a revoked key works on every gateway for up to `user_api_key_cache_ttl`.

## Not covered

- The OTel dimension cap. It's a Splunk Observability limit, and the harness has no Splunk.
- Memory growth from multiproc files, which needs a long soak.
- DB connection math, and the split gateway's built-in PgBouncer.

## Rerun

From `harness/`, with Docker running:

```bash
python3 scenarios/health_checks.py classic
python3 scenarios/multiproc.py classic
python3 scenarios/spend_gap.py classic
python3 scenarios/auth_cache.py classic
python3 scenarios/budget_reset.py classic
LITELLM_VERSION=v1.89.3 python3 scenarios/auth_cache.py classic redis-legacy
```

Swap `classic` for `split` to run the other stack.
