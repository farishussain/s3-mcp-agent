"""Environment-backed configuration for the S3 MCP server."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


DEFAULT_MAX_TRANSFER_BYTES = 100 * 1024 * 1024


@dataclass(frozen=True)
class Settings:
    allowed_paths: tuple[Path, ...]
    allowed_buckets: frozenset[str] | None
    max_transfer_bytes: int

    @classmethod
    def from_env(cls) -> Settings:
        raw_paths = os.environ.get("S3_MCP_ALLOWED_PATHS", "")
        paths = tuple(
            Path(entry.strip()).expanduser().resolve()
            for entry in raw_paths.split(os.pathsep)
            if entry.strip()
        )
        raw_buckets = os.environ.get("S3_MCP_ALLOWED_BUCKETS", "").strip()
        buckets = (
            frozenset(bucket.strip() for bucket in raw_buckets.split(",") if bucket.strip())
            if raw_buckets
            else None
        )

        raw_limit = os.environ.get("S3_MCP_MAX_TRANSFER_BYTES", str(DEFAULT_MAX_TRANSFER_BYTES))
        try:
            limit = int(raw_limit)
        except ValueError as exc:
            raise ValueError("S3_MCP_MAX_TRANSFER_BYTES must be a positive integer.") from exc
        if limit <= 0:
            raise ValueError("S3_MCP_MAX_TRANSFER_BYTES must be a positive integer.")

        return cls(
            allowed_paths=paths,
            allowed_buckets=buckets,
            max_transfer_bytes=limit,
        )
