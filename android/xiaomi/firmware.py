"""Download and inventory modem-related files from Xiaomi firmware packages."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sys
import tarfile
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path


MODEM_MEMBER_RE = re.compile(
    r"(^|/)(images/)?("
    r"NON-HLOS\.bin|modem(?:_[ab])?\.img|modemfirmware(?:_[ab])?\.img|"
    r"bluetooth(?:_[ab])?\.img|dsp(?:_[ab])?\.img|adsp(?:_[ab])?\.img|"
    r"cdsp(?:_[ab])?\.img|imagefv(?:_[ab])?\.img|xbl_config(?:_[ab])?\.elf"
    r")$",
    re.IGNORECASE,
)
USER_AGENT = "carrier-bundles-xiaomi-extractor/0.1"
MANIFEST_NAME = "xiaomi-baseband-manifest.json"


@dataclass(frozen=True)
class XiaomiModemArtifact:
    archive_member: str
    extracted_path: Path
    sha256: str
    size: int


@dataclass(frozen=True)
class ExtractedXiaomiFirmware:
    rom_path: Path
    rom_sha256: str
    modem_artifacts: tuple[XiaomiModemArtifact, ...]


def digest_file(path: Path, algorithm: str = "sha256") -> str:
    digest = hashlib.new(algorithm)
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_file(url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".part")
    offset = partial.stat().st_size if partial.exists() else 0
    headers = {"User-Agent": USER_AGENT}
    if offset:
        headers["Range"] = f"bytes={offset}-"
    request = urllib.request.Request(url, headers=headers)
    mode = "ab" if offset else "wb"
    with urllib.request.urlopen(request, timeout=120) as response, partial.open(mode) as output:
        if offset and getattr(response, "status", None) != 206:
            output.close()
            partial.unlink()
            return download_file(url, destination)
        total_header = response.headers.get("Content-Length")
        total = offset + int(total_header) if total_header else None
        written = offset
        while True:
            block = response.read(4 * 1024 * 1024)
            if not block:
                break
            output.write(block)
            written += len(block)
            if total:
                print(
                    f"\rdownloaded {written / 1024**2:.1f}/{total / 1024**2:.1f} MiB",
                    end="",
                    file=sys.stderr,
                    flush=True,
                )
    if total_header:
        print(file=sys.stderr)
    partial.replace(destination)


def ensure_rom(url: str, destination: Path, *, expected_md5: str | None = None) -> Path:
    if not destination.exists():
        download_file(url, destination)
    if expected_md5:
        actual = digest_file(destination, "md5")
        if actual.casefold() != expected_md5.casefold():
            raise RuntimeError(
                f"Xiaomi ROM MD5 mismatch for {destination}: "
                f"expected {expected_md5}, got {actual}"
            )
    return destination


def _safe_member_output(output_dir: Path, member_name: str) -> Path:
    name = Path(member_name).name
    if not name or name in {".", ".."}:
        raise RuntimeError(f"unsafe archive member name: {member_name}")
    output = (output_dir / name).resolve()
    output.relative_to(output_dir.resolve())
    return output


def _rom_cache_key(path: Path) -> dict[str, object]:
    stat = path.stat()
    return {
        "path": str(path.resolve()),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def _artifact_to_json(item: XiaomiModemArtifact) -> dict[str, object]:
    return {
        "archive_member": item.archive_member,
        "extracted_path": str(item.extracted_path),
        "sha256": item.sha256,
        "size": item.size,
    }


def _artifact_from_json(value: object) -> XiaomiModemArtifact | None:
    if not isinstance(value, dict):
        return None
    archive_member = value.get("archive_member")
    extracted_path = value.get("extracted_path")
    sha256 = value.get("sha256")
    size = value.get("size")
    if (
        not isinstance(archive_member, str)
        or not isinstance(extracted_path, str)
        or not isinstance(sha256, str)
        or not isinstance(size, int)
    ):
        return None
    path = Path(extracted_path)
    if not path.is_file() or path.stat().st_size != size:
        return None
    if digest_file(path) != sha256:
        return None
    return XiaomiModemArtifact(
        archive_member=archive_member,
        extracted_path=path,
        sha256=sha256,
        size=size,
    )


def _cached_inventory(
    rom_path: Path, output_dir: Path
) -> ExtractedXiaomiFirmware | None:
    manifest = output_dir / MANIFEST_NAME
    if not manifest.is_file():
        return None
    try:
        document = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(document, dict) or document.get("rom") != _rom_cache_key(rom_path):
        return None
    rom_sha256 = document.get("rom_sha256")
    raw_artifacts = document.get("modem_artifacts")
    if not isinstance(rom_sha256, str) or not isinstance(raw_artifacts, list):
        return None
    artifacts = [_artifact_from_json(item) for item in raw_artifacts]
    if not artifacts or any(item is None for item in artifacts):
        return None
    return ExtractedXiaomiFirmware(
        rom_path=rom_path,
        rom_sha256=rom_sha256,
        modem_artifacts=tuple(item for item in artifacts if item is not None),
    )


def _write_inventory_manifest(
    output_dir: Path, firmware: ExtractedXiaomiFirmware
) -> None:
    manifest = output_dir / MANIFEST_NAME
    manifest.write_text(
        json.dumps(
            {
                "schema": "carrier-bundles-xiaomi-baseband-manifest-v1",
                "rom": _rom_cache_key(firmware.rom_path),
                "rom_sha256": firmware.rom_sha256,
                "modem_artifacts": [
                    _artifact_to_json(item) for item in firmware.modem_artifacts
                ],
            },
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def _extract_tar_modem_artifacts(rom_path: Path, output_dir: Path) -> list[XiaomiModemArtifact]:
    modem_artifacts: list[XiaomiModemArtifact] = []
    with tarfile.open(rom_path, mode="r:*") as archive:
        members = [
            member
            for member in archive.getmembers()
            if member.isfile() and MODEM_MEMBER_RE.search(member.name)
        ]
        if not members:
            raise RuntimeError("Xiaomi archive contains no known modem artifacts")
        seen_outputs: set[Path] = set()
        for member in sorted(members, key=lambda item: item.name.casefold()):
            output = _safe_member_output(output_dir, member.name)
            if output in seen_outputs:
                output = output.with_name(f"{len(seen_outputs):02d}-{output.name}")
            seen_outputs.add(output)
            source = archive.extractfile(member)
            if source is None:
                raise RuntimeError(f"cannot extract archive member: {member.name}")
            temporary = output.with_suffix(output.suffix + ".part")
            with source, temporary.open("wb") as destination:
                shutil.copyfileobj(source, destination, length=8 * 1024 * 1024)
            temporary.replace(output)
            modem_artifacts.append(
                XiaomiModemArtifact(
                    archive_member=member.name,
                    extracted_path=output,
                    sha256=digest_file(output),
                    size=output.stat().st_size,
                )
            )
    return modem_artifacts


def _extract_zip_modem_artifacts(rom_path: Path, output_dir: Path) -> list[XiaomiModemArtifact]:
    modem_artifacts: list[XiaomiModemArtifact] = []
    with zipfile.ZipFile(rom_path) as archive:
        members = [
            info
            for info in archive.infolist()
            if not info.is_dir() and MODEM_MEMBER_RE.search(info.filename)
        ]
        if not members:
            raise RuntimeError("Xiaomi archive contains no known modem artifacts")
        seen_outputs: set[Path] = set()
        for info in sorted(members, key=lambda item: item.filename.casefold()):
            output = _safe_member_output(output_dir, info.filename)
            if output in seen_outputs:
                output = output.with_name(f"{len(seen_outputs):02d}-{output.name}")
            seen_outputs.add(output)
            temporary = output.with_suffix(output.suffix + ".part")
            with archive.open(info) as source, temporary.open("wb") as destination:
                shutil.copyfileobj(source, destination, length=8 * 1024 * 1024)
            temporary.replace(output)
            modem_artifacts.append(
                XiaomiModemArtifact(
                    archive_member=info.filename,
                    extracted_path=output,
                    sha256=digest_file(output),
                    size=output.stat().st_size,
                )
            )
    return modem_artifacts


def extract_xiaomi_modem_artifacts(rom_path: Path, output_dir: Path) -> ExtractedXiaomiFirmware:
    """Extract modem-related members from a Xiaomi firmware ZIP or fastboot tar."""

    output_dir.mkdir(parents=True, exist_ok=True)
    cached = _cached_inventory(rom_path, output_dir)
    if cached is not None:
        return cached

    if zipfile.is_zipfile(rom_path):
        modem_artifacts = _extract_zip_modem_artifacts(rom_path, output_dir)
    else:
        modem_artifacts = _extract_tar_modem_artifacts(rom_path, output_dir)

    firmware = ExtractedXiaomiFirmware(
        rom_path=rom_path,
        rom_sha256=digest_file(rom_path),
        modem_artifacts=tuple(modem_artifacts),
    )
    _write_inventory_manifest(output_dir, firmware)
    return firmware
