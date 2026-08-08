#!/usr/bin/env python3
"""List or download Apple-published IPCC carrier bundle archives."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import asdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ios.ipcc import (
    IPCC_INDEX_URL,
    download_ipcc,
    extract_ipcc,
    fetch_ipcc_index,
    select_artifacts,
    write_manifest,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--index-url", default=IPCC_INDEX_URL)
    parser.add_argument(
        "--product",
        choices=("iphone", "ipad", "watch", "ipod", "all"),
        default="iphone",
    )
    parser.add_argument(
        "--bundle",
        action="append",
        default=[],
        help="bundle name, carrier, or MCCMNC substring; repeatable",
    )
    parser.add_argument("--all-versions", action="store_true")
    parser.add_argument("--download", action="store_true")
    parser.add_argument(
        "--download-all",
        action="store_true",
        help="allow downloading every selected bundle without --bundle filters",
    )
    parser.add_argument(
        "--extract",
        action="store_true",
        help="download and normalize Payload/*.bundle trees",
    )
    parser.add_argument(
        "--output-dir", type=Path, default=ROOT / "data" / "raw" / "ipcc"
    )
    parser.add_argument(
        "--extract-dir",
        type=Path,
        default=ROOT / "data" / "tmp" / "ipcc" / "bundles",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=ROOT / "data" / "tmp" / "ipcc" / "manifest.json",
    )
    args = parser.parse_args()

    if (args.download or args.extract) and not args.bundle and not args.download_all:
        parser.error("downloading without --bundle requires explicit --download-all")

    index_data, discovered = fetch_ipcc_index(args.index_url)
    selected = select_artifacts(
        discovered,
        product=args.product,
        queries=args.bundle,
        latest_only=not args.all_versions,
    )
    if args.extract:
        args.download = True
    completed = []
    for artifact in selected:
        if args.download:
            short_id = hashlib.sha256(artifact.url.encode()).hexdigest()[:12]
            artifact = download_ipcc(
                artifact, args.output_dir / f"{short_id}-{artifact.filename}"
            )
        if args.extract:
            artifact = extract_ipcc(artifact, args.extract_dir)
        completed.append(artifact)

    index_sha256 = hashlib.sha256(index_data).hexdigest()
    write_manifest(
        args.manifest,
        index_url=args.index_url,
        index_sha256=index_sha256,
        artifacts=completed,
    )
    print(
        json.dumps(
            {
                "index_url": args.index_url,
                "index_sha256": index_sha256,
                "discovered": len(discovered),
                "selected": len(completed),
                "downloaded": sum(item.archive_path is not None for item in completed),
                "extracted": sum(bool(item.extracted_bundles) for item in completed),
                "manifest": str(args.manifest),
                "artifacts": [asdict(item) for item in completed],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
