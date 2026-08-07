#!/usr/bin/env python3
"""Create a new writable Carrier Bundles catalog build database."""

import argparse
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--generator-version", default="development")
    args = parser.parse_args()

    if args.database.exists():
        parser.error("output database already exists; catalog builds never update in place")

    args.database.parent.mkdir(parents=True, exist_ok=True)
    schema = (ROOT / "schema.sql").read_text(encoding="utf-8")
    with sqlite3.connect(args.database) as connection:
        connection.executescript(schema)
        connection.execute(
            """INSERT INTO catalog_release(
                   singleton, release_id, generated_at, generator_version, sealed
               ) VALUES (1, ?, ?, ?, 0)""",
            (
                args.release_id,
                datetime.now(timezone.utc).isoformat(),
                args.generator_version,
            ),
        )
        connection.commit()
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise SystemExit("SQLite quick_check failed")

    print(f"created writable catalog build: {args.database}")


if __name__ == "__main__":
    main()
