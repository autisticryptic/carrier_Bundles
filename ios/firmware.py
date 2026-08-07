"""Acquire and unpack public static configuration from an Apple IPSW."""

from __future__ import annotations

import contextlib
import hashlib
import os
import shutil
import stat
import subprocess
import tarfile
import time
import urllib.request
from collections.abc import Iterator
from pathlib import Path

from .remote_zip import download_remote_member, locate_remote_member
from .sources import IPSWArtifact, inspect_build_manifest


IPSW_VERSION = "3.1.707"
IPSW_ARCHIVE_SHA256 = (
    "002113c7b9eaf4d06d5bb77dcbeb809f9b942ff18ba9fe906f3a8d2aab12df00"
)
IPSW_ARCHIVE_URL = (
    "https://github.com/blacktop/ipsw/releases/download/v3.1.707/"
    "ipsw_3.1.707_linux_x86_64.tar.gz"
)
APFS_FUSE_REPOSITORY = "https://github.com/sgan81/apfs-fuse.git"
APFS_FUSE_COMMIT = "66b86bd525e8cb90f9012543be89b1f092b75cf3"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _download(url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(f"{destination.name}.part")
    request = urllib.request.Request(url, headers={"User-Agent": "carrier-bundles/0.1"})
    with urllib.request.urlopen(request, timeout=120) as response, partial.open("wb") as out:
        shutil.copyfileobj(response, out, length=4 * 1024 * 1024)
    partial.replace(destination)


def ensure_ipsw_tool(tool_root: Path) -> Path:
    """Install the pinned blacktop/ipsw release into the build cache."""

    install_dir = tool_root / "ipsw" / f"v{IPSW_VERSION}"
    executable = install_dir / "ipsw"
    if executable.is_file():
        version = subprocess.run(
            [str(executable), "version"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        if f"Version: {IPSW_VERSION}" in version:
            return executable

    archive = install_dir / "ipsw.tar.gz"
    if not archive.is_file() or sha256_file(archive) != IPSW_ARCHIVE_SHA256:
        _download(IPSW_ARCHIVE_URL, archive)
    actual = sha256_file(archive)
    if actual != IPSW_ARCHIVE_SHA256:
        raise ValueError(
            f"blacktop/ipsw archive SHA-256 mismatch: expected "
            f"{IPSW_ARCHIVE_SHA256}, got {actual}"
        )
    install_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:gz") as package:
        package.extractall(install_dir, filter="data")
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    return executable


def ensure_apfs_fuse(tool_root: Path) -> Path:
    """Build the pinned read-only APFS FUSE implementation when needed."""

    checkout = tool_root / "apfs-fuse"
    executable = checkout / "build" / "apfs-fuse"
    if executable.is_file():
        return executable
    if not checkout.exists():
        subprocess.run(
            ["git", "clone", "--recursive", APFS_FUSE_REPOSITORY, str(checkout)],
            check=True,
        )
    subprocess.run(["git", "-C", str(checkout), "fetch", "origin", APFS_FUSE_COMMIT], check=True)
    subprocess.run(["git", "-C", str(checkout), "checkout", "--detach", APFS_FUSE_COMMIT], check=True)
    subprocess.run(
        ["git", "-C", str(checkout), "submodule", "update", "--init", "--recursive"],
        check=True,
    )
    build = checkout / "build"
    subprocess.run(["cmake", "-S", str(checkout), "-B", str(build)], check=True)
    subprocess.run(["cmake", "--build", str(build), "-j", str(os.cpu_count() or 2)], check=True)
    return executable


def extract_outer_files(ipsw: Path, artifact: IPSWArtifact, output: Path) -> IPSWArtifact:
    """Extract metadata and the baseband artifact from a local IPSW."""

    tool = ensure_ipsw_tool(output.parents[2] / "tools")
    pattern = r"(^|/)(BuildManifest|Restore|SystemVersion)\.plist$|Firmware/.*\.bbfw$|Firmware/.*\.Release\.plist$"
    subprocess.run(
        [str(tool), "extract", "--pattern", pattern, "--output", str(output), str(ipsw)],
        check=True,
    )
    manifests = list(output.rglob("BuildManifest.plist"))
    if len(manifests) != 1:
        raise ValueError(f"expected one BuildManifest.plist, found {len(manifests)}")
    return inspect_build_manifest(manifests[0], artifact)


def extract_remote_outer_files(
    tool: Path, artifact: IPSWArtifact, output: Path
) -> IPSWArtifact:
    pattern = r"(^|/)(BuildManifest|Restore|SystemVersion)\.plist$|Firmware/.*\.bbfw$|Firmware/.*\.Release\.plist$"
    subprocess.run(
        [
            str(tool),
            "extract",
            "--remote",
            "--pattern",
            pattern,
            "--output",
            str(output),
            artifact.url,
        ],
        check=True,
    )
    manifests = list(output.rglob("BuildManifest.plist"))
    if len(manifests) != 1:
        raise ValueError(f"expected one BuildManifest.plist, found {len(manifests)}")
    return inspect_build_manifest(manifests[0], artifact)


def extract_file_system_aea(
    tool: Path,
    artifact: IPSWArtifact,
    output: Path,
    local_ipsw: Path | None = None,
    *,
    workers: int = 8,
) -> Path:
    if not artifact.file_system_path:
        raise ValueError("filesystem path was not resolved from BuildManifest")
    if local_ipsw is None:
        member = locate_remote_member(artifact.url, artifact.file_system_path)
        destination = (
            output
            / f"{artifact.build_id}__{artifact.product_type}"
            / Path(artifact.file_system_path).name
        )
        return download_remote_member(
            member,
            destination,
            range_dir=output / "ranges",
            workers=workers,
        ).path

    arguments = [str(tool), "extract"]
    arguments.extend(("--dmg", "fs", "--output", str(output)))
    arguments.append(str(local_ipsw))
    subprocess.run(arguments, check=True)
    matches = list(output.rglob(Path(artifact.file_system_path).name))
    if len(matches) != 1:
        raise ValueError(f"expected one filesystem AEA, found {len(matches)}")
    return matches[0]


def decrypt_aea(tool: Path, archive: Path, output: Path) -> Path:
    output.mkdir(parents=True, exist_ok=True)
    expected = output / archive.name.removesuffix(".aea")
    if expected.is_file():
        return expected
    subprocess.run([str(tool), "fw", "aea", "--output", str(output), str(archive)], check=True)
    if not expected.is_file():
        matches = list(output.glob("*.dmg"))
        if len(matches) != 1:
            raise ValueError(f"AEA decryption produced {len(matches)} DMG files")
        return matches[0]
    return expected


@contextlib.contextmanager
def mounted_apfs(apfs_fuse: Path, image: Path, mountpoint: Path) -> Iterator[Path]:
    """Mount an APFS image through FUSE and always unmount it afterwards."""

    mountpoint.mkdir(parents=True, exist_ok=True)
    subprocess.run([str(apfs_fuse), str(image), str(mountpoint)], check=True)
    for _ in range(100):
        if os.path.ismount(mountpoint):
            break
        time.sleep(0.1)
    else:
        raise RuntimeError(f"APFS image did not mount at {mountpoint}")
    try:
        # apfs-fuse exposes the selected volume below a synthetic ``root``
        # directory, while other FUSE implementations expose it directly.
        volume_root = mountpoint / "root"
        yield volume_root if volume_root.is_dir() else mountpoint
    finally:
        subprocess.run(["fusermount3", "-u", str(mountpoint)], check=True)


def export_carrier_bundles(mountpoint: Path, destination: Path) -> list[Path]:
    """Copy only public Carrier/Country Bundle trees from a mounted filesystem."""

    relative_roots = (
        Path("System/Library/Carrier Bundles/iPhone"),
        Path("System/Library/CountryBundles/iPhone"),
    )
    exported: list[Path] = []
    destination.mkdir(parents=True, exist_ok=True)
    for relative in relative_roots:
        source = mountpoint / relative
        if not source.is_dir():
            continue
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, target, symlinks=True, dirs_exist_ok=True)
        exported.append(target)
    if not exported:
        raise FileNotFoundError("no iPhone Carrier Bundle or Country Bundle tree found")
    return exported
