import plistlib
import json
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from contextlib import closing
from pathlib import Path

from ios.bundles import (
    deep_merge,
    load_carrier_bundle_variants,
    parse_supported_sim,
)
from ios.catalog import IOSBundleSource, import_ios_catalog
from ios.sources import IPHONE_16_PRO_26_6, resolve_ipsw_artifact


ROOT = Path(__file__).resolve().parents[1]


def _write_plist(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as stream:
        plistlib.dump(value, stream, fmt=plistlib.FMT_BINARY)


def _create_database(path: Path) -> None:
    with closing(sqlite3.connect(path)) as connection:
        connection.executescript((ROOT / "schema.sql").read_text(encoding="utf-8"))
        connection.execute(
            """INSERT INTO catalog_metadata(
                   singleton, release_id, generated_at, generator_name,
                   generator_version
               ) VALUES (1, 'ios-test', '2026-08-07T00:00:00+00:00',
                         'carrier-bundles', 'test')"""
        )
        connection.commit()


def _fixture(root: Path) -> Path:
    bundle_root = (
        root / "System" / "Library" / "Carrier Bundles" / "iPhone"
    )
    bundle = bundle_root / "Example_NR_US.bundle"
    _write_plist(
        bundle / "Info.plist",
        {
            "CFBundleName": "Example_NR_US",
            "CFBundleIdentifier": "com.apple.Example_NR_US",
            "CFBundleShortVersionString": "70.0.1",
        },
    )
    _write_plist(
        bundle / "carrier.plist",
        {
            "CarrierName": "Example Wireless",
            "SupportedSIMs": ["310260_GID1-54_ID-890126", "310260_GID1-55"],
            "SupportsImsCapability": True,
            "apns": [
                {
                    "apn": "ims",
                    "username": "",
                    "password": "",
                    "type-mask": 131072,
                    "tech-type-mask": 131072,
                    "AllowedProtocolMask": 3,
                    "AllowedProtocolMaskInRoaming": 2,
                    "PcscfAddressRequired": True,
                    "Support5GSaHandOver": True,
                }
            ],
            "IMSConfig": {
                "Signaling": {
                    "DefaultAuthAlgorithm": "AKAv1-MD5",
                    "OutgoingDomain": "ims.example.test",
                    "UseIPSec": True,
                    "RegistrationExpirationSeconds": 7200,
                    "AdditionalContactParams": {
                        "REGISTER,INVITE[-wifi]": '+g.3gpp.accesstype="cellular2";audio',
                        "REGISTER[wifi]": '+g.3gpp.accesstype="wlan1"',
                    },
                    "RetryAfterStatusCodes": "480,500,503",
                },
                "Voice": {
                    "EnableVolteByDefault": True,
                    "E911OverITechSupported": True,
                },
                "XCAP": {"supported": True},
                "Media": {
                    "AudioCodecs": {
                        "112": {
                            "EncodingName": "EVS",
                            "SampleRate": 16000,
                            "br": "5.9-24.4",
                            "bw": "nb-wb",
                        }
                    }
                },
            },
            "TechSettings": {
                "ExtraConfigurationAttributeRequestv4": [
                    {
                        "Identifier": 16384,
                        "Name": "AssignedPCSCFIPv4",
                        "Type": "IPv4Address",
                    }
                ],
                "IKE": {
                    "LocalIdentifier": "0$imsi@nai.epc.mnc$mnc.mcc$mcc.3gppnetwork.org",
                    "LocalIdentifierType": "IDUserFQDN",
                    "RemoteAddress": "epdg.epc.mnc$mnc.mcc$mcc.pub.3gppnetwork.org",
                    "NATTKeepAliveEnabled": True,
                    "NATTKeepAliveInterval": 20,
                    "DeadPeerDetectionEnabled": False,
                    "DeadPeerDetectionInterval": 600,
                    "DeadPeerDetectionRetryInterval": 10,
                    "DeadPeerDetectionMaxRetries": 4,
                    "ValidateRemoteCertificate": True,
                    "RemoteCertificateHostname": "epdg.example.test",
                    "Proposals": [
                        {
                            "EncryptionAlgorithm": "AES-256",
                            "IntegrityAlgorithm": "SHA2-256",
                            "PRFAlgorithm": "SHA2-256",
                            "DHGroup": 14,
                            "EAPMethod": "EAP-AKA",
                            "Lifetime": 80000,
                        }
                    ],
                },
                "ChildSAs": {
                    "FirstChild": {
                        "ChildProposals": [
                            {
                                "EncryptionAlgorithm": ["AES-256"],
                                "IntegrityAlgorithm": ["SHA2-256"],
                                "Lifetime": 80000,
                            }
                        ]
                    }
                },
            },
            "CarrierEntitlements": {
                "ServerAddress": "https://entitlement.example.test/",
                "Authentication": {
                    "Username": "0$IMSI@nai.epc.mnc$MNC.mcc$MCC.3gppnetwork.org"
                },
            },
            "EmergencyCalling": {
                "EmergencyNumbers": [{"Number": "911", "SupportsVoice": True}]
            },
            "MVNOOverrides": {
                "Configuration_1": {
                    "SupportedSIMs": ["310260_GID1-55"],
                    "OverrideConfiguration": {"CarrierName": "Example MVNO"},
                }
            },
        },
    )
    _write_plist(
        bundle / "overrides_D93_D94_D47_D48.plist",
        {
            "SupportsVoNR": True,
        },
    )
    return bundle_root


class IOSBundleTests(unittest.TestCase):
    def test_discovers_latest_signed_apple_cdn_ipsw(self) -> None:
        payload = json.dumps(
            {
                "firmwares": [
                    {
                        "version": "26.5",
                        "buildid": "23F1",
                        "releasedate": "2026-07-01",
                        "signed": True,
                        "url": "https://updates.cdn-apple.com/old.ipsw",
                        "filesize": 100,
                    },
                    {
                        "version": "26.6",
                        "buildid": "23G71",
                        "releasedate": "2026-08-01",
                        "signed": True,
                        "url": "https://updates.cdn-apple.com/new.ipsw",
                        "filesize": 200,
                    },
                    {
                        "version": "26.7",
                        "buildid": "23H1",
                        "releasedate": "2026-09-01",
                        "signed": False,
                        "url": "https://updates.cdn-apple.com/unsigned.ipsw",
                    },
                ]
            }
        ).encode()

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return payload

        with patch("ios.sources.urllib.request.urlopen", return_value=Response()):
            artifact = resolve_ipsw_artifact("iPhone17,2")
        self.assertEqual(artifact.build_id, "23G71")
        self.assertEqual(artifact.url, "https://updates.cdn-apple.com/new.ipsw")

    def test_parses_public_match_prefixes(self) -> None:
        match = parse_supported_sim("310260_GID1-54_GID2-01_ID-890126")
        self.assertIsNotNone(match)
        self.assertEqual(match.plmn, "310260")
        self.assertEqual(match.gid1, "54")
        self.assertEqual(match.gid2, "01")
        self.assertEqual(match.iccid_prefix, "890126")
        self.assertIsNone(parse_supported_sim("DefaultBundle"))

    def test_deep_merge_honors_local_exclusion(self) -> None:
        merged = deep_merge(
            {"A": 1, "Nested": {"Keep": 2, "Remove": 3}},
            {"Nested": {"_Exclude": ["Remove"], "Added": 4}},
        )
        self.assertEqual(merged, {"A": 1, "Nested": {"Keep": 2, "Added": 4}})

    def test_selects_d93_override_and_mvno_variant(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bundle_root = _fixture(Path(directory))
            (bundle_root / "310270").symlink_to("Example_NR_US.bundle")
            (bundle_root / "310260_GID1-55").symlink_to("Example_NR_US.bundle")
            variants = load_carrier_bundle_variants(
                bundle_root, "D93", include_device_override=True
            )
        self.assertEqual([item.variant_name for item in variants], ["base", "Configuration_1"])
        self.assertTrue(variants[0].config["SupportsVoNR"])
        self.assertEqual([item.plmn for item in variants[0].matches], ["310260", "310270"])
        self.assertEqual([item.gid1 for item in variants[0].matches], ["54", None])
        self.assertEqual(variants[1].config["CarrierName"], "Example MVNO")


class IOSCatalogTests(unittest.TestCase):
    def test_imports_static_ims_vowifi_and_register_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _fixture(root / "export")
            database = root / "catalog.sqlite3"
            _create_database(database)
            stats = import_ios_catalog(
                database,
                root / "export",
                artifact=IPHONE_16_PRO_26_6,
                device_class="D93",
                bundle_sources={
                    "Example_NR_US.bundle": IOSBundleSource(
                        source_uri="https://updates.cdn-apple.com/example.ipcc",
                        artifact_sha256="2" * 64,
                        source_revision="bundle=Example_NR_US;version=70.1",
                    )
                },
            )
            with closing(sqlite3.connect(database)) as connection:
                rows = connection.execute(
                    """SELECT profile_kind, priority, lte_ims_status,
                              nr_ims_status, vowifi_status, config_json
                       FROM carrier_profiles ORDER BY priority"""
                ).fetchall()
                profiles = len(rows)
                configs = [json.loads(row[5]) for row in rows]
                config = configs[-1]
                identity = config["access"]["vowifi"]["ike"]["identities"]["idi"][0]["value_template"]
                endpoint = config["access"]["vowifi"]["epdg"][0]["address"]
                ims = config["ims"]
                contacts = [
                    (item["name"], item.get("value_template"))
                    for item in config["sip"]["lte"]["contact_parameters"]
                ] + [
                    (item["name"], item.get("value_template"))
                    for item in config["sip"]["vowifi"]["contact_parameters"]
                ]
                source_columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(source_artifacts)")
                }
                priorities = connection.execute(
                    "SELECT profile_kind, priority FROM carrier_profiles ORDER BY priority"
                ).fetchall()
                schema_version = connection.execute("PRAGMA user_version").fetchone()[0]
                integrity = connection.execute("PRAGMA quick_check").fetchone()[0]
                source_uri = connection.execute(
                    "SELECT source_uri FROM source_artifacts WHERE source_kind='carrier_bundle'"
                ).fetchone()[0]

        self.assertEqual(stats.profiles_imported, 2)
        self.assertEqual(profiles, 2)
        self.assertEqual(
            identity,
            "0{imsi}@nai.epc.mnc{mnc3}.mcc{mcc}.3gppnetwork.org",
        )
        self.assertEqual(
            endpoint,
            "epdg.epc.mnc{mnc3}.mcc{mcc}.pub.3gppnetwork.org",
        )
        self.assertEqual(
            (
                ims["home_domain"],
                ims["realm"],
                ims["authentication"]["algorithm"],
                ims["security_agreement"],
            ),
            ("ims.example.test", "ims.mnc260.mcc310.3gppnetwork.org", "AKAv1-MD5", "required"),
        )
        self.assertIn(('+g.3gpp.accesstype', '"cellular2"'), contacts)
        self.assertIn(('+g.3gpp.accesstype', '"wlan1"'), contacts)
        self.assertTrue({"device_model", "os_version", "build_id"}.isdisjoint(source_columns))
        self.assertEqual(priorities, [("mvno", 50), ("default", 100)])
        ike = config["access"]["vowifi"]["ike"]
        self.assertEqual(
            (
                ike["nat_keepalive_enabled"],
                ike["dpd_enabled"],
                ike["dpd_retry_interval_seconds"],
                ike["dpd_max_retries"],
                ike["certificate"]["validate"],
                ike["certificate"]["hostname"],
            ),
            (True, False, 10, 4, True, "epdg.example.test"),
        )
        self.assertEqual(config["media"]["audio"]["codecs"][0]["name"], "EVS")
        self.assertEqual(rows[-1][2:5], ("ready", "ready", "ready"))
        self.assertEqual(schema_version, 7)
        self.assertEqual(integrity, "ok")
        self.assertEqual(source_uri, "https://updates.cdn-apple.com/example.ipcc")


if __name__ == "__main__":
    unittest.main()
