#!/usr/bin/env python3
"""Verify a sealed Carrier Bundles catalog and print a compact JSON summary."""

from __future__ import annotations

import argparse
import json
import sqlite3
import stat
from pathlib import Path


COUNT_TABLES = (
    "carrier_profiles",
    "access_configs",
    "source_snapshots",
    "raw_config_values",
    "field_evidence",
    "visual_assets",
)


def verify_catalog(database: Path) -> dict[str, object]:
    if not database.is_file():
        raise ValueError(f"catalog does not exist: {database}")
    writable_bits = stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH
    if database.stat().st_mode & writable_bits:
        raise ValueError("catalog file is not sealed read-only")

    uri = f"{database.resolve().as_uri()}?mode=ro&immutable=1"
    with sqlite3.connect(uri, uri=True) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        quick_check = connection.execute("PRAGMA quick_check").fetchone()
        if quick_check is None or quick_check[0] != "ok":
            raise ValueError(f"SQLite quick_check failed: {quick_check}")
        foreign_key_error = connection.execute("PRAGMA foreign_key_check").fetchone()
        if foreign_key_error is not None:
            raise ValueError(f"foreign_key_check failed: {foreign_key_error}")
        release = connection.execute(
            """SELECT release_id, generated_at, generator_version, sealed
               FROM catalog_release WHERE singleton = 1"""
        ).fetchone()
        if release is None or release[3] != 1:
            raise ValueError("catalog_release is missing or unsealed")
        counts = {
            table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in COUNT_TABLES
        }
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
        "release_id": release[0],
        "generated_at": release[1],
        "generator_version": release[2],
        "sealed": True,
        "quick_check": "ok",
        "foreign_key_check": "ok",
        "counts": counts,
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

