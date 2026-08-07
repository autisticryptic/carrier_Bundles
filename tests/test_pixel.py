import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

from android.pixel.catalog import import_pixel_catalog
from android.pixel.firmware import ExtractedPixelFirmware
from android.pixel.proto.carrier_list_pb2 import CarrierList
from android.pixel.proto.carrier_settings_pb2 import ApnItem, MultiCarrierSettings
from android.pixel.sources import parse_factory_page


class PixelSourceTests(unittest.TestCase):
    def test_factory_page_parser_extracts_official_metadata(self) -> None:
        digest = "a" * 64
        html = f"""
<h2 id="redfin">"redfin" for Pixel 5</h2>
<table><tr>
  <td>14.0.0 (UP1A.231105.001.B2, Feb 2024)</td>
  <td><a href="https://dl.google.com/dl/android/aosp/redfin-test-factory-deadbeef.zip">Link</a></td>
  <td>{digest}</td>
</tr></table>
<h2 id="other">Other device</h2>
"""
        artifacts = parse_factory_page(html, "redfin")
        self.assertEqual(len(artifacts), 1)
        self.assertEqual(artifacts[0].build_id, "UP1A.231105.001.B2")
        self.assertEqual(artifacts[0].device_name, "Pixel 5")
        self.assertEqual(artifacts[0].os_version, "14.0.0")
        self.assertEqual(artifacts[0].sha256, digest)


class PixelCatalogTests(unittest.TestCase):
    def _add_setting(self, settings: MultiCarrierSettings, name: str, epdg: str) -> None:
        setting = settings.setting.add()
        setting.canonical_name = name
        setting.version = 7
        apn = setting.apns.apn.add()
        apn.name = "IMS"
        apn.value = "ims"
        apn.type.append(ApnItem.IMS)
        apn.bearer_bitmask = "14|18|20"
        apn.authtype = 1
        apn.user = "public-apn-user"
        apn.password = "public-apn-password"
        apn.protocol = ApnItem.IPV4V6
        apn.roaming_protocol = ApnItem.IPV6
        apn.mtu = 1420

        values = (
            ("carrier_volte_available_bool", "bool_value", True),
            ("carrier_vonr_available_bool", "bool_value", True),
            ("carrier_wfc_ims_available_bool", "bool_value", True),
            ("iwlan.epdg_static_address_string", "text_value", epdg),
            ("ims.sip_over_ipsec_enabled_bool", "bool_value", True),
            ("ims.sip_preferred_transport_int", "int_value", 3),
            ("ims.registration_expiry_timer_sec_int", "int_value", 600),
            (
                "ims.ims_user_agent_string",
                "text_value",
                "Google #MODEL# Android #AV#",
            ),
        )
        for key, field, value in values:
            config = setting.configs.config.add()
            config.key = key
            setattr(config, field, value)

    def test_import_maps_shared_others_file_without_raw_key_collisions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "pixel.sqlite3"
            with closing(sqlite3.connect(database)) as connection:
                connection.executescript((ROOT / "schema.sql").read_text(encoding="utf-8"))
                connection.execute(
                    """INSERT INTO catalog_release(
                           singleton, release_id, generated_at, generator_version
                       ) VALUES (1, 'pixel-test', '2026-08-07T00:00:00Z', 'test')"""
                )
                connection.commit()

            carrier_dir = root / "CarrierSettings"
            carrier_dir.mkdir()
            carrier_list = CarrierList(version=101)
            for name, plmn in (("alpha_us", "310260"), ("beta_gb", "23430")):
                entry = carrier_list.entry.add()
                entry.canonical_name = name
                entry.carrier_id.add(mcc_mnc=plmn)
            (carrier_dir / "carrier_list.pb").write_bytes(
                carrier_list.SerializeToString()
            )

            settings = MultiCarrierSettings(version=101)
            self._add_setting(
                settings,
                "alpha_us",
                "epdg.epc.mnc260.mcc310.pub.3gppnetwork.org",
            )
            self._add_setting(settings, "beta_gb", "epdg.example.net")
            (carrier_dir / "others.pb").write_bytes(settings.SerializeToString())

            mcfg_dir = root / "mcfg_sw"
            mcfg = mcfg_dir / "generic" / "Pixel" / "NA" / "Test" / "mcfg_sw.mbn"
            mcfg.parent.mkdir(parents=True)
            mcfg.write_bytes(b"public test MCFG fixture")
            firmware = ExtractedPixelFirmware(
                factory_zip=root / "factory.zip",
                inner_zip=root / "image.zip",
                product_image=root / "product.img",
                vendor_image=root / "vendor.img",
                carrier_settings_dir=carrier_dir,
                mcfg_dir=mcfg_dir,
                android_info=root / "android-info.txt",
                baseband_version="test-baseband",
            )

            stats = import_pixel_catalog(
                database,
                firmware,
                device="test-device",
                os_version="14.0.0",
                build_id="TEST.001",
                source_uri="https://example.invalid/factory.zip",
                factory_sha256="1" * 64,
            )
            self.assertEqual(stats.carrier_settings_imported, 2)
            self.assertEqual(stats.profiles_imported, 2)
            self.assertEqual(stats.access_configs_imported, 6)
            self.assertEqual(stats.mcfg_files_inventoried, 1)

            with closing(sqlite3.connect(database)) as connection:
                raw_count, distinct_keys = connection.execute(
                    """SELECT count(*), count(DISTINCT source_path || ':' || key_path)
                       FROM raw_config_values
                       WHERE source_path = 'etc/CarrierSettings/others.pb'"""
                ).fetchone()
                self.assertEqual(raw_count, distinct_keys)
                raw_paths = [
                    row[0]
                    for row in connection.execute(
                        """SELECT key_path FROM raw_config_values
                           WHERE source_path = 'etc/CarrierSettings/others.pb'"""
                    )
                ]
                self.assertTrue(any('settings["alpha_us"]' in path for path in raw_paths))
                self.assertTrue(any('settings["beta_gb"]' in path for path in raw_paths))

                self.assertEqual(
                    connection.execute(
                        "SELECT DISTINCT device_model_pattern FROM profile_match_rules"
                    ).fetchall(),
                    [("test-device",)],
                )
                self.assertEqual(
                    connection.execute(
                        """SELECT platform, vendor FROM source_snapshots
                           WHERE source_kind = 'standards_reference'"""
                    ).fetchone(),
                    ("shared", "3GPP"),
                )
                self.assertEqual(
                    connection.execute(
                        """SELECT platform, vendor FROM source_snapshots
                           WHERE source_kind = 'qualcomm_mcfg'"""
                    ).fetchone(),
                    ("modem", "Qualcomm"),
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT DISTINCT transport_preference FROM ims_configs"
                    ).fetchall(),
                    [("tls",)],
                )
                self.assertEqual(
                    connection.execute(
                        """SELECT apn_auth_type, apn_username, apn_password,
                                  ip_family, roaming_ip_family, mtu
                           FROM access_configs WHERE access_kind = 'lte_epc'
                           LIMIT 1"""
                    ).fetchone(),
                    (
                        "pap",
                        "public-apn-user",
                        "public-apn-password",
                        "ipv4v6",
                        "ipv6",
                        1420,
                    ),
                )
                self.assertIsNone(
                    connection.execute("PRAGMA foreign_key_check").fetchone()
                )


if __name__ == "__main__":
    unittest.main()
