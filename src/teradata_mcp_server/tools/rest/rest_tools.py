import logging
import os
import threading
from collections.abc import MutableMapping
from importlib import resources
from importlib.resources import as_file
from pathlib import Path
from typing import Any, cast
from urllib.parse import urljoin

import requests
import yaml

from teradata_mcp_server.tools.utils import create_response

from .constants import DEFAULT_TIMEOUT
from .endpoints import TEIKEI_ENDPOINTS, Endpoint, endpoint_catalog_text, get_endpoint, match_endpoint
from .types import RestRequest

logger = logging.getLogger("teradata_mcp_server.tools.rest")


def _default_rest_config() -> dict[str, Any]:
    """Fallback REST connection configuration."""
    return {
        "base_url": "",
        "default_headers": {},
        "default_params": {},
        "timeout": DEFAULT_TIMEOUT,
        "verify_ssl": True,
        "allow_absolute_urls": False,
        "allow_unlisted_endpoints": False,
        "auth": {
            "enabled": False,
            "login_path": "",
            "method": "POST",
            "payload": {},
            "headers": {},
            "token_field": "token",
            "header_name": "Authorization",
            "header_prefix": "Bearer ",
        },
    }


def _resolve_rest_config_path() -> Path | None:
    """Resolve the rest_config.yml path with overrides and packaged fallback."""
    for env in ("REST_CONFIG_PATH", "REST_ENDPOINTS_PATH"):
        env_path = os.getenv(env)
        if env_path:
            env_candidate = Path(env_path).expanduser().resolve()
            if env_candidate.is_file():
                return env_candidate
            logger.warning("%s is set but file not found: %s", env, env_candidate)

    repo_relative = Path(__file__).resolve().parent.parent.parent / "config" / "rest_config.yml"
    if repo_relative.is_file():
        return repo_relative

    try:
        pkg_path = resources.files("teradata_mcp_server.config").joinpath("rest_config.yml")
        if pkg_path.is_file():
            with as_file(pkg_path) as resolved_path:
                return resolved_path
    except Exception:
        logger.debug("Packaged rest_config.yml not found via importlib.resources", exc_info=True)

    return None


def _load_rest_config() -> dict[str, Any]:
    """Load REST connection configuration once at module import."""
    config_path = _resolve_rest_config_path()
    if not config_path:
        logger.warning("REST config file not found, using defaults")
        return _default_rest_config()

    try:
        default_conf = _default_rest_config()
        with open(config_path, encoding="utf-8") as f:
            loaded = yaml.safe_load(f) or {}
            merged = default_conf | loaded
            if "auth" in loaded:
                merged_auth = (default_conf.get("auth") or {}).copy()
                merged_auth.update(loaded.get("auth") or {})
                merged["auth"] = merged_auth
            logger.info("Loaded REST config from %s", config_path)
            return merged
    except Exception:
        logger.exception("Failed to load REST config at %s, using defaults", config_path)
        return _default_rest_config()


REST_CONFIG = _load_rest_config()
_AUTH_TOKEN: str | None = None
_AUTH_LOCK = threading.Lock()


def _extract_body(response: requests.Response) -> Any:
    """Parse body based on response headers, prefer JSON when possible."""
    content_type = response.headers.get("Content-Type", "")
    if "application/json" in content_type.lower():
        try:
            return response.json()
        except ValueError:
            logger.warning("Response declared JSON but parsing failed; falling back to text.")
    return response.text


def _build_request_url(request: RestRequest, path_override: str | None = None) -> str:
    """Resolve target URL using request input and configured base URL."""
    if path_override is not None:
        base_url = (REST_CONFIG.get("base_url") or "").strip()
        if not base_url:
            raise ValueError("Fallback path requested but base_url is missing in config")
        base = base_url if base_url.endswith("/") else f"{base_url}/"
        return urljoin(base, path_override.lstrip("/"))

    if request.url:
        return str(request.url)

    base_url = (REST_CONFIG.get("base_url") or "").strip()
    if not base_url:
        raise ValueError("Request did not provide a full URL and base_url is missing in config")

    path = (path_override if path_override is not None else request.path or "").lstrip("/")
    base = base_url if base_url.endswith("/") else f"{base_url}/"
    return urljoin(base, path)


def _resolve_request_target(request: RestRequest) -> tuple[str, str]:
    """Resolve and validate the HTTP method and URL from the configured endpoint registry."""
    if request.endpoint:
        if request.path or request.url:
            raise ValueError("endpoint cannot be combined with path or url")
        endpoint = get_endpoint(request.endpoint)
        if request.method and request.method.upper() != endpoint.method:
            raise ValueError(
                f"Method mismatch for '{request.endpoint}': configured {endpoint.method}, received {request.method.upper()}"
            )
        path = endpoint.build_path(request.path_params)
        return endpoint.method, _build_request_url(request, path_override=path)

    if request.url:
        if not REST_CONFIG.get("allow_absolute_urls", False):
            raise ValueError("Absolute URLs are disabled; use a configured logical endpoint")
        if not request.method:
            raise ValueError("method is required when url is used")
        return request.method.upper(), str(request.url)

    if not request.path:
        raise ValueError("request.endpoint is required; legacy callers may provide request.method and request.path")

    path = request.path.lstrip("/")
    if REST_CONFIG.get("allow_unlisted_endpoints", False):
        return (request.method or "GET").upper(), _build_request_url(request)

    _, endpoint = match_endpoint(path, request.method)
    return endpoint.method, _build_request_url(request, path_override=path)


def _build_login_url(auth_conf: dict[str, Any]) -> str:
    """Build full login URL from config."""
    login_url = auth_conf.get("login_url")
    if login_url:
        return str(login_url)

    base_url = (REST_CONFIG.get("base_url") or "").strip()
    login_path = (auth_conf.get("login_path") or "").lstrip("/")
    if not base_url or not login_path:
        raise ValueError("Auth config missing base_url or login_path")

    base = base_url if base_url.endswith("/") else f"{base_url}/"
    return str(urljoin(base, login_path))


def _auth_enabled() -> bool:
    auth_conf = REST_CONFIG.get("auth") or {}
    return bool(auth_conf.get("enabled"))


def _fetch_token(auth_conf: dict[str, Any]) -> str:
    """Login to obtain token based on auth config."""
    login_url = _build_login_url(auth_conf)
    method = (auth_conf.get("method") or "POST").upper()
    payload = auth_conf.get("payload") or {}
    headers = auth_conf.get("headers") or {}

    response = requests.request(
        method,
        login_url,
        json=payload if payload else None,
        headers=headers if headers else None,
        timeout=REST_CONFIG.get("timeout") or DEFAULT_TIMEOUT,
        verify=REST_CONFIG.get("verify_ssl", True),
    )
    response.raise_for_status()

    try:
        body = response.json()
    except ValueError as exc:
        raise ValueError(f"Auth response is not JSON; cannot extract token: {exc}") from exc

    token_field = auth_conf.get("token_field") or "token"
    token = body.get(token_field)

    # Some APIs wrap token in content or a list under content
    if not token and "content" in body:
        content = body.get("content")
        if isinstance(content, dict):
            token = content.get(token_field)
        elif isinstance(content, list) and content:
            first = content[0] or {}
            token = first.get(token_field)

    if not token:
        raise ValueError(f"Auth response missing token field '{token_field}'")
    return str(token)


def _ensure_auth_header(headers: MutableMapping[str, str | bytes]) -> MutableMapping[str, str | bytes]:
    """Ensure Authorization header is set using cached or freshly fetched token."""
    auth_conf = REST_CONFIG.get("auth") or {}
    if not auth_conf.get("enabled"):
        return headers

    header_name = auth_conf.get("header_name") or "Authorization"
    if headers.get(header_name):
        return headers

    global _AUTH_TOKEN
    with _AUTH_LOCK:
        if not _AUTH_TOKEN:
            _AUTH_TOKEN = _fetch_token(auth_conf)
        token_value = _AUTH_TOKEN

    prefix = auth_conf.get("header_prefix") or ""
    headers[header_name] = f"{prefix}{token_value}"
    return headers


def handle_rest_call(conn, request: RestRequest, *args, **kwargs) -> dict[str, Any]:
    """
    Call a REST endpoint configured in rest_config.yml.

    Prefer request.endpoint plus request.path_params. Legacy method/path calls are accepted
    only when they match a configured endpoint.
    """
    timeout = request.timeout or REST_CONFIG.get("timeout") or DEFAULT_TIMEOUT
    headers: MutableMapping[str, str | bytes] = {}
    headers.update(REST_CONFIG.get("default_headers") or {})
    headers.update(request.headers or {})
    params: dict[str, Any] = {}
    params.update(REST_CONFIG.get("default_params") or {})
    params.update(request.params or {})

    url = None
    try:
        method, url = _resolve_request_target(request)
    except Exception as exc:
        return create_response(
            {},
            {"tool_name": "rest_call", "url": url},
            error={"message": str(exc)},
        )

    # Inject auth header if configured and not already provided
    try:
        headers = _ensure_auth_header(headers)
    except Exception as exc:
        logger.exception("Failed to obtain auth token")
        return create_response(
            {},
            {"tool_name": "rest_call"},
            error={"message": f"Failed to obtain auth token: {exc}"},
        )

    try:
        response = requests.request(
            method,
            url,
            headers=headers or None,
            params=params or None,
            json=request.json_body,
            timeout=timeout,
            verify=REST_CONFIG.get("verify_ssl", True),
        )

        # If 401 and auth enabled, refresh token once and retry
        if response.status_code == 401 and _auth_enabled():
            auth_conf = REST_CONFIG.get("auth") or {}
            header_name = auth_conf.get("header_name") or "Authorization"
            with _AUTH_LOCK:
                global _AUTH_TOKEN
                _AUTH_TOKEN = None
            headers.pop(header_name, None)
            headers = _ensure_auth_header(headers)
            response = requests.request(
                method,
                url,
                headers=headers or None,
                params=params or None,
                json=request.json_body,
                timeout=timeout,
                verify=REST_CONFIG.get("verify_ssl", True),
            )

        response.raise_for_status()

        payload = {
            "status_code": response.status_code,
            "headers": dict(response.headers),
            "body": _extract_body(response),
        }
        metadata = {"tool_name": "rest_call", "url": url}
        return create_response(payload, metadata)
    except requests.RequestException as exc:
        logger.exception("REST call error")
        error_payload = {
            "error": str(exc),
            "status_code": getattr(exc.response, "status_code", None),
        }
        return create_response(
            {},
            {"tool_name": "rest_call", "url": url or (str(request.url) if request.url else None)},
            error=error_payload,
        )
    except Exception as exc:
        logger.exception("REST call error")
        return create_response(
            {},
            {"tool_name": "rest_call", "url": url},
            error={"message": str(exc)},
        )


def handle_teikei(
    conn,
    template_id: str | None = None,
    instruction: str = "",
    action: str | None = None,
    json_payload: Any | None = None,
) -> dict[str, Any]:
    """
    Call teikei API based on explicit or inferred action.

    Action resolution (minimal guessing, explicit action preferred):
    - If action is provided (list/detail/execute), honor it.
    - If no action and no template_id -> list (fetch catalog).
    - If no action but template_id present:
        * If json_payload is provided -> execute (run template).
        * Otherwise -> detail (fetch template detail).
    Note: instruction is kept for logging only; no keyword matching to avoid misfires.
    """
    normalized_action = (action or "").lower().strip()
    valid_actions = set(TEIKEI_ENDPOINTS.keys())

    # Infer action when not explicitly provided
    if not normalized_action:
        if not template_id:
            normalized_action = "list"
        elif json_payload is not None:
            normalized_action = "execute"
        else:
            normalized_action = "detail"

    if normalized_action not in valid_actions:
        return create_response(
            {},
            {"tool_name": "teikei"},
            error={"message": f"Unsupported action: {normalized_action}, allowed: {valid_actions}"},
        )

    endpoint: Endpoint = TEIKEI_ENDPOINTS[normalized_action]

    # Preserve the legacy teikei wrapper while allowing placeholder names to
    # remain fully configuration-driven (for example templateID).
    if endpoint.path_parameters and not template_id:
        return create_response(
            {},
            {"tool_name": "teikei"},
            error={"message": f"Missing template_id; cannot call teikei action '{normalized_action}'"},
        )

    path_params = dict.fromkeys(endpoint.path_parameters, template_id)
    path = endpoint.build_path(path_params)

    rest_req = RestRequest(
        method=endpoint.method,
        path=path,
        json=json_payload if normalized_action in {"execute", "update"} else None,
    )

    return handle_rest_call(conn, rest_req)


handle_rest_call.__doc__ = f"""{handle_rest_call.__doc__}

Configured endpoints:
{endpoint_catalog_text()}
"""
cast(Any, handle_rest_call).requires_database = False
