import io
import json
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from contextlib import closing
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

from android.xiaomi.catalog import import_xiaomi_baseband_catalog
from android.xiaomi.firmware import extract_xiaomi_modem_artifacts
from android.xiaomi.sources import XiaomiFastbootArtifact
from tools.verify_catalog import verify_catalog


class XiaomiBasebandTests(unittest.TestCase):
    def _add_member(self, archive: tarfile.TarFile, name: str, payload: bytes) -> None:
        info = tarfile.TarInfo(name)
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))

    def test_extracts_and_imports_baseband_inventory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rom = root / "xiaomi-firmware.zip"
            with zipfile.ZipFile(rom, "w") as archive:
                archive.writestr(
                    "firmware-update/NON-HLOS.bin",
                    b"public modem firmware",
                )
                archive.writestr(
                    "firmware-update/dsp.img",
                    b"public dsp firmware",
                )
                archive.writestr(
                    "payload.bin",
                    b"not part of the modem inventory",
                )

            tar_rom = root / "xiaomi-fastboot.tgz"
            with tarfile.open(tar_rom, "w:gz") as archive:
                self._add_member(
                    archive,
                    "xuanyuan_global_images/images/NON-HLOS.bin",
                    b"public modem firmware",
                )
                self._add_member(
                    archive,
                    "xuanyuan_global_images/images/dsp.img",
                    b"public dsp firmware",
                )
                self._add_member(
                    archive,
                    "xuanyuan_global_images/images/super.img",
                    b"not part of the modem inventory",
                )
            tar_firmware = extract_xiaomi_modem_artifacts(tar_rom, root / "tar-extract")
            self.assertEqual(len(tar_firmware.modem_artifacts), 2)

            firmware = extract_xiaomi_modem_artifacts(rom, root / "extract")
            self.assertEqual(
                [item.extracted_path.name for item in firmware.modem_artifacts],
                ["dsp.img", "NON-HLOS.bin"],
            )
            self.assertTrue((root / "extract/xiaomi-baseband-manifest.json").is_file())
            cached = extract_xiaomi_modem_artifacts(rom, root / "extract")
            self.assertEqual(cached.rom_sha256, firmware.rom_sha256)
            self.assertEqual(
                [item.sha256 for item in cached.modem_artifacts],
                [item.sha256 for item in firmware.modem_artifacts],
            )

            database = root / "xiaomi.sqlite3"
            subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "tools/init_db.py"),
                    str(database),
                    "--release-id",
                    "xiaomi-test",
                    "--generator-version",
                    "test",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            artifact = XiaomiFastbootArtifact(
                device_name="Xiaomi 15 Ultra",
                codename="xuanyuan",
                region="Global",
                android_version="16",
                build_id="OS3.0.301.0.WOAMIXM",
                url="https://bigota.d.miui.com/example.tgz",
                md5=None,
                package_kind="firmware_zip",
            )
            stats = import_xiaomi_baseband_catalog(database, firmware, artifact=artifact)
            self.assertEqual(stats.modem_artifacts_imported, 2)

            with closing(sqlite3.connect(database)) as connection:
                rows = connection.execute(
                    """SELECT source_kind, source_uri, artifact_sha256, source_revision
                       FROM source_artifacts ORDER BY source_id"""
                ).fetchall()
                self.assertEqual([row[0] for row in rows], [
                    "firmware_manifest",
                    "modem_config",
                    "modem_config",
                ])
                revisions = [json.loads(row[3]) for row in rows[1:]]
                self.assertEqual(
                    [revision["archive_member"] for revision in revisions],
                    [
                        "firmware-update/dsp.img",
                        "firmware-update/NON-HLOS.bin",
                    ],
                )
                self.assertEqual(
                    connection.execute("SELECT count(*) FROM carrier_profiles").fetchone()[0],
                    0,
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
            summary = verify_catalog(database)
            self.assertEqual(summary["counts"]["source_artifacts"], 3)
            self.assertEqual(summary["counts"]["carrier_profiles"], 0)


if __name__ == "__main__":
    unittest.main()
