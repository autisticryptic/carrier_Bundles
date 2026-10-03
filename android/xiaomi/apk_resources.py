"""Read carrier XML inside APK ZIPs without extracting or executing their contents.

``iter_apk_xml(path)`` returns documents with the original APK path/hash, ZIP
member name/hash, and an ElementTree root.  The tree retains carrier_config
selector scopes, namespaces, scalar tags, array items and text.  Binary XML
attribute encodings are also available in ``document.attribute_types`` keyed
by Element, then attribute name.  No filename (notably carrierid names) is
interpreted as a PLMN, and no resource-reference or overlay resolution occurs.

The decoder implements the Android ResXMLTree/string-pool chunk format, not
``aapt``'s presentation format. Unsupported value types fail closed instead of
being mistaken for strings or carrier values. Limits apply before decompression
and while parsing; embedded paths are never used as filesystem destinations.
"""

from __future__ import annotations

import hashlib
import math
import re
import stat
import struct
import warnings
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

MAX_APK_BYTES = 128 * 1024 * 1024
MAX_ZIP_ENTRIES = 20_000
MAX_XML_BYTES = 8 * 1024 * 1024
MAX_TOTAL_XML_BYTES = 32 * 1024 * 1024
MAX_XML_DOCUMENTS = 4096
MAX_XML_NODES = 100_000
MAX_XML_DEPTH = 128
MAX_ATTRIBUTES = 4096
MAX_POOL_STRINGS = 100_000
MAX_DECODED_CHARS = 16 * 1024 * 1024
_NO_INDEX = 0xFFFFFFFF
_XML_MEMBER_RE = re.compile(r"(?:assets/.+|res/xml(?:-[A-Za-z0-9+_-]+)?/[^/]+)\.xml$")


class ApkXmlError(ValueError):
    """An unsafe, malformed, oversized, or unsupported APK/XML input."""


class ApkXmlWarning(UserWarning):
    """A rejected XML member; the warning includes its APK/member provenance."""


class UnsupportedApkXmlError(ApkXmlError):
    """A binary XML feature cannot be faithfully decoded without resources.arsc."""


@dataclass(frozen=True)
class AndroidAttribute:
    value_type: int
    data: int
    raw_value: str | None


@dataclass(frozen=True)
class ApkXmlDocument:
    member: str
    root: ET.Element
    apk_path: Path
    apk_sha256: str
    member_sha256: str
    attribute_types: dict[ET.Element, dict[str, AndroidAttribute]] = field(default_factory=dict)


class _BoundedTreeBuilder(ET.TreeBuilder):
    def __init__(self) -> None:
        super().__init__()
        self.depth = 0
        self.nodes = 0
        self.characters = 0

    def _count_characters(self, count: int) -> None:
        self.characters += count
        if self.characters > MAX_DECODED_CHARS:
            raise ApkXmlError("decoded XML character limit exceeded")

    def data(self, data: str) -> None:
        self._count_characters(len(data))
        super().data(data)

    def start(self, tag: str, attrs: dict[str, str]) -> ET.Element:
        self.depth += 1
        self.nodes += 1
        if self.depth > MAX_XML_DEPTH or self.nodes > MAX_XML_NODES:
            raise ApkXmlError("XML tree limit exceeded")
        if len(attrs) > MAX_ATTRIBUTES:
            raise ApkXmlError("XML attribute limit exceeded")
        self._count_characters(len(tag) + sum(len(key) + len(value) for key, value in attrs.items()))
        return super().start(tag, attrs)

    def end(self, tag: str) -> ET.Element:
        result = super().end(tag)
        self.depth -= 1
        return result

    def doctype(self, name: str, pubid: str | None, system: str | None) -> None:
        raise ApkXmlError("XML DTD/entity declarations are not supported")


def _chunk(data: bytes, offset: int, boundary: int) -> tuple[int, int, int]:
    if offset % 4 or offset + 8 > boundary:
        raise ApkXmlError("truncated or unaligned Android XML chunk header")
    kind, header_size, size = struct.unpack_from("<HHI", data, offset)
    if header_size < 8 or header_size % 4 or size < header_size or size % 4:
        raise ApkXmlError("invalid Android XML chunk size")
    end = offset + size
    if end > boundary:
        raise ApkXmlError("Android XML chunk exceeds parent bounds")
    return kind, header_size, end


def _length(data: bytes, offset: int, end: int, *, utf8: bool) -> tuple[int, int]:
    width, marker, mask = (1, 0x80, 0x7F) if utf8 else (2, 0x8000, 0x7FFF)
    if offset + width > end:
        raise ApkXmlError("truncated string-pool length")
    value = int.from_bytes(data[offset:offset + width], "little")
    offset += width
    if value & marker:
        if offset + width > end:
            raise ApkXmlError("truncated extended string-pool length")
        value = ((value & mask) << (width * 8)) | int.from_bytes(data[offset:offset + width], "little")
        offset += width
    return value, offset


def _string_pool(data: bytes, start: int, header_size: int, end: int) -> list[str]:
    if header_size != 28:
        raise ApkXmlError("unsupported string-pool header")
    count, styles, flags, strings_start, styles_start = struct.unpack_from("<IIIII", data, start + 8)
    if count > MAX_POOL_STRINGS:
        raise ApkXmlError("string-pool count limit exceeded")
    if styles or styles_start or flags & ~0x101:
        raise UnsupportedApkXmlError("styled/unknown Android XML string pool")
    table_end = start + header_size + 4 * count
    strings_base = start + strings_start
    if table_end > end or not table_end <= strings_base <= end:
        raise ApkXmlError("string-pool offsets exceed chunk bounds")
    utf8 = bool(flags & 0x100)
    result: list[str] = []
    cache: dict[int, str] = {}
    chars = 0
    for index in range(count):
        relative = struct.unpack_from("<I", data, start + header_size + 4 * index)[0]
        offset = strings_base + relative
        if offset >= end or (not utf8 and relative % 2):
            raise ApkXmlError("string-pool string offset out of bounds")
        if relative not in cache:
            units, offset = _length(data, offset, end, utf8=utf8)
            if utf8:
                size, offset = _length(data, offset, end, utf8=True)
                terminator = 1
            else:
                size, terminator = units * 2, 2
            if offset + size + terminator > end or any(data[offset + size:offset + size + terminator]):
                raise ApkXmlError("truncated or unterminated string-pool string")
            try:
                text = data[offset:offset + size].decode("utf-8" if utf8 else "utf-16-le")
            except UnicodeError as exc:
                raise ApkXmlError("invalid string-pool encoding") from exc
            if len(text.encode("utf-16-le")) // 2 != units:
                raise ApkXmlError("string-pool character length mismatch")
            if any(ord(char) < 32 and char not in "\t\r\n" for char in text):
                raise ApkXmlError("invalid XML control character in string pool")
            cache[relative] = text
        text = cache[relative]
        chars += len(text)
        if chars > MAX_DECODED_CHARS:
            raise ApkXmlError("decoded string-pool limit exceeded")
        result.append(text)
    return result


def _string(strings: list[str], index: int) -> str:
    if index >= len(strings):
        raise ApkXmlError("Android XML string index out of bounds")
    return strings[index]


def _typed_value(data: bytes, offset: int, strings: list[str]) -> tuple[str, int, int]:
    size, reserved, kind, value = struct.unpack_from("<HBBI", data, offset)
    if size != 8 or reserved:
        raise ApkXmlError("invalid Android typed-value header")
    if kind == 3:  # TYPE_STRING
        text = _string(strings, value)
    elif kind == 0x10:  # TYPE_INT_DEC: Android stores a signed int32.
        text = str(value if value < 0x80000000 else value - 0x100000000)
    elif kind == 0x11:  # TYPE_INT_HEX: retain its explicit hexadecimal notation.
        text = f"0x{value:x}"
    elif kind == 0x12:  # TYPE_INT_BOOLEAN
        if value not in (0, 1, 0xFFFFFFFF):
            raise ApkXmlError("noncanonical Android boolean value")
        text = "true" if value else "false"
    elif kind == 4:  # TYPE_FLOAT
        number = struct.unpack("<f", struct.pack("<I", value))[0]
        if not math.isfinite(number):
            raise UnsupportedApkXmlError("nonfinite Android float")
        text = repr(number)
    else:
        # References, attributes, dimensions, fractions, colors and null cannot
        # be silently reinterpreted as carrier strings/integers.
        raise UnsupportedApkXmlError(f"unsupported Android XML value type 0x{kind:02x}")
    return text, kind, value


def _binary_xml(data: bytes) -> tuple[ET.Element, dict[ET.Element, dict[str, AndroidAttribute]]]:
    kind, header_size, end = _chunk(data, 0, len(data))
    if kind != 3 or header_size != 8 or end != len(data):
        raise ApkXmlError("invalid Android XML document header")
    strings: list[str] | None = None
    mapped = False
    tree = _BoundedTreeBuilder()
    stack: list[str] = []
    namespaces: list[tuple[int, int, int]] = []
    attribute_types: dict[ET.Element, dict[str, AndroidAttribute]] = {}
    root_seen = False
    offset = header_size

    def qname(namespace: int, name: int) -> str:
        assert strings is not None
        local = _string(strings, name)
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", local):
            raise UnsupportedApkXmlError("unsupported XML element/attribute name")
        if namespace == _NO_INDEX:
            return local
        uri = _string(strings, namespace)
        if not uri or not any(item[1] == namespace for item in namespaces):
            raise ApkXmlError("undeclared Android XML namespace")
        return f"{{{uri}}}{local}"

    while offset < end:
        kind, header_size, chunk_end = _chunk(data, offset, end)
        body = offset + header_size
        if kind == 1:
            if strings is not None or root_seen:
                raise ApkXmlError("duplicate/misplaced Android XML string pool")
            strings = _string_pool(data, offset, header_size, chunk_end)
        elif strings is None:
            raise ApkXmlError("Android XML node precedes string pool")
        elif kind == 0x180:  # ResXMLTree resource-ID map; names remain in the pool.
            if mapped or root_seen or header_size != 8 or (chunk_end - body) // 4 > len(strings):
                raise ApkXmlError("invalid Android XML resource map")
            mapped = True
        elif kind in (0x100, 0x101, 0x102, 0x103, 0x104):
            if header_size != 16:
                raise ApkXmlError("invalid Android XML node header")
            comment = struct.unpack_from("<I", data, offset + 12)[0]
            if comment != _NO_INDEX:
                _string(strings, comment)
            required = {0x100: 8, 0x101: 8, 0x102: 20, 0x103: 8, 0x104: 12}[kind]
            if body + required > chunk_end:
                raise ApkXmlError("truncated Android XML node")
            if kind in (0x100, 0x101):
                if body + required != chunk_end:
                    raise ApkXmlError("invalid Android XML namespace size")
                prefix, uri = struct.unpack_from("<II", data, body)
                if prefix != _NO_INDEX:
                    _string(strings, prefix)
                _string(strings, uri)
                entry = (prefix, uri, len(stack))
                if kind == 0x100:
                    if root_seen and not stack:
                        raise ApkXmlError("namespace starts after root element")
                    namespaces.append(entry)
                    if len(namespaces) > MAX_XML_DEPTH:
                        raise ApkXmlError("namespace depth limit exceeded")
                elif not namespaces or namespaces.pop() != entry:
                    raise ApkXmlError("mismatched Android XML namespace end")
            elif kind == 0x102:
                namespace, name, attr_start, attr_size, count, id_index, class_index, style_index = struct.unpack_from("<IIHHHHHH", data, body)
                if attr_start != 20 or attr_size != 20:
                    raise UnsupportedApkXmlError("unsupported Android XML attribute layout")
                if count > MAX_ATTRIBUTES or max(id_index, class_index, style_index) > count:
                    raise ApkXmlError("invalid Android XML attribute count/index")
                if body + attr_start + count * attr_size != chunk_end:
                    raise ApkXmlError("Android XML attributes exceed node bounds")
                if root_seen and not stack:
                    raise ApkXmlError("multiple Android XML root elements")
                tag = qname(namespace, name)
                attrs: dict[str, str] = {}
                types: dict[str, AndroidAttribute] = {}
                for index in range(count):
                    attr = body + attr_start + index * attr_size
                    namespace, name, raw_index = struct.unpack_from("<III", data, attr)
                    key = qname(namespace, name)
                    if key in attrs:
                        raise ApkXmlError("duplicate Android XML attribute")
                    raw = None if raw_index == _NO_INDEX else _string(strings, raw_index)
                    value, value_type, encoded = _typed_value(data, attr + 12, strings)
                    # aapt retains raw spellings such as mcc="001", mnc="01"
                    # and hexadecimal-looking GID selectors even when it also
                    # infers an integer/float type. Preserve that lexical data.
                    attrs[key] = raw if raw is not None else value
                    types[key] = AndroidAttribute(value_type, encoded, raw)
                element = tree.start(tag, attrs)
                attribute_types[element] = types
                stack.append(tag)
                root_seen = True
            elif kind == 0x103:
                if body + required != chunk_end:
                    raise ApkXmlError("invalid Android XML end-element size")
                tag = qname(*struct.unpack_from("<II", data, body))
                if not stack or stack.pop() != tag:
                    raise ApkXmlError("mismatched Android XML end element")
                tree.end(tag)
            else:
                if body + required != chunk_end or not stack:
                    raise ApkXmlError("invalid Android XML text node")
                text = _string(strings, struct.unpack_from("<I", data, body)[0])
                # aapt emits an all-zero, unused Res_value for CDATA. Other
                # encodings must be supported and agree with the text string.
                if any(data[body + 4:chunk_end]):
                    typed_text, _, _ = _typed_value(data, body + 4, strings)
                    if text != typed_text:
                        raise UnsupportedApkXmlError("conflicting Android XML text value")
                tree.data(text)
        else:
            raise UnsupportedApkXmlError(f"unsupported Android XML chunk 0x{kind:04x}")
        offset = chunk_end
    if not root_seen or stack or namespaces:
        raise ApkXmlError("incomplete Android XML document")
    return tree.close(), attribute_types


def _decode_xml(data: bytes) -> tuple[ET.Element, dict[ET.Element, dict[str, AndroidAttribute]]]:
    if len(data) > MAX_XML_BYTES:
        raise ApkXmlError("XML member size limit exceeded")
    if data.startswith(b"\x03\x00"):
        return _binary_xml(data)
    tree = _BoundedTreeBuilder()
    try:
        return ET.fromstring(data, parser=ET.XMLParser(target=tree)), {}
    except ET.ParseError as exc:
        raise ApkXmlError(f"invalid plaintext XML: {exc}") from exc


def decode_xml(data: bytes) -> ET.Element:
    """Decode bounded plaintext or Android binary XML; reject unsupported data."""
    return _decode_xml(data)[0]


def _validate_member(info: zipfile.ZipInfo) -> None:
    name = info.filename
    parts = name.rstrip("/").split("/")
    if (
        not name or name != info.orig_filename or "\\" in name or ":" in name
        or any(part in ("", ".", "..") for part in parts)
        or any(ord(char) < 32 for char in name)
        or stat.S_ISLNK(info.external_attr >> 16)
    ):
        raise ApkXmlError(f"unsafe APK member name/type: {name!r}")


def iter_apk_xml(path: Path, *, strict: bool = False) -> list[ApkXmlDocument]:
    """Return bounded assets/**/*.xml and res/xml[-qualifier]/*.xml documents.

    This is a list, not a partially-yielding iterator: unsafe ZIP paths,
    duplicates and ZIP size limits reject the entire APK. Malformed/unsupported
    XML members are rejected individually with ApkXmlWarning (including their
    paths); strict=True raises instead. This matters for real ROM APKs that
    contain malformed no-SIM placeholder XML beside valid carrier policies.
    APK selection/overlay precedence/PLMN inference belong to the caller.
    AndroidManifest.xml and resources.arsc are not carrier XML and are omitted.
    """
    path = Path(path)
    if path.stat().st_size > MAX_APK_BYTES:
        raise ApkXmlError("APK size limit exceeded")
    documents: list[ApkXmlDocument] = []
    try:
        with path.open("rb") as stream:
            digest = hashlib.sha256()
            read_size = 0
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                read_size += len(block)
                if read_size > MAX_APK_BYTES:
                    raise ApkXmlError("APK size limit exceeded")
                digest.update(block)
            apk_hash = digest.hexdigest()
            stream.seek(0)
            with zipfile.ZipFile(stream) as archive:
                entries = archive.infolist()
                if len(entries) > MAX_ZIP_ENTRIES:
                    raise ApkXmlError("APK entry count limit exceeded")
                seen: set[str] = set()
                members: list[zipfile.ZipInfo] = []
                total = 0
                for info in entries:
                    _validate_member(info)
                    key = info.filename.rstrip("/")
                    if key in seen:
                        raise ApkXmlError(f"duplicate APK member: {info.filename}")
                    seen.add(key)
                    if info.is_dir() or not _XML_MEMBER_RE.fullmatch(info.filename):
                        continue
                    if info.flag_bits & 1 or info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                        raise UnsupportedApkXmlError("encrypted/unsupported-compression APK XML")
                    if info.file_size > MAX_XML_BYTES:
                        raise ApkXmlError(f"XML member size limit exceeded: {info.filename}")
                    total += info.file_size
                    members.append(info)
                    if total > MAX_TOTAL_XML_BYTES or len(members) > MAX_XML_DOCUMENTS:
                        raise ApkXmlError("APK XML aggregate limit exceeded")
                for info in sorted(members, key=lambda item: item.filename):
                    with archive.open(info) as source:
                        data = source.read(MAX_XML_BYTES + 1)
                    if len(data) != info.file_size:
                        raise ApkXmlError(f"APK XML size mismatch: {info.filename}")
                    try:
                        root, types = _decode_xml(data)
                    except ApkXmlError as exc:
                        message = f"{path}!{info.filename}: {exc}"
                        if strict:
                            raise type(exc)(message) from exc
                        warnings.warn(message, ApkXmlWarning, stacklevel=2)
                        continue
                    documents.append(ApkXmlDocument(
                        member=info.filename, root=root, apk_path=path,
                        apk_sha256=apk_hash, member_sha256=hashlib.sha256(data).hexdigest(),
                        attribute_types=types,
                    ))
    except (zipfile.BadZipFile, EOFError, struct.error) as exc:
        raise ApkXmlError(f"invalid APK ZIP: {path}: {exc}") from exc
    return documents
