"""Synthetic, offline checks for the fast editor's publication boundary.

All images are generated solid colors in temporary directories. No real comic,
OCR, translation model, server, or user's work records are loaded.
"""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / 'tools/local-manga-translation/page_fast_renderer.py'
SPEC = importlib.util.spec_from_file_location('public_test_fast_renderer', MODULE_PATH)
renderer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(renderer)


class ReviewPublishGuardTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='manga-edit-guard-test-')
        self.addCleanup(self.temporary.cleanup)
        self.output = Path(self.temporary.name) / 'result'
        self.work = self.output / '_work'
        self.work.mkdir(parents=True)
        self.image_path = self.output / 'page.png'
        Image.new('RGB', (8, 8), 'white').save(self.image_path)
        self.mapping = [{'normalized_name': '000001.png', 'output_name': 'page.png',
                         'output_sha256': hashlib.sha256(self.image_path.read_bytes()).hexdigest()}]
        self.status = {'status': 'completed', 'failed_pages': {}}
        self.context = {'output_dir': self.output, 'work_dir': self.work,
                        'output_name': 'page.png', 'normalized_name': '000001.png',
                        'existing_overrides': {}}
        self.write_json('file-map.json', self.mapping)
        self.write_json('status.json', self.status)

    def write_json(self, name, data):
        (self.work / name).write_text(json.dumps(data), encoding='utf-8')

    def snapshot(self):
        return {path.relative_to(self.output).as_posix(): path.read_bytes()
                for path in self.output.rglob('*') if path.is_file()}

    def assert_rejected_without_render_or_write(self, context=None):
        before = self.snapshot()
        with patch.object(renderer, 'render_page_image') as render:
            with self.assertRaises((OSError, ValueError, TypeError, KeyError)):
                renderer.save_and_publish_page(context or self.context, {'1': {'scale': 1.1}})
            render.assert_not_called()
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.work / 'overrides').exists())

    def test_already_published_page_can_save_and_update_its_hash(self):
        original_status = (self.work / 'status.json').read_bytes()
        old_hash = self.mapping[0]['output_sha256']
        image = Image.new('RGB', (8, 8), 'gray')
        path, digest = renderer.save_and_publish_page(self.context, {'1': {'scale': 1.1}}, image)
        self.assertEqual(path, self.image_path)
        self.assertEqual(digest, hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertNotEqual(digest, old_hash)
        self.assertEqual(json.loads((self.work / 'file-map.json').read_text('utf-8'))[0]['output_sha256'], digest)
        self.assertTrue((self.work / 'overrides/000001.json').is_file())
        self.assertEqual((self.work / 'status.json').read_bytes(), original_status)
        renderer.validate_published_page(self.context)

    def test_partial_job_can_edit_a_different_successful_page(self):
        self.status = {'status': 'partial', 'failed_pages': {
            '000002.png': {'type': 'ReviewRequired'}}}
        self.write_json('status.json', self.status)
        renderer.validate_published_page(self.context)

    def test_pending_or_failed_target_is_rejected_even_if_png_exists(self):
        for failure_type in ('ReviewRequired', 'RuntimeError'):
            with self.subTest(failure_type=failure_type):
                self.write_json('status.json', {'status': 'partial', 'failed_pages': {
                    '000001.png': {'type': failure_type}}})
                self.assert_rejected_without_render_or_write()

    def test_pre_rendered_image_does_not_bypass_pending_gate(self):
        self.write_json('status.json', {'status': 'partial', 'failed_pages': {
            '000001.png': {'type': 'ReviewRequired'}}})
        before = self.snapshot()
        with self.assertRaises(ValueError):
            renderer.save_and_publish_page(self.context, {}, Image.new('RGB', (8, 8), 'gray'))
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.work / 'overrides').exists())

    def test_unpublished_target_without_output_hash_is_rejected(self):
        self.mapping[0].pop('output_sha256')
        self.write_json('file-map.json', self.mapping)
        self.assert_rejected_without_render_or_write()

    def test_missing_or_changed_png_is_rejected(self):
        original = self.image_path.read_bytes()
        self.image_path.unlink()
        self.assert_rejected_without_render_or_write()
        self.image_path.write_bytes(original + b'changed')
        self.assert_rejected_without_render_or_write()

    def test_bad_hash_is_rejected(self):
        for digest in (None, '', 'invalid', '0' * 64):
            with self.subTest(digest=digest):
                self.mapping[0]['output_sha256'] = digest
                self.write_json('file-map.json', self.mapping)
                self.assert_rejected_without_render_or_write()

    def test_duplicate_page_or_output_mapping_is_rejected(self):
        duplicate = copy.deepcopy(self.mapping[0])
        self.write_json('file-map.json', self.mapping + [duplicate])
        self.assert_rejected_without_render_or_write()
        duplicate['normalized_name'] = '000002.png'
        self.write_json('file-map.json', self.mapping + [duplicate])
        self.assert_rejected_without_render_or_write()

    def test_missing_malformed_or_running_status_is_rejected(self):
        (self.work / 'status.json').unlink()
        self.assert_rejected_without_render_or_write()
        (self.work / 'status.json').write_text('{', encoding='utf-8')
        self.assert_rejected_without_render_or_write()
        for status in ([], {}, {'status': 'completed'},
                       {'status': 'completed', 'failed_pages': []},
                       {'status': 'completed', 'failed_pages': {'000002.png': None}},
                       {'status': 'running', 'failed_pages': {}}):
            with self.subTest(status=status):
                self.write_json('status.json', status)
                self.assert_rejected_without_render_or_write()

    def test_malformed_or_missing_mapping_is_rejected(self):
        (self.work / 'file-map.json').unlink()
        self.assert_rejected_without_render_or_write()
        for mapping in ({}, [None], [], [{'normalized_name': '000001.png', 'output_name': 'other.png'}]):
            with self.subTest(mapping=mapping):
                self.write_json('file-map.json', mapping)
                self.assert_rejected_without_render_or_write()

    def test_explicit_pending_state_and_malformed_pending_state_are_rejected(self):
        for key in ('pending_pages', 'review_required_pages'):
            for pending in (['000001.png'], {'000001.png': {}}, None, [None]):
                with self.subTest(key=key, pending=pending):
                    self.write_json('status.json', dict(self.status, **{key: pending}))
                    self.assert_rejected_without_render_or_write()

    def test_state_is_reloaded_after_view_was_opened(self):
        renderer.validate_published_page(self.context)
        self.write_json('status.json', {'status': 'partial', 'failed_pages': {
            '000001.png': {'type': 'ReviewRequired'}}})
        self.assert_rejected_without_render_or_write()

    def test_escaping_output_path_or_work_directory_is_rejected(self):
        self.assert_rejected_without_render_or_write(dict(self.context, output_name='../outside.png'))
        self.assert_rejected_without_render_or_write(dict(self.context, work_dir=self.output.parent))

    def test_offscreen_dialog_rechecks_pending_state_for_button_and_ctrl_s(self):
        os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
        from PyQt6 import QtCore, QtGui, QtWidgets

        gui_spec = importlib.util.spec_from_file_location(
            'public_test_review_window', ROOT / 'tools/local-manga-translation/review_window.py')
        gui = importlib.util.module_from_spec(gui_spec)
        with patch.dict(sys.modules, {'page_fast_renderer': renderer}):
            gui_spec.loader.exec_module(gui)
        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

        page = '000001.png'
        self.mapping[0].update(source_name='source.png', dimensions=[8, 8])
        self.write_json('file-map.json', self.mapping)
        source = self.work / 'inputs' / page
        source.parent.mkdir()
        Image.new('RGB', (8, 8), 'white').save(source)
        project = self.work / 'scheduled/batch-0001/attempt-001/project'
        (project / 'inpainted').mkdir(parents=True)
        Image.new('RGB', (8, 8), 'gray').save(project / 'inpainted' / page)
        (project / 'imgtrans_project.json').write_text(
            json.dumps({'pages': {page: []}}), encoding='utf-8')
        (self.work / 'scheduled/schedule.json').write_text(json.dumps({
            'chunks': [{'pages': [page], 'attempts': ['batch-0001/attempt-001']}]}), encoding='utf-8')
        view = {'page': page, 'source_path': str(source), 'preview_path': str(self.image_path),
                'size': [8, 8], 'regions': [], 'confirmed_ids': [], 'approved': True}
        pages = [{'page_index': 1, 'source_name': 'source.png', 'normalized_name': page,
                  'details_path': str(self.work / 'synthetic-review.json')}]

        def no_approval(*args):
            self.fail('Fast edit test must not call the approval or job pipeline')

        dialog = gui.ReviewDialog(pages, lambda path: copy.deepcopy(view),
                                  no_approval, no_approval, output_dir=self.output)
        try:
            dialog.show()
            app.processEvents()
            self.assertTrue(dialog.btn_save_publish.isEnabled())
            original_hash = hashlib.sha256(self.image_path.read_bytes()).hexdigest()
            # No text blocks exist in this fixture, so the renderer needs no font.
            with patch.object(renderer, 'get_default_font_path', return_value='unused-no-text'):
                dialog.btn_save_publish.click()
            self.assertTrue((self.work / 'overrides/000001.json').is_file())
            self.assertNotEqual(hashlib.sha256(self.image_path.read_bytes()).hexdigest(), original_hash)
            self.assertIn('已成功儲存', dialog.progress.text())

            # The open view and enabled button are deliberately stale. Only disk
            # state changes, as could happen after an external recovery/update.
            self.write_json('status.json', {'status': 'partial', 'failed_pages': {
                page: {'type': 'ReviewRequired'}}})
            self.assertTrue(dialog.btn_save_publish.isEnabled())
            self.assertTrue(dialog.view['approved'])
            before = self.snapshot()
            with patch.object(renderer, 'render_page_image') as render:
                event = QtGui.QKeyEvent(QtCore.QEvent.Type.KeyPress, QtCore.Qt.Key.Key_S,
                                        QtCore.Qt.KeyboardModifier.ControlModifier)
                app.sendEvent(dialog, event)
                self.assertIn('待審查', dialog.message.toPlainText())
                self.assertEqual(self.snapshot(), before)
                dialog.save_and_publish_current()
                render.assert_not_called()
                self.assertEqual(self.snapshot(), before)
            dialog.update_controls()
            self.assertFalse(dialog.btn_save_publish.isEnabled())
            self.assertIn('待審查', dialog.btn_save_publish.toolTip())
        finally:
            dialog.render_timer.stop()
            dialog.close()
            dialog.deleteLater()
            app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)


if __name__ == '__main__':
    unittest.main()
