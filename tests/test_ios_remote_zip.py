import http.server
import subprocess
import sys
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path


from ios.remote_zip import download_remote_member, locate_remote_member


class _RangeHandler(http.server.BaseHTTPRequestHandler):
    archive = b""

    def do_HEAD(self) -> None:
        self.send_response(200)
        self.send_header("Content-Length", str(len(self.archive)))
        self.end_headers()

    def do_GET(self) -> None:
        value = self.headers.get("Range", "")
        if not value.startswith("bytes=") or "-" not in value:
            self.send_error(416)
            return
        start_text, end_text = value.removeprefix("bytes=").split("-", 1)
        start, end = int(start_text), int(end_text)
        if start < 0 or end < start or end >= len(self.archive):
            self.send_error(416)
            return
        data = self.archive[start : end + 1]
        self.send_response(206)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Range", f"bytes {start}-{end}/{len(self.archive)}")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, _format: str, *args) -> None:
        pass


class IOSRemoteZipTests(unittest.TestCase):
    def test_locates_resumes_and_verifies_stored_member(self) -> None:
        payload = (b"public carrier bundle fixture\0" * 300) + b"end"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "sample.ipsw"
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as package:
                package.writestr("BuildManifest.plist", b"plist")
                package.writestr("rootfs.dmg.aea", payload)
            _RangeHandler.archive = archive.read_bytes()
            server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _RangeHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                url = f"http://127.0.0.1:{server.server_port}/sample.ipsw"
                member = locate_remote_member(url, "rootfs.dmg.aea")
                self.assertEqual(member.uncompressed_size, len(payload))
                ranges = root / "ranges"
                ranges.mkdir()
                (ranges / "part-00").write_bytes(payload[:137])
                result = download_remote_member(
                    member,
                    root / "rootfs.dmg.aea",
                    range_dir=ranges,
                    chunk_size=1024,
                    workers=3,
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
            self.assertEqual(result.path.read_bytes(), payload)
            self.assertEqual(result.size, len(payload))
            self.assertEqual(f"{result.crc32:08x}", "b34238e7")

    def test_cli_accepts_an_explicit_range_directory(self) -> None:
        payload = b"cli range directory" * 100
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "sample.ipsw"
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as package:
                package.writestr("rootfs.dmg.aea", payload)
            _RangeHandler.archive = archive.read_bytes()
            server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _RangeHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            ranges = root / "explicit-ranges"
            try:
                subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        "ios.remote_zip",
                        f"http://127.0.0.1:{server.server_port}/sample.ipsw",
                        "rootfs.dmg.aea",
                        str(root / "rootfs.dmg.aea"),
                        "--range-dir",
                        str(ranges),
                        "--chunk-size",
                        "128",
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)
            self.assertEqual((root / "rootfs.dmg.aea").read_bytes(), payload)
            self.assertTrue((ranges / "part-00").is_file())


if __name__ == "__main__":
    unittest.main()
