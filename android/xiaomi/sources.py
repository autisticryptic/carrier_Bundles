"""Pinned Xiaomi fastboot ROM metadata."""

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
        "https://bigota.d.miui.com/OS3.0.301.0.WOAMIXM/"
        "xuanyuan_global_images_OS3.0.301.0.WOAMIXM_20260428.0000.00_16.0_global_"
        "d98a2e098d.tgz"
    ),
    md5=None,
    size=11_805_403_314,
)
