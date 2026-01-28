"""Endpoint definitions for REST APIs (teikei and future extensions)."""

import os
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any, Dict

import yaml


@dataclass(frozen=True)
class Endpoint:
    method: str
    path_template: str  # relative path; use {template_id} placeholder when needed


def _default_teikei_endpoints() -> dict[str, Endpoint]:
    return {
        "list": Endpoint(method="GET", path_template="teikei/objects"),
        "detail": Endpoint(method="GET", path_template="teikei/objects/{template_id}"),
        "execute": Endpoint(method="POST", path_template="teikei/objects/search/{template_id}"),
        "update": Endpoint(method="PUT", path_template="teikei/objects/{template_id}"),
        "delete": Endpoint(method="DELETE", path_template="teikei/objects/{template_id}"),
    }


def _resolve_config_path() -> Path | None:
    """Locate rest_config.yml (or override via REST_CONFIG_PATH/REST_ENDPOINTS_PATH)."""
    for env in ("REST_ENDPOINTS_PATH", "REST_CONFIG_PATH"):
        env_path = os.getenv(env)
        if env_path:
            candidate = Path(env_path).expanduser().resolve()
            if candidate.is_file():
                return candidate
    repo_relative = Path(__file__).resolve().parent.parent.parent / "config" / "rest_config.yml"
    if repo_relative.is_file():
        return repo_relative
    try:
        pkg_path = resources.files("teradata_mcp_server.config").joinpath("rest_config.yml")
        if pkg_path.is_file():
            return Path(pkg_path)
    except Exception:
        pass
    return None


def _load_yaml_endpoints(path: Path) -> Dict[str, Dict[str, Endpoint]]:
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    # support two layouts: top-level "endpoints" or direct mapping
    candidates = data.get("endpoints") if isinstance(data, dict) else None
    if candidates is None:
        candidates = data if isinstance(data, dict) else {}

    parsed: Dict[str, Dict[str, Endpoint]] = {}
    for group, defs in candidates.items():
        group_map: Dict[str, Endpoint] = {}
        for name, meta in (defs or {}).items():
            method = (meta or {}).get("method")
            template = (meta or {}).get("path_template")
            if not method or not template:
                continue
            group_map[name.lower()] = Endpoint(method=method.upper(), path_template=template)
        if group_map:
            parsed[group.lower()] = group_map
    return parsed


def load_endpoints() -> Dict[str, Dict[str, Endpoint]]:
    endpoints = {"teikei": _default_teikei_endpoints()}
    path = _resolve_config_path()
    if not path:
        return endpoints
    try:
        yaml_endpoints = _load_yaml_endpoints(path)
        # merge/override per group
        for group, defs in yaml_endpoints.items():
            base = endpoints.get(group, {})
            base.update(defs)
            endpoints[group] = base
    except Exception:
        return endpoints
    return endpoints


# Loaded endpoint registry (can be extended via rest_config.yml endpoints section)
ENDPOINTS = load_endpoints()
TEIKEI_ENDPOINTS = ENDPOINTS.get("teikei", {})
