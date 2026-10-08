"""Config loading: include merging, Helm values, ConfigMaps and bad files."""

import textwrap

import pytest

from litellm_doctor.config import ConfigError, load


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return path


def test_include_merges_like_litellm(tmp_path):
    # Lists are extended, everything else is replaced by the included value
    write(tmp_path / "models.yaml", """
        model_list:
          - model_name: b
            litellm_params: {model: openai/b}
    """)
    write(tmp_path / "settings.yaml", """
        general_settings:
          background_health_checks: true
    """)
    root = write(tmp_path / "config.yaml", """
        include: [models.yaml, settings.yaml]
        model_list:
          - model_name: a
            litellm_params: {model: openai/a}
        general_settings:
          health_check_interval: 60
    """)
    loaded = load(str(root))
    assert [m["model_name"] for m in loaded.proxy_config["model_list"]] == ["a", "b"]
    assert loaded.proxy_config["general_settings"] == {"background_health_checks": True}
    assert len(loaded.included_files) == 2
    assert "include" not in loaded.proxy_config


def test_nested_include_resolves_next_to_declaring_file_and_skips_repeats(tmp_path):
    write(tmp_path / "sub" / "more.yaml", "model_list: [{model_name: c, litellm_params: {model: openai/c}}]\n")
    write(tmp_path / "sub" / "models.yaml", "include: [more.yaml, ../config.yaml]\nmodel_list: []\n")
    root = write(tmp_path / "config.yaml", "include: [sub/models.yaml]\nmodel_list: []\n")
    loaded = load(str(root))
    assert [m["model_name"] for m in loaded.proxy_config["model_list"]] == ["c"]


def test_include_falls_back_to_root_relative_path(tmp_path):
    write(tmp_path / "shared.yaml", "model_list: [{model_name: s, litellm_params: {model: openai/s}}]\n")
    write(tmp_path / "sub" / "models.yaml", "include: [shared.yaml]\n")
    root = write(tmp_path / "config.yaml", "include: [sub/models.yaml]\n")
    assert len(load(str(root)).proxy_config["model_list"]) == 1


def test_missing_include_is_an_error(tmp_path):
    root = write(tmp_path / "config.yaml", "include: [nope.yaml]\nmodel_list: []\n")
    with pytest.raises(ConfigError, match="can't read"):
        load(str(root))


def test_include_must_be_a_list(tmp_path):
    root = write(tmp_path / "config.yaml", "include: models.yaml\n")
    with pytest.raises(ConfigError, match="list of file paths"):
        load(str(root))


CLASSIC_VALUES = """
    replicaCount: 4
    numWorkers: 2
    collector:
      enabled: true
    proxy_config:
      model_list:
        - model_name: a
          litellm_params: {model: openai/a}
      general_settings:
        background_health_checks: true
"""


def test_classic_helm_values(tmp_path):
    loaded = load(str(write(tmp_path / "values.yaml", CLASSIC_VALUES)))
    h = loaded.hints
    assert (h.replicas, h.workers, h.collector, h.split) == (4, 2, True, False)
    assert loaded.proxy_config["general_settings"]["background_health_checks"] is True


def test_classic_helm_autoscaling_counts_min_and_says_so(tmp_path):
    values = CLASSIC_VALUES + "    autoscaling: {enabled: true, minReplicas: 2, maxReplicas: 10}\n"
    h = load(str(write(tmp_path / "values.yaml", values))).hints
    assert h.replicas == 2
    assert any("2 to 10" in note for note in h.notes)


def test_classic_helm_external_configmap_is_flagged(tmp_path):
    values = CLASSIC_VALUES + "    proxyConfigMap: {create: false, name: mine}\n"
    assert any("existing ConfigMap" in n for n in load(str(write(tmp_path / "v.yaml", values))).hints.notes)


def test_split_helm_values(tmp_path):
    loaded = load(str(write(tmp_path / "values.yaml", """
        gateway:
          numWorkers: 3
          replicaCount: 2
          hpa: {enabled: false}
          collector: {enabled: false}
          config:
            proxy_config:
              general_settings: {background_health_checks: true}
        backend:
          hpa: {enabled: true, minReplicas: 1, maxReplicas: 4}
    """)))
    h = loaded.hints
    assert (h.split, h.replicas, h.workers, h.backend_replicas, h.collector) == (True, 2, 3, 1, False)
    assert any("--backend-replicas" in note for note in h.notes)


def test_split_helm_backend_disabled(tmp_path):
    h = load(str(write(tmp_path / "values.yaml", """
        gateway: {config: {proxy_config: {}}}
        backend: {enabled: false}
    """))).hints
    assert h.backend_replicas == 0


def test_configmap(tmp_path):
    loaded = load(str(write(tmp_path / "cm.yaml", """
        apiVersion: v1
        kind: ConfigMap
        metadata: {name: litellm}
        data:
          config.yaml: |
            general_settings:
              background_health_checks: true
    """)))
    assert loaded.proxy_config["general_settings"]["background_health_checks"] is True
    assert "ConfigMap" in loaded.source


def test_configmap_with_include_is_refused(tmp_path):
    path = write(tmp_path / "cm.yaml", """
        kind: ConfigMap
        data:
          config.yaml: |
            include: [models.yaml]
    """)
    with pytest.raises(ConfigError, match="include"):
        load(str(path))


@pytest.mark.parametrize("text, message", [
    ("", "is empty"),
    ("# only a comment\n", "is empty"),
    ("- a\n- b\n", "should be a YAML mapping, got list"),
    ("just text\n", "should be a YAML mapping, got str"),
    ("services:\n  web: {image: nginx}\n", "doesn't look like a LiteLLM config"),
    ("model_list: [\n", "can't read"),
])
def test_bad_files(tmp_path, text, message):
    with pytest.raises(ConfigError, match=message):
        load(str(write(tmp_path / "config.yaml", text)))


def test_missing_and_empty_paths(tmp_path):
    with pytest.raises(ConfigError, match="can't read"):
        load(str(tmp_path / "nope.yaml"))
    with pytest.raises(ConfigError, match="no config path"):
        load("")


def test_reads_utf8_regardless_of_locale(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_bytes("# café — ünïcode\ngeneral_settings: {background_health_checks: true}\n".encode("utf-8"))
    assert load(str(path)).proxy_config["general_settings"]["background_health_checks"] is True


def test_undecodable_bytes_are_a_clean_error(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_bytes(b"general_settings: {x: \xff\xfe\x00}\n")
    with pytest.raises(ConfigError, match="can't read"):
        load(str(path))


def test_deep_nesting_is_a_clean_error(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("model_list: " + "[" * 20000 + "]" * 20000 + "\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load(str(path))


def test_split_helm_external_configmap_is_flagged(tmp_path):
    path = write(tmp_path / "values.yaml", "gateway:\n  config: {create: false, proxy_config: {}}\n")
    assert any("existing ConfigMap" in n for n in load(str(path)).hints.notes)


@pytest.mark.parametrize("data, message", [
    ("data: {other: x}", "no single config.yaml entry"),
    ("data:\n  config.yaml: '[unclosed'", "can't read data.config.yaml"),
    ("data:\n  config.yaml: '- a list'", "should be a YAML mapping"),
])
def test_configmap_errors(tmp_path, data, message):
    path = tmp_path / "cm.yaml"
    path.write_text("kind: ConfigMap\n" + data + "\n", encoding="utf-8")
    with pytest.raises(ConfigError, match=message):
        load(str(path))


def test_model_list_null_is_empty(tmp_path):
    loaded = load(str(write(tmp_path / "c.yaml", "model_list: null\ngeneral_settings: null\n")))
    assert loaded.proxy_config["model_list"] is None
