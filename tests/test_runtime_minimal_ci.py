"""Artifact-only rebuild and immutable publication safety (no actual build here)."""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools import rebuild_runtime_minimal_ci as ci

ROOT = Path(__file__).resolve().parents[1]


class RuntimeMinimalCiTests(unittest.TestCase):
    def test_rebuild_is_readonly_and_never_publishes(self):
        text = (ROOT / '.github/workflows/validate-runtime-minimal.yml').read_text()
        self.assertIn('contents: read', text)
        self.assertNotIn('contents: write', text)
        self.assertNotIn('action-gh-release', text)
        self.assertIn('37095019351', text)
        self.assertIn('c445d5327e721407505643d328595378e8849a2a', text)
        self.assertIn('be2572f4082d294d9b4c25d74133daeba016733406320777d916acbb647c2135', text)
        self.assertIn('sha256sum -c SHA256SUMS', text)
        self.assertIn('--rom-path', text)
        self.assertNotIn('--accept-google-terms', text)

    def test_existing_catalog_workflow_is_explicit_new_tag_only(self):
        text = (ROOT / '.github/workflows/build-catalog-set.yml').read_text()
        option = text.split('      publish_release:', 1)[1].split('      release_tag:', 1)[0]
        self.assertIn('default: false', option)
        self.assertNotIn('deleteReleaseAsset', text)
        self.assertIn('Refusing to replace an existing Release or tag', text)
        publish = text.split('- name: Publish schema-v7 catalog Release', 1)[1]
        self.assertIn("github.event_name == 'workflow_dispatch'", publish)
        self.assertIn("github.ref == 'refs/heads/main'", publish)
        self.assertIn('inputs.publish_release == true', publish)
        for source in ('pixel', 'ios', 'ipcc', 'xiaomi'):
            self.assertIn(f"needs.{source}.result == 'success'", publish)
        self.assertIn('python3 -B -m unittest discover -s tests -v', text)

    def test_local_runner_rejects_before_loading_or_building(self):
        with patch.dict(os.environ, {}, clear=True), \
             patch('sys.argv', ['ci', '--source-dir', 'none', '--xiaomi', 'none', '--output-dir', 'none']), \
             patch.object(ci, 'pinned_full_sources') as load, \
             contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            ci.main()
        self.assertEqual(error.exception.code, 2)
        load.assert_not_called()

    def test_pinned_sources_reject_tampering_and_partial_sets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entries = []
            for name in (*ci.REUSED_NAMES, 'carrier-bundles-iphone16promax-27.0.1.sqlite3'):
                path = root / name
                path.write_bytes(b'synthetic sealed source')
                digest = ci.sha256(path)
                entries.append({'source': {'sha256': digest}, 'variants': {'full': {
                    'database': name, 'sha256': digest, 'bytes': path.stat().st_size}}})
            manifest = root / 'catalog-variants.json'
            manifest.write_text(json.dumps({'catalogs': entries}))
            with patch.object(ci, 'verify_catalog'):
                self.assertEqual(len(ci.pinned_full_sources(root)), 3)
                damaged = copy.deepcopy(entries)
                damaged[0]['variants']['full']['sha256'] = '0' * 64
                manifest.write_text(json.dumps({'catalogs': damaged}))
                with self.assertRaisesRegex(ValueError, 'pinned manifest'):
                    ci.pinned_full_sources(root)
                manifest.write_text(json.dumps({'catalogs': entries[:2]}))
                with self.assertRaisesRegex(ValueError, 'exactly one'):
                    ci.pinned_full_sources(root)
            # Restore test-file permissions for Windows temporary cleanup.
            for path in root.iterdir():
                path.chmod(0o600)

    def test_incomplete_and_non_runtime_sets_rejected(self):
        for policy in (False, True):
            with self.assertRaisesRegex(ValueError, 'four runtime-minimal'):
                ci.verify_set({'catalogs': [], 'policy': {'runtime_minimal': {'enabled': policy}}}, Path('none'))


if __name__ == '__main__':
    unittest.main()
