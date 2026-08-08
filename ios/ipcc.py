"""Discover and acquire Apple-published IPCC carrier bundle archives."""

from __future__ import annotations

import gzip
import hashlib
import json
import plistlib
import re
import shutil
import urllib.request
import zipfile
from dataclasses import asdict, dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any, Iterable
from urllib.parse import urlparse


IPCC_INDEX_URL = "https://itunes.com/version"
USER_AGENT = "carrier-bundles-ipcc/0.1"
APPLE_IPCC_HOSTS = {
    "updates.cdn-apple.com",
    "updates-http.cdn-apple.com",
    "appldnld.apple.com",
    "appldnld.apple.com.edgesuite.net",
}
MAX_IPCC_FILES = 20_000
MAX_IPCC_UNCOMPRESSED_BYTES = 512 * 1024 * 1024


@dataclass(frozen=True)
class IPCCArtifact:
    bundle_id: str
    product: str
    version: str | None
    build_version: str | None
    url: str
    apple_digest_algorithm: str | None
    apple_digest_hex: str | None
    index_path: tuple[str, ...]
    sha256: str | None = None
    archive_path: str | None = None
    extracted_bundles: tuple[str, ...] = ()

    @property
    def filename(self) -> str:
        return Path(urlparse(self.url).path).name


def _product_for(path: tuple[str, ...], url: str) -> str:
    values = " ".join((*path, Path(urlparse(url).path).name)).casefold()
    for product in ("iphone", "ipad", "watch", "ipod"):
        if product in values:
            return product
    return "unknown"


def _digest_fields(value: Any) -> tuple[str | None, str | None]:
    if not isinstance(value, bytes):
        return None, None
    algorithms = {20: "sha1", 32: "sha256", 48: "sha384", 64: "sha512"}
    return algorithms.get(len(value)), value.hex()


def _bundle_id(container: dict[str, Any], path: tuple[str, ...], url: str) -> str:
    for key in ("BundleID", "BundleName"):
        value = container.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    filename = Path(urlparse(url).path).stem
    return re.sub(r"_(?:iPhone|iPad|Watch|iPod)$", "", filename, flags=re.I)


def parse_ipcc_index(data: bytes) -> list[IPCCArtifact]:
    """Parse Apple's plist index, including its HTTP-level gzip payload."""

    if data.startswith(b"\x1f\x8b"):
        data = gzip.decompress(data)
    document = plistlib.loads(data)
    artifacts: list[IPCCArtifact] = []

    def walk(value: Any, path: tuple[str, ...] = ()) -> None:
        if isinstance(value, dict):
            url = value.get("BundleURL")
            if isinstance(url, str) and urlparse(url).path.casefold().endswith(".ipcc"):
                algorithm, digest = _digest_fields(value.get("Digest"))
                artifacts.append(
                    IPCCArtifact(
                        bundle_id=_bundle_id(value, path, url),
                        product=_product_for(path, url),
                        version=(
                            str(value["BundleVersion"])
                            if value.get("BundleVersion") is not None
                            else None
                        ),
                        build_version=(
                            str(value["BuildVersion"])
                            if value.get("BuildVersion") is not None
                            else None
                        ),
                        url=url,
                        apple_digest_algorithm=algorithm,
                        apple_digest_hex=digest,
                        index_path=path,
                    )
                )
            for key, item in value.items():
                walk(item, (*path, str(key)))
        elif isinstance(value, list):
            for index, item in enumerate(value):
                walk(item, (*path, str(index)))

    walk(document)
    by_url: dict[str, IPCCArtifact] = {}
    for artifact in artifacts:
        current = by_url.get(artifact.url)
        if current is None or (current.apple_digest_hex is None and artifact.apple_digest_hex):
            by_url[artifact.url] = artifact
    return sorted(by_url.values(), key=lambda item: (item.product, item.bundle_id, item.url))


def fetch_ipcc_index(url: str = IPCC_INDEX_URL) -> tuple[bytes, list[IPCCArtifact]]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=120) as response:
        data = response.read()
    return data, parse_ipcc_index(data)


def _version_key(value: str | None) -> tuple[tuple[int, int | str], ...]:
    if not value:
        return ()
    return tuple(
        (0, int(token)) if token.isdigit() else (1, token.casefold())
        for token in re.findall(r"[0-9]+|[^0-9]+", value)
    )


def select_artifacts(
    artifacts: Iterable[IPCCArtifact],
    *,
    product: str = "iphone",
    queries: Iterable[str] = (),
    latest_only: bool = True,
) -> list[IPCCArtifact]:
    needles = tuple(query.casefold() for query in queries if query.strip())
    selected = []
    for artifact in artifacts:
        if product != "all" and artifact.product != product.casefold():
            continue
        searchable = " ".join(
            (artifact.bundle_id, artifact.url, *artifact.index_path)
        ).casefold()
        if needles and not any(needle in searchable for needle in needles):
            continue
        selected.append(artifact)
    if not latest_only:
        return selected
    newest: dict[tuple[str, str], IPCCArtifact] = {}
    for artifact in selected:
        key = (artifact.product, artifact.bundle_id.casefold())
        current = newest.get(key)
        rank = (_version_key(artifact.version or artifact.build_version), artifact.url)
        current_rank = (
            _version_key(current.version or current.build_version),
            current.url,
        ) if current else None
        if current_rank is None or rank > current_rank:
            newest[key] = artifact
    return sorted(newest.values(), key=lambda item: (item.product, item.bundle_id.casefold()))


def _validate_ipcc_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(f"unsupported IPCC URL scheme: {url}")
    if parsed.hostname not in APPLE_IPCC_HOSTS:
        raise ValueError(f"IPCC URL is not on an allowed Apple host: {url}")
    if not parsed.path.casefold().endswith(".ipcc"):
        raise ValueError(f"IPCC URL does not end in .ipcc: {url}")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def download_ipcc(artifact: IPCCArtifact, destination: Path) -> IPCCArtifact:
    _validate_ipcc_url(artifact.url)
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(f"{destination.name}.part")
    if not destination.is_file():
        request = urllib.request.Request(artifact.url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=120) as response, partial.open("wb") as output:
            shutil.copyfileobj(response, output, length=4 * 1024 * 1024)
        partial.replace(destination)
    if artifact.apple_digest_algorithm and artifact.apple_digest_hex:
        digest = hashlib.new(artifact.apple_digest_algorithm)
        with destination.open("rb") as stream:
            for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
                digest.update(block)
        if digest.hexdigest().casefold() != artifact.apple_digest_hex.casefold():
            destination.unlink(missing_ok=True)
            raise ValueError(
                f"Apple {artifact.apple_digest_algorithm} digest mismatch for {destination}"
            )
    return replace(
        artifact,
        sha256=_sha256_file(destination),
        archive_path=str(destination),
    )


def _safe_zip_members(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    members = []
    total_size = 0
    for info in archive.infolist():
        path = PurePosixPath(info.filename)
        mode = info.external_attr >> 16
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"unsafe path in IPCC: {info.filename}")
        if mode & 0o170000 == 0o120000:
            raise ValueError(f"symbolic link is not allowed in IPCC: {info.filename}")
        if not path.parts or path.parts[0] != "Payload":
            continue
        total_size += info.file_size
        if len(members) >= MAX_IPCC_FILES:
            raise ValueError("IPCC contains too many files")
        if total_size > MAX_IPCC_UNCOMPRESSED_BYTES:
            raise ValueError("IPCC uncompressed payload is too large")
        members.append(info)
    return members


def extract_ipcc(artifact: IPCCArtifact, destination: Path) -> IPCCArtifact:
    if not artifact.archive_path:
        raise ValueError("IPCC artifact has not been downloaded")
    archive_path = Path(artifact.archive_path)
    extracted: list[str] = []
    with zipfile.ZipFile(archive_path) as archive:
        members = _safe_zip_members(archive)
        bundle_roots = sorted(
            {
                PurePosixPath(info.filename).parts[1]
                for info in members
                if len(PurePosixPath(info.filename).parts) >= 2
                and PurePosixPath(info.filename).parts[1].endswith(".bundle")
            }
        )
        if not bundle_roots:
            raise ValueError(f"IPCC contains no Payload/*.bundle: {archive_path}")
        relative_root = (
            Path("System/Library/CountryBundles/iPhone")
            if artifact.index_path[:2] == ("CountryBundles", "iPhone")
            else Path("System/Library/Carrier Bundles/iPhone")
        )
        for bundle in bundle_roots:
            target_root = destination / relative_root / bundle
            if target_root.exists():
                shutil.rmtree(target_root)
            for info in members:
                parts = PurePosixPath(info.filename).parts
                if len(parts) < 2 or parts[1] != bundle or info.is_dir():
                    continue
                relative = Path(*parts[2:])
                target = target_root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
            extracted.append(str(relative_root / bundle))
    return replace(artifact, extracted_bundles=tuple(extracted))


def write_manifest(
    path: Path,
    *,
    index_url: str,
    index_sha256: str,
    artifacts: Iterable[IPCCArtifact],
    errors: Iterable[dict[str, str]] = (),
) -> None:
    document = {
        "schema": "carrier-bundles-ipcc-manifest-v1",
        "index_url": index_url,
        "index_sha256": index_sha256,
        "artifacts": [asdict(artifact) for artifact in artifacts],
        "errors": list(errors),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
