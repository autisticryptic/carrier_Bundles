"""Pinned Apple IPSW metadata and BuildManifest parsing."""

from __future__ import annotations

import plistlib
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
    sha256: str
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

