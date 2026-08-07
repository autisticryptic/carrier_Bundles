import base64
import json
import hashlib
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from icons.package_icons import package_database


class SchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = sqlite3.connect(":memory:")
        self.db.executescript((ROOT / "schema.sql").read_text(encoding="utf-8"))
        self.db.execute(
            """INSERT INTO catalog_release(
                   singleton, release_id, generated_at, generator_version
               ) VALUES (1, 'test-release', '2026-08-07T00:00:00Z', 'test')"""
        )

    def tearDown(self) -> None:
        self.db.close()

    def test_catalog_contains_no_runtime_or_subscriber_tables(self) -> None:
        table_names = {
            row[0]
            for row in self.db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        forbidden_tables = {
            "line_ims_binding",
            "sim_identity",
            "registration_attempt",
            "runtime_snapshots",
            "client_policy_parameters",
        }
        self.assertTrue(forbidden_tables.isdisjoint(table_names))

        forbidden_columns = {
            "imsi",
            "iccid",
            "msisdn",
            "imei",
            "impi",
            "impu",
            "ki",
            "opc",
            "aka_response",
        }
        actual_columns = set()
        for table in table_names:
            actual_columns.update(
                row[1].lower()
                for row in self.db.execute(f'PRAGMA table_info("{table}")')
            )
        self.assertTrue(forbidden_columns.isdisjoint(actual_columns))

    def test_lte_and_vowifi_share_one_ims_profile(self) -> None:
        self.db.execute(
            """INSERT INTO visual_assets(
                   asset_id, asset_kind, asset_data, local_path, media_type,
                   source_name
               ) VALUES ('badge-tmobile', 'carrier_badge', ?, ?,
                         'image/svg+xml', 'project')""",
            (
                (ROOT / "icons/fallback/us-t-mobile.svg").read_bytes(),
                "icons/fallback/us-t-mobile.svg",
            ),
        )
        self.db.execute(
            """INSERT INTO carriers(
                   carrier_id, canonical_name, brand_name, country_iso2, primary_asset_id
               ) VALUES ('us-t-mobile', 'T-Mobile USA', 'T-Mobile', 'US', 'badge-tmobile')"""
        )
        self.db.execute(
            """INSERT INTO plmns(plmn, carrier_id, mcc, mnc, mnc_length, country_iso2)
               VALUES ('310260', 'us-t-mobile', '310', '260', 3, 'US')"""
        )
        self.db.execute(
            """INSERT INTO carrier_profiles(
                   profile_id, carrier_id, display_name, confidence
               ) VALUES ('us-tmobile-default', 'us-t-mobile', 'T-Mobile default', 90)"""
        )
        self.db.execute(
            "INSERT INTO profile_match_rules(profile_id, plmn) VALUES ('us-tmobile-default', '310260')"
        )
        self.db.execute(
            """INSERT INTO ims_configs(
                   profile_id, home_domain, realm, private_identity_source,
                   public_identity_source
               ) VALUES ('us-tmobile-default', ?, ?, 'isim', 'isim')""",
            (
                "ims.mnc260.mcc310.3gppnetwork.org",
                "ims.mnc260.mcc310.3gppnetwork.org",
            ),
        )
        self.db.execute(
            """INSERT INTO access_configs(
                   profile_id, access_kind, apn_dnn, ip_family
               ) VALUES ('us-tmobile-default', 'lte_epc', 'ims', 'ipv4v6')"""
        )
        self.db.execute(
            """INSERT INTO access_configs(
                   profile_id, access_kind, apn_dnn, ip_family
               ) VALUES ('us-tmobile-default', 'wifi_epdg', 'ims', 'ipv4v6')"""
        )
        wifi_access = self.db.execute("SELECT last_insert_rowid()").fetchone()[0]
        self.db.execute(
            """INSERT INTO network_endpoints(
                   profile_id, access_id, service, address_kind, address,
                   port, transport, discovery_method
               ) VALUES ('us-tmobile-default', ?, 'epdg', 'fqdn', ?, 500,
                         'ikev2', 'standard_derived')""",
            (wifi_access, "epdg.epc.mnc260.mcc310.pub.3gppnetwork.org"),
        )

        carrier = self.db.execute(
            "SELECT carrier_name, ims_domain, asset_path FROM v_carrier_catalog WHERE plmn = '310260'"
        ).fetchone()
        self.assertEqual(carrier[0], "T-Mobile USA")
        self.assertEqual(carrier[1], "ims.mnc260.mcc310.3gppnetwork.org")
        self.assertEqual(carrier[2], "icons/fallback/us-t-mobile.svg")

        accesses = self.db.execute(
            "SELECT access_kind FROM v_access_catalog ORDER BY access_kind"
        ).fetchall()
        self.assertEqual(accesses, [("lte_epc",), ("wifi_epdg",)])

    def test_icon_manifest_points_to_existing_neutral_badges(self) -> None:
        manifest = json.loads(
            (ROOT / "icons/fallback/manifest.json").read_text(encoding="utf-8")
        )
        self.assertGreaterEqual(len(manifest["assets"]), 5)
        for asset in manifest["assets"]:
            self.assertFalse(asset["official"])
            path = ROOT / asset["path"]
            self.assertTrue(path.is_file())
            self.assertEqual(
                hashlib.sha256(path.read_bytes()).hexdigest(), asset["sha256"]
            )
            root = ET.parse(path).getroot()
            self.assertEqual(root.attrib["viewBox"], "0 0 64 64")

    def test_access_specific_sip_and_identity_templates(self) -> None:
        self.db.execute(
            "INSERT INTO carrier_profiles(profile_id, display_name) VALUES ('example', 'Example')"
        )
        self.db.execute(
            """INSERT INTO access_configs(
                   profile_id, access_kind, apn_dnn, ip_family
               ) VALUES ('example', 'lte_epc', 'ims', 'ipv4v6')"""
        )
        self.db.execute(
            """INSERT INTO access_configs(
                   profile_id, access_kind, apn_dnn, ip_family
               ) VALUES ('example', 'nr_5gc', 'ims', 'ipv4v6')"""
        )
        self.db.execute(
            """INSERT INTO access_configs(
                   profile_id, access_kind, apn_dnn, ip_family
               ) VALUES ('example', 'wifi_epdg', 'ims', 'ipv4v6')"""
        )
        wifi_access = self.db.execute("SELECT last_insert_rowid()").fetchone()[0]
        self.db.execute(
            """INSERT INTO ims_configs(
                   profile_id, home_domain, realm, private_identity_source,
                   public_identity_source
               ) VALUES ('example', ?, ?, 'auto', 'auto')""",
            (
                "ims.mnc260.mcc310.3gppnetwork.org",
                "ims.mnc260.mcc310.3gppnetwork.org",
            ),
        )
        self.db.execute(
            """INSERT INTO ims_identity_templates(
                   profile_id, role, source_policy, identity_type,
                   value_template, use_when, required
               ) VALUES ('example', 'impi', 'derived_imsi', 'nai', ?,
                         'if_isim_missing', 1)""",
            ("{imsi}@ims.mnc{mnc3}.mcc{mcc}.3gppnetwork.org",),
        )
        self.db.execute(
            """INSERT INTO ims_identity_templates(
                   profile_id, role, source_policy, identity_type,
                   value_template, use_when, required
               ) VALUES ('example', 'impu', 'derived_imsi', 'sip_uri', ?,
                         'if_isim_missing', 1)""",
            ("sip:{imsi}@ims.mnc{mnc3}.mcc{mcc}.3gppnetwork.org",),
        )
        self.db.execute(
            """INSERT INTO sip_register_configs(
                   profile_id, scope, request_uri_policy,
                   requested_expires_seconds, contact_mode
               ) VALUES ('example', 'common', 'home_domain', 3600, 'standard')"""
        )
        common_register = self.db.execute("SELECT last_insert_rowid()").fetchone()[0]
        self.db.execute(
            """INSERT INTO sip_register_configs(
                   profile_id, access_id, parent_register_config_id, scope,
                   requested_expires_seconds, access_network_info_template
               ) VALUES ('example', ?, ?, 'access', 600, ?)""",
            (wifi_access, common_register, "IEEE-802.11;i-wlan-node-id={bssid}"),
        )
        wifi_register = self.db.execute("SELECT last_insert_rowid()").fetchone()[0]
        self.db.execute(
            """INSERT INTO sip_header_rules(
                   register_config_id, phase, position, header_name, action,
                   value_template, required
               ) VALUES (?, 'authenticated', 0, 'P-Access-Network-Info',
                         'replace', ?, 1)""",
            (wifi_register, "IEEE-802.11;i-wlan-node-id={bssid}"),
        )
        self.db.execute(
            """INSERT INTO sip_header_rules(
                   register_config_id, phase, position, header_name, action
               ) VALUES (?, 'all', 0, 'P-Visited-Network-ID', 'omit')""",
            (wifi_register,),
        )
        self.db.execute(
            """INSERT INTO sip_contact_parameters(
                   register_config_id, position, name, value_template, required
               ) VALUES (?, 0, '+g.3gpp.accesstype', 'IEEE-802.11', 1)""",
            (wifi_register,),
        )
        self.db.execute(
            """INSERT INTO sip_security_mechanisms(
                   register_config_id, position, integrity_algorithm,
                   encryption_algorithm, protocol, mode, required
               ) VALUES (?, 0, 'hmac-sha-1-96', 'aes-cbc', 'esp', 'trans', 1)""",
            (wifi_register,),
        )
        self.db.execute(
            "INSERT INTO ike_configs(access_id, eap_method) VALUES (?, 'eap_aka')",
            (wifi_access,),
        )
        self.db.execute(
            """INSERT INTO ike_identity_rules(
                   access_id, role, identity_type, source_policy,
                   value_template, send_policy, required
               ) VALUES (?, 'idi', 'nai', 'derived_imsi', ?, 'always', 1)""",
            (
                wifi_access,
                "0{imsi}@nai.epc.mnc{mnc3}.mcc{mcc}.3gppnetwork.org",
            ),
        )
        self.db.execute(
            """INSERT INTO ike_identity_rules(
                   access_id, role, identity_type, source_policy,
                   value_template, send_policy
               ) VALUES (?, 'idr', 'id_fqdn', 'epdg_fqdn',
                         '{epdg_fqdn}', 'on_request')""",
            (wifi_access,),
        )

        resolved = self.db.execute(
            """SELECT access_kind, requested_expires_seconds, contact_mode,
                      access_network_info_template
               FROM v_sip_register_catalog
               WHERE register_config_id = ?""",
            (wifi_register,),
        ).fetchone()
        self.assertEqual(
            resolved,
            (
                "wifi_epdg",
                600,
                "standard",
                "IEEE-802.11;i-wlan-node-id={bssid}",
            ),
        )
        self.assertEqual(
            self.db.execute(
                "SELECT count(*) FROM ims_identity_templates WHERE profile_id = 'example'"
            ).fetchone()[0],
            2,
        )
        self.assertEqual(
            self.db.execute(
                "SELECT count(*) FROM ike_identity_rules WHERE access_id = ?",
                (wifi_access,),
            ).fetchone()[0],
            2,
        )

        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute(
                """INSERT INTO sip_header_rules(
                       register_config_id, phase, position, header_name, action
                   ) VALUES (?, 'refresh', 0, 'Supported', 'add')""",
                (wifi_register,),
            )

    def test_icon_packager_embeds_and_links_operator_icons(self) -> None:
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "catalog.sqlite3"
            source = root / "operator-icons"
            (source / "catalog").mkdir(parents=True)
            (source / "icons/worldwide").mkdir(parents=True)
            (source / "catalog/310.toml").write_text(
                """mcc = "310"

[[operators]]
mnc = "260"
plmn = "310260"
operator = "T-Mobile USA"
brand = "T-Mobile"
icon = "t-mobile"
icon_scope = "worldwide"

[[operators.gids]]
gid1 = "6d38"
profile_provider_names = ["Metro by T-Mobile"]
profile_names = ["Metro"]
icon = "metro"
icon_scope = "worldwide"
""",
                encoding="utf-8",
            )
            (source / "icons/worldwide/t-mobile.png").write_bytes(png)
            (source / "icons/worldwide/metro.png").write_bytes(png)

            subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "tools/init_db.py"),
                    str(database),
                    "--release-id",
                    "test-icons",
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            with sqlite3.connect(database) as connection:
                connection.execute(
                    """INSERT INTO carriers(
                           carrier_id, canonical_name, brand_name, country_iso2
                       ) VALUES ('us-t-mobile', 'T-Mobile USA', 'T-Mobile', 'US')"""
                )
                connection.execute(
                    """INSERT INTO plmns(
                           plmn, carrier_id, mcc, mnc, mnc_length, country_iso2
                       ) VALUES ('310260', 'us-t-mobile', '310', '260', 3, 'US')"""
                )
                connection.execute(
                    """INSERT INTO carrier_profiles(
                           profile_id, carrier_id, display_name
                       ) VALUES ('us-metro', 'us-t-mobile', 'Metro')"""
                )
                connection.execute(
                    """INSERT INTO profile_match_rules(
                           profile_id, plmn, gid1, spn
                       ) VALUES ('us-metro', '310260', '6d38', 'Metro by T-Mobile')"""
                )
                connection.commit()

            result = package_database(
                database,
                source_base_url=source.resolve().as_uri(),
                nekokolpa2_revision="a" * 40,
            )
            self.assertEqual(result.assets_embedded, 2)
            self.assertEqual(result.carriers_linked, 1)
            self.assertEqual(result.profiles_linked, 1)

            repeated = package_database(
                database,
                source_base_url=source.resolve().as_uri(),
                nekokolpa2_revision="a" * 40,
            )
            self.assertEqual(repeated.assets_embedded, 2)

            with sqlite3.connect(database) as connection:
                carrier_asset, profile_asset = connection.execute(
                    """SELECT c.primary_asset_id, cp.profile_asset_id
                       FROM carriers AS c
                       JOIN carrier_profiles AS cp
                         ON cp.carrier_id = c.carrier_id
                       WHERE cp.profile_id = 'us-metro'"""
                ).fetchone()
                self.assertEqual(
                    carrier_asset, "operator-icons:worldwide/t-mobile"
                )
                self.assertEqual(
                    profile_asset, "operator-icons:worldwide/metro"
                )
                embedded = connection.execute(
                    """SELECT media_type, asset_data
                       FROM v_visual_asset_catalog WHERE asset_id = ?""",
                    (profile_asset,),
                ).fetchone()
                self.assertEqual(embedded, ("image/png", png))
                self.assertEqual(
                    connection.execute(
                        """SELECT count(*) FROM source_snapshots
                           WHERE parser_name = 'icons/package_icons.py'"""
                    ).fetchone()[0],
                    2,
                )

    def test_sealed_catalog_is_opened_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "catalog.sqlite3"
            subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "tools/init_db.py"),
                    str(database),
                    "--release-id",
                    "test-sealed",
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
            self.assertEqual(
                database.stat().st_mode
                & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH),
                0,
            )
            uri = f"{database.resolve().as_uri()}?mode=ro&immutable=1"
            with sqlite3.connect(uri, uri=True) as readonly:
                self.assertEqual(
                    readonly.execute(
                        "SELECT sealed FROM catalog_release"
                    ).fetchone()[0],
                    1,
                )
                with self.assertRaises(sqlite3.OperationalError):
                    readonly.execute(
                        "INSERT INTO carriers(carrier_id, canonical_name) VALUES ('x', 'x')"
                    )


if __name__ == "__main__":
    unittest.main()
