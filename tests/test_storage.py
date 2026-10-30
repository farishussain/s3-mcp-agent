import unittest
from unittest.mock import Mock

from botocore.exceptions import ClientError

from s3_mcp_agent.config import Settings
from s3_mcp_agent.service import S3MCPError, S3Service


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.service = S3Service(self.client, Settings((), frozenset({"allowed"}), 100))

    def test_immediate_listing_has_delimiter_and_bounded_page(self):
        self.client.list_objects_v2.return_value = {
            "Contents": [
                {"Key": "data/a.CSV", "Size": 12},
                {"Key": "data/", "Size": 0},
            ],
            "CommonPrefixes": [{"Prefix": "data/nested/"}],
            "IsTruncated": True,
            "NextContinuationToken": "next",
        }
        page = self.service.storage_page("allowed", "data/", "previous")
        self.client.list_objects_v2.assert_called_once_with(
            Bucket="allowed", Prefix="data/", MaxKeys=500, Delimiter="/",
            ContinuationToken="previous",
        )
        self.assertEqual(page["scope"], "immediate_prefix_page")
        self.assertEqual(page["prefixes"], ["data/nested/"])
        self.assertEqual(page["page_totals"]["objects"], 2)
        self.assertEqual(page["page_totals"]["bytes"], 12)
        self.assertEqual(page["page_totals"]["folder_markers"], 1)
        self.assertEqual(page["objects"][0]["inferred_type"], ".csv")
        self.assertTrue(page["objects"][1]["folder_marker"])
        self.assertTrue(page["is_truncated"])

    def test_recursive_scan_pages_include_nested_keys_but_never_read_contents(self):
        self.client.list_objects_v2.side_effect = [
            {"Contents": [{"Key": "data/nested/a.json", "Size": 20}],
             "IsTruncated": True, "NextContinuationToken": "next"},
            {"Contents": [{"Key": "data/b.json", "Size": 30}], "IsTruncated": False},
        ]
        first = self.service.storage_page("allowed", "data/", recursive=True)
        second = self.service.storage_page("allowed", "data/", "next", recursive=True)
        self.assertEqual(first["scope"], "recursive_prefix_page")
        self.assertEqual(first["page_totals"]["bytes"] + second["page_totals"]["bytes"], 50)
        self.assertEqual(first["page_totals"]["types"][".json"]["objects"], 1)
        self.assertFalse(second["is_truncated"])
        for call in self.client.list_objects_v2.call_args_list:
            self.assertNotIn("Delimiter", call.kwargs)
            self.assertEqual(call.kwargs["Prefix"], "data/")
        self.client.get_object.assert_not_called()
        self.client.head_object.assert_not_called()
        self.client.put_object.assert_not_called()

    def test_extension_inference_and_folder_marker_classification(self):
        keys = [
            ("README", 2, "extensionless/unknown"),
            (".hidden", 2, "extensionless/unknown"),
            ("trailing.", 2, "extensionless/unknown"),
            ("a." + "x" * 33, 2, "extensionless/unknown"),
            ("path/dots.name/PIC.JPG", 2, ".jpg"),
            ("archive.tar.gz", 2, ".gz"),
            ("marker/", 0, "folder-marker"),
            ("nonempty/", 7, "extensionless/unknown"),
        ]
        self.client.list_objects_v2.return_value = {
            "Contents": [{"Key": key, "Size": size} for key, size, _ in keys]
        }
        page = self.service.storage_page("allowed")
        self.assertEqual(
            [item["inferred_type"] for item in page["objects"]],
            [kind for _, _, kind in keys],
        )
        self.assertEqual(page["page_totals"]["objects"], len(keys))
        self.assertEqual(sum(group["bytes"] for group in page["page_totals"]["types"].values()), 19)

    def test_denial_scope_and_broken_pagination_are_not_zero_totals(self):
        with self.assertRaises(S3MCPError):
            self.service.storage_page("blocked", recursive=True)
        self.client.list_objects_v2.assert_not_called()
        self.client.list_objects_v2.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied", "Message": "denied"}}, "ListObjectsV2"
        )
        with self.assertRaisesRegex(S3MCPError, "AccessDenied"):
            self.service.storage_page("allowed")
        self.client.list_objects_v2.side_effect = None
        self.client.list_objects_v2.return_value = {"IsTruncated": True}
        with self.assertRaisesRegex(S3MCPError, "continuation token"):
            self.service.storage_page("allowed")

    def test_empty_prefix_is_confirmed_empty_only_after_success(self):
        self.client.list_objects_v2.return_value = {}
        page = self.service.storage_page("allowed", "empty/", recursive=True)
        self.assertEqual(page["page_totals"]["objects"], 0)
        self.assertEqual(page["page_totals"]["bytes"], 0)
        self.assertFalse(page["is_truncated"])
