"""Configuration-driven endpoint registry for REST APIs."""

import re
import string
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from .config_loader import REST_CONFIG


@dataclass(frozen=True)
class Endpoint:
    method: str
    path_template: str
    description: str = ""

    @property
    def path_parameters(self) -> tuple[str, ...]:
        return tuple(
            field_name for _, field_name, _, _ in string.Formatter().parse(self.path_template) if field_name is not None
        )

    def build_path(self, path_params: dict[str, Any] | None = None) -> str:
        values = path_params or {}
        missing = [name for name in self.path_parameters if name not in values]
        if missing:
            raise ValueError(f"Missing path parameter(s): {', '.join(missing)}")

        encoded = {name: quote(str(values[name]), safe="") for name in self.path_parameters}
        return self.path_template.format_map(encoded).lstrip("/")

    def matches_path(self, path: str) -> bool:
        pattern_parts: list[str] = []
        for literal, field_name, _, _ in string.Formatter().parse(self.path_template.lstrip("/")):
            pattern_parts.append(re.escape(literal))
            if field_name is not None:
                pattern_parts.append(r"[^/]+")
        return re.fullmatch("".join(pattern_parts), path.lstrip("/")) is not None


def _parse_endpoints(config: dict[str, Any]) -> dict[str, dict[str, Endpoint]]:
    candidates = config.get("endpoints") or {}
    parsed: dict[str, dict[str, Endpoint]] = {}
    for group, defs in candidates.items():
        if not isinstance(defs, dict):
            continue
        group_map: dict[str, Endpoint] = {}
        for name, meta in (defs or {}).items():
            if not isinstance(meta, dict):
                continue
            method = (meta or {}).get("method")
            template = (meta or {}).get("path_template")
            if not method or not template:
                continue
            group_map[name.lower()] = Endpoint(
                method=str(method).upper(),
                path_template=str(template).lstrip("/"),
                description=str(meta.get("description") or ""),
            )
        if group_map:
            parsed[str(group).lower()] = group_map
    return parsed


def load_endpoints() -> dict[str, dict[str, Endpoint]]:
    return _parse_endpoints(REST_CONFIG)


def endpoint_names() -> list[str]:
    return [f"{group}.{action}" for group, actions in ENDPOINTS.items() for action in actions]


def get_endpoint(name: str) -> Endpoint:
    try:
        group, action = name.strip().lower().split(".", 1)
        return ENDPOINTS[group][action]
    except (KeyError, ValueError) as exc:
        available = ", ".join(endpoint_names())
        raise ValueError(f"Unknown endpoint '{name}'. Available endpoints: {available}") from exc


def match_endpoint(path: str, method: str | None = None) -> tuple[str, Endpoint]:
    normalized_method = method.upper() if method else None
    matches = [
        (f"{group}.{action}", endpoint)
        for group, actions in ENDPOINTS.items()
        for action, endpoint in actions.items()
        if endpoint.matches_path(path) and (normalized_method is None or endpoint.method == normalized_method)
    ]
    if not matches:
        method_text = f"{normalized_method} " if normalized_method else ""
        raise ValueError(f"REST endpoint is not configured: {method_text}{path}")
    if len(matches) > 1:
        names = ", ".join(name for name, _ in matches)
        raise ValueError(f"REST endpoint is ambiguous; specify method or logical endpoint name: {names}")
    return matches[0]


def endpoint_catalog_text() -> str:
    lines = []
    for group, actions in ENDPOINTS.items():
        for action, endpoint in actions.items():
            description = f" - {endpoint.description}" if endpoint.description else ""
            lines.append(f"- {group}.{action}: {endpoint.method} {endpoint.path_template}{description}")
    return "\n".join(lines) if lines else "- No endpoints are configured."


# Loaded endpoint registry from the layered REST configuration.
ENDPOINTS = load_endpoints()
TEIKEI_ENDPOINTS = ENDPOINTS.get("teikei", {})
