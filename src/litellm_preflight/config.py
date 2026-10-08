"""Load a LiteLLM proxy config the way the proxy does, from a config file or a Helm values file."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Set

import yaml

INCLUDE_KEY = "include"


class ConfigError(ValueError):
    """The file can't be used: unreadable, not YAML, or a shape LiteLLM would reject."""


@dataclass
class TopologyHints:
    """Numbers read from a Helm values file. None means the file doesn't say."""

    from_values_file: bool = False

    replicas: Optional[int] = None
    workers: Optional[int] = None
    split: Optional[bool] = None
    backend_replicas: Optional[int] = None
    collector: Optional[bool] = None
    notes: List[str] = field(default_factory=list)


@dataclass
class LoadedConfig:
    proxy_config: Dict[str, Any]
    source: str
    included_files: List[str] = field(default_factory=list)
    hints: TopologyHints = field(default_factory=TopologyHints)


def _read_yaml(path: str) -> Any:
    if not path:
        raise ConfigError("no config path given")
    try:
        # Binary mode lets PyYAML detect the encoding, so a UTF-8 config reads the same on every platform
        with open(path, "rb") as f:
            return yaml.safe_load(f)
    except (OSError, yaml.YAMLError) as e:
        raise ConfigError(f"can't read {path}: {e}") from None
    except RecursionError:
        raise ConfigError(f"can't read {path}: it's nested too deeply to parse") from None


def mapping(value: Any, where: str) -> Mapping[str, Any]:
    """A YAML mapping, with a missing or null value treated as empty."""
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ConfigError(f"{where} should be a mapping, got {type(value).__name__}")
    return value


def _include_entries(config: Mapping[str, Any], declared_in: str) -> List[str]:
    if INCLUDE_KEY not in config:
        return []
    entries = config[INCLUDE_KEY]
    if not isinstance(entries, list) or not all(isinstance(e, str) for e in entries):
        raise ConfigError(f"'include' in {declared_in} should be a list of file paths")
    return entries


def _resolve_include(entry: str, declared_in: str, root: str) -> str:
    # Same rule as resolve_include_file_path in litellm/proxy/common_utils/config_includes.py:
    # next to the declaring file first, then next to the root config
    declared_relative = os.path.abspath(os.path.join(os.path.dirname(declared_in), entry))
    root_relative = os.path.abspath(os.path.join(os.path.dirname(root), entry))
    if root_relative == declared_relative or not os.path.exists(root_relative):
        return declared_relative
    return declared_relative if os.path.exists(declared_relative) else root_relative


def _merge(base: Mapping[str, Any], included: Mapping[str, Any]) -> Dict[str, Any]:
    # Lists are extended and every other value is replaced, like _merged in config_includes.py
    merged = dict(base)
    for key, value in included.items():
        if isinstance(value, list) and isinstance(base.get(key), list):
            merged[key] = [*base[key], *value]
        else:
            merged[key] = value
    return merged


def resolve_includes(config: Mapping[str, Any], path: str) -> "tuple[Dict[str, Any], List[str]]":
    root = os.path.abspath(path)
    pending = [(entry, root) for entry in _include_entries(config, path)]
    loaded: Set[str] = {root}
    order: List[str] = []
    merged: Dict[str, Any] = {k: v for k, v in config.items() if k != INCLUDE_KEY}
    while pending:
        entry, declared_in = pending.pop(0)
        location = _resolve_include(entry, declared_in, root)
        if location in loaded:
            continue
        included = mapping(_read_yaml(location), location)
        merged = _merge(merged, {k: v for k, v in included.items() if k != INCLUDE_KEY})
        pending.extend((e, location) for e in _include_entries(included, location))
        loaded.add(location)
        order.append(location)
    return merged, order


def _whole(value: Any) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _replicas(section: Mapping[str, Any], hpa_key: str, label: str, notes: List[str],
              flag: str = "--replicas") -> Optional[int]:
    hpa = mapping(section.get(hpa_key), f"{label}.{hpa_key}")
    keda = mapping(section.get("keda"), f"{label}.keda") if label == "values" else {}
    for name, scaler in ((hpa_key, hpa), ("keda", keda)):
        if scaler.get("enabled"):
            low, high = _whole(scaler.get("minReplicas")) or 1, _whole(scaler.get("maxReplicas"))
            span = f"{low} to {high}" if high else f"at least {low}"
            where = name if label == "values" else f"{label}.{name}"
            notes.append(f"{where} scales replicas from {span}. Counting {low}. Pass {flag} for your usual count.")
            return low
    count = _whole(section.get("replicaCount"))
    return 1 if count is None else count


def _helm_classic(values: Mapping[str, Any]) -> "tuple[Dict[str, Any], TopologyHints]":
    """The litellm-helm chart: config under proxy_config, one Deployment running the proxy."""
    hints = TopologyHints(from_values_file=True, split=False)
    hints.replicas = _replicas(values, "autoscaling", "values", hints.notes)
    hints.workers = _whole(values.get("numWorkers")) or 1
    hints.collector = bool(mapping(values.get("collector"), "collector").get("enabled"))
    if not mapping(values.get("proxyConfigMap"), "proxyConfigMap").get("create", True):
        hints.notes.append("proxyConfigMap.create is false, so the proxy reads an existing ConfigMap. "
                           "proxy_config here may not be what runs.")
    return dict(mapping(values.get("proxy_config"), "proxy_config")), hints


def _helm_split(values: Mapping[str, Any]) -> "tuple[Dict[str, Any], TopologyHints]":
    """The split litellm chart: gateway and backend both load gateway.config.proxy_config."""
    gateway = mapping(values.get("gateway"), "gateway")
    backend = mapping(values.get("backend"), "backend")
    hints = TopologyHints(from_values_file=True, split=True)
    hints.replicas = _replicas(gateway, "hpa", "gateway", hints.notes)
    hints.workers = _whole(gateway.get("numWorkers")) or 1
    hints.collector = bool(mapping(gateway.get("collector"), "gateway.collector").get("enabled"))
    hints.backend_replicas = (_replicas(backend, "hpa", "backend", hints.notes, "--backend-replicas")
                              if backend.get("enabled", True) else 0)
    config = mapping(gateway.get("config"), "gateway.config")
    if not config.get("create", True):
        hints.notes.append("gateway.config.create is false, so the proxy reads an existing ConfigMap. "
                           "gateway.config.proxy_config here may not be what runs.")
    return dict(mapping(config.get("proxy_config"), "gateway.config.proxy_config")), hints


def _from_configmap(raw: Mapping[str, Any], path: str) -> "tuple[Dict[str, Any], str]":
    data = mapping(raw.get("data"), "data")
    keys = [k for k in data if k == "config.yaml"] or [k for k in data if str(k).endswith((".yaml", ".yml"))]
    if len(keys) != 1:
        raise ConfigError(f"{path} is a ConfigMap, but it has no single config.yaml entry under data")
    try:
        inner = yaml.safe_load(data[keys[0]]) if isinstance(data[keys[0]], str) else None
    except yaml.YAMLError as e:
        raise ConfigError(f"can't read data.{keys[0]} in {path}: {e}") from None
    if not isinstance(inner, Mapping):
        raise ConfigError(f"data.{keys[0]} in {path} should be a YAML mapping")
    if INCLUDE_KEY in inner:
        raise ConfigError(f"data.{keys[0]} in {path} uses include:, which needs the included files. "
                          "Run litellm-preflight on the files the proxy mounts instead.")
    return dict(inner), keys[0]


def load(path: str) -> LoadedConfig:
    raw = _read_yaml(path)
    if raw is None:
        raise ConfigError(f"{path} is empty")
    if not isinstance(raw, Mapping):
        raise ConfigError(f"{path} should be a YAML mapping, got {type(raw).__name__}")

    if raw.get("kind") == "ConfigMap":
        proxy_config, key = _from_configmap(raw, path)
        return LoadedConfig(proxy_config, f"{path} (ConfigMap, data.{key})")

    proxy_keys = {"model_list", "general_settings", "litellm_settings", "router_settings", INCLUDE_KEY}
    if not proxy_keys & set(raw):
        if isinstance(raw.get("proxy_config"), Mapping):
            proxy_config, hints = _helm_classic(raw)
            return LoadedConfig(proxy_config, f"{path} (litellm-helm chart values, proxy_config)", hints=hints)
        if isinstance(raw.get("gateway"), Mapping) and isinstance(raw["gateway"].get("config"), Mapping):
            proxy_config, hints = _helm_split(raw)
            return LoadedConfig(proxy_config, f"{path} (split litellm chart values, gateway.config.proxy_config)",
                                hints=hints)

        raise ConfigError(f"{path} doesn't look like a LiteLLM config: it has no model_list, general_settings, "
                          "litellm_settings, router_settings or include, and isn't a values file for the "
                          "litellm-helm or litellm chart")

    merged, included = resolve_includes(raw, path)
    return LoadedConfig(merged, path, included_files=included)
