"""Pinned Apple IPSW metadata and BuildManifest parsing."""

from __future__ import annotations

import plistlib
import json
import urllib.request
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class IPSWArtifact:
    device_name: str
    product_type: str
    os_version: str
    build_id: str
    url: str
    sha256: str | None
    size: int
    file_system_path: str | None = None
    baseband_path: str | None = None
    baseband_version: str | None = None

    @property
    def filename(self) -> str:
        return self.url.rsplit("/", 1)[-1]


IPHONE_16_PRO_26_6 = IPSWArtifact(
    device_name="iPhone 16 Pro",
    product_type="iPhone17,1",
    os_version="26.6",
    build_id="23G71",
    url=(
        "https://updates.cdn-apple.com/2026SummerFCS/fullrestores/140-57119/"
        "CBE71A83-42C2-4ED3-AAFC-190CFEF5E9C5/"
        "iPhone17,1_26.6_23G71_Restore.ipsw"
    ),
    sha256="2dbcf24e7abd0b7d1b7e5c281bc39f9298b9959c35d0a5b6fc39b30edb0992f7",
    size=11_361_094_595,
    file_system_path="094-95532-083.dmg.aea",
    baseband_path="Firmware/Mav24-2.70.01.Release.bbfw",
    baseband_version="Mav24-2.70.01",
)

# Product classes used by the modern Pro/Pro Max family.  Unknown classes are
# valid too: the bundle parser simply skips a device-specific override it
# cannot identify, while keeping the carrier-wide configuration.
PRODUCT_DEVICE_CLASSES = {
    "iPhone17,2": "D94",  # iPhone 16 Pro Max
    "iPhone17,1": "D93",  # iPhone 16 Pro
    "iPhone16,2": "D84",  # iPhone 15 Pro Max
    "iPhone16,1": "D83",  # iPhone 15 Pro
    "iPhone14,3": "D63",  # iPhone 13 Pro Max
}
PRODUCT_NAMES = {
    "iPhone17,2": "iPhone 16 Pro Max",
    "iPhone17,1": "iPhone 16 Pro",
    "iPhone16,2": "iPhone 15 Pro Max",
    "iPhone16,1": "iPhone 15 Pro",
    "iPhone14,3": "iPhone 13 Pro Max",
}
IPSW_API_URL = "https://api.ipsw.me/v4/device/{product_type}?type=ipsw"


def catalog_filename(artifact: IPSWArtifact) -> str:
    """Return the stable release filename for one device and iOS version."""

    device = re.sub(r"[^a-z0-9]+", "", artifact.device_name.casefold())
    version = re.sub(r"[^a-z0-9.]+", "-", artifact.os_version.casefold()).strip("-.")
    return f"carrier-bundles-{device or 'iphone'}-{version or 'unknown'}.sqlite3"


def resolve_ipsw_artifact(
    product_type: str = "iPhone17,2",
    version: str = "latest",
    *,
    api_url: str = IPSW_API_URL,
) -> IPSWArtifact:
    """Discover a signed Pro Max IPSW through ipsw.me's public index.

    The index is only a locator. The returned URL is the Apple CDN URL and is
    later checked against the BuildManifest; no index data is written to the
    catalog.  A caller can pin an exact version/build for reproducibility.
    """

    url = api_url.format(product_type=product_type)
    request = urllib.request.Request(
        url, headers={"User-Agent": "carrier-bundles-ios-extractor/0.3"}
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = json.loads(response.read().decode("utf-8"))
    rows = payload.get("firmwares", payload) if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise ValueError("ipsw.me response has no firmware list")
    candidates = [
        row for row in rows
        if isinstance(row, dict)
        and row.get("url")
        and row.get("signed", True) is not False
    ]
    if version.casefold() != "latest":
        candidates = [
            row for row in candidates
            if str(row.get("version", "")).casefold() == version.casefold()
            or str(row.get("buildid", "")).casefold() == version.casefold()
        ]
    if not candidates:
        raise RuntimeError(
            f"no signed IPSW found for {product_type} version {version!r}"
        )
    candidates.sort(key=lambda row: str(row.get("releasedate", row.get("version", ""))))
    row = candidates[-1]
    sha256 = row.get("sha256")
    if not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", sha256):
        sha256 = None
    return IPSWArtifact(
        device_name=str(row.get("name") or PRODUCT_NAMES.get(product_type, product_type)),
        product_type=product_type,
        os_version=str(row.get("version") or "unknown"),
        build_id=str(row.get("buildid") or "unknown"),
        url=str(row["url"]),
        sha256=sha256,
        size=int(row.get("filesize") or 0),
        baseband_version=None,
    )


def _load_plist(path: Path) -> dict[str, Any]:
    with path.open("rb") as stream:
        value = plistlib.load(stream)
    if not isinstance(value, dict):
        raise ValueError(f"expected a plist dictionary: {path}")
    return value


def inspect_build_manifest(path: Path, artifact: IPSWArtifact) -> IPSWArtifact:
    """Validate a BuildManifest and return paths declared by the IPSW."""

    manifest = _load_plist(path)
    product_types = manifest.get("SupportedProductTypes", [])
    if artifact.product_type not in product_types:
        raise ValueError(
            f"BuildManifest does not support {artifact.product_type}: {product_types}"
        )
    if manifest.get("ProductBuildVersion") != artifact.build_id:
        raise ValueError("BuildManifest build does not match requested artifact")
    if manifest.get("ProductVersion") != artifact.os_version:
        raise ValueError("BuildManifest OS version does not match requested artifact")

    identities = manifest.get("BuildIdentities", [])
    identity = next(
        (
            item
            for item in identities
            if item.get("Info", {}).get("RestoreBehavior") == "Erase"
            and "Customer" in str(item.get("Info", {}).get("Variant", ""))
        ),
        identities[0] if identities else None,
    )
    if identity is None:
        raise ValueError("BuildManifest contains no build identities")
    components = identity.get("Manifest", {})
    file_system_path = components.get("OS", {}).get("Info", {}).get("Path")
    baseband_path = (
        components.get("BasebandFirmware", {}).get("Info", {}).get("Path")
    )
    if not file_system_path:
        raise ValueError("BuildManifest does not declare an OS filesystem image")
    return IPSWArtifact(
        device_name=artifact.device_name,
        product_type=artifact.product_type,
        os_version=artifact.os_version,
        build_id=artifact.build_id,
        url=artifact.url,
        sha256=artifact.sha256,
        size=artifact.size,
        file_system_path=file_system_path,
        baseband_path=baseband_path,
        baseband_version=(
            Path(baseband_path).name.removesuffix(".Release.bbfw")
            if baseband_path
            else None
        ),
    )
