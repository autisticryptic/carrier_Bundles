#!/usr/bin/env python3
"""Verify a sealed schema-v7 catalog and print a compact JSON summary."""

from __future__ import annotations

import argparse
import copy
import json
import sqlite3
import stat
import sys
from contextlib import closing
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from catalog_contract import (  # noqa: E402
    CONFIG_CONTRACT,
    evaluate_readiness,
    validate_config,
)


COUNT_TABLES = (
    "catalog_metadata",
    "source_artifacts",
    "visual_assets",
    "carriers",
    "carrier_profiles",
    "profile_match_rules",
    "profile_sources",
    "field_evidence",
)


def _validate_profiles(connection: sqlite3.Connection) -> dict[str, int]:
    readiness = {
        "lte_ims_ready_profiles": 0,
        "nr_ims_ready_profiles": 0,
        "vowifi_ready_profiles": 0,
        "partial_profiles": 0,
    }
    for row in connection.execute(
        """SELECT profile_id, lte_ims_status, nr_ims_status,
                  vowifi_status, config_json
           FROM carrier_profiles"""
    ):
        profile_id, lte, nr, vowifi, raw = row
        try:
            config = json.loads(raw)
            validate_config(config)
        except (json.JSONDecodeError, ValueError) as error:
            raise ValueError(f"invalid config_json for {profile_id}: {error}") from error
        calculated = evaluate_readiness(copy.deepcopy(config))
        stored = {"lte": lte, "nr": nr, "vowifi": vowifi}
        if stored != calculated:
            raise ValueError(
                f"readiness mismatch for {profile_id}: stored={stored}, "
                f"calculated={calculated}"
            )
        readiness["lte_ims_ready_profiles"] += int(lte == "ready")
        readiness["nr_ims_ready_profiles"] += int(nr == "ready")
        readiness["vowifi_ready_profiles"] += int(vowifi == "ready")
        readiness["partial_profiles"] += int("partial" in stored.values())
    return readiness


def verify_catalog(database: Path) -> dict[str, object]:
    if not database.is_file():
        raise ValueError(f"catalog does not exist: {database}")
    writable_bits = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH
    if database.stat().st_mode & writable_bits:
        raise ValueError("catalog file is not sealed read-only")

    uri = f"{database.resolve().as_uri()}?mode=ro&immutable=1"
    with closing(sqlite3.connect(uri, uri=True)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        quick_check = connection.execute("PRAGMA quick_check").fetchone()
        if quick_check is None or quick_check[0] != "ok":
            raise ValueError(f"SQLite quick_check failed: {quick_check}")
        foreign_key_error = connection.execute("PRAGMA foreign_key_check").fetchone()
        if foreign_key_error is not None:
            raise ValueError(f"foreign_key_check failed: {foreign_key_error}")
        application_id = connection.execute("PRAGMA application_id").fetchone()[0]
        user_version = connection.execute("PRAGMA user_version").fetchone()[0]
        if user_version != 7:
            raise ValueError(f"unsupported schema version: {user_version}")
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type = 'table'"
            )
            if not row[0].startswith("sqlite_")
        }
        if tables != set(COUNT_TABLES):
            raise ValueError(
                f"unexpected schema tables: missing={sorted(set(COUNT_TABLES) - tables)}, "
                f"extra={sorted(tables - set(COUNT_TABLES))}"
            )
        release = connection.execute(
            """SELECT schema_name, schema_version, config_contract, release_id,
                      generated_at, generator_name, generator_version, sealed
               FROM catalog_metadata WHERE singleton = 1"""
        ).fetchone()
        if release is None or release[7] != 1:
            raise ValueError("catalog_metadata is missing or unsealed")
        if release[0] != "carrier_bundles" or release[1] != user_version:
            raise ValueError("catalog metadata does not match the SQLite schema")
        if release[2] != CONFIG_CONTRACT:
            raise ValueError(f"unsupported config contract: {release[2]}")
        counts = {
            table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in COUNT_TABLES
        }
        readiness = _validate_profiles(connection)
        try:
            connection.execute(
                "INSERT INTO carriers(carrier_id, canonical_name) VALUES ('verify', 'verify')"
            )
        except sqlite3.OperationalError:
            pass
        else:
            raise ValueError("catalog accepted a write through its read-only URI")

    return {
        "database": database.name,
        "release_id": release[3],
        "generated_at": release[4],
        "generator_name": release[5],
        "generator_version": release[6],
        "config_contract": release[2],
        "application_id": application_id,
        "schema_version": user_version,
        "sealed": True,
        "quick_check": "ok",
        "foreign_key_check": "ok",
        "counts": counts,
        "static_client_readiness": readiness,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    args = parser.parse_args()
    try:
        result = verify_catalog(args.database)
    except (OSError, ValueError, sqlite3.Error) as error:
        parser.exit(1, f"catalog verification failed: {error}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
