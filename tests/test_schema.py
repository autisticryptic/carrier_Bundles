import base64
import hashlib
import json
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from contextlib import closing
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from catalog_contract import CONFIG_CONTRACT, finalized_config
from icons.package_icons import package_database


EXPECTED_TABLES = {
    "catalog_metadata",
    "source_artifacts",
    "visual_assets",
    "carriers",
    "carrier_profiles",
    "profile_match_rules",
    "profile_sources",
    "field_evidence",
}


class SchemaTests(unittest.TestCase):
    def test_config_json_schema_is_present_and_pinned(self) -> None:
        document = json.loads((ROOT / "config.schema.json").read_text(encoding="utf-8"))
        self.assertEqual(
            document["$schema"], "https://json-schema.org/draft/2020-12/schema"
        )
        self.assertEqual(
            document["properties"]["protocol_baseline"]["const"],
            "carrier-bundles-ims-v1",
        )

    def setUp(self) -> None:
        self.db = sqlite3.connect(":memory:")
        self.db.executescript((ROOT / "schema.sql").read_text(encoding="utf-8"))
        self.db.execute(
            """INSERT INTO catalog_metadata(
                   singleton, release_id, generated_at, generator_name,
                   generator_version
               ) VALUES (1, 'test-release', '2026-08-07T00:00:00Z',
                         'carrier-bundles', 'test')"""
        )

    def tearDown(self) -> None:
        self.db.close()

    def _insert_profile(self, profile_id: str = "example") -> dict:
        config = {
            "protocol_baseline": CONFIG_CONTRACT,
            "ims": {
                "home_domain": "ims.mnc260.mcc310.3gppnetwork.org",
                "realm": "ims.mnc260.mcc310.3gppnetwork.org",
                "authentication": {"scheme": "ims_aka"},
                "identity_templates": [
                    {
                        "role": "impi",
                        "source": "derived_imsi",
                        "value_template": "{imsi}@{home_domain}",
                    }
                ],
            },
            "access": {
                "lte": {
                    "apn": "ims",
                    "ip_family": "ipv4v6",
                    "pcscf_discovery": ["pco", "epco"],
                },
                "vowifi": {
                    "epdg": [
                        {
                            "address": "epdg.epc.mnc260.mcc310.pub.3gppnetwork.org",
                            "discovery": "standard_derived",
                        }
                    ],
                    "pcscf_discovery": ["ike_cfg"],
                    "ike": {
                        "eap_method": "eap_aka",
                        "identities": {
                            "idi": [{"value_template": "0{imsi}@{home_domain}"}]
                        },
                        "ike_sa_proposals": [{"encryption": "AES-256"}],
                        "child_sa_proposals": [{"encryption": "AES-256"}],
                    },
                },
            },
            "sip": {
                "common": {
                    "register": {"requested_expires_seconds": 3600},
                    "security_client": [
                        {
                            "mechanism": "ipsec-3gpp",
                            "integrity": "hmac-sha-1-96",
                            "encryption": "aes-cbc",
                        }
                    ],
                },
                "vowifi": {
                    "contact_parameters": [
                        {"name": "+g.3gpp.accesstype", "value_template": "wlan"}
                    ]
                },
            },
            "media": {
                "audio": {"codecs": [{"name": "EVS", "payload_type": 112}]},
                "video": {"codecs": [{"name": "H.264", "payload_type": 114}]},
            },
            "services": {
                "volte": True,
                "vowifi": True,
                "vilte": True,
                "hd_voice": True,
            },
        }
        encoded, status = finalized_config(config)
        self.db.execute(
            """INSERT OR IGNORE INTO carriers(
                   carrier_id, canonical_name, brand_name, country_iso2
               ) VALUES ('us-example', 'Example Wireless', 'Example', 'US')"""
        )
        self.db.execute(
            """INSERT INTO carrier_profiles(
                   profile_id, carrier_id, display_name, confidence,
                   lte_ims_status, nr_ims_status, vowifi_status, config_json
               ) VALUES (?, 'us-example', 'Example', 90, ?, ?, ?, ?)""",
            (profile_id, status["lte"], status["nr"], status["vowifi"], encoded),
        )
        self.db.execute(
            "INSERT INTO profile_match_rules(profile_id, plmn) VALUES (?, '310260')",
            (profile_id,),
        )
        return json.loads(encoded)

    def test_schema_has_exactly_eight_tables_and_no_device_match_columns(self) -> None:
        tables = {
            row[0]
            for row in self.db.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table'"
            )
            if not row[0].startswith("sqlite_")
        }
        self.assertEqual(tables, EXPECTED_TABLES)
        columns = {
            row[1]
            for row in self.db.execute("PRAGMA table_info(profile_match_rules)")
        }
        self.assertNotIn("device_model_pattern", columns)
        self.assertNotIn("os_build_pattern", columns)
        source_columns = {
            row[1] for row in self.db.execute("PRAGMA table_info(source_artifacts)")
        }
        self.assertTrue(
            {"device_model", "platform", "os_version", "build_id"}.isdisjoint(
                source_columns
            )
        )

    def test_catalog_contains_no_runtime_or_subscriber_tables(self) -> None:
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
        actual_columns: set[str] = set()
        for table in EXPECTED_TABLES:
            actual_columns.update(
                row[1].lower()
                for row in self.db.execute(f'PRAGMA table_info("{table}")')
            )
        self.assertTrue(forbidden_columns.isdisjoint(actual_columns))

    def test_one_profile_contains_ims_vowifi_media_and_5g_sections(self) -> None:
        expected = self._insert_profile()
        row = self.db.execute(
            """SELECT lte_ims_status, nr_ims_status, vowifi_status, config_json
               FROM v_profile_catalog WHERE plmn = '310260'"""
        ).fetchone()
        self.assertEqual(row[:3], ("ready", "unknown", "ready"))
        actual = json.loads(row[3])
        self.assertEqual(actual, expected)
        self.assertEqual(actual["ims"]["authentication"]["scheme"], "ims_aka")
        self.assertEqual(actual["media"]["audio"]["codecs"][0]["name"], "EVS")
        self.assertTrue(actual["services"]["vilte"])
        self.assertEqual(
            actual["access"]["vowifi"]["ike"]["eap_method"], "eap_aka"
        )

    def test_invalid_json_and_readiness_values_are_rejected(self) -> None:
        self.db.execute(
            "INSERT INTO carriers(carrier_id, canonical_name) VALUES ('x', 'x')"
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute(
                """INSERT INTO carrier_profiles(
                       profile_id, carrier_id, display_name, config_json
                   ) VALUES ('bad-json', 'x', 'bad', 'not-json')"""
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute(
                """INSERT INTO carrier_profiles(
                       profile_id, carrier_id, display_name, lte_ims_status,
                       config_json
                   ) VALUES ('bad-status', 'x', 'bad', 'complete', '{}')"""
            )

    def test_icon_manifest_points_to_existing_neutral_badges(self) -> None:
        manifest = json.loads(
            (ROOT / "icons/fallback/manifest.json").read_text(encoding="utf-8")
        )
        self.assertGreaterEqual(len(manifest["assets"]), 5)
        for asset in manifest["assets"]:
            self.assertFalse(asset["official"])
            path = ROOT / asset["path"]
            self.assertTrue(path.is_file())
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), asset["sha256"])
            self.assertEqual(ET.parse(path).getroot().attrib["viewBox"], "0 0 64 64")

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
            with closing(sqlite3.connect(database)) as connection:
                connection.execute(
                    """INSERT INTO carriers(
                           carrier_id, canonical_name, brand_name, country_iso2
                       ) VALUES ('us-t-mobile', 'T-Mobile USA', 'T-Mobile', 'US')"""
                )
                config, status = finalized_config(
                    {"protocol_baseline": CONFIG_CONTRACT, "services": {}}
                )
                connection.execute(
                    """INSERT INTO carrier_profiles(
                           profile_id, carrier_id, display_name,
                           lte_ims_status, nr_ims_status, vowifi_status,
                           config_json
                       ) VALUES ('us-metro', 'us-t-mobile', 'Metro', ?, ?, ?, ?)""",
                    (status["lte"], status["nr"], status["vowifi"], config),
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

            with closing(sqlite3.connect(database)) as connection:
                carrier_asset, profile_asset = connection.execute(
                    """SELECT c.primary_asset_id, cp.profile_asset_id
                       FROM carriers AS c JOIN carrier_profiles AS cp USING(carrier_id)
                       WHERE cp.profile_id = 'us-metro'"""
                ).fetchone()
                self.assertEqual(carrier_asset, "operator-icons:worldwide/t-mobile")
                self.assertEqual(profile_asset, "operator-icons:worldwide/metro")
                embedded = connection.execute(
                    """SELECT media_type, asset_data FROM v_visual_asset_catalog
                       WHERE asset_id = ?""",
                    (profile_asset,),
                ).fetchone()
                self.assertEqual(embedded, ("image/png", png))
                self.assertEqual(
                    connection.execute(
                        """SELECT count(*) FROM source_artifacts
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
            with closing(sqlite3.connect(uri, uri=True)) as readonly:
                self.assertEqual(
                    readonly.execute("SELECT sealed FROM catalog_metadata").fetchone()[0],
                    1,
                )
                with self.assertRaises(sqlite3.OperationalError):
                    readonly.execute(
                        "INSERT INTO carriers(carrier_id, canonical_name) VALUES ('x', 'x')"
                    )


if __name__ == "__main__":
    unittest.main()
