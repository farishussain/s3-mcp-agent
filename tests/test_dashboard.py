from __future__ import annotations

import http.client
import json
import threading
import unittest
from importlib.resources import files
from unittest.mock import Mock

from botocore.exceptions import ClientError

from s3_mcp_agent.config import Settings
from s3_mcp_agent.dashboard import create_server
from s3_mcp_agent.service import S3Service


class DashboardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = Mock()
        self.client.list_buckets.return_value = {
            "Buckets": [{"Name": "allowed"}, {"Name": "blocked"}],
            "NextContinuationToken": "next",
        }
        for operation in (
            "get_bucket_acl", "get_bucket_policy", "get_bucket_policy_status",
            "get_public_access_block", "get_bucket_ownership_controls",
        ):
            getattr(self.client, operation).side_effect = ClientError(
                {"Error": {"Code": "AccessDenied", "Message": "<script>untrusted</script>"}},
                operation,
            )
        self.service = S3Service(self.client, Settings((), frozenset({"allowed"}), 100))
        self.server = create_server(self.service, 0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def request(self, path: str, headers=None, method="GET"):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        connection.request(method, path, headers=headers or {"X-S3-Dashboard": "1"})
        response = connection.getresponse()
        body = response.read()
        result = response.status, dict(response.getheaders()), body
        connection.close()
        return result

    def test_assets_and_page_are_local_and_do_not_call_aws(self) -> None:
        for path, content_type in (
            ("/", "text/html"), ("/app.js", "text/javascript"), ("/style.css", "text/css")
        ):
            status, headers, body = self.request(path)
            self.assertEqual(status, 200)
            self.assertIn(content_type, headers["Content-Type"])
            self.assertEqual(headers["Cache-Control"], "no-store")
            self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
            self.assertTrue(body)
        self.assertEqual(self.client.mock_calls, [])

    def test_bucket_listing_preserves_scope_and_pagination(self) -> None:
        status, _, body = self.request("/api/buckets?token=previous")
        self.assertEqual(status, 200)
        result = json.loads(body)
        self.assertEqual([bucket["name"] for bucket in result["buckets"]], ["allowed"])
        self.assertEqual(result["next_continuation_token"], "next")
        self.client.list_buckets.assert_called_once_with(MaxBuckets=100, ContinuationToken="previous")

    def test_permissions_use_existing_report_and_leave_denied_evidence_unknown(self) -> None:
        status, _, body = self.request("/api/permissions?bucket=allowed")
        self.assertEqual(status, 200)
        report = json.loads(body)
        self.assertEqual(report["effective_access"]["status"], "not_evaluated")
        self.assertEqual(report["sources"]["policy"]["status"], "access_denied")
        self.assertIsNone(report["public_access_indicators"]["bucket_policy_is_public"])
        self.assertIn("<script>untrusted</script>", report["sources"]["policy"]["message"])
        self.client.get_bucket_acl.assert_called_once_with(Bucket="allowed")
        self.client.put_object.assert_not_called()
        self.client.get_object.assert_not_called()

    def test_blocked_bucket_does_not_reach_aws(self) -> None:
        status, _, _ = self.request("/api/permissions?bucket=blocked")
        self.assertEqual(status, 400)
        self.assertEqual(self.client.mock_calls, [])

    def test_cross_origin_and_rebound_host_requests_are_rejected(self) -> None:
        for headers in (
            {"X-S3-Dashboard": "1", "Origin": "https://evil.example"},
            {"X-S3-Dashboard": "1", "Origin": "null"},
            {"X-S3-Dashboard": "1", "Host": "evil.example"},
            {"X-S3-Dashboard": "1", "Sec-Fetch-Site": "cross-site"},
            {"Accept": "application/json"},
        ):
            status, response_headers, _ = self.request("/api/buckets", headers)
            self.assertEqual(status, 403)
            self.assertNotIn("Access-Control-Allow-Origin", response_headers)
        self.assertEqual(self.client.mock_calls, [])

    def test_same_origin_is_accepted(self) -> None:
        status, _, _ = self.request("/api/buckets", {
            "X-S3-Dashboard": "1",
            "Origin": f"http://127.0.0.1:{self.port}",
            "Sec-Fetch-Site": "same-origin",
        })
        self.assertEqual(status, 200)

    def test_external_navigation_can_open_static_page_but_not_aws_data(self) -> None:
        headers = {"Sec-Fetch-Site": "cross-site"}
        self.assertEqual(self.request("/", headers)[0], 200)
        self.assertEqual(self.request("/api/buckets", headers)[0], 403)
        self.assertEqual(self.client.mock_calls, [])

    def test_no_write_or_transfer_routes(self) -> None:
        for path in ("/api/upload", "/api/download", "/api/delete", "/../config.py"):
            self.assertEqual(self.request(path)[0], 404)
        self.assertEqual(self.request("/api/permissions", method="POST")[0], 501)
        self.assertEqual(self.client.mock_calls, [])

    def test_missing_bucket_and_unexpected_failures_are_explicit_and_sanitized(self) -> None:
        self.assertEqual(self.request("/api/permissions")[0], 400)
        self.client.list_buckets.side_effect = RuntimeError("SECRET MUST NOT LEAK")
        with self.assertLogs("s3_mcp_agent.dashboard", level="ERROR") as captured:
            status, _, body = self.request("/api/buckets")
        self.assertEqual(status, 500)
        self.assertNotIn("SECRET", body.decode())
        self.assertNotIn("SECRET", str(captured.output))

    def test_ui_renders_untrusted_data_as_text_and_never_automatically_fetches(self) -> None:
        script = files("s3_mcp_agent").joinpath("static", "app.js").read_text()
        for unsafe in ("innerHTML", "outerHTML", "insertAdjacentHTML", "eval("):
            self.assertNotIn(unsafe, script)
        self.assertIn("textContent", script)
        self.assertIn('addEventListener("click"', script)
        self.assertNotIn("setInterval", script)
        self.assertNotIn("localStorage", script)


if __name__ == "__main__":
    unittest.main()
