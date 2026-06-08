import logging
import os
import threading
from collections.abc import MutableMapping
from importlib import resources
from importlib.resources import as_file
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
import yaml

from teradata_mcp_server.tools.utils import create_response

from .constants import DEFAULT_TIMEOUT
from .endpoints import ENDPOINTS, TEIKEI_ENDPOINTS, Endpoint
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


def handle_rest_call(conn, request: RestRequest | None = None, *args, **kwargs) -> dict[str, Any]:
    """
    Generic REST caller.

    Executes the HTTP request per parameters and returns status, headers, and parsed body.
    If no absolute URL is provided, it builds one from base_url + path in config.
    """
    # Allow zero-argument calls; fall back to default RestRequest
    if request is None:
        request = RestRequest()

    timeout = request.timeout or REST_CONFIG.get("timeout") or DEFAULT_TIMEOUT
    headers: MutableMapping[str, str | bytes] = {}
    headers.update(REST_CONFIG.get("default_headers") or {})
    headers.update(request.headers or {})
    params: dict[str, Any] = {}
    params.update(REST_CONFIG.get("default_params") or {})
    params.update(request.params or {})

    # If user did not provide a meaningful path/url, fall back to Teikei list endpoint
    fallback_path = None
    fallback_method = None

    def _is_generic(p: str) -> bool:
        cleaned = p.strip().strip("/")
        return cleaned in {"", "list", "objects", "items"}

    path_hint = (request.path or "").strip() if request.path is not None else ""
    path_norm = path_hint.lstrip("/")
    allowed_paths = {
        ep.path_template.lstrip("/")
        for group in ENDPOINTS.values()
        for ep in group.values()
        if isinstance(ep, Endpoint)
    }
    group_list_endpoints = {
        name: defs.get("list")
        for name, defs in ENDPOINTS.items()
        if isinstance(defs, dict) and isinstance(defs.get("list"), Endpoint)
    }

    parsed_url = None
    try:
        if request.url:
            parsed_url = urlparse(str(request.url))
    except Exception:
        parsed_url = None

    base_netloc = ""
    try:
        base_netloc = urlparse(REST_CONFIG.get("base_url") or "").netloc
    except Exception:
        base_netloc = ""

    generic_path = _is_generic(path_hint)
    generic_url_path = (
        parsed_url is not None and parsed_url.netloc == base_netloc and _is_generic(parsed_url.path or "")
    )
    unknown_path = path_norm not in allowed_paths
    no_user_path = (not request.url and (generic_path or unknown_path)) or generic_url_path

    group_fallback = None
    if not request.url and path_norm:
        if path_norm in group_list_endpoints:
            group_fallback = group_list_endpoints[path_norm]
        elif path_norm.endswith("/list"):
            group_name = path_norm.split("/", 1)[0]
            group_fallback = group_list_endpoints.get(group_name)

    if group_fallback:
        fallback_path = group_fallback.path_template
        fallback_method = group_fallback.method
        if request.method is None:
            request.method = "GET"
    elif no_user_path:
        endpoint = TEIKEI_ENDPOINTS.get("list")
        if endpoint:
            fallback_path = endpoint.path_template
            fallback_method = endpoint.method
            # Default method to GET when we auto-pick the list endpoint
            if request.method is None:
                request.method = "GET"

    # Inject auth header if configured and not already provided
    try:
        headers = _ensure_auth_header(headers)
    except Exception as exc:
        logger.exception("Failed to obtain auth token")
        return create_response({"error": f"Failed to obtain auth token: {exc}"}, {"tool_name": "rest_call"})

    url = None
    try:
        url = _build_request_url(request, path_override=fallback_path)
        response = requests.request(
            fallback_method or request.method,
            url,
            headers=headers or None,
            params=params or None,
            json=request.json_body,
            timeout=timeout,
            verify=REST_CONFIG.get("verify_ssl", True),
        )

        # If 401 and auth enabled, refresh token once and retry
        if response.status_code == 401 and _auth_enabled():
            with _AUTH_LOCK:
                global _AUTH_TOKEN
                _AUTH_TOKEN = None
            headers = _ensure_auth_header(headers)
            response = requests.request(
                fallback_method or request.method,
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
            error_payload,
            {"tool_name": "rest_call", "url": url or (str(request.url) if request.url else None)},
        )
    except Exception as exc:
        logger.exception("REST call error")
        return create_response({"error": str(exc)}, {"tool_name": "rest_call", "url": url})


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
            {"error": f"Unsupported action: {normalized_action}, allowed: {valid_actions}"},
            {"tool_name": "teikei"},
        )

    endpoint: Endpoint = TEIKEI_ENDPOINTS[normalized_action]

    # Validate template_id requirement
    needs_template = "{template_id}" in endpoint.path_template
    if needs_template and not template_id:
        return create_response(
            {"error": f"Missing template_id; cannot call teikei action '{normalized_action}'"},
            {"tool_name": "teikei"},
        )

    path = endpoint.path_template.format(template_id=template_id) if needs_template else endpoint.path_template

    rest_req = RestRequest(
        method=endpoint.method,
        path=path,
        json=json_payload if normalized_action in {"execute", "update"} else None,
    )

    return handle_rest_call(conn, rest_req)
