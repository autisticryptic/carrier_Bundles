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

from android.xiaomi.carrier_config import (
    import_xiaomi_carrier_config_catalog,
    load_xiaomi_xml_configs,
)
from android.xiaomi.catalog import import_xiaomi_baseband_catalog
from android.xiaomi.firmware import (
    ExtractedXiaomiCarrierConfig,
    extract_xiaomi_carrier_configs,
    extract_xiaomi_modem_artifacts,
)
from android.xiaomi.sources import XiaomiFastbootArtifact
from tools.verify_catalog import verify_catalog


class XiaomiBasebandTests(unittest.TestCase):
    def _add_member(self, archive: tarfile.TarFile, name: str, payload: bytes) -> None:
        info = tarfile.TarInfo(name)
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))

    def test_loads_fiveg_apn_table_as_apn_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "product/etc/fiveG-apns-conf.xml"
            config.parent.mkdir(parents=True)
            config.write_text(
                """<?xml version="1.0" encoding="utf-8"?>
<apns>
  <apn carrier="FiveG IMS" mcc="334" mnc="050" apn="ims"
       type="ims" protocol="IPV4V6"/>
</apns>
""",
                encoding="utf-8",
            )
            extracted = ExtractedXiaomiCarrierConfig(
                rom_path=root / "rom.zip",
                rom_sha256="0" * 64,
                root_dir=root,
                config_files=(config,),
                carrier_settings_dir=None,
            )

            configs, apns = load_xiaomi_xml_configs(extracted)

            self.assertEqual(configs, [])
            self.assertEqual(len(apns), 1)
            self.assertEqual(apns[0].plmn, "334050")

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
                archive.writestr(
                    "product/etc/carrier_config_310260.xml",
                    """<?xml version="1.0" encoding="utf-8"?>
<carrier_config>
  <boolean name="carrier_volte_available_bool" value="true"/>
  <boolean name="carrier_vonr_available_bool" value="true"/>
  <boolean name="carrier_wfc_ims_available_bool" value="true"/>
  <string name="iwlan.epdg_static_address_string">epdg.epc.mnc260.mcc310.pub.3gppnetwork.org</string>
  <boolean name="ims.sip_over_ipsec_enabled_bool" value="true"/>
  <int name="ims.sip_preferred_transport_int" value="3"/>
  <int name="ims.registration_expiry_timer_sec_int" value="600"/>
</carrier_config>
""",
                )
                archive.writestr(
                    "product/etc/apns-conf.xml",
                    """<?xml version="1.0" encoding="utf-8"?>
<apns>
  <apn carrier="Test Mobile" mcc="310" mnc="260" apn="ims"
       type="ims" protocol="IPV4V6" roaming_protocol="IPV6"
       authtype="1" user="ims-user" password="ims-pass" mtu="1420"/>
</apns>
""",
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
            carrier_config = extract_xiaomi_carrier_configs(rom, root / "carrier-config")
            self.assertGreaterEqual(len(carrier_config.config_files), 2)

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
            carrier_stats = import_xiaomi_carrier_config_catalog(
                database, carrier_config, artifact=artifact
            )
            self.assertEqual(carrier_stats.profiles_imported, 1)
            baseband_stats = import_xiaomi_baseband_catalog(database, firmware, artifact=artifact)
            self.assertEqual(baseband_stats.modem_artifacts_imported, 2)

            with closing(sqlite3.connect(database)) as connection:
                rows = connection.execute(
                    """SELECT source_kind, source_uri, artifact_sha256, source_revision
                       FROM source_artifacts ORDER BY source_id"""
                ).fetchall()
                self.assertEqual([row[0] for row in rows], [
                    "carrier_config",
                    "standards_reference",
                    "firmware_manifest",
                    "modem_config",
                    "modem_config",
                ])
                revisions = [json.loads(row[3]) for row in rows[3:]]
                self.assertEqual(
                    [revision["archive_member"] for revision in revisions],
                    [
                        "firmware-update/dsp.img",
                        "firmware-update/NON-HLOS.bin",
                    ],
                )
                self.assertEqual(
                    connection.execute("SELECT count(*) FROM carrier_profiles").fetchone()[0],
                    1,
                )
                self.assertEqual(
                    connection.execute("SELECT count(*) FROM profile_sources").fetchone()[0],
                    2,
                )
                config = json.loads(
                    connection.execute(
                        "SELECT config_json FROM carrier_profiles"
                    ).fetchone()[0]
                )
                self.assertEqual(config["access"]["lte"]["apn"], "ims")
                self.assertEqual(config["services"]["volte"], True)
                self.assertGreater(
                    connection.execute("SELECT count(*) FROM field_evidence").fetchone()[0],
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
            self.assertEqual(summary["counts"]["source_artifacts"], 5)
            self.assertEqual(summary["counts"]["carrier_profiles"], 1)
            self.assertEqual(summary["counts"]["profile_sources"], 2)


if __name__ == "__main__":
    unittest.main()
