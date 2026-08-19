"""Download and inventory modem-related files from Xiaomi fastboot ROMs."""

from __future__ import annotations

import hashlib
import re
import shutil
import sys
import tarfile
import urllib.request
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


def extract_xiaomi_modem_artifacts(rom_path: Path, output_dir: Path) -> ExtractedXiaomiFirmware:
    """Extract modem-related members from a Xiaomi fastboot tar/tgz archive."""

    output_dir.mkdir(parents=True, exist_ok=True)
    modem_artifacts: list[XiaomiModemArtifact] = []
    with tarfile.open(rom_path, mode="r:*") as archive:
        members = [
            member
            for member in archive.getmembers()
            if member.isfile() and MODEM_MEMBER_RE.search(member.name)
        ]
        if not members:
            raise RuntimeError("Xiaomi fastboot ROM contains no known modem artifacts")
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

    return ExtractedXiaomiFirmware(
        rom_path=rom_path,
        rom_sha256=digest_file(rom_path),
        modem_artifacts=tuple(modem_artifacts),
    )

