# -*- coding: utf-8 -*-
"""PlaylistSync + kodi_matcher: Chronicle lists written as .m3u playlists from what Kodi's library holds."""
import json
import os
import tempfile
import unittest
from unittest import mock

import tests.kodi_stubs  # noqa: F401
import xbmc

from lib import kodi_matcher, playlist_sync
from lib.playlist_sync import PlaylistSync


def _item(name, mtype, ids=None, year=None, position=0, minutes=None):
    return {'position': position, 'mediaItem': {
        'name': name, 'mediaTypeName': mtype, 'year': year, 'runtimeMinutes': minutes,
        'externalIds': [{'source': k, 'externalId': v} for k, v in (ids or {}).items()]}}


class _Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        for target, new in (
                ('translatePath', mock.Mock(side_effect=lambda p: p.replace(playlist_sync._PLAYLIST_DIR, self.dir.name + os.sep))),
                ('mkdirs', mock.Mock())):
            p = mock.patch.object(playlist_sync.xbmcvfs, target, new)
            p.start()
            self.addCleanup(p.stop)
        self.matcher = mock.patch.object(playlist_sync, 'kodi_matcher')
        self.km = self.matcher.start()
        self.addCleanup(self.matcher.stop)
        self.sync = PlaylistSync.__new__(PlaylistSync)
        self.sync._client = mock.MagicMock()

    def _read(self, name):
        with open(os.path.join(self.dir.name, name), encoding='utf-8') as f:
            return f.read().split('\n')


class TestWrite(_Base):
    def test_ordered_list_is_sorted_by_position_with_runtime(self):
        self.km.find_movie.side_effect = lambda ids, title, year: {'file': '/m/{0}.mkv'.format(title)}
        lst = {'id': 1, 'name': 'Faves', 'isOrdered': True,
               'items': [_item('B', 'Movie', position=2, minutes=100), _item('A', 'Movie', position=1, minutes=90)]}
        self.assertTrue(self.sync._write_playlist(lst))
        self.assertEqual(self._read('chronicle_Faves.m3u'),
                         ['#EXTM3U', '#EXTINF:5400,A', '/m/A.mkv', '#EXTINF:6000,B', '/m/B.mkv'])

    def test_unmatched_items_are_noted_and_no_match_at_all_writes_nothing(self):
        self.km.find_movie.return_value = None
        lst = {'id': 1, 'name': 'None found', 'items': [_item('X', 'Movie')]}
        self.assertFalse(self.sync._write_playlist(lst))
        self.assertFalse(os.path.exists(os.path.join(self.dir.name, 'chronicle_None_found.m3u')))

    def test_empty_list_counts_as_success_without_a_file(self):
        self.assertTrue(self.sync._write_playlist({'id': 1, 'name': 'Empty', 'items': []}))
        self.assertEqual(os.listdir(self.dir.name), [])

    def test_partial_match_keeps_a_not_found_marker(self):
        self.km.find_movie.side_effect = lambda ids, title, year: {'file': '/m/a.mkv'} if title == 'A' else None
        lst = {'id': 1, 'name': 'Mixed', 'items': [_item('A', 'Movie', position=1), _item('Z', 'Movie', position=2)]}
        self.assertTrue(self.sync._write_playlist(lst))
        self.assertIn('# NOT FOUND: Z', self._read('chronicle_Mixed.m3u'))

    def test_names_that_sanitise_alike_do_not_overwrite_each_other(self):
        self.km.find_movie.return_value = {'file': '/m/a.mkv'}
        used = set()
        for list_id, name in ((1, 'A/B'), (2, 'A_B')):
            self.assertTrue(self.sync._write_playlist({'id': list_id, 'name': name, 'items': [_item('A', 'Movie')]}, used))
        self.assertEqual(sorted(os.listdir(self.dir.name)), ['chronicle_A_B.m3u', 'chronicle_A_B_2.m3u'])

    def test_blank_name_gets_a_usable_filename(self):
        self.km.find_movie.return_value = {'file': '/m/a.mkv'}
        self.assertTrue(self.sync._write_playlist({'id': 1, 'name': '   ', 'items': [_item('A', 'Movie')]}))
        self.assertEqual(os.listdir(self.dir.name), ['chronicle_Untitled.m3u'])

    def test_tv_show_expands_to_its_episodes_in_order(self):
        self.km.find_tvshow.return_value = {'tvshowid': 4, 'title': 'Show'}
        self.km.get_show_episodes.return_value = [
            {'season': 1, 'episode': 1, 'file': '/tv/s01e01.mkv'}, {'season': 1, 'episode': 2, 'file': '/tv/s01e02.mkv'}]
        lst = {'id': 1, 'name': 'Shows', 'items': [_item('Show', 'TV Show', {'tvdb': '5'})]}
        self.assertTrue(self.sync._write_playlist(lst))
        self.assertEqual(self._read('chronicle_Shows.m3u'),
                         ['#EXTM3U', '#EXTINF:-1,Show S01E01', '/tv/s01e01.mkv', '#EXTINF:-1,Show S01E02', '/tv/s01e02.mkv'])
        self.km.get_show_episodes.assert_called_once_with(4)

    def test_tv_show_missing_from_kodi_never_matches_a_movie_of_the_same_name(self):
        self.km.find_tvshow.return_value = None
        self.assertEqual(self.sync._resolve_entries('tv show', {}, 'Heat', 1995, 0), [])
        self.km.find_movie.assert_not_called()

    def test_movie_without_a_file_is_not_found(self):
        self.km.find_movie.return_value = {'title': 'x'}
        self.assertEqual(self.sync._resolve_entries('movie', {}, 'x', None, 0), [])

    def test_write_failure_returns_false(self):
        self.km.find_movie.return_value = {'file': '/m/a.mkv'}
        with mock.patch('builtins.open', side_effect=OSError('disk full')):
            self.assertFalse(self.sync._write_playlist({'id': 1, 'name': 'X', 'items': [_item('A', 'Movie')]}))


class TestSyncAll(_Base):
    def setUp(self):
        super().setUp()
        p = mock.patch.object(playlist_sync.xbmcgui, 'DialogProgress')
        self.progress = p.start().return_value
        self.progress.iscanceled.return_value = False
        self.addCleanup(p.stop)
        self.km.find_movie.return_value = {'file': '/m/a.mkv'}

    def test_none_means_chronicle_could_not_be_asked(self):
        self.sync._client.get_lists.return_value = None
        self.assertIsNone(self.sync.sync_all())

    def test_no_lists_is_zero_zero(self):
        self.sync._client.get_lists.return_value = []
        self.assertEqual(self.sync.sync_all(), (0, 0))

    def test_counts_written_and_failed(self):
        self.sync._client.get_lists.return_value = [{'id': 1, 'name': 'A'}, {'id': 2, 'name': 'B'}, {'id': 3, 'name': 'C'}]
        details = {1: {'id': 1, 'name': 'A', 'items': [_item('x', 'Movie')]}, 2: {}, 3: {'id': 3, 'name': 'C', 'items': [_item('x', 'Movie')]}}
        self.sync._client.get_list.side_effect = lambda i: details[i]
        self.assertEqual(self.sync.sync_all(), (2, 1))
        self.progress.close.assert_called_once()

    def test_cancel_stops_early(self):
        self.sync._client.get_lists.return_value = [{'id': 1, 'name': 'A'}, {'id': 2, 'name': 'B'}]
        self.progress.iscanceled.return_value = True
        self.assertEqual(self.sync.sync_all(), (0, 0))
        self.sync._client.get_list.assert_not_called()


class TestMatcher(unittest.TestCase):
    def _rpc(self, handler):
        calls = []

        def fake(raw):
            req = json.loads(raw)
            calls.append(req)
            return json.dumps({'result': handler(req)})
        return mock.patch.object(xbmc, 'executeJSONRPC', side_effect=fake), calls

    def test_movie_imdb_then_uniqueid_then_tmdb_then_title_year(self):
        def handler(req):
            f = req['params']['filter']
            if f['field'] == 'title':
                return {'movies': [{'year': 1999, 'file': 'wrong'}, {'year': 1995, 'file': 'right'}]}
            return {}
        patch, calls = self._rpc(handler)
        with patch:
            m = kodi_matcher.find_movie({'imdb': 'tt1', 'tmdb': '2'}, 'Heat', 1995)
        self.assertEqual(m['file'], 'right')
        fields = [(c['params']['filter']['field'], c['params']['filter']['value']) for c in calls]
        self.assertEqual(fields, [('imdbnumber', 'tt1'), ('uniqueid', 'imdb:tt1'), ('uniqueid', 'tmdb:2'), ('title', 'Heat')])

    def test_movie_without_ids_or_title_is_none(self):
        self.assertIsNone(kodi_matcher.find_movie({}, '', None))

    def test_show_tries_tvdb_tmdb_imdb_in_order(self):
        patch, calls = self._rpc(lambda req: {'tvshows': [{'tvshowid': 1}]} if req['params']['filter']['value'] == 'tmdb:9' else {})
        with patch:
            self.assertEqual(kodi_matcher.find_tvshow({'tvdb': '1', 'tmdb': '9', 'imdb': 'tt1'}, 'S')['tvshowid'], 1)
        self.assertEqual([c['params']['filter']['value'] for c in calls], ['tvdb:1', 'tmdb:9'])

    def test_episodes_of_a_show_are_sorted_and_must_have_a_file(self):
        patch, _ = self._rpc(lambda req: {'episodes': [
            {'season': 2, 'episode': 1, 'file': 'c'}, {'season': 1, 'episode': 2, 'file': 'b'},
            {'season': 1, 'episode': 1, 'file': 'a'}, {'season': 1, 'episode': 3, 'file': ''}]})
        with patch:
            self.assertEqual([e['file'] for e in kodi_matcher.get_show_episodes(4)], ['a', 'b', 'c'])
        self.assertEqual(kodi_matcher.get_show_episodes(None), [])

    def test_find_episode_filters_by_season_and_episode(self):
        patch, calls = self._rpc(lambda req: {'episodes': [{'episodeid': 3}]})
        with patch:
            self.assertEqual(kodi_matcher.find_episode(4, 1, 2)['episodeid'], 3)
        self.assertEqual(calls[0]['params']['tvshowid'], 4)
        self.assertIsNone(kodi_matcher.find_episode(None, 1, 2))


if __name__ == '__main__':
    unittest.main()
