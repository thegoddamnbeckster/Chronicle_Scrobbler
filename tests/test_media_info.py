# -*- coding: utf-8 -*-
"""MediaInfo: what is playing, the payloads sent to Chronicle, and which external ids identify an episode."""
import json
import unittest
from unittest import mock

import tests.kodi_stubs  # noqa: F401
import xbmc

from lib import media_info
from lib.media_info import KodiJsonRpc, MediaInfo


def _rpc(responses):
    """executeJSONRPC fake: method name -> result (or an 'error' marker)."""
    calls = []

    def fake(raw):
        req = json.loads(raw)
        calls.append(req)
        out = responses[req['method']]
        if isinstance(out, dict) and 'error' in out:
            return json.dumps(out)
        return json.dumps({'result': out})

    return mock.patch.object(xbmc, 'executeJSONRPC', side_effect=fake), calls


def _movie(**kw):
    item = {'type': 'movie', 'id': 7, 'title': 'Heat', 'year': 1995, 'uniqueid': {'imdb': 'tt0113277', 'tmdb': '949'}}
    item.update(kw)
    return item


class TestKodiJsonRpc(unittest.TestCase):
    def test_error_response_gives_empty_dict(self):
        patch, _ = _rpc({'X.Y': {'error': {'message': 'bad'}}})
        with patch:
            self.assertEqual(KodiJsonRpc.call('X.Y'), {})

    def test_params_are_sent_only_when_given(self):
        patch, calls = _rpc({'A': {}, 'B': {}})
        with patch:
            KodiJsonRpc.call('A')
            KodiJsonRpc.call('B', {'k': 1})
        self.assertNotIn('params', calls[0])
        self.assertEqual(calls[1]['params'], {'k': 1})


class TestGetCurrent(unittest.TestCase):
    def setUp(self):
        media_info._show_ids_cache.clear()

    def test_nothing_playing(self):
        patch, _ = _rpc({'Player.GetActivePlayers': []})
        with patch:
            self.assertIsNone(MediaInfo.get_current())

    def test_picture_player_is_ignored(self):
        patch, calls = _rpc({'Player.GetActivePlayers': [{'playerid': 2, 'type': 'picture'}]})
        with patch:
            self.assertIsNone(MediaInfo.get_current())
        self.assertEqual(len(calls), 1)

    def test_empty_item_is_none(self):
        patch, _ = _rpc({'Player.GetActivePlayers': [{'playerid': 1, 'type': 'video'}], 'Player.GetItem': {'item': {}}})
        with patch:
            self.assertIsNone(MediaInfo.get_current())

    def test_movie_snapshot(self):
        patch, calls = _rpc({
            'Player.GetActivePlayers': [{'playerid': 1, 'type': 'video'}],
            'Player.GetItem': {'item': _movie()},
            'Player.GetProperties': {'percentage': 42.5, 'speed': 1, 'time': {'minutes': 1}, 'totaltime': {'hours': 1}},
        })
        with patch:
            m = MediaInfo.get_current()
        self.assertEqual((m.media_type, m.title, m.year, m.db_id), ('movie', 'Heat', 1995, 7))
        self.assertEqual((m.percentage, m.current_time, m.total_time), (42.5, 60.0, 3600.0))
        self.assertEqual([c['method'] for c in calls].count('VideoLibrary.GetTVShowDetails'), 0)

    def test_episode_uses_the_shows_ids_not_the_episodes(self):
        patch, calls = _rpc({
            'Player.GetActivePlayers': [{'playerid': 1, 'type': 'video'}],
            'Player.GetItem': {'item': {'type': 'episode', 'id': 9, 'title': 'Pilot', 'showtitle': 'Show', 'season': 1,
                                        'episode': 2, 'tvshowid': 4, 'uniqueid': {'tvdb': '999999', 'imdb': 'tt-episode'}}},
            'Player.GetProperties': {'percentage': 10.0},
            'VideoLibrary.GetTVShowDetails': {'tvshowdetails': {'uniqueid': {'tvdb': '81189'}, 'imdbnumber': 'tt0903747'}},
        })
        with patch:
            m = MediaInfo.get_current()
            m2 = MediaInfo.get_current()
        self.assertEqual(m.external_ids, {'tvdb': '81189', 'imdb': 'tt0903747'})
        self.assertEqual(m2.external_ids, m.external_ids)
        # the show lookup is cached: one call for two snapshots
        self.assertEqual([c['method'] for c in calls].count('VideoLibrary.GetTVShowDetails'), 1)

    def test_episode_without_a_resolvable_show_sends_no_ids(self):
        patch, _ = _rpc({
            'Player.GetActivePlayers': [{'playerid': 1, 'type': 'video'}],
            'Player.GetItem': {'item': {'type': 'episode', 'title': 'Pilot', 'showtitle': 'Show',
                                        'uniqueid': {'tvdb': '999999'}}},   # no tvshowid (e.g. played from a file)
            'Player.GetProperties': {},
        })
        with patch:
            self.assertEqual(MediaInfo.get_current().external_ids, {})

    def test_failed_show_lookup_is_not_cached(self):
        patch, calls = _rpc({
            'Player.GetActivePlayers': [{'playerid': 1, 'type': 'video'}],
            'Player.GetItem': {'item': {'type': 'episode', 'showtitle': 'S', 'tvshowid': 4}},
            'Player.GetProperties': {},
            'VideoLibrary.GetTVShowDetails': {'error': {'message': 'busy'}},
        })
        with patch:
            MediaInfo.get_current()
            MediaInfo.get_current()
        self.assertEqual([c['method'] for c in calls].count('VideoLibrary.GetTVShowDetails'), 2)


class TestAccessors(unittest.TestCase):
    def test_media_types(self):
        for kodi, ours in (('movie', 'movie'), ('episode', 'episode'), ('song', 'track'), ('musicvideo', 'unknown'), ('', 'unknown')):
            self.assertEqual(MediaInfo(1, {'type': kodi}, {}).media_type, ours)

    def test_year_zero_is_none_not_zero(self):
        self.assertIsNone(MediaInfo(1, {'type': 'episode', 'year': 0}, {}).year)
        self.assertEqual(MediaInfo(1, {'year': 2013}, {}).year, 2013)

    def test_total_time_falls_back_to_item_runtime_then_duration(self):
        self.assertEqual(MediaInfo(1, {'runtime': 5400}, {}).total_time, 5400.0)
        self.assertEqual(MediaInfo(1, {'duration': 200}, {}).total_time, 200.0)
        self.assertEqual(MediaInfo(1, {}, {}).total_time, 0.0)

    def test_paused_and_resume_position(self):
        m = MediaInfo(1, {'resume': {'position': 12.5}}, {'speed': 0})
        self.assertTrue(m.is_paused)
        self.assertEqual(m.resume_position, 12.5)
        self.assertEqual(MediaInfo(1, {'resume': None}, {}).resume_position, 0.0)

    def test_db_id_defaults_to_minus_one(self):
        self.assertEqual(MediaInfo(1, {}, {}).db_id, -1)
        self.assertEqual(MediaInfo(1, {'id': 0}, {}).db_id, -1)

    def test_movie_imdbnumber_fallbacks(self):
        self.assertEqual(MediaInfo(1, {'type': 'movie', 'imdbnumber': 'tt5'}, {}).external_ids, {'imdb': 'tt5'})
        self.assertEqual(MediaInfo(1, {'type': 'movie', 'imdbnumber': '603'}, {}).external_ids, {'tmdb': '603'})
        # a real uniqueid wins; a numeric imdbnumber never overrides an existing tmdb id
        ids = MediaInfo(1, {'type': 'movie', 'imdbnumber': '603', 'uniqueid': {'tmdb': '1'}}, {}).external_ids
        self.assertEqual(ids, {'tmdb': '1'})

    def test_blank_uniqueid_values_are_dropped(self):
        ids = MediaInfo(1, {'type': 'movie', 'uniqueid': {'imdb': ' ', 'tmdb': None, 'tvdb': '5'}}, {}).external_ids
        self.assertEqual(ids, {'tvdb': '5'})


class TestPayloads(unittest.TestCase):
    def setUp(self):
        p = mock.patch.object(media_info, 'get_device_name', return_value='Shield')
        p.start()
        self.addCleanup(p.stop)

    def test_movie_scrobble_payload(self):
        m = MediaInfo(1, _movie(), {'percentage': 33.333})
        self.assertEqual(m.to_scrobble_payload(), {
            'mediaType': 'movie', 'title': 'Heat', 'year': 1995, 'progressPercent': 33.33,
            'externalIds': {'imdb': 'tt0113277', 'tmdb': '949'}, 'deviceName': 'Shield'})

    def test_episode_payload_identifies_the_show_and_adds_episode_fields(self):
        item = {'type': 'episode', 'title': 'Pilot', 'showtitle': 'Show', 'season': 1, 'episode': 2}
        payload = MediaInfo(1, item, {'percentage': 1}, {'uniqueid': {'tvdb': '5'}}).to_scrobble_payload()
        self.assertEqual((payload['title'], payload['season'], payload['episode'], payload['episodeTitle']), ('Show', 1, 2, 'Pilot'))
        self.assertEqual(payload['externalIds'], {'tvdb': '5'})
        self.assertIsNone(payload['year'])

    def test_resume_lookup_payload_has_no_progress_or_device(self):
        item = {'type': 'episode', 'title': 'Pilot', 'showtitle': 'Show', 'season': 1, 'episode': 2}
        payload = MediaInfo(1, item, {}).to_resume_lookup_payload()
        self.assertEqual(set(payload), {'mediaType', 'title', 'year', 'externalIds', 'season', 'episode'})
        self.assertEqual(set(MediaInfo(1, _movie(), {}).to_resume_lookup_payload()), {'mediaType', 'title', 'year', 'externalIds'})


class TestHmsm(unittest.TestCase):
    def test_conversion(self):
        self.assertEqual(media_info._hmsm_to_seconds({'hours': 1, 'minutes': 2, 'seconds': 3, 'milliseconds': 500}), 3723.5)
        self.assertEqual(media_info._hmsm_to_seconds({}), 0.0)
        self.assertEqual(media_info._hmsm_to_seconds(None), 0.0)


if __name__ == '__main__':
    unittest.main()
