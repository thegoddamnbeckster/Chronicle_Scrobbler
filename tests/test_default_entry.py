# -*- coding: utf-8 -*-
"""default.py (Scrobbler): the RunScript(action=...) dispatcher and the Sync Lists result messages; service.py's entry."""
import unittest
from unittest import mock

import tests.kodi_stubs  # noqa: F401

import default


class TestDispatch(unittest.TestCase):
    def _run(self, *argv):
        names = ('_connect_to_chronicle', '_change_chronicle_url', '_test_connection', '_sync_lists', '_refresh_auth_status')
        patches = {n: mock.patch.object(default, n) for n in names}
        mocks = {n: p.start() for n, p in patches.items()}
        for p in patches.values():
            self.addCleanup(p.stop)
        reset = mock.patch.object(default, 'ResetManager')
        reset_cls = reset.start()
        self.addCleanup(reset.stop)
        addon = mock.patch.object(default, 'ADDON')
        addon_mock = addon.start()
        self.addCleanup(addon.stop)
        with mock.patch.object(default.sys, 'argv', ['default.py'] + list(argv)):
            default.show_menu()
        mocks['reset'] = reset_cls
        mocks['addon'] = addon_mock
        return mocks

    def test_each_action_runs_only_its_handler(self):
        table = {'auth': '_connect_to_chronicle', 'change_url': '_change_chronicle_url', 'test_connection': '_test_connection',
                 'sync_lists': '_sync_lists'}
        for action, handler in table.items():
            mocks = self._run('action=' + action)
            mock.patch.stopall()
            for name in table.values():
                self.assertEqual(mocks[name].called, name == handler, '{0} for {1}'.format(name, action))
            mocks['addon'].openSettings.assert_not_called()

    def test_reset_actions_use_the_reset_manager(self):
        mocks = self._run('action=reset_tv')
        mocks['reset'].return_value.prompt_reset_tvshow.assert_called_once()
        mock.patch.stopall()
        mocks = self._run('action=reset_movie')
        mocks['reset'].return_value.prompt_reset_movie.assert_called_once()

    def test_no_action_refreshes_the_status_and_opens_settings(self):
        mocks = self._run()
        mocks['_refresh_auth_status'].assert_called_once()
        mocks['addon'].openSettings.assert_called_once()


class TestSyncListsMessages(unittest.TestCase):
    def _sync(self, result):
        dialog = mock.MagicMock()
        addon = mock.MagicMock()
        addon.getLocalizedString.side_effect = lambda i: {32055: 'Done! {0} written, {1} failed.'}.get(i, 'S{0}'.format(i))
        with mock.patch.object(default, 'PlaylistSync') as sync, mock.patch.object(default, 'ADDON', addon), \
                mock.patch.object(default.xbmcgui, 'Dialog', return_value=dialog):
            sync.return_value.sync_all.return_value = result
            default._sync_lists()
        return dialog.ok.call_args[0][1]

    def test_a_failed_request_a_connected_account_with_no_lists_and_a_normal_result_read_differently(self):
        self.assertEqual(self._sync(None), 'S32056')
        self.assertEqual(self._sync((0, 0)), 'S32054')
        self.assertEqual(self._sync((3, 1)), 'Done! 3 written, 1 failed.')


class TestServiceEntry(unittest.TestCase):
    def test_service_starts_the_monitor_and_logs_around_it(self):
        import runpy
        with mock.patch('lib.monitor.ChronicleMonitor') as monitor:
            runpy.run_module('service', run_name='__main__')
        monitor.return_value.run.assert_called_once()


if __name__ == '__main__':
    unittest.main()
