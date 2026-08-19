#!/usr/bin/env python3
"""Build a sealed schema-v7 Xiaomi carrier/baseband catalog."""

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
from android.xiaomi.carrier_config import import_xiaomi_carrier_config_catalog  # noqa: E402
from android.xiaomi.catalog import import_xiaomi_baseband_catalog  # noqa: E402
from android.xiaomi.firmware import (  # noqa: E402
    digest_file,
    ensure_rom,
    extract_xiaomi_carrier_configs,
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
    parser.add_argument("--rom-path", type=Path, help="local full OTA .zip or fastboot .tgz/.tar path")
    parser.add_argument("--device-name", default=XIAOMI_15_ULTRA_GLOBAL_OS3_0_301_0.device_name)
    parser.add_argument("--device", default=XIAOMI_15_ULTRA_GLOBAL_OS3_0_301_0.codename)
    parser.add_argument("--region", default=XIAOMI_15_ULTRA_GLOBAL_OS3_0_301_0.region)
    parser.add_argument("--android-version", default=XIAOMI_15_ULTRA_GLOBAL_OS3_0_301_0.android_version)
    parser.add_argument("--build-id", default=XIAOMI_15_ULTRA_GLOBAL_OS3_0_301_0.build_id)
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--skip-icon-sync", action="store_true")
    parser.add_argument("--no-icon-repo-update", action="store_true")
    parser.add_argument(
        "--allow-empty-profiles",
        action="store_true",
        help="allow inventory-only output when the ROM has no extractable carrier profiles",
    )
    parser.add_argument(
        "--no-standard-derived",
        action="store_true",
        help="omit 3GPP-derived IMS domain and identity fallback templates",
    )
    return parser.parse_args()


def _run_tool(*arguments: str) -> None:
    subprocess.run([sys.executable, *arguments], cwd=ROOT, check=True)


def _package_kind(*, rom_path: Path | None, rom_url: str) -> str:
    value = str(rom_path or rom_url).casefold()
    if value.endswith(".zip"):
        if Path(value).name.startswith("fw_"):
            return "firmware_zip"
        return "full_ota_zip"
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

    output.parent.mkdir(parents=True, exist_ok=True)
    rom_sha256 = digest_file(rom_path)
    _run_tool(
        str(ROOT / "tools" / "init_db.py"),
        str(building),
        "--release-id",
        f"catalog-xiaomi-{rom_sha256[:16]}",
        "--generator-name",
        "carrier-bundles",
        "--generator-version",
        f"android/xiaomi {PARSER_VERSION}",
    )
    try:
        carrier_config = extract_xiaomi_carrier_configs(rom_path, work_dir / "carrier-config")
        carrier_stats = import_xiaomi_carrier_config_catalog(
            building,
            carrier_config,
            artifact=artifact,
            include_standard_derived=not args.no_standard_derived,
        )
        if carrier_stats.profiles_imported == 0 and not args.allow_empty_profiles:
            raise RuntimeError(
                "Xiaomi ROM produced zero carrier profiles; use a full OTA/fastboot ROM "
                "with CarrierConfig/APN files, not a firmware-only package"
            )
        firmware = extract_xiaomi_modem_artifacts(rom_path, work_dir / "baseband")
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
                "source_kind": "xiaomi_carrier_baseband_catalog",
                "device": artifact.codename,
                "device_name": artifact.device_name,
                "region": artifact.region,
                "android_version": artifact.android_version,
                "build_id": artifact.build_id,
                "rom_url": artifact.url,
                "rom_sha256": firmware.rom_sha256,
                "carrier_config_files": [
                    str(path) for path in carrier_config.config_files
                ],
                "modem_artifacts": [
                    {
                        "archive_member": item.archive_member,
                        "extracted_path": str(item.extracted_path),
                        "sha256": item.sha256,
                        "size": item.size,
                    }
                    for item in firmware.modem_artifacts
                ],
                "stats": {
                    "carrier_config": asdict(carrier_stats),
                    "baseband": asdict(stats),
                },
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
