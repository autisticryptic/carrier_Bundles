"""Pinned Xiaomi firmware package metadata."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class XiaomiFastbootArtifact:
    device_name: str
    codename: str
    region: str
    android_version: str
    build_id: str
    url: str
    md5: str | None
    package_kind: str = "firmware_zip"
    size: int | None = None

    @property
    def filename(self) -> str:
        return Path(self.url).name


XIAOMI_15_ULTRA_GLOBAL_OS3_0_301_0 = XiaomiFastbootArtifact(
    device_name="Xiaomi 15 Ultra",
    codename="xuanyuan",
    region="Global",
    android_version="16",
    build_id="OS3.0.301.0.WOAMIXM",
    url=(
        "https://github.com/XiaomiFirmwareUpdaterReleases/firmware_xiaomi_xuanyuan/"
        "releases/download/stable-12.05.2026/"
        "fw_xuanyuan_xuanyuan_global-ota_full-OS3.0.301.0.WOAMIXM-user-16.0-"
        "a67f21cbf3.zip"
    ),
    md5="f53a4b0b909e2977ce6f0a349ba5ea80",
    package_kind="firmware_zip",
    size=218_045_757,
)
