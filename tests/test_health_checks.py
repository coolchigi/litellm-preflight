"""Calculator tests. The harness counts come from LiteLLM v1.104.0 runs: 3 deployments,
health_check_interval 20, health calls counted at a mock model server over 120s."""

import pytest

from litellm_preflight.config import ConfigError
from litellm_preflight.health_checks import analyze, count_deployments, describe, interval_problem, redis_source
from litellm_preflight.topology import Topology


def config(models=3, **general):
    return {
        "model_list": [{"model_name": f"m{i}", "litellm_params": {"model": f"openai/m{i}"}} for i in range(models)],
        "general_settings": {"background_health_checks": True, "health_check_interval": 20, **general},
    }


REDIS = {"coordination_redis": {"host": "redis", "port": 6379}}


def per_120s(report):
    low, high = report.probes_per_day()
    assert low == high
    return low * 120 / 86_400


@pytest.mark.parametrize("topology, measured", [
    (Topology(replicas=3), 54),                    # classic, 3 replicas
    (Topology(workers=2), 36),                     # classic, 1 replica x 2 workers
    (Topology(replicas=3, split=True), 72),        # split, 3 gateways + the backend
    (Topology(workers=2, split=True), 54),         # split, 2 gateway workers + the backend
])
def test_matches_harness_counts(topology, measured):
    assert per_120s(analyze(config(), topology)) == measured


def test_shared_with_redis_probes_once_per_ttl():
    report = analyze(config(use_shared_health_check=True, **REDIS), Topology(replicas=3))
    assert report.probes_per_day() == (3 * 288, 3 * 288)  # harness: a round of 3 every ~300s


def test_shared_ttl_matched_to_interval():
    report = analyze(config(use_shared_health_check=True, **REDIS), Topology(replicas=3), shared_ttl=20)
    assert per_120s(report) == 18  # harness: 18 calls in 120s


def test_shared_without_redis_falls_back_to_every_process():
    report = analyze(config(use_shared_health_check=True), Topology(replicas=3))
    assert not report.coordinated
    assert per_120s(report) == 54
    assert any("sets no Redis" in line for line in describe(report))


def test_shared_with_redis_from_env_flag():
    report = analyze(config(use_shared_health_check=True), Topology(replicas=3), redis_from_env=True)
    assert report.coordinated


def test_shared_interval_longer_than_ttl_is_a_range():
    report = analyze(config(use_shared_health_check=True, health_check_interval=3600, **REDIS), Topology(replicas=6))
    low, high = report.probes_per_day()
    assert low == 3 * 24            # every process in step: 1 round per interval
    assert high == 3 * 144          # out of step: up to 6 rounds per interval, capped at 1 per ttl
    assert any("depends on how their start times line up" in line for line in describe(report))


def test_shared_interval_longer_than_ttl_single_process_is_exact():
    report = analyze(config(use_shared_health_check=True, health_check_interval=3600, **REDIS), Topology())
    assert report.probes_per_day() == (72, 72)


@pytest.mark.parametrize("raw", ["300", 30.5, 0, -60, None, "os.environ/HC_INTERVAL", [60], "five"])
def test_intervals_litellm_rejects_mean_the_loop_never_runs(raw):
    report = analyze(config(health_check_interval=raw), Topology(replicas=3))
    assert report.loop_problem is not None
    assert report.probes_per_day() == (0, 0)
    assert "never run" in describe(report)[-1]


def test_interval_true_is_one_second_like_litellm():
    assert interval_problem(True) is None


def test_default_interval_is_300():
    cfg = config()
    del cfg["general_settings"]["health_check_interval"]
    assert analyze(cfg, Topology()).probes_per_day() == (3 * 288, 3 * 288)


def test_off():
    report = analyze(config(background_health_checks=False), Topology(replicas=3))
    assert report.probes_per_day() == (0, 0)
    assert describe(report)[-1].startswith("Background health checks are off")


def test_quoted_false_is_on_and_flagged():
    report = analyze(config(background_health_checks="false"), Topology())
    assert report.enabled
    assert any("treats as on" in note for note in report.notes)


def test_env_reference_resolved_from_shell(monkeypatch):
    monkeypatch.setenv("BG_HC", "false")
    report = analyze(config(background_health_checks="os.environ/BG_HC"), Topology())
    assert not report.enabled
    monkeypatch.setenv("BG_HC", "true")
    assert analyze(config(background_health_checks="os.environ/BG_HC"), Topology()).enabled


def test_env_reference_unset_counts_as_on(monkeypatch):
    monkeypatch.delenv("BG_HC", raising=False)
    report = analyze(config(background_health_checks="os.environ/BG_HC"), Topology())
    assert report.enabled
    assert any("isn't set here" in note for note in report.notes)


def test_deployment_filters():
    cfg = config(models=0)
    cfg["model_list"] = [
        {"model_name": "a", "litellm_params": {"model": "openai/a"}},
        {"model_name": "a", "litellm_params": {"model": "openai/a"}},                       # identical: deduped
        {"model_name": "b", "litellm_params": {"model": "openai/b"}, "model_info": {"id": "x"}},
        {"model_name": "c", "litellm_params": {"model": "openai/c"}, "model_info": {"id": "x"}},  # same id
        {"model_name": "d", "litellm_params": {"model": "openai/d"},
         "model_info": {"disable_background_health_check": True}},
        {"model_name": "e", "litellm_params": {"model": "auto_router/e"}},
        {"model_name": "f", "litellm_params": {"model": "openai/f"}},
    ]
    d = count_deployments(cfg)
    assert (d.probed, d.duplicates, d.disabled, d.auto_router) == (3, 2, 1, 1)


def test_model_groups_router_settings_wins():
    cfg = config(background_health_check_model_groups=["m0", "m1"])
    cfg["router_settings"] = {"background_health_check_model_groups": ["m2"]}
    d = count_deployments(cfg)
    assert (d.probed, d.outside_groups) == (1, 2)


@pytest.mark.parametrize("groups", ["m0", 5, [["m0"]], [1]])
def test_model_groups_must_be_a_list_of_names(groups):
    with pytest.raises(ConfigError, match="list of model names"):
        count_deployments(config(background_health_check_model_groups=groups))


def test_disable_flag_from_unset_env_is_probed(monkeypatch):
    monkeypatch.delenv("DISABLE_A", raising=False)
    cfg = config(models=1)
    cfg["model_list"][0]["model_info"] = {"disable_background_health_check": "os.environ/DISABLE_A"}
    d = count_deployments(cfg)
    assert (d.probed, d.disabled_unknown) == (1, 1)
    monkeypatch.setenv("DISABLE_A", "true")
    assert count_deployments(cfg).disabled == 1


@pytest.mark.parametrize("bad, message", [
    ({"model_list": {"a": 1}}, "model_list should be a list"),
    ({"model_list": ["gpt-4"]}, r"model_list\[0\] should be a mapping"),
    ({"model_list": [None]}, r"model_list\[0\] should be a mapping"),
    ({"model_list": [{"model_info": "x"}]}, r"model_list\[0\].model_info should be a mapping"),
    ({"general_settings": ["x"]}, "general_settings should be a mapping"),
])
def test_bad_shapes_raise_config_error(bad, message):
    with pytest.raises(ConfigError, match=message):
        analyze(bad, Topology())


def test_media_deployments_are_called_out():
    cfg = config(models=2)
    cfg["model_list"][0]["model_info"] = {"mode": "image_generation"}
    lines = describe(analyze(cfg, Topology()))
    assert any("1 image_generation" in line for line in lines)


def test_redis_sources():
    assert redis_source({"general_settings": REDIS}) == "general_settings.coordination_redis"
    assert redis_source({"litellm_settings": {"cache": True}}) == "a Redis litellm_settings.cache"
    assert redis_source({"litellm_settings": {"cache": True, "cache_params": {"type": "redis-semantic"}}}) is None
    assert redis_source({"general_settings": {"coordination_redis": {}}}) is None


def test_db_deployments_add_to_config_count():
    report = analyze(config(store_model_in_db=True), Topology(), db_deployments=2)
    assert report.total_deployments == 5


def test_store_model_in_db_prompts_for_db_count():
    lines = describe(analyze(config(models=0, store_model_in_db=True), Topology(replicas=2)))
    assert any("--db-deployments" in line for line in lines)
    assert lines[-1] == "Nothing to probe, so no calls."


def test_restarts_add_a_round_each():
    report = analyze(config(), Topology(), restarts_per_day=10)
    assert report.probes_per_day()[0] == 3 * 4320 + 30


def test_topology_counts_collector_and_backend_workers():
    assert Topology(replicas=2, workers=3, collector=True).proxy_processes() == 2 * 3 + 2
    assert Topology(replicas=2, split=True, backend_replicas=3, backend_workers=2).proxy_processes() == 2 + 6
    assert Topology(backend_replicas=5).proxy_processes() == 1  # backend only counts with split


def test_model_list_null_counts_zero():
    assert count_deployments({"model_list": None}).probed == 0


def test_topology_describe():
    assert Topology().describe() == "1 replica x 1 worker"
    assert Topology(replicas=2, workers=3, collector=True).describe() == "2 replicas x 3 workers + 2 collector sidecars"
    assert (Topology(split=True, backend_replicas=2, backend_workers=2).describe()
            == "1 replica x 1 worker + 2 backend replicas x 2 workers")
    assert Topology(split=True, backend_replicas=0).describe() == "1 replica x 1 worker"


def test_output_lines_for_less_common_paths(monkeypatch):
    monkeypatch.delenv("DISABLE_A", raising=False)
    cfg = config(health_check_interval=20)
    cfg["model_list"][0]["model_info"] = {"disable_background_health_check": "os.environ/DISABLE_A"}
    lines = describe(analyze(cfg, Topology(replicas=2), restarts_per_day=4), cost_per_probe=0)
    text = "\n".join(lines)
    assert "env var that isn't set here" in text
    assert "That includes 4 process starts a day" in text
    assert "Cost at $0 per probe: $0 a day, $0 per 30 days" in text
    assert "probes would also slow from every 20s to about every 300s" in text


def test_shared_without_redis_fix_line():
    lines = describe(analyze(config(use_shared_health_check=True), Topology(replicas=2)))
    assert lines[-1] == "Fix: give the proxy a Redis every process can reach. Then 1 process probes for all."


def test_shared_range_cost_line():
    report = analyze(config(use_shared_health_check=True, health_check_interval=3600, **REDIS), Topology(replicas=6))
    lines = describe(report, cost_per_probe=0.01)
    assert "Probes per day: about 72 to 432" in lines
    assert "Cost at $0.01 per probe: $0.72 to $4.32 a day, $21.60 to $129.60 per 30 days" in lines


def test_shared_env_note_only_when_enabled(monkeypatch):
    monkeypatch.delenv("SHARED", raising=False)
    on = analyze(config(use_shared_health_check="os.environ/SHARED", **REDIS), Topology())
    off = analyze(config(background_health_checks=False, use_shared_health_check="os.environ/SHARED"), Topology())
    assert any("use_shared_health_check comes from env var SHARED" in n for n in on.notes)
    assert off.notes == []
