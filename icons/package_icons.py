#!/usr/bin/env python3
"""Resolve operator icons with NekokoLPA2 rules and embed them in a catalog."""

from __future__ import annotations

import argparse
import hashlib
import re
import sqlite3
import struct
import subprocess
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
NEKOKOLPA2_URL = "https://github.com/iebb/NekokoLPA2.git"
DEFAULT_NEKOKOLPA2_DIR = ROOT / "icons" / "vendor" / "NekokoLPA2"
NEKOKOLPA2_SPARSE_PATHS = (
    "/lib/services/operator_icon_source.dart",
    "/LICENSE",
)
PACKAGER_NAME = "icons/package_icons.py"
PACKAGER_VERSION = "1"
ASSET_SOURCE_NAME = "operator-icons resolved with NekokoLPA2"
USER_AGENT = "carrier-bundles-icon-packager/1"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


@dataclass(frozen=True)
class IconRef:
    scope: str
    name: str

    @property
    def asset_id(self) -> str:
        return f"operator-icons:{self.scope}/{self.name}"


@dataclass(frozen=True)
class MatchTarget:
    owner_kind: str
    owner_id: str
    plmn: str
    mcc: str
    mnc: str
    gid1: str | None = None
    gid2: str | None = None
    profile_name: str | None = None
    provider_name: str | None = None


@dataclass(frozen=True)
class DownloadedAsset:
    reference: IconRef
    remote_url: str
    content: bytes
    sha256: str
    width: int
    height: int


@dataclass(frozen=True)
class PackageResult:
    carriers_linked: int
    profiles_linked: int
    assets_embedded: int
    unresolved_targets: int
    nekokolpa2_revision: str


def _run_git(arguments: list[str], cwd: Path | None = None) -> str:
    result = subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
        raise RuntimeError(f"git {' '.join(arguments)} failed: {detail}")
    return result.stdout.strip()


def _normalized_git_url(value: str) -> str:
    return value.removesuffix("/").removesuffix(".git")


def refresh_nekokolpa2(repository: Path, update: bool = True) -> tuple[str, str]:
    """Sparse-clone/update NekokoLPA2 and return its commit plus icon base URL."""
    if not repository.exists():
        if not update:
            raise RuntimeError(f"NekokoLPA2 repository is missing: {repository}")
        repository.parent.mkdir(parents=True, exist_ok=True)
        _run_git(
            [
                "clone",
                "--depth",
                "1",
                "--filter=blob:none",
                "--sparse",
                NEKOKOLPA2_URL,
                str(repository),
            ]
        )

    if not (repository / ".git").is_dir():
        raise RuntimeError(f"not a NekokoLPA2 git checkout: {repository}")

    origin = _run_git(["remote", "get-url", "origin"], cwd=repository)
    if _normalized_git_url(origin) != _normalized_git_url(NEKOKOLPA2_URL):
        raise RuntimeError(f"unexpected NekokoLPA2 origin: {origin}")

    _run_git(
        ["sparse-checkout", "set", "--no-cone", *NEKOKOLPA2_SPARSE_PATHS],
        cwd=repository,
    )
    if update:
        _run_git(["pull", "--ff-only"], cwd=repository)

    revision = _run_git(["rev-parse", "HEAD"], cwd=repository)
    source = repository / "lib" / "services" / "operator_icon_source.dart"
    match = re.search(
        r"static\s+const\s+String\s+baseUrl\s*=\s*['\"]([^'\"]+)['\"]",
        source.read_text(encoding="utf-8"),
    )
    if match is None:
        raise RuntimeError(f"cannot find operator icon baseUrl in {source}")
    return revision, match.group(1).rstrip("/")


def _url(base_url: str, *parts: str) -> str:
    encoded = "/".join(urllib.parse.quote(part, safe="") for part in parts)
    return f"{base_url.rstrip('/')}/{encoded}"


def _fetch(url: str, timeout: float) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def _load_catalog(
    base_url: str, mcc: str, timeout: float
) -> tuple[dict[str, Any] | None, bytes | None]:
    url = _url(base_url, "catalog", f"{mcc}.toml")
    try:
        source = _fetch(url, timeout)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None, None
        raise
    try:
        return tomllib.loads(source.decode("utf-8")), source
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise RuntimeError(f"invalid operator icon catalog {url}: {error}") from error


def _normalized_optional(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip().lower()
    return normalized or None


def _matches_name(candidate: str | None, expected: Any) -> bool:
    actual = _normalized_optional(candidate)
    wanted = _normalized_optional(expected)
    if actual is None or wanted is None:
        return False
    return actual == wanted or actual in wanted or wanted in actual


def _matches_exact_name(candidate: str | None, expected: Any) -> bool:
    actual = _normalized_optional(candidate)
    wanted = _normalized_optional(expected)
    return actual is not None and actual == wanted


def _matches_gid(entry: dict[str, Any], target: MatchTarget) -> bool:
    expected1 = _normalized_optional(entry.get("gid1"))
    expected2 = _normalized_optional(entry.get("gid2"))
    actual1 = _normalized_optional(target.gid1)
    actual2 = _normalized_optional(target.gid2)
    if expected1 is None and expected2 is None:
        return False
    return (expected1 is None or expected1 == actual1) and (
        expected2 is None or expected2 == actual2
    )


def _matches_gid_name(entry: dict[str, Any], target: MatchTarget) -> bool:
    return any(
        _matches_name(target.profile_name, name)
        for name in entry.get("profile_names", [])
    ) or any(
        _matches_name(target.provider_name, name)
        for name in entry.get("profile_provider_names", [])
    )


def _matches_exact_gid_name(entry: dict[str, Any], target: MatchTarget) -> bool:
    return any(
        _matches_exact_name(target.profile_name, name)
        for name in entry.get("profile_names", [])
    ) or any(
        _matches_exact_name(target.provider_name, name)
        for name in entry.get("profile_provider_names", [])
    )


def resolve_icon(catalog: dict[str, Any], target: MatchTarget) -> IconRef | None:
    """Resolve an icon using NekokoLPA2-compatible MNC/GID/name ranking."""
    mnc_candidates = {
        target.mnc,
        target.mnc.zfill(2),
        target.mnc.zfill(3),
    }
    ranked: list[tuple[int, int, dict[str, Any], dict[str, Any] | None]] = []
    for position, operator in enumerate(catalog.get("operators", [])):
        if not isinstance(operator, dict):
            continue
        if operator.get("plmn") != target.plmn and str(
            operator.get("mnc", "")
        ) not in mnc_candidates:
            continue
        if not operator.get("icon") or not operator.get("icon_scope"):
            continue

        score = 100
        if any(
            _matches_name(candidate, expected)
            for candidate in (target.profile_name, target.provider_name)
            for expected in (operator.get("operator"), operator.get("brand"))
        ):
            score += 20

        best_gid: dict[str, Any] | None = None
        best_gid_score = 0
        for gid in operator.get("gids", []):
            if not isinstance(gid, dict):
                continue
            gid_score = (30 if _matches_gid(gid, target) else 0) + (
                20 if _matches_gid_name(gid, target) else 0
            )
            if gid_score > best_gid_score:
                best_gid = gid
                best_gid_score = gid_score
        score += best_gid_score
        ranked.append((score, -position, operator, best_gid))

    if not ranked:
        return None
    _, _, operator, gid = max(ranked, key=lambda item: (item[0], item[1]))
    use_gid_icon = gid is not None and (
        _matches_gid(gid, target) or _matches_exact_gid_name(gid, target)
    )
    icon = (gid or {}).get("icon") if use_gid_icon else None
    scope = (gid or {}).get("icon_scope") if use_gid_icon else None
    icon = icon or operator.get("icon")
    scope = scope or operator.get("icon_scope")
    if not icon or not scope:
        return None
    return IconRef(scope=str(scope), name=str(icon))


def _png_dimensions(content: bytes, url: str) -> tuple[int, int]:
    if len(content) < 24 or not content.startswith(PNG_SIGNATURE) or content[12:16] != b"IHDR":
        raise RuntimeError(f"operator icon is not a valid PNG: {url}")
    width, height = struct.unpack(">II", content[16:24])
    if width == 0 or height == 0:
        raise RuntimeError(f"operator icon has invalid dimensions: {url}")
    return width, height


def _database_targets(connection: sqlite3.Connection) -> list[MatchTarget]:
    targets = [
        MatchTarget(
            owner_kind="carrier",
            owner_id=row[0],
            plmn=row[1],
            mcc=row[2],
            mnc=row[3],
            profile_name=row[4],
            provider_name=row[5],
        )
        for row in connection.execute(
            """SELECT c.carrier_id, p.plmn, p.mcc, p.mnc,
                      c.canonical_name, c.brand_name
               FROM carriers AS c
               JOIN plmns AS p ON p.carrier_id = c.carrier_id
               ORDER BY c.carrier_id, p.plmn"""
        )
    ]
    targets.extend(
        MatchTarget(
            owner_kind="profile",
            owner_id=row[0],
            plmn=row[1],
            mcc=row[2],
            mnc=row[3],
            gid1=row[4],
            gid2=row[5],
            profile_name=row[6],
            provider_name=row[7] or row[8],
        )
        for row in connection.execute(
            """SELECT cp.profile_id, p.plmn, p.mcc, p.mnc,
                      mr.gid1, mr.gid2, cp.display_name, mr.spn,
                      COALESCE(c.brand_name, c.canonical_name)
               FROM carrier_profiles AS cp
               JOIN profile_match_rules AS mr ON mr.profile_id = cp.profile_id
               JOIN plmns AS p ON p.plmn = mr.plmn
               LEFT JOIN carriers AS c ON c.carrier_id = cp.carrier_id
               WHERE mr.is_exclusion = 0
               ORDER BY cp.profile_id, mr.priority, mr.match_rule_id"""
        )
    )
    return targets


def _verify_build_database(connection: sqlite3.Connection) -> None:
    connection.execute("PRAGMA foreign_keys = ON")
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if version < 4:
        raise RuntimeError("icon BLOB packaging requires schema version 4 or newer")
    release = connection.execute(
        "SELECT sealed FROM catalog_release WHERE singleton = 1"
    ).fetchone()
    if release is None:
        raise RuntimeError("catalog_release is missing")
    if release[0] != 0:
        raise RuntimeError("cannot package icons into a sealed catalog")


def package_database(
    database: Path,
    *,
    source_base_url: str,
    nekokolpa2_revision: str,
    timeout: float = 30.0,
) -> PackageResult:
    """Download only icons referenced by catalog PLMNs and embed them atomically."""
    if not database.is_file():
        raise RuntimeError(f"database does not exist: {database}")

    with closing(sqlite3.connect(database)) as connection, connection:
        _verify_build_database(connection)
        targets = _database_targets(connection)

        catalogs: dict[str, dict[str, Any] | None] = {}
        catalog_sources: dict[str, bytes] = {}
        for mcc in sorted({target.mcc for target in targets}):
            catalog, source = _load_catalog(source_base_url, mcc, timeout)
            catalogs[mcc] = catalog
            if source is not None:
                catalog_sources[mcc] = source

        owner_refs: dict[tuple[str, str], set[IconRef]] = defaultdict(set)
        unresolved = 0
        for target in targets:
            catalog = catalogs.get(target.mcc)
            reference = resolve_icon(catalog, target) if catalog is not None else None
            if reference is None:
                unresolved += 1
                continue
            owner_refs[(target.owner_kind, target.owner_id)].add(reference)

        references = sorted(
            {reference for refs in owner_refs.values() for reference in refs},
            key=lambda reference: (reference.scope, reference.name),
        )
        downloads: list[DownloadedAsset] = []
        for reference in references:
            remote_url = _url(
                source_base_url, "icons", reference.scope, f"{reference.name}.png"
            )
            content = _fetch(remote_url, timeout)
            width, height = _png_dimensions(content, remote_url)
            downloads.append(
                DownloadedAsset(
                    reference=reference,
                    remote_url=remote_url,
                    content=content,
                    sha256=hashlib.sha256(content).hexdigest(),
                    width=width,
                    height=height,
                )
            )

        extracted_at = datetime.now(timezone.utc).isoformat()
        with connection:
            connection.execute(
                "UPDATE carriers SET primary_asset_id = NULL WHERE primary_asset_id IN "
                "(SELECT asset_id FROM visual_assets WHERE source_name = ?)",
                (ASSET_SOURCE_NAME,),
            )
            connection.execute(
                "UPDATE carrier_profiles SET profile_asset_id = NULL WHERE profile_asset_id IN "
                "(SELECT asset_id FROM visual_assets WHERE source_name = ?)",
                (ASSET_SOURCE_NAME,),
            )
            connection.execute(
                "DELETE FROM visual_assets WHERE source_name = ?", (ASSET_SOURCE_NAME,)
            )
            connection.execute(
                "DELETE FROM source_snapshots WHERE parser_name = ?", (PACKAGER_NAME,)
            )

            connection.execute(
                """INSERT INTO source_snapshots(
                       source_kind, platform, vendor, source_revision,
                       source_uri, extracted_at, parser_name, parser_version,
                       license_note
                   ) VALUES ('icon_catalog', 'shared', 'NekokoLPA2', ?, ?, ?, ?, ?, ?)""",
                (
                    nekokolpa2_revision,
                    NEKOKOLPA2_URL,
                    extracted_at,
                    PACKAGER_NAME,
                    PACKAGER_VERSION,
                    "NekokoLPA2 source code is MIT; operator logo rights "
                    "are not asserted by this catalog.",
                ),
            )
            for mcc, source in sorted(catalog_sources.items()):
                connection.execute(
                    """INSERT INTO source_snapshots(
                           source_kind, platform, vendor, source_revision,
                           source_uri, artifact_sha256, extracted_at,
                           parser_name, parser_version, license_note
                       ) VALUES ('icon_catalog', 'shared', 'operator-icons',
                                 ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        nekokolpa2_revision,
                        _url(source_base_url, "catalog", f"{mcc}.toml"),
                        hashlib.sha256(source).hexdigest(),
                        extracted_at,
                        PACKAGER_NAME,
                        PACKAGER_VERSION,
                        "NOASSERTION; names and logos may be protected trademarks.",
                    ),
                )

            for asset in downloads:
                connection.execute(
                    """INSERT INTO visual_assets(
                           asset_id, asset_kind, asset_data, remote_url,
                           media_type, sha256, width, height, source_name,
                           source_url, license_spdx, attribution, is_official
                       ) VALUES (?, 'operator_logo', ?, ?, 'image/png', ?, ?, ?,
                                 ?, ?, 'NOASSERTION', ?, 0)""",
                    (
                        asset.reference.asset_id,
                        asset.content,
                        asset.remote_url,
                        asset.sha256,
                        asset.width,
                        asset.height,
                        ASSET_SOURCE_NAME,
                        source_base_url,
                        "Resolved using NekokoLPA2; operator names and logos "
                        "remain their owners' trademarks.",
                    ),
                )

            carriers_linked = 0
            profiles_linked = 0
            for (owner_kind, owner_id), refs in sorted(owner_refs.items()):
                if len(refs) != 1:
                    continue
                asset_id = next(iter(refs)).asset_id
                if owner_kind == "carrier":
                    connection.execute(
                        "UPDATE carriers SET primary_asset_id = ? WHERE carrier_id = ?",
                        (asset_id, owner_id),
                    )
                    carriers_linked += 1
                else:
                    connection.execute(
                        "UPDATE carrier_profiles SET profile_asset_id = ? WHERE profile_id = ?",
                        (asset_id, owner_id),
                    )
                    profiles_linked += 1

            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise RuntimeError("foreign_key_check failed after icon packaging")

    return PackageResult(
        carriers_linked=carriers_linked,
        profiles_linked=profiles_linked,
        assets_embedded=len(downloads),
        unresolved_targets=unresolved,
        nekokolpa2_revision=nekokolpa2_revision,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument(
        "--nekokolpa2-dir", type=Path, default=DEFAULT_NEKOKOLPA2_DIR
    )
    parser.add_argument(
        "--no-repo-update",
        action="store_true",
        help="use the existing checkout without git pull (offline/reproducible builds)",
    )
    parser.add_argument(
        "--source-base-url",
        help="override the operator-icons base URL discovered from NekokoLPA2",
    )
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()

    try:
        revision, discovered_url = refresh_nekokolpa2(
            args.nekokolpa2_dir, update=not args.no_repo_update
        )
        result = package_database(
            args.database,
            source_base_url=args.source_base_url or discovered_url,
            nekokolpa2_revision=revision,
            timeout=args.timeout,
        )
    except (
        RuntimeError,
        OSError,
        sqlite3.Error,
        urllib.error.URLError,
    ) as error:
        parser.exit(1, f"icon packaging failed: {error}\n")

    print(
        "embedded "
        f"{result.assets_embedded} icons; linked {result.carriers_linked} carriers "
        f"and {result.profiles_linked} profiles; "
        f"unresolved targets: {result.unresolved_targets}; "
        f"NekokoLPA2: {result.nekokolpa2_revision}"
    )


if __name__ == "__main__":
    main()
