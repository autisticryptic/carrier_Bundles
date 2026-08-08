import gzip
import hashlib
import plistlib
import tempfile
import unittest
import zipfile
from pathlib import Path

from ios.ipcc import IPCCArtifact, extract_ipcc, parse_ipcc_index, select_artifacts


def _index_fixture() -> bytes:
    return gzip.compress(
        plistlib.dumps(
            {
                "MobileDeviceCarrierBundlesByProductVersion": {
                    "TMobile_Germany": {
                        "ByProductType": {
                            "iPhone": {
                                "1": {
                                    "BundleName": "TMobile_Germany",
                                    "BundleVersion": "58.1",
                                    "BundleURL": "https://updates.cdn-apple.com/old/TMobile_Germany_iPhone.ipcc",
                                    "Digest": b"a" * 20,
                                },
                                "2": {
                                    "BundleName": "TMobile_Germany",
                                    "BundleVersion": "70.1",
                                    "BundleURL": "https://updates.cdn-apple.com/new/TMobile_Germany_iPhone.ipcc",
                                    "Digest": b"b" * 48,
                                },
                            }
                        }
                    }
                },
                "CountryBundles": {
                    "iPhone": {
                        "Bundles": {
                            "Germany_1": {
                                "BundleID": "Germany",
                                "BundleVersion": "64.1",
                                "BundleURL": "https://updates.cdn-apple.com/country/Germany_iPhone.ipcc",
                                "Digest": b"c" * 48,
                            }
                        }
                    }
                },
            }
        )
    )


class IPCCIndexTests(unittest.TestCase):
    def test_parses_gzip_plist_and_selects_latest_iphone_bundle(self) -> None:
        artifacts = parse_ipcc_index(_index_fixture())
        self.assertEqual(len(artifacts), 3)
        selected = select_artifacts(
            artifacts, product="iphone", queries=["tmobile"], latest_only=True
        )
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0].version, "70.1")
        self.assertEqual(selected[0].apple_digest_algorithm, "sha384")
        self.assertTrue(selected[0].url.endswith("new/TMobile_Germany_iPhone.ipcc"))

    def test_extracts_payload_into_normalized_carrier_bundle_tree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "example.ipcc"
            carrier_plist = plistlib.dumps({"CarrierName": "Example"})
            with zipfile.ZipFile(archive, "w") as output:
                output.writestr("Payload/Example.bundle/carrier.plist", carrier_plist)
                output.writestr("Payload/Example.bundle/Info.plist", plistlib.dumps({}))
            artifact = IPCCArtifact(
                bundle_id="Example",
                product="iphone",
                version="1.0",
                build_version=None,
                url="https://updates.cdn-apple.com/example/Example_iPhone.ipcc",
                apple_digest_algorithm=None,
                apple_digest_hex=None,
                index_path=("CarrierBundles", "iPhone", "Bundles", "Example"),
                sha256=hashlib.sha256(archive.read_bytes()).hexdigest(),
                archive_path=str(archive),
            )
            result = extract_ipcc(artifact, root / "export")
            extracted = (
                root
                / "export/System/Library/Carrier Bundles/iPhone/Example.bundle/carrier.plist"
            )
            self.assertTrue(extracted.is_file())
            self.assertEqual(
                result.extracted_bundles,
                ("System/Library/Carrier Bundles/iPhone/Example.bundle",),
            )

    def test_rejects_path_traversal_in_ipcc(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "unsafe.ipcc"
            with zipfile.ZipFile(archive, "w") as output:
                output.writestr("Payload/../outside", b"unsafe")
            artifact = IPCCArtifact(
                bundle_id="Unsafe",
                product="iphone",
                version=None,
                build_version=None,
                url="https://updates.cdn-apple.com/example/Unsafe_iPhone.ipcc",
                apple_digest_algorithm=None,
                apple_digest_hex=None,
                index_path=(),
                archive_path=str(archive),
            )
            with self.assertRaisesRegex(ValueError, "unsafe path"):
                extract_ipcc(artifact, root / "export")


if __name__ == "__main__":
    unittest.main()
