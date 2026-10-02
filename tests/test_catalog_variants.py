"""Offline, fail-closed catalog variants and bounded default-elision tests."""

import copy
import json
import sqlite3
import stat
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from catalog_contract import CONFIG_CONTRACT, finalized_config
from tools.build_variants import (
    OPTIONAL_DEFAULTS,
    STANDARD_IDENTITIES,
    build_variants,
    elide_optional_defaults,
    sha256,
)
from tools.verify_catalog import verify_catalog

ROOT = Path(__file__).resolve().parents[1]


def default_config():
    return {
        "protocol_baseline": CONFIG_CONTRACT,
        "ims": {
            "home_domain": "ims.mnc026.mcc310.3gppnetwork.org",
            "realm": "ims.mnc026.mcc310.3gppnetwork.org",
            "authentication": {"scheme": "ims_aka"},
            "identity_templates": copy.deepcopy(STANDARD_IDENTITIES),
            "transport": "udp",
            "local_port": 5060,
        },
        "access": {
            "lte": {"apn": "ims", "ip_family": "ipv4v6", "pcscf_discovery": ["pco", "epco"]},
            "nr": {"dnn": "ims", "ip_family": "ipv4v6", "pcscf_discovery": ["epco", "pco"]},
            "vowifi": {
                "apn": "ims", "ip_family": "ipv4v6",
                "epdg": [{"address": "epdg.epc.mnc026.mcc310.pub.3gppnetwork.org", "discovery": "static"}],
                "pcscf_discovery": ["ike_cfg"],
                "ike": {
                    "eap_method": "eap_aka", "initial_port": 500,
                    "nat_keepalive_seconds": 20, "dpd_interval_seconds": 600,
                    "identities": {
                        "idi": [{"identity_type": "id_rfc822_addr", "value_template": "0{imsi}@nai.epc.mnc{mnc3}.mcc{mcc}.3gppnetwork.org"}],
                        "idr": [{"identity_type": "id_fqdn", "value_template": "{epdg_fqdn}"}],
                    },
                },
            },
        },
        "services": {"volte": False, "vonr": False, "smsoip": True, "vowifi": True},
        "vendor_future_policy": {"explicit_false": False, "explicit_zero": 0, "list": [], "object": {}},
    }


def fixture(path: Path):
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.executescript((ROOT / "schema.sql").read_text(encoding="utf-8"))
        conn.execute("""INSERT INTO catalog_metadata(singleton,release_id,generated_at,
            generator_name,generator_version,sealed,notes)
            VALUES(1, ?, '2026-10-01T00:00:00Z', 'fixture', '1', 1, 'original notes')""", (path.stem,))
        conn.execute("""INSERT INTO source_artifacts(source_id,source_kind,source_uri,
            extracted_at,parser_name,parser_version) VALUES
            (1,'standards_reference','urn:fixture:standards','2026-10-01','fixture','1')""")
        conn.execute("""INSERT INTO visual_assets(asset_id,asset_kind,asset_data,
            remote_url,media_type,sha256,source_name)
            VALUES('logo','operator_logo',X'89504e47','https://example.invalid/logo.png',
            'image/png',?,'fixture')""", ("0" * 64,))
        conn.execute("""INSERT INTO carriers(carrier_id,canonical_name,primary_asset_id)
            VALUES('carrier','Test Carrier','logo')""")
        for index in range(2):
            conf = default_config()
            if index:
                conf["ims"]["transport"] = "tcp"
                conf["ims"]["identity_templates"][0]["source"] = "isim"
                conf["access"]["lte"]["apn"] = "private-ims"
                conf["access"]["vowifi"]["ike"]["initial_port"] = 4500
            config, statuses = finalized_config(conf)
            profile = f"profile-{index}"
            conn.execute("""INSERT INTO carrier_profiles(profile_id,carrier_id,display_name,
                lte_ims_status,nr_ims_status,vowifi_status,profile_asset_id,config_json)
                VALUES(?,'carrier',?,?,?,?,'logo',?)""",
                (profile, profile, statuses["lte"], statuses["nr"], statuses["vowifi"], config))
            conn.execute("""INSERT INTO profile_match_rules(profile_id,plmn,spn)
                VALUES(?, '31026', ?)""", (profile, f"brand-{index}"))
            conn.execute("""INSERT INTO profile_sources(profile_id,source_id,source_path,contribution_kind)
                VALUES(?,1,'fixture','standard_default')""", (profile,))
            for target in ("/ims/identity_templates", "/ims/home_domain", "/access/vowifi/ike/initial_port"):
                conn.execute("""INSERT INTO field_evidence(profile_id,source_id,target_kind,
                    target_path,source_value_json,evidence_kind,confidence)
                    VALUES(?,1,'config',?,'"original evidence"','standard_derived',100)""", (profile, target))
        conn.execute("""INSERT INTO profile_match_rules(profile_id,plmn,is_exclusion)
            VALUES('profile-0', '99999', 1)""")
    return path


def rows(path, table):
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)) as conn:
        return conn.execute(f"SELECT * FROM {table} ORDER BY 1, 2").fetchall()


class DefaultElisionTests(unittest.TestCase):
    def test_only_exact_optional_defaults_are_removed(self):
        config = default_config()
        original = copy.deepcopy(config)
        result, changes = elide_optional_defaults(config)
        self.assertEqual(config, original, "caller object must not be mutated")
        self.assertEqual({row["path"] for row in changes}, set(OPTIONAL_DEFAULTS))
        self.assertEqual(result["access"]["nr"], original["access"]["nr"])
        self.assertEqual(result["ims"]["home_domain"], original["ims"]["home_domain"])
        self.assertEqual(result["ims"]["realm"], original["ims"]["realm"])
        self.assertEqual(result["ims"]["authentication"], original["ims"]["authentication"])
        for key in ("epdg", "pcscf_discovery"):
            self.assertEqual(result["access"]["vowifi"][key], original["access"]["vowifi"][key])
        self.assertEqual(result["access"]["vowifi"]["ike"]["identities"], original["access"]["vowifi"]["ike"]["identities"])
        self.assertEqual(result["services"], original["services"])
        self.assertEqual(result["vendor_future_policy"], original["vendor_future_policy"])

    def test_transport_elision_does_not_expose_competing_fallback(self):
        for alternate in ("tcp", "", "auto", None, False, [], {}):
            with self.subTest(alternate=alternate):
                config = default_config()
                config["sip"] = {"common": {"transport": alternate}}
                result, _ = elide_optional_defaults(config)
                self.assertEqual(result["ims"]["transport"], "udp")
        config["sip"]["common"]["transport"] = "udp"
        result, _ = elide_optional_defaults(config)
        self.assertNotIn("transport", result["ims"])

    def test_null_zero_false_empty_and_nondefault_are_not_normalized(self):
        for pointer, default in OPTIONAL_DEFAULTS.items():
            for special in (None, 0, False, [], {}, "", "operator-specific", 500.0):
                if type(special) is type(default) and special == default:
                    continue
                with self.subTest(pointer=pointer, special=special):
                    config = default_config()
                    parent = config
                    tokens = pointer.strip("/").split("/")
                    for token in tokens[:-1]:
                        parent = parent[token]
                    parent[tokens[-1]] = special
                    result, changes = elide_optional_defaults(config)
                    self.assertNotIn(pointer, {row["path"] for row in changes})
                    parent = result
                    for token in tokens[:-1]:
                        parent = parent[token]
                    self.assertEqual(parent[tokens[-1]], special)

    def test_templates_with_operator_annotations_or_alternate_order_survive(self):
        for mutate in (
            lambda values: values.reverse(),
            lambda values: values[0].update(use_when="always"),
            lambda values: values[0].update(value_template="operator-{imsi}@{home_domain}"),
            lambda values: values[1].update(source="isim"),
        ):
            config = default_config()
            mutate(config["ims"]["identity_templates"])
            result, _ = elide_optional_defaults(config)
            self.assertEqual(result["ims"]["identity_templates"], config["ims"]["identity_templates"])

    def test_sparse_unknown_profiles_remain_unknown_and_unmodified(self):
        config = {"protocol_baseline": CONFIG_CONTRACT, "access": {}, "readiness": {"lte_missing": ["/access/lte/apn"]}}
        result, changes = elide_optional_defaults(config)
        self.assertEqual(result, config)
        self.assertEqual(changes, [])

    def test_elision_is_idempotent_and_preserves_readiness_diagnostics(self):
        raw, statuses = finalized_config(default_config())
        result, changes = elide_optional_defaults(json.loads(raw))
        twice, second_changes = elide_optional_defaults(result)
        self.assertTrue(changes)
        self.assertEqual(result, twice)
        self.assertEqual(second_changes, [])
        self.assertEqual(finalized_config(result)[1], statuses)


class VariantBuildTests(unittest.TestCase):
    def test_four_sources_each_get_three_verified_independent_variants(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            originals = [fixture(root / f"carrier-{name}.sqlite3") for name in ("ios", "ipcc", "pixel", "xiaomi")]
            snapshots = [(path.read_bytes(), path.stat().st_mode) for path in originals]
            destination = root / "variants"
            manifest = build_variants(originals, destination)
            self.assertEqual(len(list(destination.glob("*.sqlite3"))), 12)
            self.assertEqual(len(manifest["catalogs"]), 4)
            for source, (before, mode), entry in zip(originals, snapshots, manifest["catalogs"]):
                self.assertEqual(source.read_bytes(), before)
                self.assertEqual(source.stat().st_mode, mode)
                self.assertEqual((destination / source.name).read_bytes(), before)
                self.assertFalse(manifest["policy"]["registration_guarantee"])
                for variant, summary in entry["variants"].items():
                    path = destination / summary["database"]
                    self.assertEqual(verify_catalog(path)["counts"], summary["counts"])
                    self.assertEqual(summary["sha256"], sha256(path))
                    self.assertEqual(summary["counts"]["carrier_profiles"], 2)
                    self.assertEqual(summary["counts"]["visual_assets"], 1 if variant == "full" else 0)
                    for table in ("profile_match_rules", "profile_sources", "source_artifacts"):
                        self.assertEqual(rows(path, table), rows(source, table))
                    if variant != "full":
                        with closing(sqlite3.connect(path)) as conn:
                            self.assertEqual(conn.execute("SELECT count(*) FROM carriers WHERE primary_asset_id IS NOT NULL").fetchone()[0], 0)
                            self.assertEqual(conn.execute("SELECT count(*) FROM carrier_profiles WHERE profile_asset_id IS NOT NULL").fetchone()[0], 0)
                            self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                            self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
                no_icons = destination / entry["variants"]["no-icons"]["database"]
                self.assertEqual(rows(no_icons, "field_evidence"), rows(source, "field_evidence"))
                with closing(sqlite3.connect(no_icons)) as conn, closing(sqlite3.connect(source)) as original:
                    sql = "SELECT profile_id,lte_ims_status,nr_ims_status,vowifi_status,config_json FROM carrier_profiles ORDER BY profile_id"
                    self.assertEqual(conn.execute(sql).fetchall(), original.execute(sql).fetchall())
                report = json.loads((destination / entry["minimal_elision"]["report"]).read_text())
                self.assertEqual(report["profiles_removed"], 0)
                self.assertEqual(report["profiles_changed"], 2)
                self.assertEqual(report["fields_removed"], len(OPTIONAL_DEFAULTS) * 2 - 3)
                self.assertEqual(report["evidence_rows_preserved_but_deselected"], 2)
                minimal = destination / entry["variants"]["minimal-no-icons"]["database"]
                self.assertEqual(len(rows(minimal, "field_evidence")), 6)
                with closing(sqlite3.connect(minimal)) as conn:
                    self.assertEqual(conn.execute("SELECT sum(selected) FROM field_evidence WHERE target_path='/ims/home_domain'").fetchone()[0], 2)
            for line in (destination / "SHA256SUMS").read_text().splitlines():
                digest, name = line.split("  ", 1)
                self.assertEqual(digest, sha256(destination / name))

    def test_existing_output_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = fixture(root / "catalog.sqlite3")
            destination = root / "variants"
            destination.mkdir()
            marker = destination / "user-file"
            marker.write_text("keep")
            with self.assertRaisesRegex(ValueError, "never overwrite"):
                build_variants([source], destination)
            self.assertEqual(marker.read_text(), "keep")

    def test_bad_input_never_publishes_a_partial_set(self):
        for corruption in ("PRAGMA application_id=0", "UPDATE catalog_metadata SET sealed=0", "UPDATE carrier_profiles SET lte_ims_status='unknown'", "UPDATE catalog_metadata SET schema_version=7,config_contract='wrong'"):
            with self.subTest(corruption=corruption), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                good = fixture(root / "good.sqlite3")
                bad = fixture(root / "bad.sqlite3")
                with closing(sqlite3.connect(bad)) as conn, conn:
                    conn.execute("PRAGMA ignore_check_constraints=ON")
                    conn.execute(corruption)
                with self.assertRaises(ValueError):
                    build_variants([good, bad], root / "variants")
                self.assertFalse((root / "variants").exists())
                self.assertEqual(list(root.glob(".*-building-*")), [])

    def test_duplicate_names_empty_sources_and_derived_inputs_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = fixture(root / "catalog.sqlite3")
            for sources in ([], [source, source], [root / "catalog-no-icons.sqlite3"]):
                with self.subTest(sources=sources), self.assertRaises(ValueError):
                    build_variants(sources, root / "variants")
            self.assertFalse((root / "variants").exists())

    def test_sqlite_sidecars_are_rejected_without_modifying_source(self):
        for suffix in ("-wal", "-shm", "-journal"):
            with self.subTest(suffix=suffix), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = fixture(root / "catalog.sqlite3")
                digest = sha256(source)
                sidecar = Path(str(source) + suffix)
                sidecar.write_bytes(b"")
                with self.assertRaisesRegex(ValueError, "sidecar"):
                    build_variants([source], root / "variants")
                self.assertEqual(sha256(source), digest)
                self.assertTrue(sidecar.exists())

    def test_release_verifier_remains_strict_about_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            source = fixture(Path(directory) / "catalog.sqlite3")
            source.chmod(stat.S_IRUSR | stat.S_IWUSR)
            with self.assertRaisesRegex(ValueError, "not sealed read-only"):
                verify_catalog(source)
            self.assertTrue(verify_catalog(source, require_readonly=False)["sealed"])


if __name__ == "__main__":
    unittest.main()
