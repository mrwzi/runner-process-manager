from __future__ import annotations

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtGui import QTextOption
from PySide6.QtWidgets import QApplication, QPlainTextEdit

from ui.app import clean_log_display_text, configure_log_wrapping


class LogDisplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt = QApplication.instance() or QApplication([])

    def test_terminal_color_and_title_sequences_are_removed_only_for_display(self):
        raw = "\x1b[31mERROR\x1b[0m title\x1b]0;Runner title\x07\n"
        self.assertEqual(clean_log_display_text(raw), "ERROR title\n")
        self.assertIn("\x1b[31m", raw)  # The source/persisted log string is not mutated.

    def test_wrap_mode_breaks_long_tokens_and_hides_horizontal_scrollbar(self):
        editor = QPlainTextEdit()
        configure_log_wrapping(editor, True)
        self.assertEqual(editor.lineWrapMode(), QPlainTextEdit.WidgetWidth)
        self.assertEqual(editor.wordWrapMode(), QTextOption.WrapAtWordBoundaryOrAnywhere)
        self.assertEqual(editor.horizontalScrollBarPolicy(), Qt.ScrollBarAlwaysOff)

        configure_log_wrapping(editor, False)
        self.assertEqual(editor.lineWrapMode(), QPlainTextEdit.NoWrap)
        self.assertEqual(editor.horizontalScrollBarPolicy(), Qt.ScrollBarAsNeeded)


if __name__ == "__main__":
    unittest.main()
