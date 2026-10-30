"""Loopback-only, read-only browser interface for bucket permissions."""

from __future__ import annotations

import argparse
import json
import logging
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from urllib.parse import parse_qs, urlsplit

from botocore.exceptions import BotoCoreError

from .config import Settings
from .service import S3MCPError, S3Service

LOGGER = logging.getLogger(__name__)


def create_server(service: S3Service, port: int = 8765) -> ThreadingHTTPServer:
    """Create a server bound exclusively to IPv4 loopback; port 0 is useful for tests."""
    server = ThreadingHTTPServer(
        ("127.0.0.1", port), partial(DashboardHandler, service=service)
    )
    return server


class DashboardHandler(BaseHTTPRequestHandler):
    def __init__(self, *args, service: S3Service, **kwargs):
        self.service = service
        super().__init__(*args, **kwargs)

    def log_message(self, format: str, *args) -> None:
        # URLs may contain bucket names; do not record request data.
        return

    def _respond(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
        )
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, value: object) -> None:
        self._respond(status, json.dumps(value).encode(), "application/json; charset=utf-8")

    def do_GET(self) -> None:
        port = self.server.server_address[1]
        host = f"127.0.0.1:{port}"
        origin = f"http://{host}"
        if self.headers.get("Host") != host:
            self._json(403, {"error": "Only same-origin loopback requests are accepted."})
            return

        url = urlsplit(self.path)
        assets = {
            "/": ("index.html", "text/html; charset=utf-8"),
            "/app.js": ("app.js", "text/javascript; charset=utf-8"),
            "/style.css": ("style.css", "text/css; charset=utf-8"),
        }
        if url.path in assets:
            name, content_type = assets[url.path]
            body = files("s3_mcp_agent").joinpath("static", name).read_bytes()
            self._respond(200, body, content_type)
            return
        if url.path not in {"/api/buckets", "/api/permissions", "/api/storage", "/api/storage-scan"}:
            self._json(404, {"error": "Not found."})
            return
        # A non-simple header forces foreign origins through a CORS preflight,
        # which this server never authorizes.
        if (
            self.headers.get("X-S3-Dashboard") != "1"
            or self.headers.get("Origin") not in (None, origin)
            or self.headers.get("Sec-Fetch-Site") not in (None, "none", "same-origin")
        ):
            self._json(403, {"error": "Same-origin dashboard request required."})
            return
        query = parse_qs(url.query, keep_blank_values=True)
        try:
            if url.path == "/api/buckets":
                result = self.service.list_buckets(
                    continuation_token=query.get("token", [None])[0], limit=100
                )
            else:
                bucket = query.get("bucket", [""])[0]
                if not bucket:
                    self._json(400, {"error": "A bucket name is required."})
                    return
                if url.path == "/api/permissions":
                    result = self.service.permission_report(bucket)
                else:
                    result = self.service.storage_page(
                        bucket,
                        prefix=query.get("prefix", [""])[0],
                        continuation_token=query.get("token", [None])[0],
                        recursive=url.path == "/api/storage-scan",
                    )
            self._json(200, result)
        except S3MCPError:
            self._json(400, {"error": "Request failed. Check the bucket allowlist and AWS permissions."})
        except BotoCoreError:
            self._json(503, {"error": "AWS is unavailable. Check your credentials, region and connection."})
        except Exception:
            LOGGER.error("Dashboard request failed unexpectedly (details withheld).")
            self._json(500, {"error": "Unexpected server error; see the local server status."})


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    settings = Settings.from_env()
    import boto3

    service = S3Service(boto3.client("s3"), settings)
    with create_server(service, args.port) as server:
        print(f"S3 permissions dashboard: http://127.0.0.1:{args.port}")
        print("Read-only local interface. Press Ctrl+C to stop.")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
