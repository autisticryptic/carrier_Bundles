#!/usr/bin/env python3
"""Build a sealed schema-v7 Xiaomi baseband inventory catalog."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from android.xiaomi import PARSER_VERSION  # noqa: E402
from android.xiaomi.catalog import import_xiaomi_baseband_catalog  # noqa: E402
from android.xiaomi.firmware import (  # noqa: E402
    ensure_rom,
    extract_xiaomi_modem_artifacts,
)
from android.xiaomi.sources import (  # noqa: E402
    XIAOMI_15_ULTRA_GLOBAL_OS3_0_301_0,
    XiaomiFastbootArtifact,
)


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rom-url", default=XIAOMI_15_ULTRA_GLOBAL_OS3_0_301_0.url)
    parser.add_argument("--rom-md5", default=XIAOMI_15_ULTRA_GLOBAL_OS3_0_301_0.md5)
    parser.add_argument("--rom-path", type=Path, help="local firmware .zip or fastboot .tgz/.tar path")
    parser.add_argument("--device-name", default=XIAOMI_15_ULTRA_GLOBAL_OS3_0_301_0.device_name)
    parser.add_argument("--device", default=XIAOMI_15_ULTRA_GLOBAL_OS3_0_301_0.codename)
    parser.add_argument("--region", default=XIAOMI_15_ULTRA_GLOBAL_OS3_0_301_0.region)
    parser.add_argument("--android-version", default=XIAOMI_15_ULTRA_GLOBAL_OS3_0_301_0.android_version)
    parser.add_argument("--build-id", default=XIAOMI_15_ULTRA_GLOBAL_OS3_0_301_0.build_id)
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--skip-icon-sync", action="store_true")
    parser.add_argument("--no-icon-repo-update", action="store_true")
    return parser.parse_args()


def _run_tool(*arguments: str) -> None:
    subprocess.run([sys.executable, *arguments], cwd=ROOT, check=True)


def _package_kind(*, rom_path: Path | None, rom_url: str) -> str:
    value = str(rom_path or rom_url).casefold()
    if value.endswith(".zip"):
        return "firmware_zip"
    return "fastboot_archive"


def main() -> None:
    args = _parse_args()
    artifact = XiaomiFastbootArtifact(
        device_name=args.device_name,
        codename=args.device,
        region=args.region,
        android_version=args.android_version,
        build_id=args.build_id,
        url=args.rom_url,
        md5=args.rom_md5 or None,
        package_kind=_package_kind(rom_path=args.rom_path, rom_url=args.rom_url),
    )
    rom_path = args.rom_path or ROOT / "data" / "raw" / "xiaomi" / artifact.filename
    if args.rom_path is not None:
        if not args.rom_path.is_file():
            raise SystemExit(f"local Xiaomi ROM does not exist: {args.rom_path}")
    else:
        try:
            ensure_rom(artifact.url, rom_path, expected_md5=artifact.md5)
        except (OSError, RuntimeError) as error:
            raise SystemExit(f"cannot acquire Xiaomi ROM: {error}") from error

    build_slug = _slug(artifact.build_id)
    work_dir = args.work_dir or ROOT / "data" / "tmp" / "xiaomi" / artifact.codename / build_slug
    output = args.output or (
        ROOT
        / "data"
        / (
            f"carrier-bundles-{_slug(artifact.device_name)}-"
            f"{_slug(artifact.codename)}-{build_slug}-baseband.sqlite3"
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

    try:
        firmware = extract_xiaomi_modem_artifacts(rom_path, work_dir / "baseband")
    except (OSError, RuntimeError) as error:
        raise SystemExit(f"cannot extract Xiaomi modem artifacts: {error}") from error
    output.parent.mkdir(parents=True, exist_ok=True)
    _run_tool(
        str(ROOT / "tools" / "init_db.py"),
        str(building),
        "--release-id",
        f"catalog-xiaomi-{firmware.rom_sha256[:16]}",
        "--generator-name",
        "carrier-bundles",
        "--generator-version",
        f"android/xiaomi {PARSER_VERSION}",
    )
    try:
        stats = import_xiaomi_baseband_catalog(building, firmware, artifact=artifact)
        seal_arguments = [str(ROOT / "tools" / "seal_db.py"), str(building)]
        if args.skip_icon_sync:
            seal_arguments.append("--skip-icon-sync")
        if args.no_icon_repo_update:
            seal_arguments.append("--no-icon-repo-update")
        _run_tool(*seal_arguments)
        building.replace(output)
    except Exception:
        print(f"Xiaomi build failed; unsealed diagnostic database retained at {building}", file=sys.stderr)
        raise

    print(
        json.dumps(
            {
                "database": str(output),
                "source_kind": "xiaomi_firmware_baseband_inventory",
                "device": artifact.codename,
                "device_name": artifact.device_name,
                "region": artifact.region,
                "android_version": artifact.android_version,
                "build_id": artifact.build_id,
                "rom_url": artifact.url,
                "rom_sha256": firmware.rom_sha256,
                "modem_artifacts": [
                    {
                        "archive_member": item.archive_member,
                        "extracted_path": str(item.extracted_path),
                        "sha256": item.sha256,
                        "size": item.size,
                    }
                    for item in firmware.modem_artifacts
                ],
                "stats": asdict(stats),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
