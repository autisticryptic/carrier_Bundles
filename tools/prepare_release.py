#!/usr/bin/env python3
"""Validate staged release metadata and generate checksums and a summary."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools.verify_catalog import verify_catalog  # noqa: E402


@dataclass(frozen=True)
class PreparedRelease:
    tag_name: str
    release_name: str
    target_commitish: str
    prerelease: bool
    database_path: Path
    summary_path: Path
    checksums_path: Path
    notes_path: Path


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value or "\n" in value or "\r" in value:
        raise ValueError(f"{field} must be a non-empty single-line string")
    return value


def _path_in_root(root: Path, value: Any, field: str) -> Path:
    relative = Path(_text(value, field))
    if relative.is_absolute():
        raise ValueError(f"{field} must be relative to the repository root")
    resolved = (root / relative).resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as error:
        raise ValueError(f"{field} escapes the repository root") from error
    return resolved


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def prepare_release(manifest_path: Path, *, root: Path = ROOT) -> PreparedRelease:
    with manifest_path.open("r", encoding="utf-8") as stream:
        manifest = json.load(stream)
    if not isinstance(manifest, dict):
        raise ValueError("release manifest must be a JSON object")

    tag_name = _text(manifest.get("tag_name"), "tag_name")
    if not re.fullmatch(r"v[0-9A-Za-z][0-9A-Za-z._-]*", tag_name):
        raise ValueError("tag_name must be a simple version tag beginning with v")
    release_name = _text(manifest.get("release_name"), "release_name")
    target = _text(manifest.get("target_commitish"), "target_commitish").lower()
    if not re.fullmatch(r"[0-9a-f]{40}", target):
        raise ValueError("target_commitish must be a full 40-character commit SHA")
    prerelease = manifest.get("prerelease")
    if not isinstance(prerelease, bool):
        raise ValueError("prerelease must be a boolean")

    database = _path_in_root(root, manifest.get("database"), "database")
    notes = _path_in_root(root, manifest.get("notes"), "notes")
    if database.suffix != ".sqlite3":
        raise ValueError("release database must use the .sqlite3 extension")
    if not notes.is_file():
        raise ValueError(f"release notes do not exist: {notes}")

    summary = verify_catalog(database)
    output_dir = manifest_path.resolve().parent
    summary_path = output_dir / "catalog-summary.json"
    checksums_path = output_dir / "SHA256SUMS"
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    checksums_path.write_text(
        f"{_sha256(database)}  {database.name}\n", encoding="ascii"
    )
    return PreparedRelease(
        tag_name=tag_name,
        release_name=release_name,
        target_commitish=target,
        prerelease=prerelease,
        database_path=database,
        summary_path=summary_path,
        checksums_path=checksums_path,
        notes_path=notes,
    )


def _write_github_outputs(release: PreparedRelease, output: Path) -> None:
    values = {
        "tag_name": release.tag_name,
        "release_name": release.release_name,
        "target_commitish": release.target_commitish,
        "prerelease": str(release.prerelease).lower(),
        "database_path": str(release.database_path),
        "summary_path": str(release.summary_path),
        "checksums_path": str(release.checksums_path),
        "notes_path": str(release.notes_path),
    }
    with output.open("a", encoding="utf-8") as stream:
        for key, value in values.items():
            stream.write(f"{key}={value}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args()
    try:
        prepared = prepare_release(args.manifest)
        github_output = os.environ.get("GITHUB_OUTPUT")
        if github_output:
            _write_github_outputs(prepared, Path(github_output))
    except (OSError, ValueError, json.JSONDecodeError) as error:
        parser.exit(1, f"release preparation failed: {error}\n")
    print(
        json.dumps(
            {
                "tag_name": prepared.tag_name,
                "release_name": prepared.release_name,
                "target_commitish": prepared.target_commitish,
                "prerelease": prepared.prerelease,
                "database": str(prepared.database_path),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()

