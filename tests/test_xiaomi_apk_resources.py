"""Synthetic, real-source-shaped Android XML/APK safety and extraction tests.

The fixture encoder mirrors observed ResXMLTree chunks in the xuanyuan
CarrierConfig APKs (including unused zero CDATA Res_value and raw selectors).
It does not require aapt, firmware downloads, device access or a real ROM.
"""

import hashlib
import json
import stat
import struct
import tempfile
import unittest
import warnings
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from unittest.mock import patch

from android.xiaomi.apk_resources import (
    ApkXmlError, ApkXmlWarning, UnsupportedApkXmlError, decode_xml, iter_apk_xml,
)
from android.xiaomi.firmware import (
    CONFIG_FALLBACK_PAYLOAD_PARTITIONS, CONFIG_MANIFEST_NAME,
    CONFIG_MANIFEST_SCHEMA, PARTITION_IMAGE_RE, _collect_config_files,
    _erofs_config_paths, _extract_config_from_partition,
    _extract_erofs_config_from_partition, _partition_member_matches,
    extract_xiaomi_carrier_configs,
)

NO_INDEX = 0xFFFFFFFF
ANDROID_NS = "http://schemas.android.com/apk/res/android"
SOURCE = (
    '<carrier_config_list xmlns:android="' + ANDROID_NS + '">'
    '<carrier_config mcc="001" mnc="01" spn="Example" android:enabled="true">'
    '<boolean name="carrier_wfc_ims_available_bool" value="true"/>'
    '<int name="wfc_spn_format_idx_int" value="-1"/>'
    '<float name="test_float" value="1.5"/>'
    '<string name="epdg_server_address_string" value="epdg.example.invalid"/>'
    '<string-array name="test_string_array" num="2">'
    '<item value="one"/><item>two &amp; three 😀</item></string-array>'
    '<int-array name="test_int_array" num="2"><item value="1"/>'
    '<item value="2"/></int-array>'
    '<bundle name="ims"><string name="nested_string">hello</string></bundle>'
    '</carrier_config><carrier_config carrier_id="12345" gid1="0A">'
    '<boolean name="carrier_wfc_ims_available_bool" value="false"/>'
    '</carrier_config></carrier_config_list>'
)


def _chunk(kind, header, body):
    size = 8 + len(header) + len(body)
    assert size % 4 == 0
    return struct.pack("<HHI", kind, 8 + len(header), size) + header + body


def _node(kind, body):
    return _chunk(kind, struct.pack("<II", 1, NO_INDEX), body)


def _length(value, utf8):
    if utf8:
        return bytes((value,)) if value < 128 else bytes((0x80 | (value >> 8), value & 255))
    return struct.pack("<H", value) if value < 32768 else struct.pack("<HH", 0x8000 | (value >> 16), value & 65535)


def binary_fixture(source=SOURCE, *, utf8=True, raw=True):
    """Small independent encoder for the subset used by carrier XML fixtures."""
    root = ET.fromstring(source)
    strings = ["android", ANDROID_NS]
    indexes = {value: index for index, value in enumerate(strings)}

    def string(value):
        if value not in indexes:
            indexes[value] = len(strings)
            strings.append(value)
        return indexes[value]

    def name(value):
        if value.startswith("{"):
            uri, local = value[1:].split("}", 1)
            return string(uri), string(local)
        return NO_INDEX, string(value)

    def visit(element, parent=None):
        attrs = b""
        for key, value in element.attrib.items():
            ns, local = name(key)
            raw_index = string(value) if raw else NO_INDEX
            kind, encoded = 3, string(value)
            if key in {"mcc", "mnc", "num"} or (key == "value" and (element.tag == "int" or parent == "int-array")):
                kind, encoded = 0x10, int(value) & NO_INDEX
            elif key == "value" and element.tag == "boolean":
                kind, encoded = 0x12, NO_INDEX if value == "true" else 0
            elif key == "value" and element.tag == "float":
                kind, encoded = 4, struct.unpack("<I", struct.pack("<f", float(value)))[0]
            attrs += struct.pack("<IIIHBBI", ns, local, raw_index, 8, 0, kind, encoded)
        ns, local = name(element.tag)
        result = _node(0x102, struct.pack("<IIHHHHHH", ns, local, 20, 20, len(element.attrib), 0, 0, 0) + attrs)
        if element.text:
            result += _node(0x104, struct.pack("<III", string(element.text), 0, 0))
        for child in element:
            result += visit(child, element.tag)
            if child.tail:
                result += _node(0x104, struct.pack("<III", string(child.tail), 0, 0))
        return result + _node(0x103, struct.pack("<II", ns, local))

    nodes = _node(0x100, struct.pack("<II", 0, 1)) + visit(root) + _node(0x101, struct.pack("<II", 0, 1))
    pool = b""
    offsets = []
    for value in strings:
        offsets.append(len(pool))
        units = len(value.encode("utf-16-le")) // 2
        if utf8:
            encoded = value.encode("utf-8")
            pool += _length(units, True) + _length(len(encoded), True) + encoded + b"\x00"
        else:
            pool += _length(units, False) + value.encode("utf-16-le") + b"\x00\x00"
    pool += b"\x00" * (-len(pool) % 4)
    header = struct.pack("<IIIII", len(strings), 0, 0x100 if utf8 else 0, 28 + 4 * len(strings), 0)
    string_pool = _chunk(1, header, struct.pack(f"<{len(offsets)}I", *offsets) + pool)
    return _chunk(3, b"", string_pool + _chunk(0x180, b"", b"") + nodes)


def shape(root):
    return root.tag, root.attrib, root.text, root.tail, [shape(child) for child in root]


def chunks(data):
    offset = 8
    while offset < len(data):
        kind, header, size = struct.unpack_from("<HHI", data, offset)
        yield kind, offset, header, size
        offset += size


class AndroidBinaryXmlTests(unittest.TestCase):
    def test_roundtrip_real_source_shaped_scopes_arrays_namespaces_and_types(self):
        for utf8 in (True, False):
            with self.subTest(utf8=utf8):
                decoded = decode_xml(binary_fixture(utf8=utf8))
                self.assertEqual(shape(decoded), shape(ET.fromstring(SOURCE)))
                self.assertEqual(decoded[0].get("mnc"), "01")
                self.assertEqual(decoded[1].get("carrier_id"), "12345")

    def test_plaintext_and_utf16_xml(self):
        for encoding in ("utf-8", "utf-16"):
            self.assertEqual(shape(decode_xml(SOURCE.encode(encoding))), shape(ET.fromstring(SOURCE)))

    def test_typed_values_without_raw_spelling(self):
        root = decode_xml(binary_fixture(raw=False))
        self.assertEqual(root[0][0].get("value"), "true")
        self.assertEqual(root[0][1].get("value"), "-1")
        self.assertEqual(root[0][2].get("value"), "1.5")
        self.assertEqual(root[1][0].get("value"), "false")

    def test_extended_utf8_and_utf16_string_lengths(self):
        for text, utf8 in (("é" * 150 + "😀", True), ("x" * 33000, False)):
            source = f'<carrier_config><string name="test">{text}</string></carrier_config>'
            self.assertEqual(decode_xml(binary_fixture(source, utf8=utf8))[0].text, text)

    def test_all_truncation_points_and_trailing_bytes_rejected(self):
        data = binary_fixture('<carrier_config_list/>')
        for end in range(len(data)):
            with self.subTest(end=end), self.assertRaises(ApkXmlError):
                decode_xml(data[:end])
        with self.assertRaises(ApkXmlError):
            decode_xml(data + b"\x00" * 4)

    def test_malformed_chunk_and_pool_bounds(self):
        original = binary_fixture()
        edits = (
            ("<I", 4, 0), ("<H", 2, 4),
            ("<I", 12, len(original) * 2),  # string-pool chunk extends past parent
            ("<I", 12, 0), ("<H", 10, 12),
            ("<I", 16, 100001),  # string count
            ("<I", 28, 0),  # stringsStart precedes offset table
            ("<I", 36, NO_INDEX),  # first string offset
        )
        for fmt, offset, value in edits:
            with self.subTest(offset=offset, value=value):
                data = bytearray(original)
                struct.pack_into(fmt, data, offset, value)
                with self.assertRaises(ApkXmlError):
                    decode_xml(bytes(data))

    def test_bad_string_length_terminator_and_encoding(self):
        original = binary_fixture()
        base = 8 + struct.unpack_from("<I", original, 28)[0]
        # First string is 'android', UTF8 byte length 7.
        for offset, value in ((base, 8), (base + 1, 255), (base + 2, 255), (base + 9, 1)):
            data = bytearray(original)
            data[offset] = value
            with self.subTest(offset=offset), self.assertRaises(ApkXmlError):
                decode_xml(bytes(data))

    def test_attribute_bounds_duplicates_and_invalid_indexes(self):
        original = binary_fixture()
        node = next(offset for kind, offset, _, _ in chunks(original) if kind == 0x102 and struct.unpack_from("<H", original, offset + 28)[0] > 1)
        for offset, fmt, value in ((node + 24, "<H", 0), (node + 26, "<H", 0), (node + 28, "<H", 5000), (node + 40, "<I", NO_INDEX), (node + 48, "<H", 7)):
            data = bytearray(original)
            struct.pack_into(fmt, data, offset, value)
            with self.subTest(offset=offset), self.assertRaises(ApkXmlError):
                decode_xml(bytes(data))
        data = bytearray(original)
        data[node + 56:node + 64] = data[node + 36:node + 44]
        with self.assertRaisesRegex(ApkXmlError, "duplicate"):
            decode_xml(bytes(data))

    def test_unsupported_typed_values_rejected_even_when_raw_exists(self):
        original = binary_fixture()
        node = next(offset for kind, offset, _, _ in chunks(original) if kind == 0x102 and struct.unpack_from("<H", original, offset + 28)[0])
        for kind in (0, 1, 2, 5, 6, 0x1c, 0xff):
            data = bytearray(original)
            data[node + 51] = kind
            with self.subTest(kind=kind), self.assertRaises(UnsupportedApkXmlError):
                decode_xml(bytes(data))

    def test_unsupported_chunks_and_mismatched_elements(self):
        original = binary_fixture()
        end = next(offset for kind, offset, _, _ in chunks(original) if kind == 0x103)
        for offset, fmt, value in ((end, "<H", 0x9999), (end + 20, "<I", 0)):
            data = bytearray(original)
            struct.pack_into(fmt, data, offset, value)
            with self.assertRaises(ApkXmlError):
                decode_xml(bytes(data))

    def test_tree_depth_node_and_character_limits(self):
        with patch("android.xiaomi.apk_resources.MAX_XML_DEPTH", 2), self.assertRaises(ApkXmlError):
            decode_xml(binary_fixture())
        with patch("android.xiaomi.apk_resources.MAX_XML_NODES", 2), self.assertRaises(ApkXmlError):
            decode_xml(SOURCE.encode())
        with patch("android.xiaomi.apk_resources.MAX_DECODED_CHARS", 10), self.assertRaises(ApkXmlError):
            decode_xml(SOURCE.encode())

    def test_dtd_and_entities_are_rejected(self):
        for declaration in ('<!DOCTYPE c [<!ENTITY evil "expanded">]>', '<!DOCTYPE c SYSTEM "file:///etc/passwd">'):
            with self.assertRaises(ApkXmlError):
                decode_xml((declaration + '<c>&evil;</c>').encode())


class ApkZipTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "CarrierConfig.apk"

    def make_apk(self, members):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(self.path, "w", zipfile.ZIP_DEFLATED) as archive:
                for name, data in members:
                    archive.writestr(name, data)
        return self.path

    def test_members_hashes_original_paths_and_binary_attribute_metadata(self):
        binary = binary_fixture()
        self.make_apk([
            ("assets/carrier_config_carrierid_12345_Example.xml", SOURCE),
            ("res/xml/vendor.xml", binary),
            ("assets/default/default_qcom_v2.8.4.xml", SOURCE),
            ("res/xml-en/vendor_device.xml", binary),
            ("AndroidManifest.xml", "not carrier XML"),
            ("classes.dex", b"not executed"), ("res/layout/carrier.xml", "ignored"),
        ])
        docs = iter_apk_xml(self.path, strict=True)
        self.assertEqual(len(docs), 4)
        for doc in docs:
            self.assertEqual(doc.apk_path, self.path)
            self.assertEqual(doc.apk_sha256, hashlib.sha256(self.path.read_bytes()).hexdigest())
            self.assertEqual(shape(doc.root), shape(ET.fromstring(SOURCE)))
        document = next(doc for doc in docs if doc.member == "res/xml/vendor.xml")
        self.assertEqual(document.member_sha256, hashlib.sha256(binary).hexdigest())
        selector = document.attribute_types[document.root[0]]["mnc"]
        self.assertEqual((selector.value_type, selector.data, selector.raw_value), (0x10, 1, "01"))
        self.assertEqual(document.attribute_types[document.root[0][0]]["value"].value_type, 0x12)
        self.assertEqual(document.root[1].attrib, {"carrier_id": "12345", "gid1": "0A"})
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_duplicate_paths_reject_entire_apk(self):
        self.make_apk([("assets/test.xml", SOURCE), ("assets/test.xml", SOURCE)])
        with self.assertRaisesRegex(ApkXmlError, "duplicate"):
            iter_apk_xml(self.path)

    def test_traversal_absolute_backslash_and_unsafe_unselected_entries(self):
        for name in ("../assets/test.xml", "/assets/test.xml", "assets/../test.xml", "assets//test.xml", "assets/./test.xml", "assets\\test.xml", "C:/test.xml", "../unrelated.dex"):
            with self.subTest(name=name):
                self.make_apk([(name, SOURCE)])
                with self.assertRaisesRegex(ApkXmlError, "unsafe"):
                    iter_apk_xml(self.path)

    def test_symlink_and_unsupported_compression_rejected(self):
        link = zipfile.ZipInfo("assets/link.xml")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        self.make_apk([(link, "../../outside.xml")])
        with self.assertRaisesRegex(ApkXmlError, "unsafe"):
            iter_apk_xml(self.path)
        entry = zipfile.ZipInfo("assets/test.xml")
        entry.compress_type = zipfile.ZIP_BZIP2
        self.make_apk([(entry, SOURCE)])
        with self.assertRaises(UnsupportedApkXmlError):
            iter_apk_xml(self.path)

    def test_xml_apk_aggregate_and_entry_count_limits(self):
        self.make_apk([("assets/a.xml", SOURCE), ("assets/b.xml", SOURCE)])
        for limit, value in (("MAX_XML_BYTES", 10), ("MAX_APK_BYTES", 10), ("MAX_TOTAL_XML_BYTES", len(SOURCE.encode())), ("MAX_ZIP_ENTRIES", 1), ("MAX_XML_DOCUMENTS", 1)):
            with self.subTest(limit=limit), patch(f"android.xiaomi.apk_resources.{limit}", value), self.assertRaises(ApkXmlError):
                iter_apk_xml(self.path)

    def test_member_rejection_is_explicit_and_strict_mode_is_available(self):
        # This is the malformed structure actually shipped in the no-SIM asset.
        self.make_apk([("assets/carrier_config_no_sim.xml", '<carrier_config_list><carrier_config_list />'), ("res/xml/vendor.xml", binary_fixture())])
        with self.assertWarnsRegex(ApkXmlWarning, "carrier_config_no_sim.xml"):
            docs = iter_apk_xml(self.path)
        self.assertEqual([doc.member for doc in docs], ["res/xml/vendor.xml"])
        with self.assertRaises(ApkXmlError):
            iter_apk_xml(self.path, strict=True)

    def test_bad_zip_and_bad_crc_rejected(self):
        self.path.write_bytes(b"not a zip")
        with self.assertRaises(ApkXmlError):
            iter_apk_xml(self.path)
        entry = zipfile.ZipInfo("assets/test.xml")
        entry.compress_type = zipfile.ZIP_STORED
        self.make_apk([(entry, SOURCE)])
        data = bytearray(self.path.read_bytes())
        data[data.index(b"<carrier_config_list")] = ord("!")
        self.path.write_bytes(data)
        with self.assertRaises(ApkXmlError):
            iter_apk_xml(self.path)


class XiaomiApkExtractionTests(unittest.TestCase):
    def test_recognized_apks_only_are_inventoried(self):
        supported = (
            "system_ext/priv-app/CarrierConfig/CarrierConfig.apk",
            "system/system/app/CarrierConfig/CarrierConfig.apk",
            "product/overlay/MiuiCarrierConfigOverlay.apk",
            "product/overlay/CarrierConfigResCommon_Sys.apk",
            "product/overlay/CarrierConfigResDevice/CarrierConfigResDevice.apk",
        )
        unrelated = ("product/app/CarrierServices/CarrierServices.apk", "product/overlay/NotCarrierConfig.apk", "product/overlay/SettingsOverlay.apk", "etc/CarrierConfig/unrelated.apk", "priv-app/CarrierConfig/Other.apk")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in supported + unrelated:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"fixture")
            self.assertEqual({p.relative_to(root).as_posix() for p in _collect_config_files(root)}, set(supported))
        self.assertFalse(_partition_member_matches("../priv-app/CarrierConfig/CarrierConfig.apk"))

    def test_erofs_scans_apks_without_etc_and_handles_nested_overlays(self):
        image = Path("system_ext.img")
        entries = {
            "/priv-app/CarrierConfig": [(1, "CarrierConfig.apk"), (1, "irrelevant.odex")],
            "/overlay": [(1, "MiuiCarrierConfigOverlay.apk"), (1, "SettingsOverlay.apk"), (2, "CarrierConfigResDevice"), (2, "OtherApp")],
            "/overlay/CarrierConfigResDevice": [(1, "CarrierConfigResDevice.apk")],
        }
        with patch("android.xiaomi.firmware._erofs_ls", side_effect=lambda _, path: entries.get(path, [])) as listing:
            paths = _erofs_config_paths(image)
        self.assertEqual(set(paths), {"/priv-app/CarrierConfig/CarrierConfig.apk", "/overlay/MiuiCarrierConfigOverlay.apk", "/overlay/CarrierConfigResDevice/CarrierConfigResDevice.apk"})
        self.assertNotIn("/overlay/OtherApp", [call.args[1] for call in listing.call_args_list])

    def test_system_as_root_and_partition_selection(self):
        self.assertIn("system", CONFIG_FALLBACK_PAYLOAD_PARTITIONS)
        for name in ("system", "mi_ext", "system_ext"):
            self.assertIsNotNone(PARTITION_IMAGE_RE.search(f"images/{name}.img"))
        with patch("android.xiaomi.firmware._erofs_ls", side_effect=lambda _, path: [(1, "CarrierConfig.apk")] if path == "/system/priv-app/CarrierConfig" else []):
            self.assertEqual(_erofs_config_paths(Path("system.img")), ["/system/priv-app/CarrierConfig/CarrierConfig.apk"])

    def test_empty_erofs_scan_does_not_unpack_full_system(self):
        with patch("android.xiaomi.firmware.shutil.which", return_value="dump.erofs"), patch("android.xiaomi.firmware._erofs_config_paths", return_value=[]), patch("android.xiaomi.firmware.subprocess.run") as run:
            self.assertEqual(_extract_erofs_config_from_partition(Path("system.img"), Path("unused")), [])
        run.assert_not_called()

    def test_selective_erofs_and_seven_zip_extraction_keep_partition_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / "system.img"
            selected = "/system/priv-app/CarrierConfig/CarrierConfig.apk"

            def cat_file(command, **kwargs):
                self.assertIn(f"--path={selected}", command)
                kwargs["stdout"].write(b"selected apk")
                return type("Completed", (), {"returncode": 0})()

            with patch("android.xiaomi.firmware.shutil.which", return_value="dump.erofs"), patch("android.xiaomi.firmware._erofs_config_paths", return_value=[selected]), patch("android.xiaomi.firmware.subprocess.run", side_effect=cat_file):
                paths = _extract_erofs_config_from_partition(image, root / "erofs")
            self.assertEqual(paths[0].relative_to(root / "erofs").as_posix(), "system" + selected)
            with patch("android.xiaomi.firmware._extract_erofs_config_from_partition", return_value=[]), patch("android.xiaomi.firmware.shutil.which", return_value="7z"), patch("android.xiaomi.firmware.subprocess.run") as run:
                _extract_config_from_partition(image, root / "seven")
            command = run.call_args.args[0]
            self.assertIn("priv-app/CarrierConfig/CarrierConfig.apk", command)
            self.assertIn("system/priv-app/CarrierConfig/CarrierConfig.apk", command)
            self.assertNotIn("*.apk", command)

    def test_inventory_cache_bump_and_apk_hash_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rom, output = root / "rom.zip", root / "out"
            member = "system_ext/priv-app/CarrierConfig/CarrierConfig.apk"
            with zipfile.ZipFile(rom, "w") as archive:
                archive.writestr(member, b"fixture apk")
                archive.writestr("system_ext/app/Other/Other.apk", b"not selected")
            first = extract_xiaomi_carrier_configs(rom, output)
            self.assertEqual([p.relative_to(output).as_posix() for p in first.config_files], [member])
            manifest = output / CONFIG_MANIFEST_NAME
            document = json.loads(manifest.read_text())
            self.assertEqual(document["schema"], CONFIG_MANIFEST_SCHEMA)
            self.assertEqual(document["apk_artifacts"], [{"relative_path": member, "sha256": hashlib.sha256(b"fixture apk").hexdigest(), "size": 11}])
            document["schema"] = "carrier-bundles-xiaomi-carrier-config-manifest-v2"
            manifest.write_text(json.dumps(document))
            with patch("android.xiaomi.firmware._copy_zip_config_members", wraps=__import__("android.xiaomi.firmware", fromlist=["_copy_zip_config_members"])._copy_zip_config_members) as copy:
                extract_xiaomi_carrier_configs(rom, output)
                self.assertEqual(copy.call_count, 1)
                extract_xiaomi_carrier_configs(rom, output)
                self.assertEqual(copy.call_count, 1)
                first.config_files[0].write_bytes(b"changed apk")
                extract_xiaomi_carrier_configs(rom, output)
                self.assertEqual(copy.call_count, 2)
            self.assertEqual(first.config_files[0].read_bytes(), b"fixture apk")


if __name__ == "__main__":
    unittest.main()
