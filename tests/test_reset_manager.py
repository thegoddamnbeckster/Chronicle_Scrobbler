# -*- coding: utf-8 -*-
"""ResetManager: picking, confirming and resetting Kodi watch progress."""
import unittest
from unittest import mock

import tests.kodi_stubs  # noqa: F401

from lib import reset_manager
from lib.reset_manager import ResetManager


class TestReset(unittest.TestCase):
    def setUp(self):
        self.rpc = mock.Mock()
        for target, new in (('KodiJsonRpc', mock.Mock(call=self.rpc)),):
            p = mock.patch.object(reset_manager, target, new)
            p.start()
            self.addCleanup(p.stop)
        self.dialog = mock.MagicMock()
        p = mock.patch.object(reset_manager.xbmcgui, 'Dialog', return_value=self.dialog)
        p.start()
        self.addCleanup(p.stop)
        self.progress = mock.MagicMock()
        self.progress.iscanceled.return_value = False
        p = mock.patch.object(reset_manager.xbmcgui, 'DialogProgress', return_value=self.progress)
        p.start()
        self.addCleanup(p.stop)

    def test_no_shows_or_movies_tells_the_user(self):
        self.rpc.return_value = {}
        ResetManager().prompt_reset_tvshow()
        ResetManager().prompt_reset_movie()
        self.assertEqual(self.dialog.ok.call_count, 2)

    def test_cancelled_pick_or_declined_confirmation_resets_nothing(self):
        self.rpc.return_value = {'movies': [{'movieid': 1, 'label': 'Heat'}]}
        self.dialog.select.return_value = -1
        ResetManager().prompt_reset_movie()
        self.dialog.select.return_value = 0
        self.dialog.yesno.return_value = False
        ResetManager().prompt_reset_movie()
        self.assertEqual([c[0][0] for c in self.rpc.call_args_list], ['VideoLibrary.GetMovies'] * 2)

    def test_movie_reset_clears_playcount_lastplayed_and_resume(self):
        self.rpc.side_effect = [{'movies': [{'movieid': 5, 'label': 'Heat'}]}, 'OK']
        self.dialog.select.return_value = 0
        self.dialog.yesno.return_value = True
        ResetManager().prompt_reset_movie()
        method, params = self.rpc.call_args_list[1][0]
        self.assertEqual(method, 'VideoLibrary.SetMovieDetails')
        self.assertEqual(params, {'movieid': 5, 'playcount': 0, 'lastplayed': '', 'resume': {'position': 0, 'total': 0}})

    def test_movie_reset_failure_is_reported(self):
        self.rpc.return_value = {}
        ResetManager()._reset_movie(5, 'Heat')
        self.dialog.ok.assert_called_once()

    def test_show_reset_visits_every_episode_and_counts_errors(self):
        episodes = [{'episodeid': i, 'season': 1, 'episode': i} for i in (1, 2, 3)]
        self.rpc.side_effect = [{'episodes': episodes}, 'OK', {}, 'OK']
        ResetManager()._reset_tvshow(4, 'Show')
        sets = [c for c in self.rpc.call_args_list if c[0][0] == 'VideoLibrary.SetEpisodeDetails']
        self.assertEqual([c[0][1]['episodeid'] for c in sets], [1, 2, 3])
        self.assertTrue(all(c[0][1]['playcount'] == 0 for c in sets))
        self.progress.close.assert_called_once()

    def test_show_reset_can_be_cancelled(self):
        self.rpc.side_effect = [{'episodes': [{'episodeid': 1}, {'episodeid': 2}]}, 'OK']
        self.progress.iscanceled.return_value = True
        ResetManager()._reset_tvshow(4, 'Show')
        self.assertEqual(self.rpc.call_count, 1)

    def test_show_without_episodes_does_nothing(self):
        self.rpc.return_value = {'episodes': []}
        ResetManager()._reset_tvshow(4, 'Show')
        self.progress.create.assert_not_called()


if __name__ == '__main__':
    unittest.main()
