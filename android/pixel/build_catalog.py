#!/usr/bin/env python3
"""Build a sealed Carrier Bundles SQLite catalog from a Pixel factory image."""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import venv
from contextlib import closing
from dataclasses import asdict, replace
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
VENV = ROOT / ".venv"
REQUIREMENTS = Path(__file__).with_name("requirements.txt")


def _venv_python() -> Path:
    return VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _runtime_is_ready(executable: Path) -> bool:
    check = subprocess.run(
        [
            str(executable),
            "-c",
            "import importlib.metadata as m; "
            "assert m.version('protobuf') == '6.31.1'",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    return check.returncode == 0


def _bootstrap_runtime() -> None:
    target = _venv_python()
    running_in_target = Path(sys.prefix).resolve() == VENV.resolve()
    if not target.exists():
        print(f"creating Python environment: {VENV}", file=sys.stderr)
        venv.EnvBuilder(with_pip=True).create(VENV)
    if not _runtime_is_ready(target):
        print("installing Pixel extractor dependencies", file=sys.stderr)
        subprocess.run(
            [
                str(target),
                "-m",
                "pip",
                "install",
                "--disable-pip-version-check",
                "-r",
                str(REQUIREMENTS),
            ],
            check=True,
        )
    if not running_in_target:
        os.execv(str(target), [str(target), str(__file__), *sys.argv[1:]])


_bootstrap_runtime()
sys.path.insert(0, str(ROOT))

from android.pixel import PARSER_VERSION  # noqa: E402
from android.pixel.catalog import import_pixel_catalog  # noqa: E402
from android.pixel.firmware import (  # noqa: E402
    ensure_factory_zip,
    extract_pixel_factory,
    sha256_file,
)
from android.pixel.sources import FactoryArtifact, resolve_factory_artifact  # noqa: E402


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", default="redfin", help="Pixel device codename")
    parser.add_argument(
        "--device-name",
        help="marketing model name; discovered from Google unless --offline is used",
    )
    parser.add_argument("--build-id", default="latest", help="official build id")
    parser.add_argument(
        "--accept-google-terms",
        action="store_true",
        help="acknowledge the terms on Google's Factory Images page",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="use a local factory ZIP without fetching Google's metadata page",
    )
    parser.add_argument("--factory-zip", type=Path, help="local cache/input ZIP path")
    parser.add_argument("--factory-sha256", help="expected SHA-256 for offline input")
    parser.add_argument("--source-uri", help="original firmware URL for offline input")
    parser.add_argument("--os-version", help="Android version for offline input")
    parser.add_argument("--work-dir", type=Path, help="partition extraction cache")
    parser.add_argument("--output", type=Path, help="published SQLite path")
    parser.add_argument(
        "--skip-icon-sync",
        action="store_true",
        help="seal without downloading and embedding operator icons",
    )
    parser.add_argument(
        "--no-icon-repo-update",
        action="store_true",
        help="use the current local NekokoLPA2 revision",
    )
    parser.add_argument(
        "--no-standard-derived",
        action="store_true",
        help="omit 3GPP-derived IMS domain and identity fallback templates",
    )
    return parser.parse_args()


def _offline_artifact(args: argparse.Namespace) -> FactoryArtifact:
    if args.factory_zip is None or not args.factory_zip.is_file():
        raise ValueError("--offline requires an existing --factory-zip")
    if args.build_id.casefold() == "latest":
        raise ValueError("--offline requires an explicit --build-id")
    if not args.os_version:
        raise ValueError("--offline requires --os-version")
    actual_sha256 = sha256_file(args.factory_zip)
    if args.factory_sha256 and actual_sha256.casefold() != args.factory_sha256.casefold():
        raise ValueError(
            "offline factory image SHA-256 mismatch: "
            f"expected {args.factory_sha256}, got {actual_sha256}"
        )
    source_uri = args.source_uri
    if source_uri is None and re.fullmatch(r".+-factory-[0-9a-f]{8}\.zip", args.factory_zip.name):
        source_uri = f"https://dl.google.com/dl/android/aosp/{args.factory_zip.name}"
    return FactoryArtifact(
        device=args.device,
        device_name=args.device_name or ("Pixel 5" if args.device == "redfin" else args.device),
        build_id=args.build_id,
        os_version=args.os_version,
        description=f"offline Pixel factory image {args.factory_zip.name}",
        url=source_uri or f"local-artifact:{args.factory_zip.name}",
        sha256=actual_sha256,
    )


def _resolve_artifact(args: argparse.Namespace) -> FactoryArtifact:
    if args.offline:
        return _offline_artifact(args)
    artifact = resolve_factory_artifact(
        args.device,
        args.build_id,
        accept_google_terms=args.accept_google_terms,
    )
    if args.device_name:
        artifact = replace(artifact, device_name=args.device_name)
    if args.factory_sha256 and artifact.sha256.casefold() != args.factory_sha256.casefold():
        raise ValueError("--factory-sha256 does not match Google's published hash")
    if args.os_version and artifact.os_version != args.os_version:
        raise ValueError("--os-version does not match Google's published metadata")
    return artifact


def _run_tool(*arguments: str) -> None:
    subprocess.run([sys.executable, *arguments], cwd=ROOT, check=True)


def _label_release(database: Path, artifact: FactoryArtifact) -> None:
    notes = (
        f"Google {artifact.device_name} ({artifact.device}) factory image; "
        f"Android {artifact.os_version}; build {artifact.build_id}."
    )
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute(
            "UPDATE catalog_release SET notes = ? WHERE singleton = 1", (notes,)
        )
        connection.commit()


def main() -> None:
    args = _parse_args()
    try:
        artifact = _resolve_artifact(args)
    except (OSError, RuntimeError, ValueError) as error:
        raise SystemExit(f"cannot resolve Pixel factory image: {error}") from error

    factory_zip = args.factory_zip or ROOT / "data" / "raw" / "pixel" / artifact.filename
    if args.offline:
        factory_zip = args.factory_zip
    else:
        ensure_factory_zip(artifact.url, artifact.sha256, factory_zip)

    build_slug = _slug(artifact.build_id)
    work_dir = args.work_dir or ROOT / "data" / "tmp" / "pixel" / args.device / build_slug
    output = args.output or (
        ROOT
        / "data"
        / (
            f"carrier-bundles-{_slug(artifact.device_name)}-"
            f"{_slug(args.device)}-{build_slug}.sqlite3"
        )
    )
    output = output.resolve()
    building = output.with_name(f"{output.name}.building")
    if output.exists():
        raise SystemExit(f"output database already exists: {output}")
    if building.exists():
        raise SystemExit(
            f"unfinished build database already exists: {building}; inspect or remove it first"
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    release_id = f"pixel-{_slug(args.device)}-{build_slug}"
    _run_tool(
        str(ROOT / "tools" / "init_db.py"),
        str(building),
        "--release-id",
        release_id,
        "--generator-version",
        f"android/pixel {PARSER_VERSION}",
    )
    _label_release(building, artifact)

    try:
        firmware = extract_pixel_factory(factory_zip, work_dir)
        stats = import_pixel_catalog(
            building,
            firmware,
            device=args.device,
            device_name=artifact.device_name,
            os_version=artifact.os_version,
            build_id=artifact.build_id,
            source_uri=artifact.url,
            factory_sha256=artifact.sha256,
            include_standard_derived=not args.no_standard_derived,
        )
        seal_arguments = [str(ROOT / "tools" / "seal_db.py"), str(building)]
        if args.skip_icon_sync:
            seal_arguments.append("--skip-icon-sync")
        if args.no_icon_repo_update:
            seal_arguments.append("--no-icon-repo-update")
        _run_tool(*seal_arguments)
        building.replace(output)
    except Exception:
        print(f"build failed; unsealed diagnostic database retained at {building}", file=sys.stderr)
        raise

    print(
        json.dumps(
            {
                "database": str(output),
                "device": artifact.device,
                "device_name": artifact.device_name,
                "build_id": artifact.build_id,
                "os_version": artifact.os_version,
                "factory_sha256": artifact.sha256,
                "baseband_version": firmware.baseband_version,
                "stats": asdict(stats),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
