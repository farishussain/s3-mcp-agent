"""S3 operations and local transfer safeguards."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from botocore.exceptions import ClientError

from .config import Settings

MAX_PUT_OBJECT_BYTES = 5 * 1024 * 1024 * 1024


class S3MCPError(RuntimeError):
    """An actionable error safe to return from an MCP tool."""


class S3Service:
    def __init__(self, client: Any, settings: Settings) -> None:
        self._client = client
        self._settings = settings

    def list_buckets(
        self,
        continuation_token: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        if not 1 <= limit <= 1000:
            raise S3MCPError("limit must be between 1 and 1000.")
        request: dict[str, Any] = {"MaxBuckets": limit}
        if continuation_token:
            request["ContinuationToken"] = continuation_token
        try:
            response = self._client.list_buckets(**request)
        except ClientError as exc:
            raise self._aws_error("list buckets", exc) from exc

        buckets = response.get("Buckets", [])
        if self._settings.allowed_buckets is not None:
            buckets = [
                bucket for bucket in buckets
                if bucket.get("Name") in self._settings.allowed_buckets
            ]
        return {
            "buckets": [
                {
                    "name": bucket["Name"],
                    "creation_date": bucket.get("CreationDate").isoformat()
                    if bucket.get("CreationDate")
                    else None,
                }
                for bucket in buckets
            ],
            "is_truncated": bool(response.get("NextContinuationToken")),
            "next_continuation_token": response.get("NextContinuationToken"),
        }

    def list_objects(
        self,
        bucket: str,
        prefix: str = "",
        continuation_token: str | None = None,
        limit: int = 100,
        *,
        delimiter: str | None = None,
    ) -> dict[str, Any]:
        self._check_bucket(bucket)
        if not 1 <= limit <= 1000:
            raise S3MCPError("limit must be between 1 and 1000.")
        request: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix, "MaxKeys": limit}
        if delimiter is not None:
            request["Delimiter"] = delimiter
        if continuation_token:
            request["ContinuationToken"] = continuation_token
        try:
            response = self._client.list_objects_v2(**request)
        except ClientError as exc:
            raise self._aws_error(f"list objects in bucket {bucket!r}", exc) from exc

        return {
            "bucket": bucket,
            "prefix": prefix,
            "objects": [
                {
                    "key": item["Key"],
                    "size": item["Size"],
                    "last_modified": item.get("LastModified").isoformat()
                    if item.get("LastModified")
                    else None,
                    "etag": item.get("ETag"),
                    "storage_class": item.get("StorageClass"),
                }
                for item in response.get("Contents", [])
            ],
            "is_truncated": response.get("IsTruncated", False),
            "next_continuation_token": response.get("NextContinuationToken"),
            "prefixes": [item["Prefix"] for item in response.get("CommonPrefixes", [])],
        }

    def storage_page(
        self,
        bucket: str,
        prefix: str = "",
        continuation_token: str | None = None,
        *,
        recursive: bool = False,
    ) -> dict[str, Any]:
        """Read one bounded metadata page, never object bodies or previous versions."""
        page = self.list_objects(
            bucket, prefix, continuation_token, 500,
            delimiter=None if recursive else "/",
        )
        if page["is_truncated"] and not page["next_continuation_token"]:
            raise S3MCPError("S3 returned an incomplete page without a continuation token.")
        types: dict[str, dict[str, int]] = {}
        marker_count = 0
        total_bytes = 0
        for item in page["objects"]:
            marker = item["key"].endswith("/") and item["size"] == 0
            kind = "folder-marker" if marker else _inferred_extension(item["key"])
            item["inferred_type"] = kind
            item["folder_marker"] = marker
            marker_count += int(marker)
            total_bytes += item["size"]
            group = types.setdefault(kind, {"objects": 0, "bytes": 0})
            group["objects"] += 1
            group["bytes"] += item["size"]
        return {
            **page,
            "scope": "recursive_prefix_page" if recursive else "immediate_prefix_page",
            "listed_at": datetime.now(timezone.utc).isoformat(),
            "page_totals": {
                "objects": len(page["objects"]),
                "bytes": total_bytes,
                "folder_markers": marker_count,
                "types": types,
            },
        }

    def inspect_bucket(self, bucket: str) -> dict[str, Any]:
        self._check_bucket(bucket)
        inspection: dict[str, Any] = {"bucket": bucket}
        operations = {
            "location": ("get_bucket_location", {}),
            "acl": ("get_bucket_acl", {}),
            "policy": ("get_bucket_policy", {}),
            "policy_status": ("get_bucket_policy_status", {}),
            "public_access_block": ("get_public_access_block", {}),
            "encryption": ("get_bucket_encryption", {}),
            "versioning": ("get_bucket_versioning", {}),
            "lifecycle": ("get_bucket_lifecycle_configuration", {}),
            "cors": ("get_bucket_cors", {}),
            "notifications": ("get_bucket_notification_configuration", {}),
            "replication": ("get_bucket_replication", {}),
            "logging": ("get_bucket_logging", {}),
            "website": ("get_bucket_website", {}),
            "ownership_controls": ("get_bucket_ownership_controls", {}),
            "object_lock": ("get_object_lock_configuration", {}),
            "tags": ("get_bucket_tagging", {}),
        }
        coverage_checks: dict[str, dict[str, Any]] = {}
        for name, (operation, extra) in operations.items():
            data, status = self._read_configuration(bucket, name, operation, extra)
            if status in {"access_denied", "unsupported", "error", "not_configured"}:
                inspection[name] = data
                coverage_checks[name] = {"status": status, **data}
                continue
            inspection[name] = data
            coverage_checks[name] = {"status": status, **data}

        collected = sum(check["status"] == "collected" for check in coverage_checks.values())
        not_configured = sum(
            check["status"] == "not_configured" for check in coverage_checks.values()
        )
        access_denied = sum(check["status"] == "access_denied" for check in coverage_checks.values())
        unsupported = sum(check["status"] == "unsupported" for check in coverage_checks.values())
        errors = sum(check["status"] == "error" for check in coverage_checks.values())
        inspection["coverage"] = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "summary": {
                "total_checks": len(coverage_checks),
                "collected": collected,
                "not_configured": not_configured,
                "access_denied": access_denied,
                "unsupported": unsupported,
                "errors": errors,
            },
            "checks": coverage_checks,
        }
        return inspection

    def permission_report(self, bucket: str) -> dict[str, Any]:
        """Report observed bucket-level permission configuration without inferring effective access."""
        self._check_bucket(bucket)
        definitions = {
            "acl": "get_bucket_acl",
            "policy": "get_bucket_policy",
            "policy_status": "get_bucket_policy_status",
            "public_access_block": "get_public_access_block",
            "ownership_controls": "get_bucket_ownership_controls",
        }
        sources: dict[str, dict[str, Any]] = {}
        for name, operation in definitions.items():
            data, status = self._read_configuration(bucket, name, operation)
            if status in {"collected", "not_configured"}:
                sources[name] = {"status": status, "observed": data}
            else:
                sources[name] = {
                    "status": status,
                    "observed": None,
                    "error_code": data.get("error_code"),
                    "message": data.get("message"),
                }

        acl_observed = sources["acl"]["observed"]
        acl_collected = sources["acl"]["status"] == "collected"
        acl_grants = []
        if acl_observed and acl_collected:
            acl_grants = [
                _summarize_acl_grant(grant)
                for grant in acl_observed.get("Grants", [])
            ]

        policy_observed = sources["policy"]["observed"]
        policy_document = (
            policy_observed.get("Policy")
            if policy_observed and sources["policy"]["status"] == "collected"
            else None
        )
        policy_statements, policy_parse_status = _summarize_policy_statements(policy_document)

        all_users_acl_grants = [
            grant
            for grant in acl_grants
            if str(grant.get("grantee", {}).get("URI", "")).endswith("/AllUsers")
        ]
        authenticated_users_acl_grants = [
            grant
            for grant in acl_grants
            if str(grant.get("grantee", {}).get("URI", "")).endswith("/AuthenticatedUsers")
            or str(grant.get("grantee", {}).get("URI", "")).endswith("/AuthenticatedUsers")
        ]
        policy_status_observed = sources["policy_status"]["observed"]
        policy_is_public = (
            policy_status_observed.get("PolicyStatus", {}).get("IsPublic")
            if policy_status_observed
            and sources["policy_status"]["status"] == "collected"
            else False
            if sources["policy_status"]["status"] == "not_configured"
            else None
        )
        block_observed = sources["public_access_block"]["observed"]
        block_configuration = (
            block_observed.get("PublicAccessBlockConfiguration")
            if block_observed and sources["public_access_block"]["status"] == "collected"
            else None
        )
        ownership_observed = sources["ownership_controls"]["observed"]
        ownership = (
            ownership_observed.get("OwnershipControls", {}).get("Rules", [])
            if ownership_observed
            and sources["ownership_controls"]["status"] == "collected"
            else []
        )
        ownership_modes = [rule.get("ObjectOwnership") for rule in ownership]
        acl_applicability = (
            "disabled"
            if "BucketOwnerEnforced" in ownership_modes
            else "applicable"
            if sources["ownership_controls"]["status"] == "collected"
            else "unknown"
        )

        return {
            "bucket": bucket,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "sources": sources,
            "acl": {
                "applicability": acl_applicability,
                "grants": acl_grants,
            },
            "bucket_policy": {
                "document": policy_document,
                "statements": policy_statements,
                "parse_status": policy_parse_status,
            },
            "public_access_indicators": {
                "bucket_policy_is_public": policy_is_public,
                "acl_has_public_grants": bool(all_users_acl_grants) if acl_collected else None,
                "public_acl_grants": all_users_acl_grants if acl_collected else None,
                "authenticated_users_acl_grants": (
                    authenticated_users_acl_grants if acl_collected else None
                ),
                "block_public_acls": (
                    block_configuration.get("BlockPublicAcls", False)
                    if block_configuration is not None
                    else False
                    if sources["public_access_block"]["status"] == "not_configured"
                    else None
                ),
                "ignore_public_acls": (
                    block_configuration.get("IgnorePublicAcls", False)
                    if block_configuration is not None
                    else False
                    if sources["public_access_block"]["status"] == "not_configured"
                    else None
                ),
                "block_public_policy": (
                    block_configuration.get("BlockPublicPolicy", False)
                    if block_configuration is not None
                    else False
                    if sources["public_access_block"]["status"] == "not_configured"
                    else None
                ),
                "restrict_public_buckets": (
                    block_configuration.get("RestrictPublicBuckets", False)
                    if block_configuration is not None
                    else False
                    if sources["public_access_block"]["status"] == "not_configured"
                    else None
                ),
            },
            "effective_access": {
                "status": "not_evaluated",
                "explanation": (
                    "These are observed bucket-level settings, not an effective-access decision."
                ),
                "not_evaluated_controls": [
                    "account-level S3 Block Public Access",
                    "IAM identity and role policies",
                    "service control policies",
                    "permissions boundaries and session policies",
                    "access-point policies and configurations",
                    "VPC endpoint policies",
                    "KMS key policies and grants",
                    "organization or external authorization controls",
                ],
            },
        }

    def _read_configuration(
        self,
        bucket: str,
        name: str,
        operation: str,
        extra: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], str]:
        try:
            result = getattr(self._client, operation)(Bucket=bucket, **(extra or {}))
        except ClientError as exc:
            error = exc.response.get("Error", {})
            code = error.get("Code", "Unknown")
            message = error.get("Message", "AWS returned an unspecified error.")
            if code in _ABSENT_CONFIGURATION_CODES[name]:
                return {"configured": False}, "not_configured"
            if code in _ACCESS_DENIED_CODES:
                return {"error_code": code, "message": message}, "access_denied"
            if code in _UNSUPPORTED_CODES:
                return {"error_code": code, "message": message}, "unsupported"
            return {"error_code": code, "message": message}, "error"

        if name == "policy":
            policy = result.get("Policy")
            if policy:
                try:
                    result["Policy"] = json.loads(policy)
                except json.JSONDecodeError:
                    result["Policy"] = policy
        result.pop("ResponseMetadata", None)
        if name == "policy_status" and result.get("PolicyStatus", {}).get("IsPublic") is False:
            configured = True
        else:
            configured = _is_configuration_present(name, result)
        return {"configured": configured, **result}, (
            "collected" if configured else "not_configured"
        )

    def bucket_coverage_report(self, bucket: str) -> dict[str, Any]:
        """Return a timestamped per-configuration coverage report for one bucket."""
        inspection = self.inspect_bucket(bucket)
        return {"bucket": bucket, **inspection["coverage"]}

    def download_object(self, bucket: str, key: str, local_path: str) -> dict[str, Any]:
        self._check_bucket(bucket)
        destination = self._resolve_allowed_path(local_path, for_write=True)
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            response = self._client.get_object(Bucket=bucket, Key=key)
        except ClientError as exc:
            raise self._aws_error(f"download object {key!r} from bucket {bucket!r}", exc) from exc

        content_length = response.get("ContentLength")
        if content_length is not None and content_length > self._settings.max_transfer_bytes:
            response["Body"].close()
            raise S3MCPError(
                f"Object is larger than the configured transfer limit "
                f"({self._settings.max_transfer_bytes} bytes)."
            )

        file_descriptor: int | None = None
        destination_created = False
        try:
            file_descriptor = os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
            destination_created = True
            total = 0
            with os.fdopen(file_descriptor, "wb") as target:
                file_descriptor = None
                for chunk in response["Body"].iter_chunks(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > self._settings.max_transfer_bytes:
                        raise S3MCPError(
                            f"Object exceeded the configured transfer limit "
                            f"({self._settings.max_transfer_bytes} bytes)."
                        )
                    target.write(chunk)
        except FileExistsError as exc:
            raise S3MCPError(f"Local destination already exists: {destination}") from exc
        except Exception:
            if destination_created:
                destination.unlink(missing_ok=True)
            raise
        finally:
            if file_descriptor is not None:
                os.close(file_descriptor)
            response["Body"].close()

        return {"bucket": bucket, "key": key, "local_path": str(destination), "bytes": total}

    def upload_object(
        self,
        bucket: str,
        key: str,
        local_path: str,
        content_type: str | None = None,
    ) -> dict[str, Any]:
        self._check_bucket(bucket)
        source = self._resolve_allowed_path(local_path, for_write=False)
        if not source.is_file():
            raise S3MCPError(f"Local source is not a regular file: {source}")
        size = source.stat().st_size
        upload_limit = min(self._settings.max_transfer_bytes, MAX_PUT_OBJECT_BYTES)
        if size > upload_limit:
            raise S3MCPError(
                f"File is larger than the upload limit ({upload_limit} bytes)."
            )

        request: dict[str, Any] = {
            "Bucket": bucket,
            "Key": key,
            "IfNoneMatch": "*",
            "ContentLength": size,
        }
        if content_type:
            request["ContentType"] = content_type
        try:
            with source.open("rb") as body:
                self._client.put_object(Body=body, **request)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in {"PreconditionFailed", "ConditionalRequestConflict"}:
                raise S3MCPError(
                    f"Object {key!r} already exists or was created concurrently; "
                    "the existing object was not overwritten."
                ) from exc
            raise self._aws_error(f"upload object {key!r} to bucket {bucket!r}", exc) from exc
        return {"bucket": bucket, "key": key, "bytes": size, "overwrote_existing_object": False}

    def _check_bucket(self, bucket: str) -> None:
        if not bucket:
            raise S3MCPError("bucket must not be empty.")
        if (
            self._settings.allowed_buckets is not None
            and bucket not in self._settings.allowed_buckets
        ):
            raise S3MCPError(f"Bucket {bucket!r} is not in S3_MCP_ALLOWED_BUCKETS.")

    def _resolve_allowed_path(self, raw_path: str, *, for_write: bool) -> Path:
        if not self._settings.allowed_paths:
            raise S3MCPError(
                "File transfers are disabled. Configure S3_MCP_ALLOWED_PATHS "
                "with one or more local directories."
            )
        candidate = Path(raw_path).expanduser()
        if not candidate.is_absolute():
            raise S3MCPError("Local transfer paths must be absolute.")

        try:
            if for_write:
                resolved = candidate.parent.resolve() / candidate.name
            else:
                resolved = candidate.resolve(strict=True)
        except OSError as exc:
            raise S3MCPError(f"Local path cannot be resolved: {candidate}") from exc
        if not any(resolved == root or root in resolved.parents for root in self._settings.allowed_paths):
            raise S3MCPError(f"Local path is outside the configured transfer directories: {resolved}")
        if for_write and resolved.exists():
            raise S3MCPError(f"Local destination already exists: {resolved}")
        return resolved

    @staticmethod
    def _aws_error(action: str, error: ClientError) -> S3MCPError:
        error_info = error.response.get("Error", {})
        code = error_info.get("Code", "Unknown")
        message = error_info.get("Message", "AWS returned an unspecified error.")
        return S3MCPError(f"Could not {action} ({code}): {message}")


_ABSENT_CONFIGURATION_CODES: dict[str, set[str]] = {
    "location": set(),
    "acl": set(),
    "policy": {"NoSuchBucketPolicy"},
    "policy_status": {"NoSuchBucketPolicy", "NoSuchPublicAccessBlockConfiguration"},
    "public_access_block": {"NoSuchPublicAccessBlockConfiguration"},
    "encryption": {"ServerSideEncryptionConfigurationNotFoundError"},
    "versioning": set(),
    "lifecycle": {"NoSuchLifecycleConfiguration"},
    "cors": {"NoSuchCORSConfiguration"},
    "notifications": set(),
    "replication": {"ReplicationConfigurationNotFoundError"},
    "logging": set(),
    "website": {"NoSuchWebsiteConfiguration"},
    "ownership_controls": {"OwnershipControlsNotFoundError"},
    "object_lock": {"ObjectLockConfigurationNotFoundError"},
    "tags": {"NoSuchTagSet"},
}

_ACCESS_DENIED_CODES = {"AccessDenied", "AccessDeniedException", "AllAccessDisabled", "403"}
_UNSUPPORTED_CODES = {"NotImplemented", "UnsupportedOperation"}


def _inferred_extension(key: str) -> str:
    name = key.rsplit("/", 1)[-1]
    if "." not in name or name.startswith(".") and name.count(".") == 1:
        return "extensionless/unknown"
    extension = name.rsplit(".", 1)[-1].lower()
    if not extension or len(extension) > 32 or not extension.isascii() or not extension.isalnum():
        return "extensionless/unknown"
    return "." + extension


def _is_configuration_present(name: str, response: dict[str, Any]) -> bool:
    if name == "versioning":
        return bool(response.get("Status") or response.get("MFADelete"))
    if name == "logging":
        return bool(response.get("LoggingEnabled"))
    if name == "notifications":
        return any(
            response.get(key)
            for key in ("TopicConfigurations", "QueueConfigurations", "LambdaFunctionConfigurations", "EventBridgeConfiguration")
        )
    return bool(response) or name in {"location", "acl", "policy_status"}


def _summarize_acl_grant(grant: dict[str, Any]) -> dict[str, Any]:
    grantee = grant.get("Grantee", {})
    return {
        "grantee": {
            key: grantee[key]
            for key in ("Type", "ID", "URI", "DisplayName")
            if key in grantee
        },
        "permission": grant.get("Permission"),
    }


def _summarize_policy_statements(
    document: Any,
) -> tuple[list[dict[str, Any]], str]:
    if document is None:
        return [], "not_present_or_unavailable"
    if not isinstance(document, dict):
        return [], "unrecognized_document"
    statements = document.get("Statement", [])
    if isinstance(statements, dict):
        statements = [statements]
    if not isinstance(statements, list):
        return [], "unrecognized_statement_format"
    fields = (
        "Sid",
        "Effect",
        "Principal",
        "NotPrincipal",
        "Action",
        "NotAction",
        "Resource",
        "NotResource",
        "Condition",
    )
    summarized = [
        {field: statement[field] for field in fields if field in statement}
        for statement in statements
        if isinstance(statement, dict)
    ]
    return summarized, "parsed"
