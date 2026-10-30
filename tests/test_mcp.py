import unittest
from unittest.mock import Mock, patch

from s3_mcp_agent.server import mcp
from s3_mcp_agent.service import S3MCPError


class MCPRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_dashboard_does_not_change_mcp_tool_set(self) -> None:
        tools = await mcp.list_tools()
        self.assertEqual({tool.name for tool in tools}, {
            "list_buckets", "list_objects", "inspect_bucket", "bucket_coverage_report",
            "permission_report", "download_object", "upload_object",
        })

    async def test_permission_tool_delegates_to_shared_service(self) -> None:
        service = Mock()
        service.permission_report.return_value = {"bucket": "allowed"}
        with patch("s3_mcp_agent.server._service", service):
            await mcp.call_tool("permission_report", {"bucket": "allowed"})
        service.permission_report.assert_called_once_with("allowed")

    async def test_permission_tool_surfaces_errors(self) -> None:
        service = Mock()
        service.permission_report.side_effect = S3MCPError("Bucket is not allowed")
        with patch("s3_mcp_agent.server._service", service):
            with self.assertRaisesRegex(Exception, "Bucket is not allowed"):
                await mcp.call_tool("permission_report", {"bucket": "blocked"})
