"""Opt-in audit-row reduction is separate from simulation/config pruning."""

from contextlib import closing
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

from tools.build_variants import (
    POLICY_ID,
    PRUNING_POLICY_ID,
    RUNTIME_MINIMAL_POLICY_ID,
    _remove_runtime_evidence,
    build_variants,
    sha256,
)
from tools.verify_catalog import COUNT_TABLES, verify_catalog
from test_catalog_variants import fixture, rows
from test_simulation_pruning import report_fixture, set_profile, standard

ROOT = Path(__file__).resolve().parents[1]


def evidence_fixture(path):
    """Whole-profile + partial pruning, retained NR/custom policy, real audit rows."""
    fixture(path)
    mixed = standard()
    mixed["access"].update(standard(True)["access"])
    mixed["access"]["vowifi"]["epdg"][0]["address"] = "special.operator.example"
    mixed["access"]["nr"] = {"dnn": "operator-nr", "pcscf_discovery": ["epco", "pco"]}
    mixed["sip"]["nr"] = {"operator_specific": {"false": False, "zero": 0}}
    with closing(sqlite3.connect(path)) as conn, conn:
        set_profile(conn, "profile-0", standard())
        set_profile(conn, "profile-1", mixed)
        # Even an unused source artifact must survive runtime reduction.
        conn.execute("""INSERT INTO source_artifacts(source_id,source_kind,source_uri,
            extracted_at,parser_name,parser_version) VALUES
            (2,'operator_metadata','urn:fixture:unused','2026-10-01','fixture','1')""")
        facts = [
            ("config", "/access/lte/apn", "ims", 1),  # ordinary pruning removes this
            ("config", "/access", mixed["access"], 1),  # ordinary pruning trims this
            ("config", "/access/nr", mixed["access"]["nr"], 1),
            ("match_rule", "/spn", "brand-1", 0),
            ("profile", "/notes", None, 0),
            ("carrier", "/notes", "public audit evidence: \u8fd0\u8425\u5546 " * 4096, 0),
        ]
        for kind, target, value, selected in facts:
            conn.execute("""INSERT INTO field_evidence(profile_id,source_id,target_kind,
                target_path,source_value_json,evidence_kind,confidence,selected)
                VALUES('profile-1',1,?,?,?,'extracted',90,?)""",
                (kind, target, json.dumps(value, ensure_ascii=False) if value is not None else None, selected))
    return path, mixed


def schema(path):
    with closing(sqlite3.connect(path)) as conn:
        return conn.execute("""SELECT type,name,tbl_name,sql FROM sqlite_schema
            WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name""").fetchall()


def provenance(path):
    with closing(sqlite3.connect(path)) as conn:
        notes = conn.execute("SELECT notes FROM catalog_metadata").fetchone()[0]
    return json.loads(notes.splitlines()[-1])


def variant_path(root, manifest, variant):
    return root / manifest["catalogs"][0]["variants"][variant]["database"]


class RuntimeMinimalTests(unittest.TestCase):
    def test_only_post_pruning_evidence_is_removed_and_full_remains_original(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, mixed = evidence_fixture(root / "catalog.sqlite3")
            before, mode = source.read_bytes(), source.stat().st_mode
            report = root / "simulation.json"
            report_fixture(report)
            ordinary_dir, runtime_dir = root / "ordinary", root / "runtime"
            ordinary = build_variants([source], ordinary_dir, simulation_report=report)
            runtime = build_variants([source], runtime_dir, simulation_report=report, runtime_minimal=True)
            self.assertEqual(source.read_bytes(), before)
            self.assertEqual(source.stat().st_mode, mode)
            self.assertEqual(variant_path(runtime_dir, runtime, "full").read_bytes(), before)
            self.assertFalse(ordinary["policy"]["runtime_minimal"]["enabled"])
            self.assertTrue(runtime["policy"]["runtime_minimal"]["enabled"])
            self.assertFalse(runtime["policy"]["registration_guarantee"])

            for variant in ("full", "no-icons", "minimal-no-icons"):
                normal_path = variant_path(ordinary_dir, ordinary, variant)
                runtime_path = variant_path(runtime_dir, runtime, variant)
                verified = verify_catalog(runtime_path)
                self.assertEqual(verified["schema_version"], 7)
                self.assertEqual(verified["application_id"], 1128419922)
                self.assertEqual(verified["config_contract"], "carrier-bundles-ims-v1")
                self.assertEqual(len(verified["counts"]), 8)
                self.assertEqual(schema(runtime_path), schema(source), "all table/index/view definitions survive")
                with closing(sqlite3.connect(runtime_path)) as conn:
                    self.assertEqual(conn.execute("PRAGMA integrity_check").fetchone()[0], "ok")
                    self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
                    self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 7)
                    self.assertEqual(conn.execute("SELECT count(*) FROM sqlite_schema WHERE type='table'").fetchone()[0], 8)
                if variant != "minimal-no-icons":
                    self.assertEqual(runtime_path.read_bytes(), normal_path.read_bytes())
                    self.assertEqual(rows(runtime_path, "field_evidence"), rows(source, "field_evidence"))
                else:
                    self.assertEqual(verified["counts"]["field_evidence"], 0)
                    self.assertGreater(len(rows(normal_path, "field_evidence")), 0)
                    self.assertLess(runtime_path.stat().st_size, normal_path.stat().st_size)
                    for table in COUNT_TABLES:
                        if table not in ("catalog_metadata", "field_evidence"):
                            self.assertEqual(rows(runtime_path, table), rows(normal_path, table), table)
                    self.assertEqual(verified["static_client_readiness"], verify_catalog(normal_path)["static_client_readiness"])
                    with closing(sqlite3.connect(runtime_path)) as conn:
                        kept = json.loads(conn.execute("SELECT config_json FROM carrier_profiles").fetchone()[0])
                    self.assertNotIn("lte", kept["access"])
                    self.assertEqual(kept["access"]["nr"], mixed["access"]["nr"])
                    self.assertEqual(kept["sip"]["nr"], mixed["sip"]["nr"])
                    self.assertEqual(kept["access"]["vowifi"], mixed["access"]["vowifi"])

            ordinary_summary = ordinary["catalogs"][0]["minimal_elision"]
            runtime_summary = runtime["catalogs"][0]["minimal_elision"]
            self.assertEqual(ordinary_summary["runtime_evidence"]["status"], "not_requested")
            self.assertEqual(ordinary_summary["runtime_evidence"]["rows_removed"], 0)
            for key in ordinary_summary:
                if key not in ("report_sha256", "runtime_evidence"):
                    self.assertEqual(runtime_summary[key], ordinary_summary[key], key)
            self.assertEqual(runtime_summary["profiles_removed"], 1)
            self.assertEqual(runtime_summary["access_sections_removed"], 2)
            self.assertEqual(runtime_summary["nr_accesses_removed"], 0)
            self.assertEqual(runtime_summary["evidence_rows_deleted_for_partial_accesses"], 1)
            self.assertEqual(runtime_summary["normalized_evidence_parents_trimmed"], 1)
            audit = runtime_summary["runtime_evidence"]
            ordinary_minimal = variant_path(ordinary_dir, ordinary, "minimal-no-icons")
            runtime_minimal = variant_path(runtime_dir, runtime, "minimal-no-icons")
            with closing(sqlite3.connect(ordinary_minimal)) as conn:
                values = conn.execute("SELECT source_value_json FROM field_evidence").fetchall()
                try:
                    expected_payload = conn.execute("""SELECT sum(payload) FROM dbstat
                        WHERE name IN (SELECT name FROM sqlite_schema WHERE tbl_name='field_evidence')""").fetchone()[0]
                except sqlite3.OperationalError:
                    expected_payload = None
            self.assertEqual(audit["rows_removed"], len(values))
            self.assertLess(audit["rows_removed"], len(rows(source, "field_evidence")))
            self.assertEqual(audit["source_value_json_bytes_removed"], sum(len(v.encode("utf-8")) for (v,) in values if v is not None))
            self.assertEqual(audit["sqlite_payload_bytes_removed"], expected_payload)
            self.assertEqual(audit["status"], "applied")
            self.assertEqual(audit["policy"], RUNTIME_MINIMAL_POLICY_ID)
            self.assertEqual(audit["original_evidence"], {"database": source.name, "sha256": sha256(source), "variant": "full"})
            for key in ("configuration_rows_changed", "match_rules_changed", "nr_fields_removed"):
                self.assertEqual(audit["safety"][key], 0)
            self.assertTrue(audit["safety"]["schema_and_indexes_preserved"])
            self.assertTrue(audit["safety"]["source_artifacts_and_profile_sources_unchanged"])
            self.assertFalse(audit["safety"]["registration_guarantee"])
            for path in (ordinary_minimal, runtime_minimal):
                self.assertEqual(provenance(path)["variant_policy"], PRUNING_POLICY_ID)
            self.assertNotIn("runtime_evidence", provenance(ordinary_minimal))
            self.assertEqual(provenance(runtime_minimal)["runtime_evidence"], audit)
            self.assertTrue(verify_catalog(runtime_minimal)["release_id"].endswith("+standard-pruned+runtime-minimal"))
            pruning_report = json.loads((runtime_dir / runtime_summary["report"]).read_text())
            self.assertEqual(pruning_report["runtime_evidence"], audit)
            self.assertEqual((runtime_dir / "simulation-evidence.json").read_bytes(), report.read_bytes())
            for line in (runtime_dir / "SHA256SUMS").read_text().splitlines():
                digest, name = line.split("  ", 1)
                self.assertEqual(digest, sha256(runtime_dir / name))

    def test_legacy_default_still_preserves_evidence_and_its_own_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = fixture(root / "catalog.sqlite3")
            manifest = build_variants([source], root / "out")
            minimal = variant_path(root / "out", manifest, "minimal-no-icons")
            self.assertEqual(len(rows(minimal, "field_evidence")), len(rows(source, "field_evidence")))
            self.assertEqual(provenance(minimal)["variant_policy"], POLICY_ID)
            self.assertEqual(manifest["catalogs"][0]["minimal_elision"]["runtime_evidence"]["status"], "not_requested")

    def test_missing_or_invalid_simulation_fails_before_publishing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, _ = evidence_fixture(root / "catalog.sqlite3")
            before = source.read_bytes()
            with self.assertRaisesRegex(ValueError, "runtime-minimal requires a simulation report"):
                build_variants([source], root / "out", runtime_minimal=True)
            report = root / "simulation.json"
            data = report_fixture(report)
            data["passed"] = False
            report.write_text(json.dumps(data))
            with self.assertRaises(ValueError):
                build_variants([source], root / "out", simulation_report=report, runtime_minimal=True)
            self.assertFalse((root / "out").exists())
            self.assertEqual(list(root.glob(".*-building-*")), [])
            self.assertEqual(source.read_bytes(), before)

    def test_empty_evidence_is_valid_and_never_changes_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = fixture(root / "catalog.sqlite3")
            with closing(sqlite3.connect(source)) as conn, conn:
                conn.execute("DELETE FROM field_evidence")
            report = root / "simulation.json"
            report_fixture(report)
            ordinary = build_variants([source], root / "ordinary", simulation_report=report)
            runtime = build_variants([source], root / "runtime", simulation_report=report, runtime_minimal=True)
            audit = runtime["catalogs"][0]["minimal_elision"]["runtime_evidence"]
            self.assertEqual(audit["status"], "applied")
            self.assertEqual(audit["rows_removed"], 0)
            self.assertEqual(audit["source_value_json_bytes_removed"], 0)
            self.assertIn(audit["sqlite_payload_bytes_removed"], (0, None))
            self.assertEqual(rows(variant_path(root / "ordinary", ordinary, "minimal-no-icons"), "carrier_profiles"),
                             rows(variant_path(root / "runtime", runtime, "minimal-no-icons"), "carrier_profiles"))

    def test_dbstat_is_not_a_required_sqlite_extension(self):
        with tempfile.TemporaryDirectory() as directory:
            source = fixture(Path(directory) / "catalog.sqlite3")
            with closing(sqlite3.connect(source)) as conn, conn:
                class WithoutDbstat:
                    @property
                    def total_changes(self):
                        return conn.total_changes

                    def execute(self, sql):
                        if "FROM dbstat" in sql:
                            raise sqlite3.OperationalError("no such table: dbstat")
                        return conn.execute(sql)
                result = _remove_runtime_evidence(WithoutDbstat(), {"database": source.name, "sha256": sha256(source)})
                self.assertEqual(result["rows_removed"], 6)
                self.assertGreater(result["source_value_json_bytes_removed"], 0)
                self.assertIsNone(result["sqlite_payload_bytes_removed"])
                self.assertEqual(conn.execute("SELECT count(*) FROM field_evidence").fetchone()[0], 0)

    def test_evidence_delete_side_effects_fail_closed_without_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = fixture(root / "catalog.sqlite3")
            with closing(sqlite3.connect(source)) as conn, conn:
                conn.execute("""CREATE TRIGGER unexpected_evidence_cascade
                    AFTER DELETE ON field_evidence BEGIN
                    UPDATE carriers SET canonical_name='unexpected change'; END""")
            before = source.read_bytes()
            report = root / "simulation.json"
            report_fixture(report)
            with self.assertRaisesRegex(ValueError, "unexpected additional changes"):
                build_variants([source], root / "out", simulation_report=report, runtime_minimal=True)
            self.assertFalse((root / "out").exists())
            self.assertEqual(list(root.glob(".*-building-*")), [])
            self.assertEqual(source.read_bytes(), before)

    def test_cli_is_opt_in_and_existing_output_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, _ = evidence_fixture(root / "catalog.sqlite3")
            report = root / "simulation.json"
            report_fixture(report)
            output = root / "out"
            command = [sys.executable, str(ROOT / "tools/build_variants.py"), str(source),
                       "--output-dir", str(output), "--runtime-minimal"]
            failed = subprocess.run(command, capture_output=True, text=True)
            self.assertNotEqual(failed.returncode, 0)
            self.assertIn("requires a simulation report", failed.stderr)
            self.assertFalse(output.exists())
            command += ["--simulation-report", str(report)]
            succeeded = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(succeeded.returncode, 0, succeeded.stderr)
            self.assertIn("Runtime audit evidence only:", succeeded.stdout)
            self.assertIn("source JSON bytes removed", succeeded.stdout)
            snapshot = {p.name: p.read_bytes() for p in output.iterdir()}
            repeated = subprocess.run(command, capture_output=True, text=True)
            self.assertNotEqual(repeated.returncode, 0)
            self.assertIn("never overwrite", repeated.stderr)
            with self.assertRaisesRegex(ValueError, "never overwrite"):
                build_variants([source], output, simulation_report=report, runtime_minimal=True)
            self.assertEqual({p.name: p.read_bytes() for p in output.iterdir()}, snapshot)


if __name__ == "__main__":
    unittest.main()
