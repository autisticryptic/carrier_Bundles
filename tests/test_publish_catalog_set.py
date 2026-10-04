"""Publication safety unit tests; no database creation, network or API writes."""
import copy
import importlib.util
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

SCRIPT = Path(__file__).resolve().parents[1] / "tools/publish_catalog_set.py"
if not SCRIPT.exists():
    SCRIPT = Path(__file__).with_name("publish_catalog_set.py")
spec = importlib.util.spec_from_file_location("publication", SCRIPT)
pub = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pub)
PINS = Path(__file__).resolve().parents[1] / "release-staging/catalog-set/pins.json"
if not PINS.exists():
    PINS = Path(__file__).with_name("pins.json")


class PublicationSafety(unittest.TestCase):
    def setUp(self):
        self.pins = json.loads(PINS.read_text(encoding="utf-8"))

    def test_pinned_set(self):
        pub.pins_check(self.pins)
        self.assertEqual(len(pub.DB_NAMES), 12)
        self.assertEqual(len(pub.ASSET_NAMES), 20)

    def test_wrong_commit_fails(self):
        self.pins["commit"] = "0" * 40
        with self.assertRaises(ValueError):
            pub.pins_check(self.pins)

    def test_wrong_digest_fails(self):
        self.pins["artifact_digest"] = "sha256:" + "0" * 64
        with self.assertRaises(ValueError):
            pub.pins_check(self.pins)

    def test_extra_asset_fails(self):
        self.pins["assets"]["extra"] = next(iter(self.pins["assets"].values()))
        with self.assertRaises(ValueError):
            pub.pins_check(self.pins)

    def test_checksum_traversal_fails(self):
        with self.assertRaises(ValueError):
            pub.parse_sums("a" * 64 + "  ../bad")

    def test_checksum_duplicate_fails(self):
        line = "a" * 64 + "  database.sqlite3\n"
        with self.assertRaises(ValueError):
            pub.parse_sums(line + line)

    def test_checksum_accepts_strict_format(self):
        self.assertEqual(pub.parse_sums("a" * 64 + "  catalog.json\n"), {"catalog.json": "a" * 64})

    def test_existing_tag_refused_without_write(self):
        api = Mock()
        api.request.return_value = {"object": {"sha": pub.COMMIT}}
        with self.assertRaises(ValueError):
            pub.check_unused(api)
        self.assertEqual(api.request.call_count, 1)
        self.assertNotIn("POST", str(api.mock_calls))

    def test_existing_release_refused_without_write(self):
        api = Mock()
        api.request.side_effect = [None, {"tag_name": pub.TAG}]
        with self.assertRaises(ValueError):
            pub.check_unused(api)
        self.assertNotIn("POST", str(api.mock_calls))

    def test_existing_draft_refused_without_write(self):
        api = Mock()
        api.request.side_effect = [None, None, [{"tag_name": pub.TAG, "draft": True}]]
        with self.assertRaises(ValueError):
            pub.check_unused(api)
        self.assertNotIn("POST", str(api.mock_calls))

    def test_unused_tag(self):
        api = Mock()
        api.request.side_effect = [None, None, []]
        pub.check_unused(api)

    def uploaded(self):
        return [{"name": name, "size": item["bytes"], "digest": "sha256:" + item["sha256"],
                 "state": "uploaded"} for name, item in self.pins["assets"].items()]

    def test_upload_set_exact(self):
        pub.check_uploaded(self.uploaded(), self.pins)

    def test_missing_upload_blocks_publication(self):
        with self.assertRaises(ValueError):
            pub.check_uploaded(self.uploaded()[1:], self.pins)

    def test_bad_upload_hash_blocks_publication(self):
        assets = self.uploaded()
        assets[0]["digest"] = "sha256:" + "0" * 64
        with self.assertRaises(ValueError):
            pub.check_uploaded(assets, self.pins)

    def test_bad_upload_size_blocks_publication(self):
        assets = self.uploaded()
        assets[0]["size"] += 1
        with self.assertRaises(ValueError):
            pub.check_uploaded(assets, self.pins)

    def test_duplicate_upload_blocks_publication(self):
        assets = self.uploaded()
        assets[-1] = assets[0]
        with self.assertRaises(ValueError):
            pub.check_uploaded(assets, self.pins)

    def test_unarmed_publish_never_calls_api(self):
        api = Mock()
        with self.assertRaises(ValueError):
            pub.publish(api, Path("unused"), self.pins, {"publish": False})
        api.request.assert_not_called()

    def test_existing_release_never_mutated(self):
        api = Mock()
        api.request.return_value = {"tag_name": pub.TAG}
        with patch.object(pub, "check_dry_run"), self.assertRaises(ValueError):
            pub.publish(api, Path("unused"), self.pins,
                        {"publish": True, "tag": pub.TAG, "target": pub.COMMIT})
        self.assertNotIn("POST", str(api.mock_calls))
        self.assertNotIn("PATCH", str(api.mock_calls))

    def test_no_auth_to_storage_host(self):
        with self.assertRaises(ValueError):
            pub.GitHub("fixture-not-a-secret").request("https://storage.example.invalid/path")


if __name__ == "__main__":
    unittest.main()
