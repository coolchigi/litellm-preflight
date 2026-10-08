"""How many provider calls LiteLLM's background health checks make.

Mirrors LiteLLM v1.104.0 (litellm/proxy/proxy_server.py, _run_background_health_check):

- The loop runs in every process that runs proxy_server's startup (see topology.py).
- It starts only when general_settings.health_check_interval is a whole number above 0.
  Anything else (a string, a float, 0, an os.environ/ reference) and it never runs.
- Each round probes every deployment once, minus deployments with
  model_info.disable_background_health_check, auto_router/ deployments, duplicates
  (deduped by model id), and deployments outside background_health_check_model_groups.
  It runs right at startup, then sleeps the interval after each round.
- use_shared_health_check only applies when the proxy has Redis. One process then
  probes and the rest reuse its results for DEFAULT_SHARED_HEALTH_CHECK_TTL seconds.

Both defaults below can be changed with env vars of the same name on the proxy.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, List, Mapping, Optional, Tuple

from litellm_preflight.config import ConfigError, mapping
from litellm_preflight.topology import Topology

DEFAULT_INTERVAL = 300  # DEFAULT_HEALTH_CHECK_INTERVAL in litellm/constants.py
DEFAULT_SHARED_TTL = 300  # DEFAULT_SHARED_HEALTH_CHECK_TTL in litellm/constants.py
SECONDS_PER_DAY = 86_400
ENV_PREFIX = "os.environ/"
MEDIA_MODES = ("image_generation", "video_generation", "audio_speech")


@dataclass
class Deployments:
    probed: int = 0
    disabled: int = 0
    disabled_unknown: int = 0
    auto_router: int = 0
    duplicates: int = 0
    outside_groups: int = 0
    media: Dict[str, int] = field(default_factory=dict)


def _env_name(value: Any) -> Optional[str]:
    if isinstance(value, str) and value.startswith(ENV_PREFIX):
        return value[len(ENV_PREFIX):]
    return None


def setting(value: Any, name: str) -> Tuple[Any, Optional[str]]:
    """A config value as LiteLLM would see it, plus a note when that isn't what it looks like.

    LiteLLM swaps os.environ/NAME for the variable's value, turning "true" and "false" into
    booleans and leaving anything else as text. It checks on/off settings by truthiness, so
    the text "false" in YAML counts as on.
    """
    env = _env_name(value)
    if env:
        actual = os.environ.get(env)
        if actual is None:
            return value, (f"{name} comes from env var {env}, which isn't set here. "
                           "Counting it as set to something other than \"false\".")
        resolved = {"true": True, "false": False}.get(actual, actual)
        return resolved, f"{name} comes from env var {env}, read from this shell as {actual!r}."
    if isinstance(value, str) and value.strip().lower() in ("false", "no", "off", "0", ""):
        return value, f"{name} is the text {value!r}, which LiteLLM treats as on. Use false without quotes."
    return value, None


def _model_groups(config: Mapping[str, Any]) -> Optional[set]:
    # router_settings is applied after general_settings when the router is built, so it wins
    for section in ("router_settings", "general_settings"):
        groups = mapping(config.get(section), section).get("background_health_check_model_groups")
        if groups is None:
            continue
        if not isinstance(groups, list) or not all(isinstance(g, str) for g in groups):
            raise ConfigError(f"{section}.background_health_check_model_groups should be a list of model names")
        return set(groups)
    return None


def count_deployments(config: Mapping[str, Any]) -> Deployments:
    model_list = config.get("model_list")
    if model_list is None:
        model_list = []
    if not isinstance(model_list, list):
        raise ConfigError(f"model_list should be a list, got {type(model_list).__name__}")
    groups = _model_groups(config)
    result = Deployments()
    seen = set()
    for i, deployment in enumerate(model_list):
        if not isinstance(deployment, Mapping):
            raise ConfigError(f"model_list[{i}] should be a mapping, got {type(deployment).__name__}")
        model_info = mapping(deployment.get("model_info"), f"model_list[{i}].model_info")
        params = mapping(deployment.get("litellm_params"), f"model_list[{i}].litellm_params")
        disable = model_info.get("disable_background_health_check")
        env = _env_name(disable)
        if env is not None:
            actual = os.environ.get(env)
            if actual is None:
                result.disabled_unknown += 1  # unset means None to LiteLLM, so it gets probed
                disable = None
            else:
                disable = {"true": True, "false": False}.get(actual, actual)
        if disable:
            result.disabled += 1
            continue
        model = params.get("model")
        if isinstance(model, str) and model.startswith("auto_router/"):
            result.auto_router += 1
            continue
        if groups is not None and deployment.get("model_name") not in groups:
            result.outside_groups += 1
            continue
        identity = model_info.get("id")
        key = (str(identity) if identity is not None
               else json.dumps([deployment.get("model_name"), params], sort_keys=True, default=str))
        if key in seen:
            result.duplicates += 1
            continue
        seen.add(key)
        result.probed += 1
        mode = model_info.get("mode")
        if mode in MEDIA_MODES:
            result.media[mode] = result.media.get(mode, 0) + 1
    return result


def redis_source(config: Mapping[str, Any]) -> Optional[str]:
    """Where the proxy gets the Redis that shared health checks need, if the config says."""
    general = mapping(config.get("general_settings"), "general_settings")
    coordination = general.get("coordination_redis")
    if isinstance(coordination, Mapping) and any(
            coordination.get(k) for k in ("host", "url", "startup_nodes", "sentinel_nodes")):
        return "general_settings.coordination_redis"
    litellm_settings = mapping(config.get("litellm_settings"), "litellm_settings")
    if litellm_settings.get("cache"):
        cache_type = mapping(litellm_settings.get("cache_params"), "litellm_settings.cache_params").get("type")
        if cache_type in (None, "redis"):
            return "a Redis litellm_settings.cache"
    return None


def interval_problem(raw: Any) -> Optional[str]:
    """Why LiteLLM won't start the loop with this interval, or None if it will."""
    if isinstance(raw, int) and raw > 0:  # bool counts too, same as in LiteLLM
        return None
    env = _env_name(raw)
    if env:
        return (f"health_check_interval comes from env var {env}, and LiteLLM reads env values as text. "
                "It only starts the loop for a whole number of seconds, so the loop never runs.")
    if isinstance(raw, str):
        return (f"health_check_interval is the text {raw!r}. LiteLLM only starts the loop for a whole number "
                "of seconds above 0, so it never runs. Remove the quotes.")
    shown = "empty" if raw is None else repr(raw)
    return (f"health_check_interval is {shown}. LiteLLM only starts the loop for a whole number "
            "of seconds above 0, so it never runs.")


@dataclass
class HealthCheckReport:
    topology: Topology
    enabled: bool
    deployments: Deployments
    interval: Optional[int] = None
    loop_problem: Optional[str] = None
    shared: bool = False
    redis: Optional[str] = None
    shared_ttl: int = DEFAULT_SHARED_TTL
    store_model_in_db: bool = False
    db_deployments: int = 0
    restarts_per_day: int = 0
    notes: List[str] = field(default_factory=list)

    @property
    def total_deployments(self) -> int:
        return self.deployments.probed + self.db_deployments

    @property
    def runs_loop(self) -> bool:
        return self.enabled and self.loop_problem is None and self.total_deployments > 0

    @property
    def coordinated(self) -> bool:
        return self.shared and self.redis is not None

    def rounds_per_day(self, period: int) -> int:
        return max(1, round(SECONDS_PER_DAY / period))

    def one_loop_per_day(self) -> int:
        return self.total_deployments * self.rounds_per_day(self.interval) if self.runs_loop else 0

    def probes_per_day(self) -> Tuple[int, int]:
        """(low, high) probes per day. Equal unless shared checks make the count depend on timing."""
        if not self.runs_loop:
            return 0, 0
        n = self.total_deployments
        if not self.coordinated:
            total = self.topology.proxy_processes() * self.one_loop_per_day() + self.restarts_per_day * n
            return total, total
        ttl_rounds = self.rounds_per_day(self.shared_ttl)
        if self.interval <= self.shared_ttl:
            return n * ttl_rounds, n * ttl_rounds
        interval_rounds = self.rounds_per_day(self.interval)
        high_rounds = min(self.topology.proxy_processes() * interval_rounds, ttl_rounds)
        return n * interval_rounds, n * max(interval_rounds, high_rounds)


def analyze(config: Mapping[str, Any], topology: Topology, shared_ttl: int = DEFAULT_SHARED_TTL,
            db_deployments: int = 0, redis_from_env: bool = False, restarts_per_day: int = 0) -> HealthCheckReport:
    general = mapping(config.get("general_settings"), "general_settings")
    enabled, enabled_note = setting(general.get("background_health_checks", False), "background_health_checks")
    shared, shared_note = setting(general.get("use_shared_health_check", False), "use_shared_health_check")
    store_db, _ = setting(general.get("store_model_in_db", False), "store_model_in_db")
    interval_raw = general.get("health_check_interval", DEFAULT_INTERVAL)
    problem = interval_problem(interval_raw)
    report = HealthCheckReport(
        topology=topology,
        enabled=bool(enabled),
        deployments=count_deployments(config),
        interval=None if problem else int(interval_raw),
        loop_problem=problem,
        shared=bool(shared),
        redis="REDIS_HOST or REDIS_URL in the proxy's environment (--redis-env)" if redis_from_env
        else redis_source(config),
        shared_ttl=shared_ttl,
        store_model_in_db=bool(store_db),
        db_deployments=db_deployments,
        restarts_per_day=restarts_per_day,
        notes=[n for n in (enabled_note, shared_note if enabled else None) if n],
    )
    return report


def _count(n: int) -> str:
    return f"{n:,}"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _plural_count(n: int, word: str) -> str:
    return f"{n:,} {word}" if n == 1 else f"{n:,} {word}s"


def _usd(amount: float) -> str:
    if amount == 0:
        return "$0"
    if amount < 0.01:
        return "under $0.01"
    return f"${amount:,.2f}"


def _price(amount: float) -> str:
    return "$" + format(Decimal(repr(amount)).normalize(), "f")


def _range(low: int, high: int, fmt=_count) -> str:
    return fmt(low) if low == high else f"{fmt(low)} to {fmt(high)}"


def describe(report: HealthCheckReport, cost_per_probe: Optional[float] = None) -> List[str]:
    lines: List[str] = list(report.notes)
    if not report.enabled:
        return lines + ["Background health checks are off (general_settings.background_health_checks), "
                        "so they make no calls."]
    if report.loop_problem:
        lines.append(f"Background health checks are on but never run. {report.loop_problem}")
        return lines

    d = report.deployments
    skipped = [f"{n} {why}" for n, why in (
        (d.disabled, "with disable_background_health_check"),
        (d.auto_router, "auto_router (never probed)"),
        (d.duplicates, "duplicate"),
        (d.outside_groups, "outside background_health_check_model_groups"),
    ) if n]
    probed = _plural_count(report.total_deployments, "deployment")
    if report.db_deployments:
        probed += f" ({report.db_deployments} from the database)"
    lines.append(f"Background health checks: on, every {report.interval}s, {probed} probed"
                 + (f". Skipped: {', '.join(skipped)}." if skipped else ""))
    if d.disabled_unknown:
        lines.append(f"{_plural_count(d.disabled_unknown, 'deployment')} set disable_background_health_check "
                     "from an env var that isn't set here. LiteLLM probes those when the variable is unset, "
                     "so they're counted.")
    if report.store_model_in_db and not report.db_deployments:
        lines.append("store_model_in_db is on, so models added through the API or UI get probed too. "
                     "Count them with --db-deployments N.")
    if report.total_deployments == 0:
        lines.append("Nothing to probe, so no calls.")
        return lines

    processes = report.topology.proxy_processes()
    low, high = report.probes_per_day()
    if report.coordinated:
        lines.append(f"Shared health checks: on, with Redis from {report.redis}. 1 process probes "
                     f"and the others reuse its results for {report.shared_ttl}s "
                     "(DEFAULT_SHARED_HEALTH_CHECK_TTL).")
        if report.interval < report.shared_ttl:
            lines.append(f"So your {report.interval}s interval becomes about {report.shared_ttl}s.")
        lines.append(f"Probes per day: about {_range(low, high)}")
        if low != high:
            lines.append(f"Each of the {processes} processes checks on its own {report.interval}s schedule, and any "
                         f"process that wakes after the {report.shared_ttl}s cache expires probes again. Where in "
                         "that range you land depends on how their start times line up.")
    else:
        if report.shared:
            lines.append("Shared health checks are on, but this config sets no Redis "
                         "(general_settings.coordination_redis or a Redis litellm_settings.cache). Without Redis, "
                         "every process probes on its own. If the proxy gets Redis from REDIS_HOST or REDIS_URL, "
                         "pass --redis-env.")
        lines.append(f"Processes running the loop: {processes} ({report.topology.describe()})."
                     + (" Each one probes on its own." if processes > 1 else ""))
        lines.append(f"Probes per day: up to {_count(high)}")
        if report.restarts_per_day:
            lines.append(f"That includes {_plural(report.restarts_per_day, 'process start')} a day, "
                         "each probing every deployment right away.")
        if processes > 1:
            lines.append(f"1 loop would make {_count(report.one_loop_per_day())}. "
                         f"You're making {processes}x that.")

    if cost_per_probe is not None:
        per_day = _range(low, high, lambda n: _usd(n * cost_per_probe))
        per_month = _range(low, high, lambda n: _usd(n * cost_per_probe * 30))
        lines.append(f"Cost at {_price(cost_per_probe)} per probe: {per_day} a day, {per_month} per 30 days")

    if d.media:
        kinds = ", ".join(f"{n} {mode}" for mode, n in sorted(d.media.items()))
        lines.append(f"Watch out: {kinds}. Those probes generate real media every round, so each one costs far "
                     "more than a chat probe. Set model_info.disable_background_health_check: true on them "
                     "if you don't need it.")

    if not report.coordinated and processes > 1:
        if report.shared:
            lines.append("Fix: give the proxy a Redis every process can reach. Then 1 process probes for all.")
        else:
            fix = ("Fix: set general_settings.use_shared_health_check: true, with a Redis every process can "
                   "reach (general_settings.coordination_redis, a Redis litellm_settings.cache, or REDIS_HOST). "
                   "Then 1 process probes for all.")
            if report.interval < report.shared_ttl:
                fix += (f" Shared results are reused for {report.shared_ttl}s, so probes would also slow from every "
                        f"{report.interval}s to about every {report.shared_ttl}s.")
            lines.append(fix)
    return lines
