import unittest
from pathlib import Path
from unittest.mock import patch

import requests

from teradata_mcp_server.tools.rest.config_loader import load_rest_config
from teradata_mcp_server.tools.rest.rest_tools import (
    REST_CONFIG,
    _resolve_request_target,
    handle_rest_call,
)
from teradata_mcp_server.tools.rest.types import RestRequest
from teradata_mcp_server.tools.module_loader import ModuleLoader


class FakeResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body if body is not None else {"ok": True}
        self.headers = {"Content-Type": "application/json"}
        self.text = ""

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)


class RestToolTests(unittest.TestCase):
    def test_module_loader_excludes_rest_config_files(self):
        loader = ModuleLoader()
        loader.determine_required_modules({"tool": [r"^rest_.*"]})

        names = {path.name for path in loader.get_required_yaml_paths()}

        self.assertIn("rest_objects.yml", names)
        self.assertNotIn("rest_config.yml", names)
        self.assertNotIn("rest_config.local.yml", names)

    def test_deployment_override_preserves_module_endpoints(self):
        with (
            patch(
                "teradata_mcp_server.tools.rest.config_loader._override_paths",
                return_value=[Path("rest_config.local.yml")],
            ),
            patch(
                "teradata_mcp_server.tools.rest.config_loader._module_default_config",
                return_value={
                    "base_url": "",
                    "auth": {"enabled": False, "header_name": "Authorization"},
                    "endpoints": {"meisai": {"detail": {"method": "GET"}}},
                },
            ),
            patch(
                "teradata_mcp_server.tools.rest.config_loader._load_yaml",
                return_value={
                    "base_url": "https://example.test/api/v1",
                    "auth": {"enabled": True, "header_name": "X-Test-Auth"},
                },
            ),
        ):
            config = load_rest_config()

        self.assertEqual(config["base_url"], "https://example.test/api/v1")
        self.assertTrue(config["auth"]["enabled"])
        self.assertEqual(config["auth"]["header_name"], "X-Test-Auth")
        self.assertIn("meisai", config["endpoints"])
        self.assertEqual(config["endpoints"]["meisai"]["detail"]["method"], "GET")

    def test_logical_endpoint_builds_encoded_path(self):
        method, url = _resolve_request_target(
            RestRequest(endpoint="meisai.detail", path_params={"objectID": "abc/123"})
        )

        self.assertEqual(method, "GET")
        self.assertTrue(url.endswith("/meisai/objects/abc%2F123"))

    def test_legacy_dynamic_path_matches_template(self):
        method, url = _resolve_request_target(RestRequest(method="GET", path="requests/REQ-123/info"))

        self.assertEqual(method, "GET")
        self.assertTrue(url.endswith("/requests/REQ-123/info"))

    def test_unknown_path_is_rejected_without_http_request(self):
        with patch("teradata_mcp_server.tools.rest.rest_tools.requests.request") as request:
            response = handle_rest_call(None, RestRequest(method="GET", path="unknown/path"))

        request.assert_not_called()
        self.assertEqual(response["status"], "error")
        self.assertIn("not configured", response["message"]["message"])

    def test_method_mismatch_is_rejected(self):
        response = handle_rest_call(
            None,
            RestRequest(endpoint="meisai.list", method="DELETE"),
        )

        self.assertEqual(response["status"], "error")
        self.assertIn("Method mismatch", response["message"]["message"])

    def test_401_refreshes_and_replaces_auth_header(self):
        calls = []

        def fake_request(method, url, **kwargs):
            calls.append(dict(kwargs.get("headers") or {}))
            return FakeResponse(401 if len(calls) == 1 else 200)

        with (
            patch("teradata_mcp_server.tools.rest.rest_tools.requests.request", side_effect=fake_request),
            patch(
                "teradata_mcp_server.tools.rest.rest_tools._fetch_token",
                side_effect=["old-token", "new-token"],
            ),
            patch("teradata_mcp_server.tools.rest.rest_tools._AUTH_TOKEN", None),
        ):
            response = handle_rest_call(None, RestRequest(endpoint="teikei.list"))

        header_name = REST_CONFIG["auth"]["header_name"]
        self.assertEqual(response["status"], "success")
        self.assertEqual(calls[0][header_name], "old-token")
        self.assertEqual(calls[1][header_name], "new-token")


if __name__ == "__main__":
    unittest.main()
