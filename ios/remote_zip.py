"""Locate and resume-download a stored member from a remote ZIP64 archive."""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import math
import os
import re
import struct
import sys
import time
import urllib.error
import urllib.request
import zlib
from dataclasses import dataclass
from pathlib import Path


EOCD = struct.Struct("<4s4H2LH")
ZIP64_LOCATOR = struct.Struct("<4sLQL")
ZIP64_EOCD = struct.Struct("<4sQ2H2L4Q")
CENTRAL_HEADER = struct.Struct("<4s6H3L5H2L")
LOCAL_HEADER = struct.Struct("<4s5H3L2H")
CONTENT_RANGE = re.compile(r"bytes ([0-9]+)-([0-9]+)/([0-9]+|\*)$")


@dataclass(frozen=True)
class RemoteZipMember:
    archive_url: str
    archive_size: int
    name: str
    compression_method: int
    compressed_size: int
    uncompressed_size: int
    crc32: int
    local_header_offset: int
    data_offset: int


@dataclass(frozen=True)
class DownloadResult:
    path: Path
    size: int
    crc32: int
    sha256: str


def _request(url: str, *, start: int, end: int, timeout: int = 120):
    request = urllib.request.Request(
        url,
        headers={
            "Range": f"bytes={start}-{end}",
            "User-Agent": "carrier-bundles-ios/0.1",
        },
    )
    response = urllib.request.urlopen(request, timeout=timeout)
    if response.status != 206:
        response.close()
        raise RuntimeError(f"server ignored byte range {start}-{end}: HTTP {response.status}")
    content_range = response.headers.get("Content-Range", "")
    match = CONTENT_RANGE.fullmatch(content_range)
    if not match or int(match.group(1)) != start or int(match.group(2)) != end:
        response.close()
        raise RuntimeError(f"unexpected Content-Range: {content_range!r}")
    return response


def _read_range(url: str, start: int, end: int) -> bytes:
    with _request(url, start=start, end=end) as response:
        data = response.read()
    expected = end - start + 1
    if len(data) != expected:
        raise RuntimeError(f"short byte range: expected {expected}, received {len(data)}")
    return data


def _archive_size(url: str) -> int:
    request = urllib.request.Request(
        url,
        method="HEAD",
        headers={"User-Agent": "carrier-bundles-ios/0.1"},
    )
    with urllib.request.urlopen(request, timeout=120) as response:
        value = response.headers.get("Content-Length")
    if value is None or not value.isdigit():
        raise RuntimeError("remote ZIP did not provide a valid Content-Length")
    return int(value)


def _zip64_value(extra: bytes, values: dict[str, int]) -> dict[str, int]:
    position = 0
    while position + 4 <= len(extra):
        field_id, field_size = struct.unpack_from("<HH", extra, position)
        data = extra[position + 4 : position + 4 + field_size]
        position += 4 + field_size
        if field_id != 0x0001:
            continue
        cursor = 0
        for key, marker, size, fmt in (
            ("uncompressed_size", 0xFFFFFFFF, 8, "<Q"),
            ("compressed_size", 0xFFFFFFFF, 8, "<Q"),
            ("local_header_offset", 0xFFFFFFFF, 8, "<Q"),
            ("disk", 0xFFFF, 4, "<L"),
        ):
            if values[key] != marker:
                continue
            if cursor + size > len(data):
                raise RuntimeError("truncated ZIP64 extended information field")
            values[key] = struct.unpack_from(fmt, data, cursor)[0]
            cursor += size
        return values
    return values


def locate_remote_member(url: str, member_name: str) -> RemoteZipMember:
    """Read the remote central directory and locate one exact member name."""

    archive_size = _archive_size(url)
    tail_size = min(1024 * 1024, archive_size)
    tail_start = archive_size - tail_size
    tail = _read_range(url, tail_start, archive_size - 1)
    eocd_position = tail.rfind(b"PK\x05\x06")
    if eocd_position < 0 or eocd_position + EOCD.size > len(tail):
        raise RuntimeError("ZIP end-of-central-directory record was not found")
    (
        _signature,
        _disk,
        _central_disk,
        _disk_entries,
        total_entries,
        central_size,
        central_offset,
        _comment_length,
    ) = EOCD.unpack_from(tail, eocd_position)

    if central_offset == 0xFFFFFFFF or central_size == 0xFFFFFFFF:
        locator_position = tail.rfind(b"PK\x06\x07", 0, eocd_position)
        if locator_position < 0:
            raise RuntimeError("ZIP64 locator was not found")
        _, _zip64_disk, zip64_offset, _disk_count = ZIP64_LOCATOR.unpack_from(
            tail, locator_position
        )
        record = _read_range(url, zip64_offset, zip64_offset + ZIP64_EOCD.size - 1)
        (
            signature,
            _record_size,
            _made_version,
            _needed_version,
            _current_disk,
            _directory_disk,
            _entries_on_disk,
            total_entries,
            central_size,
            central_offset,
        ) = ZIP64_EOCD.unpack(record)
        if signature != b"PK\x06\x06":
            raise RuntimeError("invalid ZIP64 end-of-central-directory signature")

    directory = _read_range(
        url, central_offset, central_offset + central_size - 1
    )
    position = 0
    for _ in range(total_entries):
        if position + CENTRAL_HEADER.size > len(directory):
            raise RuntimeError("truncated ZIP central directory")
        values = CENTRAL_HEADER.unpack_from(directory, position)
        if values[0] != b"PK\x01\x02":
            raise RuntimeError("invalid ZIP central-directory signature")
        (
            _signature,
            _made_version,
            _needed_version,
            flags,
            method,
            _modified_time,
            _modified_date,
            crc32,
            compressed_size,
            uncompressed_size,
            name_length,
            extra_length,
            comment_length,
            disk,
            _internal_attributes,
            _external_attributes,
            local_header_offset,
        ) = values
        name_start = position + CENTRAL_HEADER.size
        name_data = directory[name_start : name_start + name_length]
        extra = directory[
            name_start + name_length : name_start + name_length + extra_length
        ]
        encoding = "utf-8" if flags & 0x0800 else "cp437"
        name = name_data.decode(encoding)
        sizes = _zip64_value(
            extra,
            {
                "uncompressed_size": uncompressed_size,
                "compressed_size": compressed_size,
                "local_header_offset": local_header_offset,
                "disk": disk,
            },
        )
        position += CENTRAL_HEADER.size + name_length + extra_length + comment_length
        if name != member_name:
            continue
        local = _read_range(
            url,
            sizes["local_header_offset"],
            sizes["local_header_offset"] + LOCAL_HEADER.size - 1,
        )
        local_values = LOCAL_HEADER.unpack(local)
        if local_values[0] != b"PK\x03\x04":
            raise RuntimeError("invalid ZIP local-header signature")
        local_name_length, local_extra_length = local_values[-2:]
        data_offset = (
            sizes["local_header_offset"]
            + LOCAL_HEADER.size
            + local_name_length
            + local_extra_length
        )
        return RemoteZipMember(
            archive_url=url,
            archive_size=archive_size,
            name=name,
            compression_method=method,
            compressed_size=sizes["compressed_size"],
            uncompressed_size=sizes["uncompressed_size"],
            crc32=crc32,
            local_header_offset=sizes["local_header_offset"],
            data_offset=data_offset,
        )
    raise FileNotFoundError(f"remote ZIP member was not found: {member_name}")


def _download_part(
    member: RemoteZipMember,
    range_dir: Path,
    index: int,
    chunk_size: int,
    retries: int,
) -> Path:
    offset = index * chunk_size
    wanted = min(chunk_size, member.compressed_size - offset)
    part = range_dir / f"part-{index:02d}"
    if part.exists() and part.stat().st_size > wanted:
        raise RuntimeError(f"range part exceeds expected size: {part}")
    failures = 0
    while not part.exists() or part.stat().st_size < wanted:
        current = part.stat().st_size if part.exists() else 0
        start = member.data_offset + offset + current
        end = member.data_offset + offset + wanted - 1
        try:
            with _request(member.archive_url, start=start, end=end) as response, part.open(
                "ab"
            ) as output:
                remaining = wanted - current
                while remaining:
                    block = response.read(min(4 * 1024 * 1024, remaining))
                    if not block:
                        raise RuntimeError(
                            f"short response for part {index}: {remaining} bytes missing"
                        )
                    output.write(block)
                    remaining -= len(block)
            failures = 0
        except (OSError, RuntimeError, urllib.error.URLError) as error:
            failures += 1
            if failures > retries:
                raise RuntimeError(f"cannot download ZIP range part {index}: {error}") from error
            time.sleep(min(2**failures, 15))
    print(f"remote ZIP part {index} complete: {wanted} bytes", file=sys.stderr)
    return part


def _checksums(path: Path) -> tuple[int, str]:
    crc32 = 0
    sha256 = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            crc32 = zlib.crc32(block, crc32)
            sha256.update(block)
    return crc32 & 0xFFFFFFFF, sha256.hexdigest()


def download_remote_member(
    member: RemoteZipMember,
    destination: Path,
    *,
    range_dir: Path | None = None,
    chunk_size: int = 512 * 1024 * 1024,
    workers: int = 8,
    retries: int = 20,
) -> DownloadResult:
    """Download a stored member with resumable ranges and verify its ZIP CRC."""

    if member.compression_method != 0:
        raise ValueError("only stored remote ZIP members can be downloaded directly")
    if member.compressed_size != member.uncompressed_size:
        raise ValueError("stored ZIP member has inconsistent sizes")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file() and destination.stat().st_size == member.uncompressed_size:
        crc32, sha256 = _checksums(destination)
        if crc32 == member.crc32:
            return DownloadResult(destination, member.uncompressed_size, crc32, sha256)

    ranges = range_dir or destination.parent / "ranges"
    ranges.mkdir(parents=True, exist_ok=True)
    count = math.ceil(member.compressed_size / chunk_size)
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        parts = list(
            executor.map(
                lambda index: _download_part(member, ranges, index, chunk_size, retries),
                range(count),
            )
        )

    assembling = destination.with_name(f"{destination.name}.assembling")
    crc32 = 0
    sha256 = hashlib.sha256()
    size = 0
    with assembling.open("wb") as output:
        for part in parts:
            with part.open("rb") as source:
                for block in iter(lambda: source.read(8 * 1024 * 1024), b""):
                    output.write(block)
                    size += len(block)
                    crc32 = zlib.crc32(block, crc32)
                    sha256.update(block)
    crc32 &= 0xFFFFFFFF
    if size != member.uncompressed_size:
        raise RuntimeError(f"assembled member size mismatch: {size}")
    if crc32 != member.crc32:
        raise RuntimeError(
            f"assembled member CRC mismatch: expected {member.crc32:08x}, got {crc32:08x}"
        )
    os.replace(assembling, destination)
    return DownloadResult(destination, size, crc32, sha256.hexdigest())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    parser.add_argument("member")
    parser.add_argument("destination", type=Path)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--chunk-size", type=int, default=512 * 1024 * 1024)
    parser.add_argument(
        "--range-dir",
        type=Path,
        help="explicit resumable range directory (defaults beside destination)",
    )
    args = parser.parse_args()
    try:
        member = locate_remote_member(args.url, args.member)
        result = download_remote_member(
            member,
            args.destination,
            range_dir=args.range_dir,
            workers=args.workers,
            chunk_size=args.chunk_size,
        )
    except (OSError, RuntimeError, ValueError) as error:
        parser.exit(1, f"remote ZIP extraction failed: {error}\n")
    print(
        f"downloaded {member.name}: {result.size} bytes; "
        f"CRC32 {result.crc32:08x}; SHA-256 {result.sha256}"
    )


if __name__ == "__main__":
    main()
