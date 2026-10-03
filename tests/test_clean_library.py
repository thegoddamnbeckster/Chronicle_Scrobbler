# -*- coding: utf-8 -*-
"""
Tests for ChronicleMonitor._maybe_clean_video_library -- the VideoLibrary.Clean that should run after
every video library scan (added 2026-10-03).

It called VideoLibrary.Clean with {'showdialog': False}; Kodi's parameter is `showdialogs`, so Kodi
rejected every call ("Too many parameters") and the clean-up never ran. The rejection was logged as
"VideoLibrary.Clean triggered", so nothing looked wrong: renamed/moved folders just kept stale
duplicate library entries forever.
"""
import json
import os
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _install_kodi_stubs():
    xbmc = types.ModuleType('xbmc')
    xbmc.LOGDEBUG, xbmc.LOGINFO, xbmc.LOGWARNING, xbmc.LOGERROR = 0, 2, 3, 4
    xbmc.log = MagicMock()
    xbmc.executeJSONRPC = MagicMock(return_value='{"id":1,"jsonrpc":"2.0","result":"OK"}')
    xbmc.Monitor = type('Monitor', (), {'__init__': lambda self, *a, **k: None})
    xbmc.Player = type('Player', (), {'__init__': lambda self, *a, **k: None})

    xbmcaddon = types.ModuleType('xbmcaddon')
    xbmcaddon.Addon = MagicMock()

    xbmcgui = types.ModuleType('xbmcgui')
    xbmcgui.Dialog = MagicMock()
    xbmcgui.NOTIFICATION_INFO = xbmcgui.NOTIFICATION_WARNING = xbmcgui.NOTIFICATION_ERROR = 0

    xbmcvfs = types.ModuleType('xbmcvfs')
    xbmcvfs.translatePath = MagicMock(side_effect=lambda p: p)
    xbmcvfs.exists = MagicMock(return_value=False)
    xbmcvfs.File = MagicMock()

    for name, module in (('xbmc', xbmc), ('xbmcaddon', xbmcaddon), ('xbmcgui', xbmcgui), ('xbmcvfs', xbmcvfs)):
        sys.modules[name] = module


_install_kodi_stubs()

from lib import monitor  # noqa: E402


def _the_monitor():
    return monitor.ChronicleMonitor.__new__(monitor.ChronicleMonitor)


# Kodi's own names for VideoLibrary.Clean's parameters (JSONRPC.Introspect on Kodi 21).
_KODI_CLEAN_PARAMS = {'showdialogs', 'content', 'directory'}


class TestCleanLibraryCall(unittest.TestCase):

    def setUp(self):
        monitor.xbmcvfs.exists.return_value = False      # no throttle marker: the call is made
        monitor.xbmcvfs.File.reset_mock()
        monitor.xbmc.executeJSONRPC.reset_mock()
        monitor.xbmc.executeJSONRPC.return_value = '{"id":1,"jsonrpc":"2.0","result":"OK"}'

    def _clean(self):
        with patch.object(monitor.log, 'info') as info, \
             patch.object(monitor.log, 'error') as error:
            _the_monitor()._maybe_clean_video_library()
        return info, error

    def test_calls_videolibrary_clean_with_only_parameters_kodi_actually_has(self):
        self._clean()

        request = json.loads(monitor.xbmc.executeJSONRPC.call_args.args[0])
        self.assertEqual(request['method'], 'VideoLibrary.Clean')
        self.assertTrue(set(request['params']) <= _KODI_CLEAN_PARAMS,
                        'unknown VideoLibrary.Clean parameter(s): {0}'.format(
                            sorted(set(request['params']) - _KODI_CLEAN_PARAMS)))
        self.assertEqual(request['params'], {'showdialogs': False})   # no dialog over the user's screen

    def test_a_successful_call_is_logged_and_starts_the_throttle_window(self):
        info, error = self._clean()

        error.assert_not_called()
        self.assertTrue(any('triggered' in c.args[0] for c in info.call_args_list))
        monitor.xbmcvfs.File.assert_called()               # the throttle marker was written

    def test_a_call_kodi_rejects_is_an_error_and_does_not_start_the_throttle_window(self):
        monitor.xbmc.executeJSONRPC.return_value = json.dumps(
            {'error': {'code': -32602, 'message': 'Invalid params.'}, 'id': 1, 'jsonrpc': '2.0'})

        info, error = self._clean()

        error.assert_called_once()
        self.assertIn('rejected', error.call_args.args[0])
        self.assertFalse(any('triggered' in c.args[0] for c in info.call_args_list))   # not "triggered"
        monitor.xbmcvfs.File.assert_not_called()           # a failed call must not delay the retry

    def test_a_recent_clean_is_throttled(self):
        import time
        monitor.xbmcvfs.exists.return_value = True
        marker = MagicMock()
        marker.read.return_value = str(time.time())
        monitor.xbmcvfs.File.return_value = marker

        self._clean()

        monitor.xbmc.executeJSONRPC.assert_not_called()


if __name__ == '__main__':
    unittest.main()
