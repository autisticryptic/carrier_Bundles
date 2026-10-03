"""Regression tests for the APN-only Xiaomi extraction failure.

The partition contents here are synthetic; these tests do not claim that an
unavailable Xiaomi APK or full OTA has been decoded successfully.
"""

import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from android.xiaomi.carrier_config import _build_config, load_xiaomi_xml_configs
from android.xiaomi.firmware import (
    CONFIG_FALLBACK_PAYLOAD_PARTITIONS,
    CONFIG_MANIFEST_NAME,
    CONFIG_MANIFEST_SCHEMA,
    CONFIG_PRIMARY_PAYLOAD_PARTITIONS,
    ExtractedXiaomiCarrierConfig,
    _collect_config_files,
    _extract_config_from_partition,
    extract_xiaomi_carrier_configs,
)


APNS = '<apns><apn carrier="Test" mcc="310" mnc="260" apn="ims" type="ims"/></apns>'
WFC = '''<carrier_config>
<boolean name="carrier_wfc_ims_available_bool" value="true"/>
</carrier_config>'''


class XiaomiExtractionTests(unittest.TestCase):
    def test_inventory_excludes_unrelated_protobufs_from_real_catalog_manifest(self):
        # These three unrelated paths were present in the xuanyuan catalog's
        # config_files metadata, alongside only the two product APN tables.
        unrelated = (
            "etc/aconfig_flags.pb",
            "etc/linker.config.pb",
            "usr/srec/en-US/offline_action_data.pb",
        )
        supported = (
            "etc/apns-conf.xml",
            "etc/fiveG-apns-conf.xml",
            "etc/CarrierSettings/carrier_list.pb",
            "etc/CarrierSettings/others.pb",
            "etc/CarrierConfig/carrier_config_310260.xml",
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in (*unrelated, *supported):
                path = root / "extracted/product/erofs-extract" / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"test")
            files = _collect_config_files(root)
            self.assertEqual(len(files), len(supported))
            self.assertEqual({p.name for p in files}, {Path(p).name for p in supported})

    def test_seven_zip_does_not_admit_unrelated_protobufs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / "product.img"
            image.write_bytes(b"test image")
            output = root / "extracted"

            def extract(*args, **kwargs):
                for name in ("etc/apns-conf.xml", "etc/linker.config.pb"):
                    path = output / "product" / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(b"test")

            with (
                patch("android.xiaomi.firmware._extract_erofs_config_from_partition", return_value=[]),
                patch("android.xiaomi.firmware.shutil.which", return_value="7z"),
                patch("android.xiaomi.firmware.subprocess.run", side_effect=extract),
            ):
                files = _extract_config_from_partition(image, output)
            self.assertEqual([p.name for p in files], ["apns-conf.xml"])

    def test_apns_do_not_stop_payload_scan_and_v1_cache_is_invalidated(self):
        for loose_apns in (False, True):
            with self.subTest(loose_apns=loose_apns), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                rom = root / "rom.zip"
                with zipfile.ZipFile(rom, "w") as archive:
                    archive.writestr("payload.bin", b"synthetic payload")
                    if loose_apns:
                        archive.writestr("product/etc/apns-conf.xml", APNS)
                output = root / "carrier-config"
                output.mkdir()
                calls = []

                def partitions(rom_path, output_dir, names):
                    calls.append(names)
                    images = []
                    for name in names:
                        image = root / f"{name}.img"
                        image.write_bytes(b"synthetic image")
                        images.append(image)
                    return images

                def configs(image, output_dir):
                    # Product has APNs; the WFC policy is only in system_ext.
                    name = {
                        "product": "apns-conf.xml",
                        "system_ext": "carrier_config_310260.xml",
                    }.get(image.stem)
                    if name is None:
                        return []
                    path = output_dir / image.stem / "etc" / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(APNS if image.stem == "product" else WFC)
                    return [path]

                with (
                    patch("android.xiaomi.firmware._extract_zip_payload_partitions", side_effect=partitions),
                    patch("android.xiaomi.firmware._extract_config_from_partition", side_effect=configs),
                ):
                    extracted = extract_xiaomi_carrier_configs(rom, output)
                    self.assertEqual(calls, [CONFIG_PRIMARY_PAYLOAD_PARTITIONS, CONFIG_FALLBACK_PAYLOAD_PARTITIONS])
                    xml_configs, apns = load_xiaomi_xml_configs(extracted)
                    self.assertTrue(xml_configs[0].values["carrier_wfc_ims_available_bool"])
                    self.assertTrue(apns)

                    # A valid new manifest avoids redundant extraction.
                    calls.clear()
                    cached = extract_xiaomi_carrier_configs(rom, output)
                    self.assertEqual(cached.config_files, extracted.config_files)
                    self.assertEqual(calls, [])

                    # Reproduce the old product-only manifest, including its
                    # original schema. Upgrading must actually scan again.
                    manifest = output / CONFIG_MANIFEST_NAME
                    document = json.loads(manifest.read_text())
                    self.assertEqual(document["schema"], CONFIG_MANIFEST_SCHEMA)
                    document["schema"] = "carrier-bundles-xiaomi-carrier-config-manifest-v1"
                    for path in extracted.config_files:
                        if path.name.startswith("carrier_config"):
                            path.unlink()
                    document["config_files"] = [str(p) for p in _collect_config_files(output)]
                    manifest.write_text(json.dumps(document))
                    rescanned = extract_xiaomi_carrier_configs(rom, output)
                    self.assertEqual(calls, [CONFIG_PRIMARY_PAYLOAD_PARTITIONS, CONFIG_FALLBACK_PAYLOAD_PARTITIONS])
                    self.assertTrue(any(p.name.startswith("carrier_config") for p in rescanned.config_files))

    def test_apn_only_profiles_do_not_imply_vowifi_or_epdg(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "apns-conf.xml"
            path.write_text(APNS)
            extracted = ExtractedXiaomiCarrierConfig(
                rom_path=root / "rom.zip", rom_sha256="0" * 64,
                root_dir=root, config_files=(path,), carrier_settings_dir=None,
            )
            configs, apns = load_xiaomi_xml_configs(extracted)
            self.assertEqual(configs, [])
            for standard_derived in (False, True):
                config = _build_config({}, apns, "310260", include_standard_derived=standard_derived)
                self.assertNotIn("vowifi", config["access"])
                self.assertNotIn("vowifi", config["services"])
                self.assertNotIn("epdg", json.dumps(config))


if __name__ == "__main__":
    unittest.main()
