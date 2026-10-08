# Changelog

All notable changes to this project are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [PEP 440](https://peps.python.org/pep-0440/).

## [0.1.0] - 2026-10-07

First release.

### Added

- `litellm-doctor health-checks`: counts the provider calls LiteLLM's background health checks make per day, with an optional cost.
  - Counts every process that runs the loop: replicas, uvicorn workers, the collector sidecar, and the backend on the split images.
  - Flags checks that are on but never run, because `health_check_interval` isn't a whole number above 0.
  - Mirrors LiteLLM's deployment filters: `disable_background_health_check`, `auto_router/` deployments, duplicates, and `background_health_check_model_groups` from `general_settings` or `router_settings`.
  - Models shared health checks: the Redis requirement, the 300s result reuse, and the range you get when the interval is longer than that.
  - Reads `config.yaml` (following `include:`), Helm values files for the `litellm-helm` and split `litellm` charts, and Kubernetes ConfigMaps.
  - Resolves `os.environ/` references from your shell and says when one isn't set.
- `python -m litellm_doctor` works the same as the `litellm-doctor` command.

[0.1.0]: https://github.com/coolchigi/litellm-doctor/releases/tag/v0.1.0
