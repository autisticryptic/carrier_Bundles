import sqlite3
import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

from android.pixel.catalog import import_pixel_catalog
from android.pixel.firmware import ExtractedPixelFirmware
from android.pixel.proto.carrier_list_pb2 import CarrierList
from android.pixel.proto.carrier_settings_pb2 import ApnItem, MultiCarrierSettings
from android.pixel.sources import choose_latest_artifact, parse_factory_page


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

    def test_latest_prefers_global_build_over_carrier_row(self) -> None:
        digest = "b" * 64
        html = f'''
<h2 id="mustang">"mustang" for Pixel 10 Pro XL</h2>
<table>
  <tr><td>17.0.0 (CP2A.260805.005, Aug 2026)</td>
      <td><a href="https://dl.google.com/dl/android/aosp/mustang-global-factory-aaaa1111.zip">Link</a></td>
      <td>{digest}</td></tr>
  <tr><td>17.0.0 (CP2A.260805.005.A1, Aug 2026) Rogers</td>
      <td><a href="https://dl.google.com/dl/android/aosp/mustang-rogers-factory-bbbb2222.zip">Link</a></td>
      <td>{digest}</td></tr>
</table>'''
        artifacts = parse_factory_page(html, "mustang")
        self.assertEqual(choose_latest_artifact(artifacts).build_id, "CP2A.260805.005")


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

        for key, items in (
            ("iwlan.supported_ike_session_encryption_algorithms_int_array", [12]),
            ("iwlan.supported_child_session_encryption_algorithms_int_array", [12]),
            ("iwlan.ike_session_encryption_aes_cbc_key_size_int_array", [128, 256]),
            ("iwlan.child_session_aes_cbc_key_size_int_array", [128, 256]),
            ("iwlan.supported_integrity_algorithms_int_array", [2, 12, 13]),
            ("iwlan.supported_prf_algorithms_int_array", [2, 5]),
            ("iwlan.diffie_hellman_groups_int_array", [2, 14]),
        ):
            config = setting.configs.config.add()
            config.key = key
            config.int_array.item.extend(items)

        for key, value in (
            ("iwlan.natt_keep_alive_timer_sec_int", 20),
            ("iwlan.dpd_timer_sec_int", 120),
            ("iwlan.ike_local_id_type_int", 3),
            ("iwlan.ike_remote_id_type_int", 2),
        ):
            config = setting.configs.config.add()
            config.key = key
            config.int_value = value

    def test_import_maps_shared_others_file_without_raw_key_collisions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "pixel.sqlite3"
            with closing(sqlite3.connect(database)) as connection:
                connection.executescript((ROOT / "schema.sql").read_text(encoding="utf-8"))
                connection.execute(
                    """INSERT INTO catalog_metadata(
                           singleton, release_id, generated_at, generator_name,
                           generator_version
                       ) VALUES (1, 'pixel-test', '2026-08-07T00:00:00Z',
                                 'carrier-bundles', 'test')"""
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
                rows = connection.execute(
                    """SELECT profile_id, lte_ims_status, nr_ims_status,
                              vowifi_status, config_json
                       FROM carrier_profiles ORDER BY profile_id"""
                ).fetchall()
                self.assertEqual(len(rows), 2)
                self.assertFalse(any("test-device" in row[0] for row in rows))
                config = json.loads(rows[0][4])
                self.assertEqual(config["ims"]["transport"], "tls")
                self.assertEqual(
                    config["access"]["lte"],
                    {
                        "apn": "ims",
                        "auth_type": "pap",
                        "ip_family": "ipv4v6",
                        "mtu": 1420,
                        "password": "public-apn-password",
                        "pcscf_discovery": ["pco", "epco"],
                        "roaming_ip_family": "ipv6",
                        "username": "public-apn-user",
                    },
                )
                self.assertEqual(rows[0][1:4], ("ready", "ready", "ready"))
                ike = config["access"]["vowifi"]["ike"]
                self.assertEqual(ike["nat_keepalive_seconds"], 20)
                self.assertEqual(ike["dpd_interval_seconds"], 120)
                self.assertEqual(ike["local_id_type"], "id_rfc822_addr")
                self.assertEqual(ike["remote_id_type"], "id_fqdn")
                self.assertIn(
                    {
                        "encryption": "AES-128",
                        "integrity": "SHA2-256",
                        "prf": "SHA1-160",
                        "dh_group": 2,
                    },
                    ike["ike_sa_proposals"],
                )
                self.assertTrue(ike["child_sa_proposals"])
                match_columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(profile_match_rules)")
                }
                source_columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(source_artifacts)")
                }
                self.assertNotIn("device_model_pattern", match_columns)
                self.assertTrue(
                    {"platform", "device_model", "os_version", "build_id"}.isdisjoint(
                        source_columns
                    )
                )
                self.assertGreater(
                    connection.execute("SELECT count(*) FROM field_evidence").fetchone()[0],
                    0,
                )
                self.assertIsNone(
                    connection.execute("PRAGMA foreign_key_check").fetchone()
                )


if __name__ == "__main__":
    unittest.main()
