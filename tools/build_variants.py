#!/usr/bin/env python3
"""Build full, no-icons and minimal catalogs offline in the original v7 format.

With validated simulation evidence, minimal directly deletes covered LTE/VoWiFi
access configurations (or wholly covered rows). Unknown policies and NR remain.
Without evidence the legacy optional-default elision mode is retained for API
compatibility. Opt-in runtime-minimal additionally clears audit evidence rows
only from the pruned minimal file; the original evidence stays in full. Inputs
are immutable; no hardware/network registration is done.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import sys
import tempfile
from collections import Counter
from contextlib import closing
from pathlib import Path
from typing import Any, Callable, Sequence

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from catalog_contract import evaluate_readiness, validate_config  # noqa: E402
from tools.verify_catalog import verify_catalog  # noqa: E402
from simulation_pruning.prune import prune_profiles  # noqa: E402
from simulation_pruning.evidence import validate_evidence  # noqa: E402

PRUNING_POLICY_ID = "simadmin-simulated-standard-pruning-v1"
POLICY_ID = "simadmin-1.1.5-optional-defaults-v1"
RUNTIME_MINIMAL_POLICY_ID = "simadmin-v7-runtime-evidence-elision-v1"
VARIANTS = ("full", "no-icons", "minimal-no-icons")
# These are exact defaults of SimAdmin's carrier_catalog_v7::project_config /
# project_vowifi_access, NOT a list of all 3GPP-standard fields. In particular,
# home_domain/authentication/APN/P-CSCF/ePDG/IDi are readiness requirements;
# realm omission changes derivation provenance; IDr omission changes IKE_AUTH.
# None of those are removed. Do not extend this list without consumer tests.
STANDARD_IDENTITIES = [
    {
        "identity_type": "nai",
        "role": "impi",
        "source": "derived_imsi",
        "use_when": "if_isim_missing",
        "value_template": "{imsi}@{home_domain}",
    },
    {
        "identity_type": "sip_uri",
        "role": "impu",
        "source": "derived_imsi",
        "use_when": "if_isim_missing",
        "value_template": "sip:{imsi}@{home_domain}",
    },
]
OPTIONAL_DEFAULTS: dict[str, Any] = {
    "/ims/identity_templates": STANDARD_IDENTITIES,
    "/ims/transport": "udp",
    "/ims/local_port": 5060,
    "/access/lte/ip_family": "ipv4v6",
    "/access/vowifi/ip_family": "ipv4v6",
    "/access/vowifi/apn": "ims",
    "/access/vowifi/ike/initial_port": 500,
    "/access/vowifi/ike/nat_keepalive_seconds": 20,
    "/access/vowifi/ike/dpd_interval_seconds": 600,
}
_MISSING = object()


def encoded(value: Any) -> str:
    """Canonical JSON without dropping nulls, empty arrays, false or zero."""
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _json_sha256(value: Any) -> str:
    return hashlib.sha256(encoded(value).encode("utf-8")).hexdigest()


def _at(config: dict[str, Any], pointer: str) -> Any:
    value: Any = config
    for token in pointer.lstrip("/").split("/"):
        if not isinstance(value, dict) or token not in value:
            return _MISSING
        value = value[token]
    return value


def elide_optional_defaults(config: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Remove only a finite, audited set of exact default-valued overrides.

    Retain containers and every other value. Never infer equivalence from a
    ready flag, a standard-looking domain, an empty list, or a scalar coercion.
    """
    result = copy.deepcopy(config)
    removed: list[dict[str, Any]] = []
    for pointer, default in OPTIONAL_DEFAULTS.items():
        value = _at(result, pointer)
        if value is _MISSING or encoded(value) != encoded(default):
            continue
        if pointer == "/ims/transport":
            # Removing UDP must not expose an alternate common TCP override.
            alternate = _at(result, "/sip/common/transport")
            if alternate is not _MISSING and alternate != "udp":
                continue
        parent_pointer, key = pointer.rsplit("/", 1)
        parent = _at(result, parent_pointer)
        del parent[key]
        removed.append({"path": pointer, "value": value, "rule": POLICY_ID})
    validate_config(result)
    if evaluate_readiness(copy.deepcopy(config)) != evaluate_readiness(copy.deepcopy(result)):
        raise ValueError("default-elision policy changed static readiness")
    # Stored diagnostic lists must be retained, not regenerated or hidden.
    if _at(config, "/readiness") != _at(result, "/readiness"):
        raise ValueError("default-elision policy changed readiness diagnostics")
    return result, removed


def _readonly(path: Path) -> None:
    mode = path.stat().st_mode
    path.chmod((mode | stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH) & ~(
        stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH |
        stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    ))


def _check_input(path: Path) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*\.sqlite3", path.name):
        raise ValueError(f"input must have a simple .sqlite3 filename: {path}")
    if path.name.endswith(("-no-icons.sqlite3", "-with-icons.sqlite3")):
        raise ValueError(f"use the original full catalog, not a derived variant: {path}")
    # An immutable snapshot cannot depend on a live WAL or rollback journal.
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(str(path) + suffix)
        if sidecar.exists():
            raise ValueError(f"input has a SQLite sidecar; close/seal it first: {sidecar}")
    # Archives/Windows transfers can lose POSIX write bits. Check sealed metadata
    # and content read-only, without chmod-ing the user's original snapshot.
    summary = verify_catalog(path, require_readonly=False)
    return {**summary, "sha256": sha256(path), "bytes": path.stat().st_size}


def _remove_runtime_evidence(connection: sqlite3.Connection, source: dict[str, Any]) -> dict[str, Any]:
    """Remove audit rows, not registration configuration or schema objects.

    SimAdmin's carrier_catalog_v7::validate_schema requires this table's
    existence; its profile/match/projection queries never read its rows. Keep
    all table/index definitions, including the empty B-tree roots. This policy
    is specific to that consumer, not a claim about other catalog consumers.
    """
    rows, value_bytes = connection.execute(
        """SELECT count(*), coalesce(sum(length(CAST(source_value_json AS BLOB))), 0)
           FROM field_evidence"""
    ).fetchone()
    # dbstat is optional in SQLite. Its payload count includes the table and
    # indexes, but excludes page headers/free space. Unlike file/page deltas,
    # it does not misattribute earlier pruning's freed space to this operation.
    try:
        payload_bytes = connection.execute(
            """SELECT coalesce(sum(payload), 0) FROM dbstat
               WHERE name IN (SELECT name FROM sqlite_schema
                              WHERE tbl_name = 'field_evidence'
                                AND type IN ('table', 'index'))"""
        ).fetchone()[0]
    except sqlite3.OperationalError:
        payload_bytes = None
    changes_before = connection.total_changes
    deleted = connection.execute("DELETE FROM field_evidence").rowcount
    if deleted != rows:
        raise ValueError("runtime evidence deletion count mismatch")
    # Canonical v7 has no evidence-delete triggers or incoming cascades. Fail
    # closed if a noncanonical input would change anything beyond these rows.
    if connection.total_changes - changes_before != rows:
        raise ValueError("runtime evidence deletion caused unexpected additional changes")
    return {
        "policy": RUNTIME_MINIMAL_POLICY_ID,
        "status": "applied",
        "table": "field_evidence",
        "rows_removed": rows,
        "source_value_json_bytes_removed": value_bytes,
        "sqlite_payload_bytes_removed": payload_bytes,
        "bytes_accounting": {
            "source_value_json": "stored JSON bytes via SQLite CAST AS BLOB; NULL counts as zero",
            "sqlite_payload": "dbstat table + index record payload; unavailable if null; not file-size savings",
        },
        "original_evidence": {"database": source["database"], "sha256": source["sha256"], "variant": "full"},
        "safety": {
            "scope": "after ordinary simulation pruning, minimal-no-icons only",
            "consumer": "SimAdmin schema-v7: field_evidence required as a table, rows not read",
            "schema_and_indexes_preserved": True,
            "configuration_rows_changed": 0,
            "match_rules_changed": 0,
            "nr_fields_removed": 0,
            "source_artifacts_and_profile_sources_unchanged": True,
            "registration_guarantee": False,
        },
    }


def _transform_variant(
    database: Path, variant: str, source: dict[str, Any], evidence: dict | None = None,
    *, runtime_minimal: bool = False,
) -> dict[str, Any]:
    if runtime_minimal and (variant != "minimal-no-icons" or evidence is None):
        raise ValueError("runtime-minimal requires simulation-pruned minimal-no-icons")
    database.chmod(stat.S_IRUSR | stat.S_IWUSR)
    pruning_summary = None
    changes: list[dict[str, Any]] = []
    path_counts: Counter[str] = Counter()
    evidence_deselected = 0
    runtime_evidence = {
        "status": "not_requested", "rows_removed": 0,
        "source_value_json_bytes_removed": 0, "sqlite_payload_bytes_removed": 0,
    }
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA cache_size = -65536")
        connection.execute("PRAGMA journal_mode = DELETE")
        connection.execute("UPDATE catalog_metadata SET sealed = 0 WHERE singleton = 1")
        # Do not rely only on FK actions: explicitly clear both kinds of pointers.
        connection.execute("UPDATE carriers SET primary_asset_id = NULL")
        connection.execute("UPDATE carrier_profiles SET profile_asset_id = NULL")
        connection.execute("DELETE FROM visual_assets")
        if variant == "minimal-no-icons" and evidence is not None:
            pruning_summary = prune_profiles(connection, evidence)
        elif variant == "minimal-no-icons":
            rows = connection.execute(
                "SELECT profile_id, config_json FROM carrier_profiles ORDER BY profile_id"
            ).fetchall()
            for profile_id, raw in rows:
                original = json.loads(raw)
                config, removed = elide_optional_defaults(original)
                if not removed:
                    continue
                connection.execute(
                    "UPDATE carrier_profiles SET config_json = ? WHERE profile_id = ?",
                    (encoded(config), profile_id),
                )
                for change in removed:
                    pointer = change["path"]
                    path_counts[pointer] += 1
                    # Keep the full original evidence and value for auditing, but
                    # no longer claim it selects an explicitly serialized field.
                    evidence_deselected += connection.execute(
                        """UPDATE field_evidence SET selected = 0
                           WHERE profile_id = ? AND target_kind = 'config' AND selected = 1
                             AND (target_path = ? OR substr(target_path, 1, ?) = ?)""",
                        (profile_id, pointer, len(pointer) + 1, pointer + "/"),
                    ).rowcount
                changes.append({
                    "profile_id": profile_id,
                    "config_sha256_before": _json_sha256(original),
                    "config_sha256_after": _json_sha256(config),
                    "removed": removed,
                })
        if runtime_minimal:
            # Count only rows remaining AFTER whole/partial access pruning.
            runtime_evidence = _remove_runtime_evidence(connection, source)
        old_notes = connection.execute(
            "SELECT notes FROM catalog_metadata WHERE singleton = 1"
        ).fetchone()[0]
        provenance_data = {
            "variant": variant,
            "variant_policy": PRUNING_POLICY_ID if pruning_summary is not None else POLICY_ID,
            "source_release_id": source["release_id"],
            "source_database_sha256": source["sha256"],
            "pruning_evidence": evidence if pruning_summary is not None else None,
        }
        if runtime_minimal:
            provenance_data["runtime_evidence"] = runtime_evidence
        provenance = encoded(provenance_data)
        connection.execute(
            """UPDATE catalog_metadata SET release_id = ?, sealed = 1, notes = ?
               WHERE singleton = 1""",
            (f'{source["release_id"]}+{variant}' + ('+standard-pruned' if pruning_summary is not None else '')
             + ('+runtime-minimal' if runtime_minimal else ''),
             f"{old_notes}\n{provenance}" if old_notes else provenance),
        )
        connection.commit()
        connection.execute("VACUUM")
    _readonly(database)
    summary = verify_catalog(database)
    for table, count in source["counts"].items():
        expected = 0 if table == "visual_assets" else count
        mutable = {"carrier_profiles", "profile_match_rules", "profile_sources", "field_evidence"}
        if runtime_minimal and table == "field_evidence":
            if summary["counts"][table] != 0:
                raise ValueError("runtime-minimal retained field_evidence rows")
        elif pruning_summary is not None and table in mutable:
            if summary["counts"][table] > count:
                raise ValueError(f"pruning unexpectedly added rows to {table}")
        elif summary["counts"][table] != expected:
            raise ValueError(f"{variant} changed {table} row coverage")
    if pruning_summary is not None:
        return {"source_database": source["database"], "source_sha256": source["sha256"],
                **pruning_summary, "runtime_evidence": runtime_evidence}
    if summary["static_client_readiness"] != source["static_client_readiness"]:
        raise ValueError(f"{variant} changed readiness coverage")
    return {
        "policy": POLICY_ID,
        "source_database": source["database"],
        "source_sha256": source["sha256"],
        "profiles_removed": 0,
        "access_sections_removed": 0,
        "nr_fields_removed": 0,
        "profiles_changed": len(changes),
        "fields_removed": sum(path_counts.values()),
        "removed_by_path": dict(sorted(path_counts.items())),
        "evidence_rows_preserved_but_deselected": evidence_deselected,
        "runtime_evidence": runtime_evidence,
        "changes": changes,
    }


def _rewrite_variant(
    database: Path, variant: str, source: dict[str, Any], evidence: dict | None = None,
    *, runtime_minimal: bool = False,
) -> dict[str, Any]:
    # Avoid thousands of tiny journal writes on network/Windows mounted output
    # directories. Transform in the OS temporary filesystem; only a verified,
    # closed snapshot is copied back into the unpublished output staging area.
    with tempfile.TemporaryDirectory(prefix="carrier-variant-") as temporary:
        working = Path(temporary) / database.name
        shutil.copyfile(database, working)
        try:
            report = _transform_variant(working, variant, source, evidence, runtime_minimal=runtime_minimal)
            shutil.copyfile(working, database)
        finally:
            if working.exists():
                working.chmod(stat.S_IRUSR | stat.S_IWUSR)
    _readonly(database)
    return report


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def build_variants(
    databases: Sequence[Path], output_dir: Path,
    *, progress: Callable[[str], None] | None = None,
    simulation_report: Path | None = None, simadmin_source: Path | None = None,
    runtime_minimal: bool = False,
) -> dict[str, Any]:
    """Publish validated variants; runtime_minimal is an explicit audit-row opt-in.

    It requires direct simulation pruning, never changes the legacy elision
    mode, and leaves full/no-icons byte-identical to builds without the flag.
    """
    if runtime_minimal and simulation_report is None:
        raise ValueError("runtime-minimal requires a simulation report")
    notify = progress or (lambda message: None)
    evidence = validate_evidence(simulation_report, simadmin_source) if simulation_report is not None else None
    if simadmin_source is not None and simulation_report is None:
        raise ValueError("consumer source requires a simulation report")
    if not databases:
        raise ValueError("at least one full catalog is required")
    output_dir = output_dir.absolute()
    if output_dir.exists() or output_dir.is_symlink():
        raise ValueError(f"output already exists; builds never overwrite: {output_dir}")
    inputs = [Path(path).absolute() for path in databases]
    names = [path.name.casefold() for path in inputs]
    if len(set(names)) != len(names):
        raise ValueError("source filenames must be unique (including case)")
    sources = []
    for path in inputs:
        notify(f"Validating input: {path.name}")
        sources.append((path, _check_input(path)))
    output_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}-building-", dir=output_dir.parent))
    try:
        catalogs = []
        for path, source in sources:
            variants = {}
            pruning = None
            for variant in VARIANTS:
                notify(f"Building {variant}: {path.name}")
                name = path.name if variant == "full" else f"{path.stem}-{variant}.sqlite3"
                destination = staging / name
                shutil.copyfile(path, destination)
                if sha256(destination) != source["sha256"]:
                    raise ValueError(f"input changed while copying: {path}")
                if variant == "full":
                    _readonly(destination)
                else:
                    report = _rewrite_variant(
                        destination, variant, source, evidence,
                        runtime_minimal=runtime_minimal and variant == "minimal-no-icons",
                    )
                    if variant == "minimal-no-icons":
                        report_path = staging / f"{path.stem}-minimal-no-icons.pruning.json"
                        _write_json(report_path, report)
                        pruning = {
                            "report": report_path.name,
                            "report_sha256": sha256(report_path),
                            **{key: value for key, value in report.items() if key not in (
                                "changes", "source_database", "source_sha256", "policy"
                            )},
                        }
                variants[variant] = {
                    **verify_catalog(destination),
                    "sha256": sha256(destination),
                    "bytes": destination.stat().st_size,
                }
            if sha256(path) != source["sha256"]:
                raise ValueError(f"input changed during build: {path}")
            catalogs.append({"source": source, "variants": variants, "minimal_elision": pruning})
        manifest = {
            "manifest_version": 1,
            "policy": {
                "id": PRUNING_POLICY_ID if evidence is not None else POLICY_ID,
                "consumer": "SimAdmin schema-v7 carrier-bundles-ims-v1",
                "simulation_evidence": evidence,
                "minimal_representation": "direct removal of simulated-standard access configurations; unchanged schema/contract" if evidence is not None else "optional default elision",
                "builder_sha256": sha256(Path(__file__)),
                "optional_defaults": OPTIONAL_DEFAULTS,
                "whole_profile_pruning": evidence is not None,
                "runtime_minimal": {
                    "enabled": runtime_minimal,
                    "policy": RUNTIME_MINIMAL_POLICY_ID if runtime_minimal else None,
                    "scope": "field_evidence rows only, after pruning, in minimal-no-icons only",
                    "original_evidence": "preserved in byte-identical full catalog",
                    "schema_and_indexes_preserved": True,
                },
                "registration_guarantee": False,
                "notes": [
                    "Each source stays independent; no iOS/Android cross-source field merging.",
                    "Full files are byte-identical to the sealed input snapshots.",
                    "No-icons retains every configuration; only visual assets and pointers are removed.",
                    "With a simulation report, minimal directly removes covered LTE/Wi-Fi accesses or wholly covered rows; others remain.",
                    "No new format, reconstruction marker or runtime decoder; removed accesses fall back through the existing derived resolver.",
                    "Not proof of 4G/5G or VoWiFi registration; operator policy, subscription, modem and network still matter.",
                ],
            },
            "catalogs": catalogs,
        }
        if simulation_report is not None:
            shutil.copyfile(simulation_report, staging / "simulation-evidence.json")
        _write_json(staging / "catalog-variants.json", manifest)
        checksummed = sorted(file for file in staging.iterdir() if file.is_file())
        (staging / "SHA256SUMS").write_text("".join(
            f"{sha256(file)}  {file.name}\n" for file in checksummed
        ), encoding="ascii")
        if output_dir.exists() or output_dir.is_symlink():
            raise ValueError(f"output appeared during build: {output_dir}")
        os.rename(staging, output_dir)
        return manifest
    finally:
        if staging.exists():
            # Windows requires write permission to remove read-only temporary files.
            for file in staging.iterdir():
                if file.is_file():
                    file.chmod(stat.S_IRUSR | stat.S_IWUSR)
            shutil.rmtree(staging)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("databases", type=Path, nargs="+", help="sealed full catalogs (one per source)")
    parser.add_argument("--output-dir", type=Path, required=True, help="new output directory; never overwritten")
    parser.add_argument("--simulation-report", type=Path, help="verified real-code matrix evidence; enables direct access/profile pruning in the original v7 format")
    parser.add_argument("--simadmin-source", type=Path, help="optionally require evidence hashes to match this consumer checkout")
    parser.add_argument("--runtime-minimal", action="store_true", help="requires --simulation-report; additionally clear field_evidence audit rows from minimal-no-icons only, preserving schema/indexes and original evidence in full")
    args = parser.parse_args()
    try:
        result = build_variants(
            args.databases, args.output_dir,
            simulation_report=args.simulation_report, simadmin_source=args.simadmin_source,
            runtime_minimal=args.runtime_minimal,
            progress=lambda message: print(message, file=sys.stderr, flush=True),
        )
    except (OSError, ValueError, sqlite3.Error) as error:
        parser.exit(1, f"catalog variant build failed: {error}\n")
    for entry in result["catalogs"]:
        print(f'{entry["source"]["database"]}: 3 variants; '
              f'{entry["minimal_elision"]["access_sections_removed"]} access configs / {entry["minimal_elision"]["profiles_removed"]} profiles removed; '
              f'{entry["variants"]["minimal-no-icons"]["counts"]["carrier_profiles"]} profiles retained')
        runtime = entry["minimal_elision"]["runtime_evidence"]
        if runtime["status"] == "applied":
            print(f'  Runtime audit evidence only: {runtime["rows_removed"]} rows / '
                  f'{runtime["source_value_json_bytes_removed"]} source JSON bytes removed; '
                  'no additional configuration or match changes; original evidence retained in full')
    print(f"Verified catalogs, pruning reports and SHA256SUMS: {args.output_dir}")


if __name__ == "__main__":
    main()
