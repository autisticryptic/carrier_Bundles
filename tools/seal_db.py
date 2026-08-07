#!/usr/bin/env python3
"""Seal a completed catalog and make the database file read-only."""

import argparse
import os
import sqlite3
import stat
import sys
import urllib.error
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from icons.package_icons import (  # noqa: E402
    DEFAULT_NEKOKOLPA2_DIR,
    package_database,
    refresh_nekokolpa2,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument(
        "--skip-icon-sync",
        action="store_true",
        help="seal without refreshing or embedding operator icons",
    )
    parser.add_argument(
        "--no-icon-repo-update",
        action="store_true",
        help="package icons using the current NekokoLPA2 checkout",
    )
    parser.add_argument(
        "--nekokolpa2-dir", type=Path, default=DEFAULT_NEKOKOLPA2_DIR
    )
    parser.add_argument(
        "--icon-source-base-url",
        help="override the operator-icons URL discovered from NekokoLPA2",
    )
    args = parser.parse_args()

    if not args.database.is_file():
        parser.error("database does not exist")

    if not args.skip_icon_sync:
        try:
            revision, discovered_url = refresh_nekokolpa2(
                args.nekokolpa2_dir, update=not args.no_icon_repo_update
            )
            result = package_database(
                args.database,
                source_base_url=args.icon_source_base_url or discovered_url,
                nekokolpa2_revision=revision,
            )
        except (
            RuntimeError,
            OSError,
            sqlite3.Error,
            urllib.error.URLError,
        ) as error:
            parser.exit(1, f"icon packaging failed; catalog was not sealed: {error}\n")
        print(
            f"embedded {result.assets_embedded} icons from NekokoLPA2 "
            f"{result.nekokolpa2_revision}"
        )

    with sqlite3.connect(args.database) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise SystemExit("foreign_key_check failed")
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise SystemExit("quick_check failed")
        row = connection.execute(
            "SELECT release_id, sealed FROM catalog_release WHERE singleton = 1"
        ).fetchone()
        if row is None:
            raise SystemExit("catalog_release is missing")
        if row[1] != 0:
            raise SystemExit("catalog is already sealed")
        connection.execute("UPDATE catalog_release SET sealed = 1 WHERE singleton = 1")
        connection.commit()
        connection.execute("VACUUM")

    current = args.database.stat().st_mode
    readonly = (current | stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH) & ~(
        stat.S_IWUSR
        | stat.S_IWGRP
        | stat.S_IWOTH
        | stat.S_IXUSR
        | stat.S_IXGRP
        | stat.S_IXOTH
    )
    os.chmod(args.database, readonly)
    print(f"sealed {row[0]}: file:{args.database}?mode=ro&immutable=1")


if __name__ == "__main__":
    main()
