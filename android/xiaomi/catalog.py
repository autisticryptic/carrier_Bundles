"""Import Xiaomi modem inventory into a schema-v7 catalog."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import PARSER_VERSION
from .firmware import ExtractedXiaomiFirmware
from .sources import XiaomiFastbootArtifact


PARSER_NAME = "android/xiaomi/baseband-inventory"


@dataclass(frozen=True)
class ImportStats:
    modem_artifacts_imported: int


def _artifact_summary(item) -> dict[str, object]:
    return {
        "archive_member": item.archive_member,
        "extracted_path": str(item.extracted_path),
        "sha256": item.sha256,
        "size": item.size,
    }


def import_xiaomi_baseband_catalog(
    database: Path,
    firmware: ExtractedXiaomiFirmware,
    *,
    artifact: XiaomiFastbootArtifact,
) -> ImportStats:
    """Store Xiaomi modem artifact inventory without guessing field semantics."""

    extracted_at = datetime.now(timezone.utc).isoformat()
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("PRAGMA foreign_keys = ON")
        release = connection.execute(
            "SELECT sealed FROM catalog_metadata WHERE singleton = 1"
        ).fetchone()
        if release is None or release[0] != 0:
            raise RuntimeError("Xiaomi importer requires an unsealed v7 catalog")

        rom_revision = json.dumps(
            {
                "device_name": artifact.device_name,
                "codename": artifact.codename,
                "region": artifact.region,
                "android_version": artifact.android_version,
                "build_id": artifact.build_id,
                "md5": artifact.md5,
                "package_kind": artifact.package_kind,
                "members": len(firmware.modem_artifacts),
            },
            ensure_ascii=True,
            sort_keys=True,
        )
        connection.execute(
            """INSERT INTO source_artifacts(
                   source_kind, source_uri, artifact_sha256, source_revision,
                   extracted_at, parser_name, parser_version, license_note
               ) VALUES ('firmware_manifest', ?, ?, ?, ?, ?, ?, ?)""",
            (
                artifact.url,
                firmware.rom_sha256,
                rom_revision,
                extracted_at,
                PARSER_NAME,
                PARSER_VERSION,
                "Official Xiaomi fastboot ROM inventory; no subscriber data.",
            ),
        )

        for item in firmware.modem_artifacts:
            member_revision = json.dumps(
                {
                    "archive_member": item.archive_member,
                    "device_name": artifact.device_name,
                    "codename": artifact.codename,
                    "region": artifact.region,
                    "android_version": artifact.android_version,
                    "build_id": artifact.build_id,
                    "package_kind": artifact.package_kind,
                    "size": item.size,
                },
                ensure_ascii=True,
                sort_keys=True,
            )
            connection.execute(
                """INSERT INTO source_artifacts(
                       source_kind, source_uri, artifact_sha256, source_revision,
                       extracted_at, parser_name, parser_version, license_note
                   ) VALUES ('modem_config', ?, ?, ?, ?, ?, ?, ?)""",
                (
                    f"{artifact.url}#{item.archive_member}",
                    item.sha256,
                    member_revision,
                    extracted_at,
                    PARSER_NAME,
                    PARSER_VERSION,
                    "Raw baseband/modem-related firmware artifact inventory only.",
                ),
            )

        notes = {
            "source_kind": "xiaomi_firmware_baseband_inventory",
            "device_name": artifact.device_name,
            "codename": artifact.codename,
            "region": artifact.region,
            "android_version": artifact.android_version,
            "build_id": artifact.build_id,
            "package_kind": artifact.package_kind,
            "rom_sha256": firmware.rom_sha256,
            "modem_artifacts": [
                _artifact_summary(item) for item in firmware.modem_artifacts
            ],
            "semantic_boundary": (
                "This catalog inventories public modem firmware artifacts only; "
                "it does not infer IMS, VoLTE, VoNR or VoWiFi runtime parameters."
            ),
        }
        connection.execute(
            "UPDATE catalog_metadata SET notes = ? WHERE singleton = 1",
            (json.dumps(notes, ensure_ascii=True, sort_keys=True),),
        )
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise RuntimeError("foreign key check failed after Xiaomi import")
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("SQLite quick_check failed after Xiaomi import")

    return ImportStats(modem_artifacts_imported=len(firmware.modem_artifacts))
