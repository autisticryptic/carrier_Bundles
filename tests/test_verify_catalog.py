import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

from tools.verify_catalog import verify_catalog


class VerifyCatalogTests(unittest.TestCase):
    def test_sealed_catalog_passes_release_and_readonly_checks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "catalog.sqlite3"
            subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "tools/init_db.py"),
                    str(database),
                    "--release-id",
                    "verify-test",
                    "--generator-version",
                    "test-suite",
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

            result = verify_catalog(database)
            self.assertEqual(result["release_id"], "verify-test")
            self.assertEqual(result["generator_version"], "test-suite")
            self.assertEqual(result["schema_version"], 6)
            self.assertEqual(result["application_id"], 1128419922)
            self.assertEqual(result["quick_check"], "ok")
            self.assertEqual(result["foreign_key_check"], "ok")
            self.assertEqual(result["counts"]["carrier_profiles"], 0)


if __name__ == "__main__":
    unittest.main()
