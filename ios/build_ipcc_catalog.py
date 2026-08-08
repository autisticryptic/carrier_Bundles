#!/usr/bin/env python3
"""Build a sealed schema-v7 catalog from Apple-published iPhone IPCC files."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ios import PARSER_VERSION  # noqa: E402
from ios.catalog import IOSBundleSource, import_ios_catalog  # noqa: E402
from ios.ipcc import (  # noqa: E402
    IPCC_INDEX_URL,
    IPCCArtifact,
    download_ipcc,
    extract_ipcc,
    fetch_ipcc_index,
    select_artifacts,
    write_manifest,
)
from ios.sources import IPSWArtifact  # noqa: E402


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index-url", default=IPCC_INDEX_URL)
    parser.add_argument(
        "--bundle",
        action="append",
        default=[],
        help="optional carrier or MCCMNC filter; repeatable",
    )
    parser.add_argument("--workers", type=int, default=12)
    parser.add_argument("--work-dir", type=Path, default=ROOT / "data" / "tmp" / "ipcc-catalog")
    parser.add_argument("--output", type=Path, default=ROOT / "data" / "carrier-bundles-ios-ipcc.sqlite3")
    parser.add_argument("--skip-icon-sync", action="store_true")
    parser.add_argument("--no-icon-repo-update", action="store_true")
    parser.add_argument("--no-standard-derived", action="store_true")
    parser.add_argument(
        "--strict-downloads",
        action="store_true",
        help="fail the build if any selected Apple IPCC cannot be downloaded or extracted",
    )
    return parser.parse_args()


def _run_tool(*arguments: str) -> None:
    subprocess.run([sys.executable, *arguments], cwd=ROOT, check=True)


def _archive_path(raw_dir: Path, artifact: IPCCArtifact) -> Path:
    short_id = hashlib.sha256(artifact.url.encode()).hexdigest()[:12]
    return raw_dir / f"{short_id}-{artifact.filename}"


def _acquire_ipccs(
    selected: list[IPCCArtifact],
    *,
    raw_dir: Path,
    extract_dir: Path,
    workers: int,
) -> tuple[list[IPCCArtifact], list[dict[str, str]]]:
    downloaded: list[IPCCArtifact] = []
    errors: list[dict[str, str]] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {
            executor.submit(download_ipcc, item, _archive_path(raw_dir, item)): item
            for item in selected
        }
        for completed_count, future in enumerate(as_completed(futures), start=1):
            source = futures[future]
            try:
                downloaded.append(future.result())
            except Exception as error:  # keep other public bundles buildable
                errors.append(
                    {
                        "bundle_id": source.bundle_id,
                        "url": source.url,
                        "stage": "download",
                        "error": str(error),
                    }
                )
            if completed_count % 50 == 0 or completed_count == len(selected):
                print(
                    f"IPCC downloads: {completed_count}/{len(selected)} "
                    f"({len(errors)} failed)",
                    file=sys.stderr,
                )

    extracted: list[IPCCArtifact] = []
    for item in sorted(downloaded, key=lambda artifact: artifact.bundle_id.casefold()):
        try:
            extracted.append(extract_ipcc(item, extract_dir))
        except Exception as error:
            errors.append(
                {
                    "bundle_id": item.bundle_id,
                    "url": item.url,
                    "stage": "extract",
                    "error": str(error),
                }
            )
    return extracted, errors


def _bundle_sources(artifacts: list[IPCCArtifact]) -> dict[str, IOSBundleSource]:
    result: dict[str, IOSBundleSource] = {}
    for artifact in artifacts:
        revision_parts = [
            f"bundle={artifact.bundle_id}",
            f"version={artifact.version or artifact.build_version or 'unknown'}",
        ]
        if artifact.apple_digest_algorithm and artifact.apple_digest_hex:
            revision_parts.append(
                f"apple-{artifact.apple_digest_algorithm}={artifact.apple_digest_hex}"
            )
        source = IOSBundleSource(
            source_uri=artifact.url,
            artifact_sha256=artifact.sha256,
            source_revision=";".join(revision_parts),
        )
        for relative in artifact.extracted_bundles:
            if "Carrier Bundles/iPhone/" in relative:
                result[Path(relative).name] = source
    return result


def main() -> None:
    args = _parse_args()
    if args.workers < 1 or args.workers > 64:
        raise SystemExit("--workers must be between 1 and 64")
    output = args.output.resolve()
    building = output.with_name(f"{output.name}.building")
    if output.exists() or building.exists():
        raise SystemExit(f"output or unfinished database already exists: {output}")

    index_data, discovered = fetch_ipcc_index(args.index_url)
    index_sha256 = hashlib.sha256(index_data).hexdigest()
    selected = select_artifacts(
        discovered,
        product="iphone",
        queries=args.bundle,
        latest_only=True,
    )
    if not selected:
        raise SystemExit("Apple index selection produced no iPhone IPCC artifacts")

    work_dir = args.work_dir.resolve()
    extracted, errors = _acquire_ipccs(
        selected,
        raw_dir=work_dir / "raw",
        extract_dir=work_dir / "bundles",
        workers=args.workers,
    )
    manifest = work_dir / "manifest.json"
    write_manifest(
        manifest,
        index_url=args.index_url,
        index_sha256=index_sha256,
        artifacts=extracted,
        errors=errors,
    )
    sources = _bundle_sources(extracted)
    if not sources:
        raise SystemExit("no downloaded IPCC contained an iPhone Carrier Bundle")
    if args.strict_downloads and errors:
        raise SystemExit(f"{len(errors)} IPCC artifacts failed; see {manifest}")

    output.parent.mkdir(parents=True, exist_ok=True)
    _run_tool(
        str(ROOT / "tools" / "init_db.py"),
        str(building),
        "--release-id",
        f"catalog-ipcc-{index_sha256[:16]}",
        "--generator-name",
        "carrier-bundles",
        "--generator-version",
        f"ios/ipcc {PARSER_VERSION}",
    )
    artifact = IPSWArtifact(
        device_name="Apple IPCC index",
        product_type="IPCC",
        os_version="not-applicable",
        build_id=index_sha256[:16],
        url=args.index_url,
        sha256=index_sha256,
        size=len(index_data),
    )
    try:
        stats = import_ios_catalog(
            building,
            work_dir / "bundles",
            artifact=artifact,
            device_class="UNKNOWN",
            include_standard_derived=not args.no_standard_derived,
            include_device_overrides=False,
            bundle_sources=sources,
        )
        seal_arguments = [str(ROOT / "tools" / "seal_db.py"), str(building)]
        if args.skip_icon_sync:
            seal_arguments.append("--skip-icon-sync")
        if args.no_icon_repo_update:
            seal_arguments.append("--no-icon-repo-update")
        _run_tool(*seal_arguments)
        building.replace(output)
    except Exception:
        print(f"IPCC build failed; unsealed database retained at {building}", file=sys.stderr)
        raise

    print(
        json.dumps(
            {
                "database": str(output),
                "source_kind": "apple_ipcc",
                "index_url": args.index_url,
                "index_sha256": index_sha256,
                "discovered": len(discovered),
                "selected": len(selected),
                "downloaded_and_extracted": len(extracted),
                "carrier_bundle_sources": len(sources),
                "errors": errors,
                "manifest": str(manifest),
                "stats": asdict(stats),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
