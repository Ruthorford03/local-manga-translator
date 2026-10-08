"""Exercise model setup with synthetic bytes; no network or model execution."""
import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/prepare_models.py'
SPEC = importlib.util.spec_from_file_location('model_setup_under_test', SCRIPT)
setup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(setup)


class ModelSetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='manga-model-setup-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root / 'docs').mkdir()
        self.root_patch = patch.object(setup, 'ROOT', self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)
        self.payload = b'synthetic fixture; not a model'
        self.item = {
            'path': 'models/example.bin', 'url': 'https://example.invalid/model',
            'bytes': len(self.payload),
            'sha256': hashlib.sha256(self.payload).hexdigest(),
        }
        # Every unexpected request fails before any connection can be attempted.
        self.network = patch.object(setup.urllib.request, 'urlopen', side_effect=AssertionError('Unexpected network request'))
        self.urlopen = self.network.start()
        self.addCleanup(self.network.stop)

    def run_setup(self, *, download=False):
        (self.root / 'docs/MODELS.json').write_text(json.dumps({'files': [self.item]}), encoding='utf-8')
        self.stdout = io.StringIO()
        with patch.object(sys, 'argv', ['prepare_models.py'] + (['--download'] if download else [])), contextlib.redirect_stdout(self.stdout):
            return setup.main()

    def response(self, payload):
        self.urlopen.side_effect = lambda *args, **kwargs: io.BytesIO(payload)

    def archive(self, entries):
        raw = io.BytesIO()
        with zipfile.ZipFile(raw, 'w') as archive:
            for name, contents in entries:
                archive.writestr(name, contents)
        self.item['archive_member'] = 'feature.pkl'
        self.response(raw.getvalue())

    @property
    def target(self):
        return self.root / self.item['path']

    @property
    def partial(self):
        return self.target.with_name(self.target.name + '.part')

    def test_missing_is_reported_without_a_download(self):
        self.assertEqual(self.run_setup(), 1)
        self.assertEqual(json.loads(self.stdout.getvalue())['missing'], [self.item['path']])
        self.assertFalse(self.target.exists())
        self.urlopen.assert_not_called()

    def test_existing_valid_file_is_reused(self):
        self.target.parent.mkdir()
        self.target.write_bytes(self.payload)
        self.assertEqual(self.run_setup(download=True), 0)
        self.assertEqual(self.target.read_bytes(), self.payload)
        self.urlopen.assert_not_called()

    def test_existing_mismatch_is_preserved(self):
        self.target.parent.mkdir()
        self.target.write_bytes(b'user-owned bytes')
        with self.assertRaisesRegex(ValueError, 'Existing file does not match'):
            self.run_setup(download=True)
        self.assertEqual(self.target.read_bytes(), b'user-owned bytes')
        self.urlopen.assert_not_called()

    def test_verified_direct_download_is_published(self):
        self.response(self.payload)
        self.assertEqual(self.run_setup(download=True), 0)
        self.assertEqual(self.target.read_bytes(), self.payload)
        self.assertFalse(self.partial.exists())

    def test_wrong_hash_retains_partial_and_no_final_file(self):
        received = bytes([self.payload[0] ^ 1]) + self.payload[1:]
        self.response(received)
        with self.assertRaisesRegex(ValueError, 'failed SHA-256'):
            self.run_setup(download=True)
        self.assertFalse(self.target.exists())
        self.assertEqual(self.partial.read_bytes(), received)

    def test_oversized_response_never_becomes_a_final_file(self):
        self.response(self.payload + b'extra')
        with self.assertRaisesRegex(ValueError, 'exceeds expected size'):
            self.run_setup(download=True)
        self.assertFalse(self.target.exists())
        self.assertTrue(self.partial.is_file())

    def test_archive_extracts_only_the_selected_member(self):
        self.archive([('model/feature.pkl', self.payload), ('unrelated/private.txt', b'unselected')])
        self.assertEqual(self.run_setup(download=True), 0)
        self.assertEqual(self.target.read_bytes(), self.payload)
        self.assertFalse((self.root / 'unrelated').exists())
        self.assertFalse(self.partial.exists())

    def test_ambiguous_archive_member_is_rejected(self):
        self.archive([('one/feature.pkl', self.payload), ('two/feature.pkl', self.payload)])
        with self.assertRaisesRegex(ValueError, 'missing, ambiguous, or wrong size'):
            self.run_setup(download=True)
        self.assertFalse(self.target.exists())

    def test_archive_member_wrong_size_is_rejected(self):
        self.archive([('model/feature.pkl', self.payload + b'extra')])
        with self.assertRaisesRegex(ValueError, 'missing, ambiguous, or wrong size'):
            self.run_setup(download=True)
        self.assertFalse(self.target.exists())

    def test_traversal_name_is_never_used_as_an_extraction_path(self):
        self.archive([('../../feature.pkl', self.payload)])
        self.assertEqual(self.run_setup(download=True), 0)
        self.assertEqual(self.target.read_bytes(), self.payload)
        # The implementation streams member contents to its inventory destination.
        self.assertFalse((self.root / 'feature.pkl').exists())
        self.assertEqual(len(list((self.root / 'models').rglob('*'))), 1)

    def test_inventory_destination_cannot_escape_repository(self):
        self.item['path'] = '../escape.bin'
        with self.assertRaisesRegex(ValueError, 'escapes repository'):
            self.run_setup(download=True)
        self.urlopen.assert_not_called()


if __name__ == '__main__':
    unittest.main()
