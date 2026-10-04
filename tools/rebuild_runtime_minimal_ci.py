#!/usr/bin/env python3
"""Rebuild the four-source runtime set on Actions, without publishing anything."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.build_variants import build_variants, sha256
from tools.verify_catalog import verify_catalog

REUSED_NAMES = (
    "carrier-bundles-ios-ipcc.sqlite3",
    "carrier-bundles-pixel-mustang.sqlite3",
)
XIAOMI_NAME = "carrier-bundles-xiaomi15ultra-xuanyuan-baseband.sqlite3"


def pinned_full_sources(directory: Path) -> list[Path]:
    manifest = json.loads((directory / "catalog-variants.json").read_text(encoding="utf-8"))
    entries = manifest["catalogs"]
    sources = []
    for entry in entries:
        full = entry["variants"]["full"]
        name = full["database"]
        if Path(name).name != name or "\\" in name:
            raise ValueError("unsafe full database path")
        if name not in REUSED_NAMES and not (name.startswith("carrier-bundles-iphone") and name.endswith(".sqlite3")):
            continue
        if "no-icons" in name or "minimal" in name:
            raise ValueError("a derived variant cannot be used as a full source")
        path = directory / name
        if path.is_symlink() or sha256(path) != full["sha256"] or path.stat().st_size != full["bytes"]:
            raise ValueError("full source does not match pinned manifest: " + name)
        if entry["source"]["sha256"] != full["sha256"]:
            raise ValueError("full source was not byte-identical to the original input")
        # Artifact ZIPs do not preserve Unix read-only mode; content was checked
        # above before restoring the sealed-file permission contract.
        path.chmod(0o444)
        verify_catalog(path)
        sources.append(path)
    names = [path.name for path in sources]
    if len(names) != 3 or len(set(names)) != 3 or not set(REUSED_NAMES).issubset(names):
        raise ValueError("expected exactly one IPCC, Pixel and IPSW full source")
    return sources


def verify_set(manifest: dict, directory: Path) -> list[dict]:
    if len(manifest["catalogs"]) != 4 or not manifest["policy"]["runtime_minimal"]["enabled"]:
        raise ValueError("four runtime-minimal catalogs are required")
    rows = []
    names = set()
    for entry in manifest["catalogs"]:
        variants = entry["variants"]
        if set(variants) != {"full", "no-icons", "minimal-no-icons"}:
            raise ValueError("incomplete variant set")
        for name, value in variants.items():
            path = directory / value["database"]
            if path.name in names or path.is_symlink() or sha256(path) != value["sha256"]:
                raise ValueError("duplicate or damaged output")
            names.add(path.name)
            checked = verify_catalog(path)
            if checked["schema_version"] != 7 or checked["config_contract"] != "carrier-bundles-ims-v1":
                raise ValueError("catalog contract changed")
        full, noicons, minimal = (variants[name] for name in ("full", "no-icons", "minimal-no-icons"))
        if minimal["counts"]["field_evidence"] != 0:
            raise ValueError("runtime audit evidence was not removed")
        if noicons["bytes"] <= minimal["bytes"]:
            raise ValueError("runtime minimal did not reduce this source")
        if full["database"] == XIAOMI_NAME:
            for value in variants.values():
                if value["static_client_readiness"]["vowifi_ready_profiles"] != 380:
                    raise ValueError("Xiaomi full-OTA WFC recovery regressed")
        rows.append({"source": full["database"], "no_icons_bytes": noicons["bytes"],
                     "minimal_bytes": minimal["bytes"],
                     "reduction_percent": round(100 * (1 - minimal["bytes"] / noicons["bytes"]), 4)})
    if len(names) != 12 or names != {p.name for p in directory.glob("*.sqlite3")}:
        raise ValueError("expected exactly twelve output databases")
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--xiaomi", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    if os.environ.get("GITHUB_ACTIONS") != "true":
        parser.error("catalog builds for this maintenance are Actions-only")
    sources = pinned_full_sources(args.source_dir)
    if args.xiaomi.name != XIAOMI_NAME:
        parser.error("unexpected Xiaomi output name")
    xiaomi = verify_catalog(args.xiaomi)
    if xiaomi["static_client_readiness"]["vowifi_ready_profiles"] != 380:
        raise ValueError("expected the fixed full-OTA Xiaomi source, not the old zero-WFC catalog")
    manifest = build_variants(sources + [args.xiaomi], args.output_dir,
                             simulation_report=ROOT / "simulation_pruning/verified-evidence.json",
                             runtime_minimal=True, progress=print)
    rows = verify_set(manifest, args.output_dir)
    evidence = {"repository": os.environ["GITHUB_REPOSITORY"], "commit": os.environ["GITHUB_SHA"],
                "run_id": os.environ["GITHUB_RUN_ID"], "reused_source_run": "37095019351",
                "xiaomi_rebuilt_from_full_ota": True, "publication": False,
                "live_network_verified": False, "sizes": rows}
    evidence_path = args.output_dir / "actions-verification.json"
    evidence_path.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    with (args.output_dir / "SHA256SUMS").open("a", encoding="ascii") as sums:
        sums.write(f"{sha256(evidence_path)}  {evidence_path.name}\n")
    print(json.dumps(evidence, indent=2))


if __name__ == "__main__":
    main()
