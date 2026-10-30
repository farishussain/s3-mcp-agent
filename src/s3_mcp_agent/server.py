"""MCP stdio server exposing the supported S3 operations."""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from .config import Settings
from .service import S3Service

mcp = FastMCP("s3-mcp-agent")
_service: S3Service | None = None


def _get_service() -> S3Service:
    global _service
    if _service is None:
        import boto3

        _service = S3Service(boto3.client("s3"), Settings.from_env())
    return _service


@mcp.tool()
def list_buckets(
    continuation_token: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """List one bounded page of buckets visible to the configured AWS identity."""
    return _get_service().list_buckets(continuation_token, limit)


@mcp.tool()
def list_objects(
    bucket: str,
    prefix: str = "",
    continuation_token: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """List one bounded page of objects in a bucket."""
    return _get_service().list_objects(bucket, prefix, continuation_token, limit)


@mcp.tool()
def inspect_bucket(bucket: str) -> dict[str, Any]:
    """Inspect bucket configuration and include per-check collection coverage."""
    return _get_service().inspect_bucket(bucket)


@mcp.tool()
def bucket_coverage_report(bucket: str) -> dict[str, Any]:
    """Report which supported configuration checks were collected, absent, denied or unsupported."""
    return _get_service().bucket_coverage_report(bucket)


@mcp.tool()
def permission_report(bucket: str) -> dict[str, Any]:
    """Report observed bucket ACLs, policy, public-access controls and ownership without inferring effective access."""
    return _get_service().permission_report(bucket)


@mcp.tool()
def download_object(bucket: str, key: str, local_path: str) -> dict[str, Any]:
    """Download an object to a new file under S3_MCP_ALLOWED_PATHS."""
    return _get_service().download_object(bucket, key, local_path)


@mcp.tool()
def upload_object(
    bucket: str,
    key: str,
    local_path: str,
    content_type: str | None = None,
) -> dict[str, Any]:
    """Upload a local file without overwriting an existing object."""
    return _get_service().upload_object(bucket, key, local_path, content_type)


def main() -> None:
    """Start the server using MCP's stdio transport."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
