"""Download and extract public carrier/baseband files from Xiaomi ROMs."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sys
import tarfile
import subprocess
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

from android.pixel.firmware import convert_sparse_image


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
CONFIG_MANIFEST_NAME = "xiaomi-carrier-config-manifest.json"
CONFIG_PARTITIONS = ("product", "system_ext", "vendor", "odm")
MODEM_PAYLOAD_PARTITIONS = (
    "bluetooth",
    "dsp",
    "imagefv",
    "modem",
    "modemfirmware",
)
PARTITION_IMAGE_RE = re.compile(
    r"(^|/)images/(?P<name>product|system_ext|vendor|odm|super)\.img$",
    re.IGNORECASE,
)
CONFIG_MEMBER_RE = re.compile(
    r"(^|/)(?:"
    r"etc/CarrierSettings/[^/]+\.pb|"
    r"etc/CarrierConfig/[^/]+\.xml|"
    r"etc/(?:apns-conf|epdg_apns_conf)\.xml|"
    r"etc/[^/]*carrier[^/]*config[^/]*\.xml"
    r")$",
    re.IGNORECASE,
)


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


@dataclass(frozen=True)
class ExtractedXiaomiCarrierConfig:
    rom_path: Path
    rom_sha256: str
    root_dir: Path
    config_files: tuple[Path, ...]
    carrier_settings_dir: Path | None


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


def _safe_relative_output(output_dir: Path, member_name: str) -> Path:
    parts = [part for part in Path(member_name).parts if part not in {"", ".", ".."}]
    if not parts:
        raise RuntimeError(f"unsafe archive member name: {member_name}")
    output = (output_dir / Path(*parts)).resolve()
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


def _cached_config(
    rom_path: Path, output_dir: Path
) -> ExtractedXiaomiCarrierConfig | None:
    manifest = output_dir / CONFIG_MANIFEST_NAME
    if not manifest.is_file():
        return None
    try:
        document = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(document, dict) or document.get("rom") != _rom_cache_key(rom_path):
        return None
    rom_sha256 = document.get("rom_sha256")
    raw_files = document.get("config_files")
    raw_carrier_settings_dir = document.get("carrier_settings_dir")
    if not isinstance(rom_sha256, str) or not isinstance(raw_files, list):
        return None
    files = tuple(Path(item) for item in raw_files if isinstance(item, str))
    if not files or any(not item.is_file() for item in files):
        return None
    carrier_settings_dir = (
        Path(raw_carrier_settings_dir)
        if isinstance(raw_carrier_settings_dir, str)
        else None
    )
    if carrier_settings_dir is not None and not carrier_settings_dir.is_dir():
        carrier_settings_dir = None
    return ExtractedXiaomiCarrierConfig(
        rom_path=rom_path,
        rom_sha256=rom_sha256,
        root_dir=output_dir,
        config_files=files,
        carrier_settings_dir=carrier_settings_dir,
    )


def _write_config_manifest(
    output_dir: Path, config: ExtractedXiaomiCarrierConfig
) -> None:
    manifest = output_dir / CONFIG_MANIFEST_NAME
    manifest.write_text(
        json.dumps(
            {
                "schema": "carrier-bundles-xiaomi-carrier-config-manifest-v1",
                "rom": _rom_cache_key(config.rom_path),
                "rom_sha256": config.rom_sha256,
                "config_files": [str(path) for path in config.config_files],
                "carrier_settings_dir": (
                    str(config.carrier_settings_dir)
                    if config.carrier_settings_dir is not None
                    else None
                ),
            },
            ensure_ascii=True,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )


def _copy_zip_config_members(rom_path: Path, output_dir: Path) -> list[Path]:
    result: list[Path] = []
    with zipfile.ZipFile(rom_path) as archive:
        members = [
            info
            for info in archive.infolist()
            if not info.is_dir() and CONFIG_MEMBER_RE.search(info.filename)
        ]
        for info in sorted(members, key=lambda item: item.filename.casefold()):
            output = _safe_relative_output(output_dir, info.filename)
            output.parent.mkdir(parents=True, exist_ok=True)
            temporary = output.with_suffix(output.suffix + ".part")
            with archive.open(info) as source, temporary.open("wb") as destination:
                shutil.copyfileobj(source, destination, length=8 * 1024 * 1024)
            temporary.replace(output)
            result.append(output)
    return result


def _extract_payload_partitions(
    rom_path: Path, output_dir: Path, partitions: tuple[str, ...]
) -> list[Path]:
    dumper = shutil.which("payload-dumper-go")
    if not dumper:
        raise RuntimeError(
            "Xiaomi full OTA contains payload.bin; install payload-dumper-go "
            "to extract selected partitions"
        )
    output_dir.mkdir(parents=True, exist_ok=True)
    command = [
        dumper,
        "-p",
        ",".join(partitions),
        "-o",
        str(output_dir),
        str(rom_path),
    ]
    subprocess.run(command, check=True)
    return [
        output_dir / f"{name}.img"
        for name in partitions
        if (output_dir / f"{name}.img").is_file()
    ]


def _extract_zip_payload_partitions(rom_path: Path, output_dir: Path) -> list[Path]:
    return _extract_payload_partitions(
        rom_path, output_dir / "payload-partitions", CONFIG_PARTITIONS
    )


def _extract_tar_partition_images(rom_path: Path, output_dir: Path) -> list[Path]:
    result: list[Path] = []
    partition_dir = output_dir / "fastboot-partitions"
    partition_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(rom_path, mode="r:*") as archive:
        members = [
            member
            for member in archive.getmembers()
            if member.isfile() and PARTITION_IMAGE_RE.search(member.name)
        ]
        for member in sorted(members, key=lambda item: item.name.casefold()):
            name = Path(member.name).name
            output = partition_dir / name
            if output.exists() and output.stat().st_size == member.size:
                result.append(output)
                continue
            source = archive.extractfile(member)
            if source is None:
                raise RuntimeError(f"cannot extract archive member: {member.name}")
            temporary = output.with_suffix(output.suffix + ".part")
            with source, temporary.open("wb") as destination:
                shutil.copyfileobj(source, destination, length=8 * 1024 * 1024)
            temporary.replace(output)
            result.append(output)
    return result


def _materialize_sparse_image(image: Path, output_dir: Path) -> Path:
    with image.open("rb") as stream:
        magic = stream.read(4)
    if magic != b"\x3a\xff\x26\xed":
        return image
    raw = output_dir / f"{image.stem}.raw.img"
    if not raw.exists():
        convert_sparse_image(image, raw)
    return raw


def _extract_config_from_partition(image: Path, output_dir: Path) -> list[Path]:
    seven_zip = shutil.which("7z") or shutil.which("7zz")
    if not seven_zip:
        raise RuntimeError("7-Zip is required to extract Xiaomi carrier config partitions")
    extracted = output_dir / image.stem
    before = {path.resolve() for path in extracted.rglob("*")} if extracted.exists() else set()
    extracted.mkdir(parents=True, exist_ok=True)
    patterns = [
        "etc/CarrierSettings/*",
        "etc/CarrierConfig/*",
        "etc/apns-conf.xml",
        "etc/epdg_apns_conf.xml",
        "etc/*carrier*config*.xml",
    ]
    command = [seven_zip, "x", "-y", "-aoa", f"-o{extracted}", str(image), *patterns]
    subprocess.run(command, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return [
        path
        for path in sorted(extracted.rglob("*"))
        if path.is_file()
        and path.resolve() not in before
        and (
            path.suffix.casefold() == ".pb"
            or CONFIG_MEMBER_RE.search(str(path.relative_to(extracted)))
        )
    ]


def _carrier_settings_dir(root_dir: Path) -> Path | None:
    for path in sorted(root_dir.rglob("CarrierSettings")):
        if (path / "carrier_list.pb").is_file() and (path / "others.pb").is_file():
            return path
    return None


def _collect_config_files(root_dir: Path) -> tuple[Path, ...]:
    files = [
        path
        for path in root_dir.rglob("*")
        if path.is_file()
        and (
            path.suffix.casefold() == ".pb"
            or CONFIG_MEMBER_RE.search(str(path.relative_to(root_dir)))
        )
    ]
    return tuple(sorted(files, key=lambda item: str(item).casefold()))


def extract_xiaomi_carrier_configs(
    rom_path: Path, output_dir: Path
) -> ExtractedXiaomiCarrierConfig:
    """Extract public carrier configuration files from a Xiaomi ROM package."""

    output_dir.mkdir(parents=True, exist_ok=True)
    cached = _cached_config(rom_path, output_dir)
    if cached is not None:
        return cached

    if zipfile.is_zipfile(rom_path):
        _copy_zip_config_members(rom_path, output_dir)
        if not _collect_config_files(output_dir):
            with zipfile.ZipFile(rom_path) as archive:
                has_payload = any(info.filename == "payload.bin" for info in archive.infolist())
            if has_payload:
                for image in _extract_zip_payload_partitions(rom_path, output_dir):
                    materialized = _materialize_sparse_image(image, output_dir / "raw")
                    _extract_config_from_partition(materialized, output_dir / "extracted")
    else:
        for image in _extract_tar_partition_images(rom_path, output_dir):
            if image.name.casefold() == "super.img":
                continue
            materialized = _materialize_sparse_image(image, output_dir / "raw")
            _extract_config_from_partition(materialized, output_dir / "extracted")

    config_files = _collect_config_files(output_dir)
    if not config_files:
        raise RuntimeError(
            "Xiaomi ROM contains no extractable CarrierSettings, CarrierConfig, APN or ePDG config files"
        )
    result = ExtractedXiaomiCarrierConfig(
        rom_path=rom_path,
        rom_sha256=digest_file(rom_path),
        root_dir=output_dir,
        config_files=config_files,
        carrier_settings_dir=_carrier_settings_dir(output_dir),
    )
    _write_config_manifest(output_dir, result)
    return result


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
            has_payload = any(info.filename == "payload.bin" for info in archive.infolist())
            if not has_payload:
                raise RuntimeError("Xiaomi archive contains no known modem artifacts")
            for path in _extract_payload_partitions(
                rom_path, output_dir / "payload-modem", MODEM_PAYLOAD_PARTITIONS
            ):
                modem_artifacts.append(
                    XiaomiModemArtifact(
                        archive_member=f"payload.bin#{path.name}",
                        extracted_path=path,
                        sha256=digest_file(path),
                        size=path.stat().st_size,
                    )
                )
            if modem_artifacts:
                return modem_artifacts
            raise RuntimeError("Xiaomi payload contains no known modem artifacts")
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
    """Extract modem-related members from a Xiaomi full OTA, firmware ZIP or fastboot tar."""

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
