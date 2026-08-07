"""Download and selectively unpack Pixel factory images."""

from __future__ import annotations

import hashlib
import shutil
import struct
import subprocess
import sys
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path


SPARSE_MAGIC = 0xED26FF3A
SPARSE_HEADER = struct.Struct("<I4H4I")
CHUNK_HEADER = struct.Struct("<2H2I")
CHUNK_RAW = 0xCAC1
CHUNK_FILL = 0xCAC2
CHUNK_DONT_CARE = 0xCAC3
CHUNK_CRC32 = 0xCAC4


@dataclass(frozen=True)
class ExtractedPixelFirmware:
    factory_zip: Path
    inner_zip: Path
    product_image: Path
    vendor_image: Path
    carrier_settings_dir: Path
    mcfg_dir: Path | None
    android_info: Path
    baseband_version: str | None


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download_file(url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".part")
    offset = partial.stat().st_size if partial.exists() else 0
    headers = {"User-Agent": "carrier-bundles-pixel-extractor/0.1"}
    if offset:
        headers["Range"] = f"bytes={offset}-"
    request = urllib.request.Request(url, headers=headers)
    mode = "ab" if offset else "wb"
    with urllib.request.urlopen(request, timeout=120) as response, partial.open(mode) as output:
        if offset and response.status != 206:
            output.close()
            partial.unlink()
            return download_file(url, destination)
        total_header = response.headers.get("Content-Length")
        total = offset + int(total_header) if total_header else None
        written = offset
        while True:
            chunk = response.read(4 * 1024 * 1024)
            if not chunk:
                break
            output.write(chunk)
            written += len(chunk)
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


def ensure_factory_zip(url: str, expected_sha256: str, destination: Path) -> Path:
    if not destination.exists():
        download_file(url, destination)
    actual = sha256_file(destination)
    if actual.casefold() != expected_sha256.casefold():
        raise RuntimeError(
            f"factory image SHA-256 mismatch for {destination}: "
            f"expected {expected_sha256}, got {actual}"
        )
    return destination


def _extract_zip_member(archive: zipfile.ZipFile, info: zipfile.ZipInfo, output: Path) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists() and output.stat().st_size == info.file_size:
        return output
    temporary = output.with_suffix(output.suffix + ".part")
    if temporary.exists():
        temporary.unlink()
    with archive.open(info) as source, temporary.open("wb") as destination:
        shutil.copyfileobj(source, destination, length=8 * 1024 * 1024)
    if temporary.stat().st_size != info.file_size:
        raise RuntimeError(f"incomplete ZIP member extraction: {info.filename}")
    temporary.replace(output)
    return output


def _find_member(archive: zipfile.ZipFile, predicate, description: str) -> zipfile.ZipInfo:
    matches = [info for info in archive.infolist() if predicate(info.filename)]
    if len(matches) != 1:
        raise RuntimeError(
            f"expected one {description} in {archive.filename}, found {len(matches)}"
        )
    return matches[0]


def _materialize_ext_image(image: Path, work_dir: Path) -> Path:
    with image.open("rb") as stream:
        magic = stream.read(4)
    if len(magic) == 4 and struct.unpack("<I", magic)[0] == SPARSE_MAGIC:
        raw = work_dir / f"{image.stem}.raw.img"
        if not raw.exists():
            convert_sparse_image(image, raw)
        return raw
    return image


def convert_sparse_image(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".part")
    with source.open("rb") as input_stream, temporary.open("w+b") as output:
        header_data = input_stream.read(SPARSE_HEADER.size)
        if len(header_data) != SPARSE_HEADER.size:
            raise RuntimeError(f"truncated Android sparse header: {source}")
        (
            magic,
            major,
            _minor,
            file_header_size,
            chunk_header_size,
            block_size,
            total_blocks,
            total_chunks,
            _checksum,
        ) = SPARSE_HEADER.unpack(header_data)
        if magic != SPARSE_MAGIC or major != 1:
            raise RuntimeError(f"unsupported Android sparse image: {source}")
        input_stream.seek(file_header_size)
        blocks_written = 0
        for _ in range(total_chunks):
            chunk_data = input_stream.read(CHUNK_HEADER.size)
            if len(chunk_data) != CHUNK_HEADER.size:
                raise RuntimeError(f"truncated sparse chunk header: {source}")
            chunk_type, _reserved, chunk_blocks, total_size = CHUNK_HEADER.unpack(chunk_data)
            if chunk_header_size > CHUNK_HEADER.size:
                input_stream.seek(chunk_header_size - CHUNK_HEADER.size, 1)
            data_size = total_size - chunk_header_size
            output_size = chunk_blocks * block_size
            if chunk_type == CHUNK_RAW:
                if data_size != output_size:
                    raise RuntimeError("invalid raw sparse chunk size")
                remaining = data_size
                while remaining:
                    data = input_stream.read(min(8 * 1024 * 1024, remaining))
                    if not data:
                        raise RuntimeError("truncated raw sparse chunk")
                    output.write(data)
                    remaining -= len(data)
            elif chunk_type == CHUNK_FILL:
                if data_size != 4:
                    raise RuntimeError("invalid fill sparse chunk size")
                fill = input_stream.read(4)
                block = fill * (block_size // 4)
                for _ in range(chunk_blocks):
                    output.write(block)
            elif chunk_type == CHUNK_DONT_CARE:
                if data_size:
                    raise RuntimeError("invalid don't-care sparse chunk size")
                output.seek(output_size, 1)
            elif chunk_type == CHUNK_CRC32:
                input_stream.seek(data_size, 1)
            else:
                raise RuntimeError(f"unknown sparse chunk type 0x{chunk_type:04x}")
            blocks_written += chunk_blocks
        if blocks_written != total_blocks:
            raise RuntimeError("sparse image block count mismatch")
        output.truncate(total_blocks * block_size)
    temporary.replace(destination)


def _extract_ext_paths(image: Path, output: Path, patterns: list[str]) -> None:
    seven_zip = shutil.which("7z") or shutil.which("7zz")
    if not seven_zip:
        raise RuntimeError("7-Zip is required to extract ext4 Pixel partitions")
    output.mkdir(parents=True, exist_ok=True)
    command = [seven_zip, "x", "-y", "-aoa", f"-o{output}", str(image), *patterns]
    subprocess.run(command, check=True)


def _baseband_from_android_info(path: Path) -> str | None:
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("require version-baseband="):
            return line.split("=", 1)[1].strip()
    return None


def extract_pixel_factory(factory_zip: Path, work_dir: Path) -> ExtractedPixelFirmware:
    work_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(factory_zip) as outer:
        inner_info = _find_member(
            outer,
            lambda name: name.rsplit("/", 1)[-1].startswith("image-") and name.endswith(".zip"),
            "inner image ZIP",
        )
        inner_zip = _extract_zip_member(outer, inner_info, work_dir / Path(inner_info.filename).name)

    partition_dir = work_dir / "partitions"
    with zipfile.ZipFile(inner_zip) as inner:
        product_info = _find_member(inner, lambda name: name == "product.img", "product.img")
        vendor_info = _find_member(inner, lambda name: name == "vendor.img", "vendor.img")
        info = _find_member(inner, lambda name: name == "android-info.txt", "android-info.txt")
        product = _extract_zip_member(inner, product_info, partition_dir / "product.img")
        vendor = _extract_zip_member(inner, vendor_info, partition_dir / "vendor.img")
        android_info = _extract_zip_member(inner, info, partition_dir / "android-info.txt")

    product = _materialize_ext_image(product, partition_dir)
    vendor = _materialize_ext_image(vendor, partition_dir)
    extracted = work_dir / "extracted"
    carrier_settings = extracted / "product" / "etc" / "CarrierSettings"
    if not (carrier_settings / "carrier_list.pb").exists():
        _extract_ext_paths(product, extracted / "product", ["etc/CarrierSettings/*"])
    if not (carrier_settings / "carrier_list.pb").is_file():
        raise RuntimeError("product image does not contain etc/CarrierSettings/carrier_list.pb")

    vendor_output = extracted / "vendor"
    mcfg_root = vendor_output / "rfs" / "msm" / "mpss" / "readonly" / "vendor" / "mbn" / "mcfg_sw"
    has_cached_mcfg = mcfg_root.exists() and any(mcfg_root.rglob("mcfg_sw.mbn"))
    if not has_cached_mcfg:
        _extract_ext_paths(
            vendor,
            vendor_output,
            ["rfs/msm/mpss/readonly/vendor/mbn/mcfg_sw/*", "build.prop"],
        )
    if not mcfg_root.exists() or not any(mcfg_root.rglob("mcfg_sw.mbn")):
        mcfg: Path | None = None
    else:
        mcfg = mcfg_root

    return ExtractedPixelFirmware(
        factory_zip=factory_zip,
        inner_zip=inner_zip,
        product_image=product,
        vendor_image=vendor,
        carrier_settings_dir=carrier_settings,
        mcfg_dir=mcfg,
        android_info=android_info,
        baseband_version=_baseband_from_android_info(android_info),
    )
