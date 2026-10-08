"""Construct the actual folder GUI offscreen without jobs, network or artwork."""
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

os.environ['QT_QPA_PLATFORM'] = 'offscreen'
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools/local-manga-translation'))


class FolderGuiSmokeTests(unittest.TestCase):
    def test_new_window_does_not_start_a_job(self):
        from PyQt6 import QtCore
        import folder_translator
        with patch('subprocess.Popen', side_effect=AssertionError('Smoke must not create a subprocess')):
            app, window = folder_translator.open_gui()
            try:
                app.processEvents()
                self.assertEqual(window.process.state(), QtCore.QProcess.ProcessState.NotRunning)
                self.assertIsNone(window.source)
                self.assertIsNone(window.output)
                self.assertFalse(window.start.isEnabled())
                self.assertFalse(window.stop.isEnabled())
                self.assertEqual(window.issue_table.rowCount(), 0)
            finally:
                window.close()
                window.deleteLater()
                app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)


if __name__ == '__main__':
    unittest.main()
