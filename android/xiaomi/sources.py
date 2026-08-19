"""Pinned Xiaomi ROM package metadata."""

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
    package_kind: str = "full_ota_zip"
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
        "https://bkt-sgp-miui-ota-update-alisgp.oss-ap-southeast-1.aliyuncs.com/"
        "OS3.0.301.0.WOAMIXM/"
        "xuanyuan_global-ota_full-OS3.0.301.0.WOAMIXM-user-16.0-a67f21cbf3.zip"
    ),
    md5=None,
    package_kind="full_ota_zip",
    size=9_035_445_935,
)
