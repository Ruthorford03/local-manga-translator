"""Offline checks for model recipe identity across different install locations.

These tests use synthetic manifests only. They do not read model weights, start
Ollama, download files, or use private project data.
"""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / 'tools/local-manga-translation/model_identity.py'
SPEC = importlib.util.spec_from_file_location('public_test_model_identity', MODULE_PATH)
identity = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(identity)


def recipe(source='F:/example-alpha/model.gguf'):
    """A neutral fixture: hashes are public recipe identifiers, not test weights."""
    layers = [dict(mediaType=kind, digest=digest)
              for kind, digest in identity.EXPECTED_LAYERS.items()]
    layers[0]['from'] = source
    return {
        'schemaVersion': 2,
        'config': {'digest': identity.EXPECTED_CONFIG},
        'layers': layers,
    }


class ModelIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='manga-identity-test-')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def write_manifest(self, data, name='manifest.json'):
        path = self.root / name
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n',
                        encoding='utf-8')
        return path

    def test_recipe_returns_exact_local_manifest_digest(self):
        path = self.write_manifest(recipe())
        expected = hashlib.sha256(path.read_bytes()).hexdigest()
        self.assertEqual(identity.model_manifest_digest(path, required=True), expected)

    def test_different_install_from_path_is_accepted_with_distinct_resume_identity(self):
        first = self.write_manifest(recipe(), 'first.json')
        second = self.write_manifest(recipe('G:/example-beta/model.gguf'), 'second.json')
        first_digest = identity.model_manifest_digest(first, required=True)
        second_digest = identity.model_manifest_digest(second, required=True)
        self.assertNotEqual(first_digest, second_digest)
        self.assertEqual(second_digest, hashlib.sha256(second.read_bytes()).hexdigest())

    def test_changed_config_digest_is_rejected(self):
        data = recipe()
        data['config']['digest'] = 'sha256:' + '0' * 64
        with self.assertRaisesRegex(ValueError, 'model/template/parameters differ'):
            identity.model_manifest_digest(self.write_manifest(data), required=True)

    def test_each_changed_model_template_or_params_digest_is_rejected(self):
        original = recipe()
        for index, layer in enumerate(original['layers']):
            with self.subTest(media_type=layer['mediaType']):
                data = copy.deepcopy(original)
                data['layers'][index]['digest'] = 'sha256:' + '0' * 64
                with self.assertRaisesRegex(ValueError, 'model/template/parameters differ'):
                    identity.model_manifest_digest(self.write_manifest(data), required=True)

    def test_missing_extra_and_duplicate_layers_are_rejected(self):
        original = recipe()
        missing = copy.deepcopy(original)
        missing['layers'].pop()
        extra = copy.deepcopy(original)
        extra['layers'].append({'mediaType': 'application/test-extra',
                                'digest': 'sha256:' + '0' * 64})
        duplicate = copy.deepcopy(original)
        duplicate['layers'][-1] = copy.deepcopy(duplicate['layers'][0])
        for name, data in [('missing', missing), ('extra', extra), ('duplicate', duplicate)]:
            with self.subTest(case=name):
                with self.assertRaises(ValueError):
                    identity.model_manifest_digest(self.write_manifest(data), required=True)

    def test_missing_manifest_is_optional_for_source_inspection(self):
        self.assertIsNone(identity.model_manifest_digest(self.root / 'missing.json'))

    def test_missing_manifest_is_rejected_when_required(self):
        with self.assertRaisesRegex(FileNotFoundError, 'Sakura is not installed'):
            identity.model_manifest_digest(self.root / 'missing.json', required=True)


if __name__ == '__main__':
    unittest.main()
