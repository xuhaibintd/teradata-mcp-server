"""Layered configuration loading for the REST tool module."""

import os
from copy import deepcopy
from importlib import resources
from importlib.resources import as_file
from pathlib import Path
from typing import Any

import yaml

CONFIG_NAME = "rest_config.yml"
LOCAL_CONFIG_NAME = "rest_config.local.yml"


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    with open(path, encoding="utf-8") as config_file:
        loaded = yaml.safe_load(config_file) or {}
    if not isinstance(loaded, dict):
        raise ValueError(f"REST config must contain a YAML mapping: {path}")
    return loaded


def _module_default_config() -> dict[str, Any]:
    package_file = resources.files("teradata_mcp_server.tools.rest").joinpath(CONFIG_NAME)
    if not package_file.is_file():
        return {}
    with as_file(package_file) as resolved_path:
        return _load_yaml(resolved_path)


def _override_paths() -> list[Path]:
    paths: list[Path] = []

    local_path = Path(__file__).resolve().parent / LOCAL_CONFIG_NAME
    if local_path.is_file():
        paths.append(local_path)

    # REST_CONFIG_PATH is the current full-config override and takes precedence
    # over the legacy endpoint-only variable when both are set.
    for env_name in ("REST_ENDPOINTS_PATH", "REST_CONFIG_PATH"):
        env_path = os.getenv(env_name)
        if not env_path:
            continue
        candidate = Path(env_path).expanduser().resolve()
        if not candidate.is_file():
            raise FileNotFoundError(f"{env_name} points to a missing file: {candidate}")
        if candidate not in paths:
            paths.append(candidate)

    return paths


def load_rest_config() -> dict[str, Any]:
    """Load module defaults and recursively merge deployment-specific overrides."""
    config = _module_default_config()
    for override_path in _override_paths():
        config = _deep_merge(config, _load_yaml(override_path))
    return config


REST_CONFIG = load_rest_config()
