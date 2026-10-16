# -*- coding: utf-8 -*-
"""ProgressTracker: when a scrobble is due."""
import unittest
from unittest import mock

import tests.kodi_stubs  # noqa: F401

from lib import progress_tracker
from lib.progress_tracker import MIN_INTERVAL, ProgressTracker


class _Media:
    media_type, title, season, episode, show_title = 'movie', 'Heat', 0, 0, ''
    external_ids = {'imdb': 'tt1'}
    db_id = 7
    total_time = 3600.0

    def __init__(self, percentage=10.0, paused=False):
        self.percentage, self.is_paused = percentage, paused


def _settings(poll='30', threshold='80'):
    addon = mock.MagicMock()
    addon.getSetting.side_effect = lambda k: {'poll_interval': poll, 'watched_threshold': threshold}.get(k, '')
    return mock.patch.object(progress_tracker, 'ADDON', addon)


class TestTracker(unittest.TestCase):
    def setUp(self):
        p = _settings()
        p.start()
        self.addCleanup(p.stop)
        self.t = ProgressTracker()

    def test_no_session_never_scrobbles(self):
        self.assertFalse(self.t.has_session)
        self.assertFalse(self.t.should_scrobble(_Media(), 1000))
        self.assertFalse(self.t.record_scrobble(_Media(), 1000))

    def test_session_carries_the_rating_signal_fields(self):
        self.t.start_session(_Media())
        s = self.t.snapshot()
        self.assertEqual((s.title, s.db_id, s.external_ids, s.watched_sent), ('Heat', 7, {'imdb': 'tt1'}, False))
        self.t.end_session()
        self.assertIsNone(self.t.snapshot())
        self.assertFalse(self.t.has_session)

    def test_first_update_fires_immediately(self):
        self.t.start_session(_Media())
        self.assertTrue(self.t.should_scrobble(_Media(), 10_000.0))

    def test_paused_never_fires(self):
        self.t.start_session(_Media())
        self.assertFalse(self.t.should_scrobble(_Media(paused=True), 10_000.0))

    def test_hard_floor_between_scrobbles(self):
        m = _Media(10)
        self.t.start_session(m)
        self.t.record_scrobble(m, 1000.0)
        self.assertFalse(self.t.should_scrobble(_Media(50), 1000.0 + MIN_INTERVAL - 1))   # big seek, but too soon

    def test_timed_interval(self):
        m = _Media(10)
        self.t.start_session(m)
        self.t.record_scrobble(m, 1000.0)
        self.assertFalse(self.t.should_scrobble(_Media(10.5), 1000.0 + 29))
        self.assertTrue(self.t.should_scrobble(_Media(10.5), 1000.0 + 30))

    def test_seek_delta_after_the_floor(self):
        m = _Media(10)
        self.t.start_session(m)
        self.t.record_scrobble(m, 1000.0)
        self.assertTrue(self.t.should_scrobble(_Media(15.0), 1000.0 + MIN_INTERVAL))
        self.assertFalse(self.t.should_scrobble(_Media(14.9), 1000.0 + MIN_INTERVAL))

    def test_watched_threshold_fires_once(self):
        m = _Media(10)
        self.t.start_session(m)
        self.t.record_scrobble(m, 1000.0)
        crossing = _Media(80.0)
        self.assertTrue(self.t.should_scrobble(crossing, 1000.0 + MIN_INTERVAL))
        self.assertTrue(self.t.record_scrobble(crossing, 1000.0 + MIN_INTERVAL))
        self.assertTrue(self.t.snapshot().watched_sent)
        # after crossing: no more watched-triggered sends, and record_scrobble reports the crossing only once
        self.assertFalse(self.t.should_scrobble(_Media(81.0), 1000.0 + 2 * MIN_INTERVAL))
        self.assertFalse(self.t.record_scrobble(_Media(82.0), 1000.0 + 2 * MIN_INTERVAL))

    def test_settings_blank_fall_back_to_defaults(self):
        with _settings(poll='', threshold=''):
            m = _Media(10)
            self.t.start_session(m)
            self.t.record_scrobble(m, 1000.0)
            self.assertFalse(self.t.should_scrobble(_Media(10.1), 1000.0 + 29))
            self.assertTrue(self.t.should_scrobble(_Media(10.1), 1000.0 + 30))
            self.assertTrue(self.t.record_scrobble(_Media(80.0), 1100.0))


if __name__ == '__main__':
    unittest.main()
