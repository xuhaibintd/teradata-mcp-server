import unittest
from unittest.mock import patch

import requests

from teradata_mcp_server.tools.rest.rest_tools import (
    REST_CONFIG,
    _resolve_request_target,
    handle_rest_call,
)
from teradata_mcp_server.tools.rest.types import RestRequest


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
