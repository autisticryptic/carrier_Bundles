import json
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

from tools.prepare_release import prepare_release


class PrepareReleaseTests(unittest.TestCase):
    def test_prepares_verified_catalog_assets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            assets = root / "release-assets"
            assets.mkdir()
            database = assets / "catalog.sqlite3"
            subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "tools/init_db.py"),
                    str(database),
                    "--release-id",
                    "release-test",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "tools/seal_db.py"),
                    str(database),
                    "--skip-icon-sync",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            # Git restores blobs as writable files even when the source catalog
            # was sealed. Release preparation must restore the lost mode bits.
            database.chmod(database.stat().st_mode | stat.S_IWUSR | stat.S_IXUSR)
            notes = assets / "RELEASE_NOTES.md"
            notes.write_text("Release test.\n", encoding="utf-8")
            manifest = assets / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "tag_name": "v0.0.0-test",
                        "release_name": "Release test",
                        "target_commitish": "a" * 40,
                        "prerelease": True,
                        "database": "release-assets/catalog.sqlite3",
                        "notes": "release-assets/RELEASE_NOTES.md",
                    }
                ),
                encoding="utf-8",
            )

            prepared = prepare_release(manifest, root=root)
            self.assertEqual(prepared.tag_name, "v0.0.0-test")
            self.assertTrue(prepared.prerelease)
            self.assertEqual(
                database.stat().st_mode
                & (
                    stat.S_IWUSR
                    | stat.S_IWGRP
                    | stat.S_IWOTH
                    | stat.S_IXUSR
                    | stat.S_IXGRP
                    | stat.S_IXOTH
                ),
                0,
            )
            self.assertTrue(prepared.summary_path.is_file())
            self.assertIn("release-test", prepared.summary_path.read_text())
            checksum = prepared.checksums_path.read_text(encoding="ascii")
            self.assertRegex(checksum, r"^[0-9a-f]{64}  catalog\.sqlite3\n$")


if __name__ == "__main__":
    unittest.main()
