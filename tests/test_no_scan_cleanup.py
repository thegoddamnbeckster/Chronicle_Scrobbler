# -*- coding: utf-8 -*-
"""
Guard: this add-on must not clean the video library as part of a scan (v2.4.16, 2026-10-03).

It used to call VideoLibrary.Clean from onScanFinished. That call was misspelled for years, so Kodi
rejected it and nothing happened; fixing the spelling (v2.4.15) made it run after every scan, and the
user's verdict was: "The scan for new items is not the place to clean." Cleaning is left to Kodi's own
Clean library action, run when the user chooses.
"""
import ast
import os
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _python_files():
    for folder in (_ROOT, os.path.join(_ROOT, 'lib')):
        for name in sorted(os.listdir(folder)):
            if name.endswith('.py'):
                yield os.path.join(folder, name)


class TestNoCleanAfterScan(unittest.TestCase):

    def test_nothing_in_the_addon_calls_videolibrary_clean(self):
        offenders = []
        for path in _python_files():
            with open(path, encoding='utf-8') as f:
                tree = ast.parse(f.read())
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                        and node.value == 'VideoLibrary.Clean':
                    offenders.append('{0}:{1}'.format(os.path.relpath(path, _ROOT), node.lineno))
        self.assertEqual(offenders, [])

    def test_the_monitor_has_no_scan_finished_hook(self):
        with open(os.path.join(_ROOT, 'lib', 'monitor.py'), encoding='utf-8') as f:
            tree = ast.parse(f.read())
        methods = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
        self.assertNotIn('onScanFinished', methods)
        self.assertNotIn('_maybe_clean_video_library', methods)


if __name__ == '__main__':
    unittest.main()
