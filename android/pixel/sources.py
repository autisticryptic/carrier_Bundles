"""Resolve Pixel factory image metadata from Google's official download page."""

from __future__ import annotations

import re
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser


FACTORY_IMAGES_URL = "https://developers.google.com/android/images?hl=en"
FACTORY_TERMS_COOKIE = "devsite_wall_acks=nexus-image-tos"


@dataclass(frozen=True)
class FactoryArtifact:
    device: str
    device_name: str
    build_id: str
    os_version: str
    description: str
    url: str
    sha256: str

    @property
    def filename(self) -> str:
        return self.url.rsplit("/", 1)[-1]


class _FactoryPageParser(HTMLParser):
    def __init__(self, device: str) -> None:
        super().__init__(convert_charrefs=True)
        self.device = device
        self.in_device_section = False
        self.in_device_heading = False
        self.device_heading_parts: list[str] = []
        self.device_name = device
        self.in_row = False
        self.in_cell = False
        self.cell_parts: list[str] = []
        self.cells: list[str] = []
        self.link: str | None = None
        self.artifacts: list[FactoryArtifact] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "h2":
            self.in_device_section = attributes.get("id") == self.device
            self.in_device_heading = self.in_device_section
            self.device_heading_parts = []
        elif tag == "tr" and self.in_device_section:
            self.in_row = True
            self.cells = []
            self.link = None
        elif tag == "td" and self.in_row:
            self.in_cell = True
            self.cell_parts = []
        elif tag == "a" and self.in_row:
            href = attributes.get("href")
            if href and re.search(rf"/{re.escape(self.device)}-.+-factory-.+\.zip$", href):
                self.link = href

    def handle_data(self, data: str) -> None:
        if self.in_device_heading:
            self.device_heading_parts.append(data)
        if self.in_cell:
            self.cell_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "h2" and self.in_device_heading:
            heading = " ".join("".join(self.device_heading_parts).split())
            if heading:
                codename_heading = re.fullmatch(
                    rf'["\']?{re.escape(self.device)}["\']?\s+for\s+(.+)',
                    heading,
                    flags=re.IGNORECASE,
                )
                self.device_name = (
                    codename_heading.group(1) if codename_heading else heading
                )
            self.in_device_heading = False
        elif tag == "td" and self.in_cell:
            self.cells.append(" ".join("".join(self.cell_parts).split()))
            self.in_cell = False
        elif tag == "tr" and self.in_row:
            self._finish_row()
            self.in_row = False

    def _finish_row(self) -> None:
        if not self.link:
            return
        description = self.cells[0] if self.cells else ""
        build_match = re.search(r"\(([^,()]+),", description)
        version_match = re.match(r"\s*([0-9]+(?:\.[0-9]+)*)", description)
        hashes = [cell.lower() for cell in self.cells if re.fullmatch(r"[0-9a-fA-F]{64}", cell)]
        if not build_match or not version_match or len(hashes) != 1:
            return
        self.artifacts.append(
            FactoryArtifact(
                device=self.device,
                device_name=self.device_name,
                build_id=build_match.group(1),
                os_version=version_match.group(1),
                description=description,
                url=self.link,
                sha256=hashes[0],
            )
        )


def parse_factory_page(html: str, device: str) -> list[FactoryArtifact]:
    parser = _FactoryPageParser(device)
    parser.feed(html)
    return parser.artifacts


def resolve_factory_artifact(
    device: str,
    build_id: str = "latest",
    *,
    accept_google_terms: bool,
) -> FactoryArtifact:
    if not accept_google_terms:
        raise ValueError(
            "Google requires acknowledgement of the Pixel factory image terms; "
            "pass --accept-google-terms after reviewing the official page"
        )
    request = urllib.request.Request(
        FACTORY_IMAGES_URL,
        headers={
            "Cookie": FACTORY_TERMS_COOKIE,
            "User-Agent": "carrier-bundles-pixel-extractor/0.1",
        },
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        html = response.read().decode("utf-8")
    artifacts = parse_factory_page(html, device)
    if not artifacts:
        raise RuntimeError(f"no factory images found for Pixel device {device!r}")
    if build_id.lower() == "latest":
        return artifacts[-1]
    for artifact in artifacts:
        if artifact.build_id.casefold() == build_id.casefold():
            return artifact
    available = ", ".join(item.build_id for item in artifacts[-5:])
    raise RuntimeError(
        f"build {build_id!r} was not found for {device}; latest builds: {available}"
    )
