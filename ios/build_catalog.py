#!/usr/bin/env python3
"""Build a sealed catalog from a discovered or pinned Apple IPSW."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ios import PARSER_VERSION  # noqa: E402
from ios.catalog import import_ios_catalog  # noqa: E402
from ios.firmware import (  # noqa: E402
    decrypt_aea,
    ensure_apfs_fuse,
    ensure_ipsw_tool,
    export_carrier_bundles,
    extract_file_system_aea,
    extract_outer_files,
    extract_remote_outer_files,
    mounted_apfs,
)
from ios.sources import (  # noqa: E402
    IPHONE_16_PRO_26_6,
    PRODUCT_DEVICE_CLASSES,
    catalog_filename,
    inspect_build_manifest,
    resolve_ipsw_artifact,
)


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--bundle-root",
        type=Path,
        help="existing exported root; skips IPSW, AEA and APFS extraction",
    )
    parser.add_argument("--baseband", type=Path, help="optional extracted .bbfw path")
    parser.add_argument("--ipsw", type=Path, help="optional complete local IPSW")
    parser.add_argument(
        "--product-type",
        help="Apple product type to discover (for example iPhone17,2 for 16 Pro Max)",
    )
    parser.add_argument(
        "--version",
        default="latest",
        help="exact iOS version/build or latest when discovering an IPSW",
    )
    parser.add_argument(
        "--device-class",
        help="carrier-bundle device class; defaults to the known product mapping",
    )
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--workers", type=int, default=8)
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


def _run_tool(*arguments: str) -> None:
    subprocess.run([sys.executable, *arguments], cwd=ROOT, check=True)


def _find_outer_file(outer: Path, name: str) -> Path | None:
    matches = list(outer.rglob(name))
    if len(matches) > 1:
        raise ValueError(f"multiple extracted {name} files were found")
    return matches[0] if matches else None


def _acquire_bundles(
    args: argparse.Namespace, work_dir: Path, artifact
) -> tuple[Path, Path | None]:
    if args.bundle_root is not None:
        if not args.bundle_root.is_dir():
            raise ValueError(f"bundle root does not exist: {args.bundle_root}")
        return args.bundle_root.resolve(), args.baseband.resolve() if args.baseband else None

    tool_root = ROOT / "data" / "tmp" / "tools"
    ipsw_tool = ensure_ipsw_tool(tool_root)
    outer = work_dir / "outer"
    manifest = _find_outer_file(outer, "BuildManifest.plist")
    if manifest is None:
        if args.ipsw is not None:
            if not args.ipsw.is_file():
                raise ValueError(f"local IPSW does not exist: {args.ipsw}")
            resolved_artifact = extract_outer_files(args.ipsw, artifact, outer)
        else:
            resolved_artifact = extract_remote_outer_files(ipsw_tool, artifact, outer)
    else:
        resolved_artifact = inspect_build_manifest(manifest, artifact)

    baseband = args.baseband
    if baseband is None and resolved_artifact.baseband_path:
        baseband = _find_outer_file(outer, Path(resolved_artifact.baseband_path).name)

    archive = extract_file_system_aea(
        ipsw_tool,
        resolved_artifact,
        work_dir / "filesystem",
        args.ipsw,
        workers=args.workers,
    )
    image = decrypt_aea(ipsw_tool, archive, work_dir / "rootfs")
    apfs_fuse = ensure_apfs_fuse(tool_root)
    exported = work_dir / "carrier-extract"
    with mounted_apfs(apfs_fuse, image, work_dir / "mount") as mountpoint:
        export_carrier_bundles(mountpoint, exported)
    return exported, baseband


def main() -> None:
    args = _parse_args()
    if args.bundle_root is not None and args.product_type is None:
        # Preserve the offline fixture/legacy command's pinned metadata.  A
        # downloaded build always uses explicit discovery below.
        artifact = IPHONE_16_PRO_26_6
    else:
        try:
            artifact = resolve_ipsw_artifact(
                args.product_type or "iPhone17,2", args.version
            )
        except (OSError, RuntimeError, ValueError) as error:
            raise SystemExit(f"cannot resolve Apple IPSW metadata: {error}") from error
    work_dir = args.work_dir or (
        ROOT / "data" / "tmp" / "ios" / f"{_slug(artifact.product_type)}-{_slug(artifact.build_id)}"
    )
    output = args.output or (
        ROOT / "data" / catalog_filename(artifact)
    )
    output = output.resolve()
    building = output.with_name(f"{output.name}.building")
    if output.exists():
        raise SystemExit(f"output database already exists: {output}")
    if building.exists():
        raise SystemExit(
            f"unfinished build database already exists: {building}; inspect or remove it first"
        )

    try:
        extracted_root, baseband = _acquire_bundles(args, work_dir.resolve(), artifact)
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as error:
        raise SystemExit(f"cannot extract iPhone Carrier Bundles: {error}") from error

    output.parent.mkdir(parents=True, exist_ok=True)
    release_id = f"catalog-{(artifact.sha256 or _slug(artifact.build_id))[:16]}"
    _run_tool(
        str(ROOT / "tools" / "init_db.py"),
        str(building),
        "--release-id",
        release_id,
        "--generator-name",
        "carrier-bundles",
        "--generator-version",
        f"ios {PARSER_VERSION}",
    )
    try:
        stats = import_ios_catalog(
            building,
            extracted_root,
            artifact=artifact,
            device_class=args.device_class
            or PRODUCT_DEVICE_CLASSES.get(artifact.product_type, "UNKNOWN"),
            baseband_path=baseband,
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
        print(f"build failed; unsealed database retained at {building}", file=sys.stderr)
        raise

    print(
        json.dumps(
            {
                "database": str(output),
                "device": artifact.product_type,
                "device_class": args.device_class
                or PRODUCT_DEVICE_CLASSES.get(artifact.product_type, "UNKNOWN"),
                "device_name": artifact.device_name,
                "os_version": artifact.os_version,
                "build_id": artifact.build_id,
                "baseband_version": artifact.baseband_version,
                "ipsw_sha256": artifact.sha256,
                "stats": asdict(stats),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
