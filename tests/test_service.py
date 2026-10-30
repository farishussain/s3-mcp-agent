from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from botocore.exceptions import ClientError

from s3_mcp_agent.config import Settings
from s3_mcp_agent.service import S3MCPError, S3Service


class SettingsTests(unittest.TestCase):
    def test_from_env_parses_path_and_bucket_allowlists(self) -> None:
        with tempfile.TemporaryDirectory() as transfer_dir:
            with patch.dict(
                os.environ,
                {
                    "S3_MCP_ALLOWED_PATHS": f" {transfer_dir} ",
                    "S3_MCP_ALLOWED_BUCKETS": "one, two",
                    "S3_MCP_MAX_TRANSFER_BYTES": "256",
                },
                clear=True,
            ):
                settings = Settings.from_env()

        self.assertEqual(settings.allowed_paths, (Path(transfer_dir).resolve(),))
        self.assertEqual(settings.allowed_buckets, frozenset({"one", "two"}))
        self.assertEqual(settings.max_transfer_bytes, 256)

    def test_from_env_rejects_non_positive_transfer_limit(self) -> None:
        with patch.dict(os.environ, {"S3_MCP_MAX_TRANSFER_BYTES": "0"}, clear=True):
            with self.assertRaisesRegex(ValueError, "positive integer"):
                Settings.from_env()


def client_error(code: str, operation: str = "TestOperation") -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": "test failure"}},
        operation,
    )


class S3ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name).resolve()
        self.client = Mock()
        self.service = S3Service(
            self.client,
            Settings(
                allowed_paths=(self.root,),
                allowed_buckets=None,
                max_transfer_bytes=16,
            ),
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_list_buckets_uses_a_bounded_page_and_returns_token(self) -> None:
        self.client.list_buckets.return_value = {
            "Buckets": [{"Name": "bucket"}],
            "NextContinuationToken": "next",
        }

        result = self.service.list_buckets(limit=10)

        self.client.list_buckets.assert_called_once_with(MaxBuckets=10)
        self.assertEqual(result["next_continuation_token"], "next")
        self.assertTrue(result["is_truncated"])

    def test_list_buckets_rejects_an_unbounded_page_size(self) -> None:
        with self.assertRaisesRegex(S3MCPError, "between 1 and 1000"):
            self.service.list_buckets(limit=1001)
        self.client.list_buckets.assert_not_called()

    def test_list_objects_uses_a_bounded_page_and_returns_token(self) -> None:
        self.client.list_objects_v2.return_value = {
            "Contents": [{"Key": "folder/file.txt", "Size": 4}],
            "IsTruncated": True,
            "NextContinuationToken": "next",
        }

        result = self.service.list_objects("bucket", prefix="folder/", limit=25)

        self.client.list_objects_v2.assert_called_once_with(
            Bucket="bucket",
            Prefix="folder/",
            MaxKeys=25,
        )
        self.assertEqual(result["next_continuation_token"], "next")
        self.assertEqual(result["objects"][0]["key"], "folder/file.txt")

    def test_list_objects_rejects_an_unbounded_page_size(self) -> None:
        with self.assertRaisesRegex(S3MCPError, "between 1 and 1000"):
            self.service.list_objects("bucket", limit=1001)
        self.client.list_objects_v2.assert_not_called()

    def test_upload_uses_conditional_put_to_prevent_overwrite(self) -> None:
        source = self.root / "source.txt"
        source.write_bytes(b"hello")
        uploaded: list[bytes] = []
        self.client.put_object.side_effect = lambda **request: uploaded.append(
            request["Body"].read()
        )

        result = self.service.upload_object("bucket", "object", str(source), "text/plain")

        request = self.client.put_object.call_args.kwargs
        self.assertEqual(request["IfNoneMatch"], "*")
        self.assertEqual(request["ContentLength"], 5)
        self.assertEqual(request["ContentType"], "text/plain")
        self.assertEqual(uploaded, [b"hello"])
        self.assertFalse(result["overwrote_existing_object"])

    def test_upload_reports_concurrent_existing_object(self) -> None:
        source = self.root / "source.txt"
        source.write_bytes(b"hello")
        self.client.put_object.side_effect = client_error("PreconditionFailed")

        with self.assertRaisesRegex(S3MCPError, "not overwritten"):
            self.service.upload_object("bucket", "object", str(source))

    def test_upload_rejects_paths_outside_the_allowlist(self) -> None:
        with tempfile.NamedTemporaryFile() as source:
            with self.assertRaisesRegex(S3MCPError, "outside the configured"):
                self.service.upload_object("bucket", "object", source.name)
        self.client.put_object.assert_not_called()

    def test_download_writes_only_a_new_allowed_file(self) -> None:
        body = Mock()
        body.iter_chunks.return_value = iter([b"hello"])
        self.client.get_object.return_value = {"Body": body, "ContentLength": 5}
        destination = self.root / "nested" / "download.txt"

        result = self.service.download_object("bucket", "object", str(destination))

        self.assertEqual(destination.read_bytes(), b"hello")
        self.assertEqual(result["bytes"], 5)
        body.close.assert_called_once()

    def test_download_does_not_remove_existing_destination(self) -> None:
        destination = self.root / "already-here.txt"
        destination.write_bytes(b"keep")
        body = Mock()
        body.iter_chunks.return_value = iter([b"replacement"])
        self.client.get_object.return_value = {"Body": body, "ContentLength": 11}

        with self.assertRaisesRegex(S3MCPError, "already exists"):
            self.service.download_object("bucket", "object", str(destination))

        self.assertEqual(destination.read_bytes(), b"keep")
        self.client.get_object.assert_not_called()
        body.close.assert_not_called()

    def test_download_removes_partial_file_after_stream_failure(self) -> None:
        body = Mock()
        body.iter_chunks.return_value = iter([b"first", b"second-chunk"])
        self.client.get_object.return_value = {"Body": body, "ContentLength": 16}
        destination = self.root / "too-large.txt"

        with self.assertRaisesRegex(S3MCPError, "exceeded the configured transfer limit"):
            self.service.download_object("bucket", "object", str(destination))

        self.assertFalse(destination.exists())
        body.close.assert_called_once()

    def test_missing_optional_bucket_configuration_is_reported_as_unconfigured(self) -> None:
        self.client.get_bucket_location.return_value = {"LocationConstraint": None}
        self.client.get_bucket_acl.return_value = {"Grants": []}
        self.client.get_bucket_policy.side_effect = client_error("NoSuchBucketPolicy")
        self.client.get_bucket_policy_status.side_effect = client_error("NoSuchBucketPolicy")
        self.client.get_public_access_block.side_effect = client_error(
            "NoSuchPublicAccessBlockConfiguration"
        )
        self.client.get_bucket_encryption.side_effect = client_error(
            "ServerSideEncryptionConfigurationNotFoundError"
        )
        self.client.get_bucket_versioning.return_value = {}
        self.client.get_bucket_lifecycle_configuration.side_effect = client_error(
            "NoSuchLifecycleConfiguration"
        )
        self.client.get_bucket_cors.side_effect = client_error("NoSuchCORSConfiguration")
        self.client.get_bucket_notification_configuration.return_value = {}
        self.client.get_bucket_replication.side_effect = client_error(
            "ReplicationConfigurationNotFoundError"
        )
        self.client.get_bucket_logging.return_value = {}
        self.client.get_bucket_website.side_effect = client_error("NoSuchWebsiteConfiguration")
        self.client.get_bucket_ownership_controls.side_effect = client_error(
            "OwnershipControlsNotFoundError"
        )
        self.client.get_object_lock_configuration.side_effect = client_error(
            "ObjectLockConfigurationNotFoundError"
        )
        self.client.get_bucket_tagging.side_effect = client_error("NoSuchTagSet")

        result = self.service.inspect_bucket("bucket")

        self.assertEqual(result["policy"], {"configured": False})
        self.assertEqual(result["encryption"], {"configured": False})
        self.assertEqual(result["lifecycle"], {"configured": False})
        self.assertEqual(result["cors"], {"configured": False})
        self.assertEqual(result["website"], {"configured": False})
        self.assertEqual(result["ownership_controls"], {"configured": False})
        self.assertEqual(result["object_lock"], {"configured": False})
        self.assertEqual(result["tags"], {"configured": False})
        self.assertEqual(result["coverage"]["summary"]["total_checks"], 16)
        self.assertEqual(result["coverage"]["summary"]["not_configured"], 14)
        self.assertEqual(
            result["coverage"]["checks"]["cors"]["status"],
            "not_configured",
        )
        self.assertTrue(result["coverage"]["generated_at"].endswith("+00:00"))

    def test_coverage_report_records_denied_unsupported_and_failed_checks(self) -> None:
        self.client.get_bucket_location.return_value = {"LocationConstraint": "us-east-1"}
        self.client.get_bucket_acl.side_effect = client_error("AccessDenied")
        self.client.get_bucket_policy.side_effect = client_error("NoSuchBucketPolicy")
        self.client.get_bucket_policy_status.side_effect = client_error("NoSuchBucketPolicy")
        self.client.get_public_access_block.side_effect = client_error(
            "NoSuchPublicAccessBlockConfiguration"
        )
        self.client.get_bucket_encryption.side_effect = client_error(
            "ServerSideEncryptionConfigurationNotFoundError"
        )
        self.client.get_bucket_versioning.return_value = {}
        self.client.get_bucket_lifecycle_configuration.side_effect = client_error(
            "NoSuchLifecycleConfiguration"
        )
        self.client.get_bucket_cors.side_effect = client_error("UnsupportedOperation")
        self.client.get_bucket_notification_configuration.return_value = {}
        self.client.get_bucket_replication.side_effect = client_error(
            "ReplicationConfigurationNotFoundError"
        )
        self.client.get_bucket_logging.return_value = {}
        self.client.get_bucket_website.side_effect = client_error("NoSuchWebsiteConfiguration")
        self.client.get_bucket_ownership_controls.side_effect = client_error(
            "OwnershipControlsNotFoundError"
        )
        self.client.get_object_lock_configuration.side_effect = client_error("InternalError")
        self.client.get_bucket_tagging.side_effect = client_error("NoSuchTagSet")

        report = self.service.bucket_coverage_report("bucket")

        self.assertEqual(report["checks"]["acl"]["status"], "access_denied")
        self.assertEqual(report["checks"]["cors"]["status"], "unsupported")
        self.assertEqual(report["checks"]["object_lock"]["status"], "error")
        self.assertEqual(report["summary"]["access_denied"], 1)
        self.assertEqual(report["summary"]["unsupported"], 1)
        self.assertEqual(report["summary"]["errors"], 1)
        self.assertEqual(
            report["summary"]["total_checks"],
            sum(
                report["summary"][status]
                for status in ("collected", "not_configured", "access_denied", "unsupported", "errors")
            ),
        )
        self.assertEqual(report["checks"]["acl"]["error_code"], "AccessDenied")

    def test_permission_report_summarizes_bucket_permission_sources(self) -> None:
        self.client.get_bucket_acl.return_value = {
            "Owner": {"ID": "owner-id"},
            "Grants": [
                {
                    "Grantee": {
                        "Type": "Group",
                        "URI": "http://acs.amazonaws.com/groups/global/AllUsers",
                    },
                    "Permission": "READ",
                },
                {
                    "Grantee": {"Type": "CanonicalUser", "ID": "user-id"},
                    "Permission": "FULL_CONTROL",
                },
            ],
        }
        self.client.get_bucket_policy.return_value = {
            "Policy": json.dumps(
                {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Sid": "PublicRead",
                            "Effect": "Allow",
                            "Principal": "*",
                            "Action": "s3:GetObject",
                            "Resource": "arn:aws:s3:::bucket/*",
                            "Condition": {"Bool": {"aws:SecureTransport": "false"}},
                        }
                    ],
                }
            )
        }
        self.client.get_bucket_policy_status.return_value = {
            "PolicyStatus": {"IsPublic": True}
        }
        self.client.get_public_access_block.return_value = {
            "PublicAccessBlockConfiguration": {
                "BlockPublicAcls": True,
                "IgnorePublicAcls": True,
                "BlockPublicPolicy": False,
                "RestrictPublicBuckets": False,
            }
        }
        self.client.get_bucket_ownership_controls.return_value = {
            "OwnershipControls": {
                "Rules": [{"ObjectOwnership": "BucketOwnerEnforced"}]
            }
        }

        report = self.service.permission_report("bucket")

        self.assertEqual(report["acl"]["applicability"], "disabled")
        self.assertEqual(len(report["acl"]["grants"]), 2)
        statement = report["bucket_policy"]["statements"][0]
        self.assertEqual(statement["Principal"], "*")
        self.assertEqual(statement["Action"], "s3:GetObject")
        self.assertIn("Condition", statement)
        self.assertTrue(report["public_access_indicators"]["bucket_policy_is_public"])
        self.assertTrue(report["public_access_indicators"]["acl_has_public_grants"])
        self.assertTrue(report["public_access_indicators"]["block_public_acls"])
        self.assertEqual(report["effective_access"]["status"], "not_evaluated")
        self.assertEqual(report["sources"]["policy"]["status"], "collected")

    def test_permission_report_keeps_denied_evidence_unknown(self) -> None:
        self.client.get_bucket_acl.side_effect = client_error("AccessDenied")
        self.client.get_bucket_policy.side_effect = client_error("AccessDenied")
        self.client.get_bucket_policy_status.side_effect = client_error("AccessDenied")
        self.client.get_public_access_block.side_effect = client_error("AccessDenied")
        self.client.get_bucket_ownership_controls.side_effect = client_error("AccessDenied")

        report = self.service.permission_report("bucket")

        self.assertEqual(report["sources"]["acl"]["status"], "access_denied")
        self.assertIsNone(report["sources"]["acl"]["observed"])
        self.assertIsNone(report["public_access_indicators"]["acl_has_public_grants"])
        self.assertIsNone(report["public_access_indicators"]["bucket_policy_is_public"])
        self.assertIsNone(report["public_access_indicators"]["block_public_policy"])
        self.assertEqual(report["acl"]["applicability"], "unknown")
        self.assertEqual(report["effective_access"]["status"], "not_evaluated")

    def test_authenticated_users_acl_grant_is_not_reported_as_anonymous_public(self) -> None:
        self.client.get_bucket_acl.return_value = {
            "Grants": [
                {
                    "Grantee": {
                        "Type": "Group",
                        "URI": "http://acs.amazonaws.com/groups/global/AuthenticatedUsers",
                    },
                    "Permission": "READ",
                }
            ]
        }
        self.client.get_bucket_policy.side_effect = client_error("NoSuchBucketPolicy")
        self.client.get_bucket_policy_status.side_effect = client_error("NoSuchBucketPolicy")
        self.client.get_public_access_block.side_effect = client_error(
            "NoSuchPublicAccessBlockConfiguration"
        )
        self.client.get_bucket_ownership_controls.return_value = {
            "OwnershipControls": {"Rules": [{"ObjectOwnership": "BucketOwnerPreferred"}]}
        }

        report = self.service.permission_report("bucket")

        self.assertFalse(report["public_access_indicators"]["acl_has_public_grants"])
        self.assertEqual(report["public_access_indicators"]["public_acl_grants"], [])
        self.assertEqual(
            report["public_access_indicators"]["authenticated_users_acl_grants"][0]["permission"],
            "READ",
        )

    def test_bucket_allowlist_is_applied_to_every_bucket_operation(self) -> None:
        restricted = S3Service(
            self.client,
            Settings(
                allowed_paths=(self.root,),
                allowed_buckets=frozenset({"permitted"}),
                max_transfer_bytes=16,
            ),
        )

        with self.assertRaisesRegex(S3MCPError, "not in S3_MCP_ALLOWED_BUCKETS"):
            restricted.permission_report("blocked")
        self.client.get_bucket_location.assert_not_called()
        self.client.get_bucket_acl.assert_not_called()


if __name__ == "__main__":
    unittest.main()
