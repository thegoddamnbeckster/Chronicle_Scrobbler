# -*- coding: utf-8 -*-
"""ChroniclePlayer / ChronicleMonitor: session lifecycle, the idle-poll cost, resume-from-Chronicle and the rating signal."""
import json
import threading
import unittest
from unittest import mock

import tests.kodi_stubs as stubs

from lib import monitor
from lib.monitor import ChroniclePlayer
from lib.progress_tracker import ProgressTracker


class _Media:
    def __init__(self, media_type='movie', percentage=10.0, resume=0.0, total=3600.0, paused=False):
        self.media_type, self.percentage, self.resume_position = media_type, percentage, resume
        self.total_time, self.is_paused = total, paused
        self.title, self.show_title, self.season, self.episode = 'Heat', 'Show', 1, 2
        self.external_ids, self.db_id = {'imdb': 'tt1'}, 7

    def to_scrobble_payload(self):
        return {'progressPercent': self.percentage}

    def to_resume_lookup_payload(self):
        return {'title': self.title}


class TestPlayer(unittest.TestCase):
    def setUp(self):
        stubs.reset_vfs()
        self.addon = mock.MagicMock()
        self.addon.getSettingBool.return_value = True
        self.addon.getLocalizedString.side_effect = lambda i: '{{0}}#{0}'.format(i)
        for target, new in (('ADDON', self.addon),):
            p = mock.patch.object(monitor, target, new)
            p.start()
            self.addCleanup(p.stop)
        self.client = mock.MagicMock()
        self.client.scrobble.return_value = True
        self.client.get_resume_state.return_value = {}
        self.tracker = ProgressTracker()
        t = mock.patch('lib.progress_tracker.ADDON')
        self.tracker_addon = t.start()
        self.tracker_addon.getSetting.side_effect = lambda k: ''
        self.addCleanup(t.stop)
        self.player = ChroniclePlayer(self.client, self.tracker, threading.Lock())
        n = mock.patch.object(monitor.xbmcgui, 'Dialog')
        self.dialog = n.start()
        self.addCleanup(n.stop)

    def _playing(self, media):
        return mock.patch.object(monitor.MediaInfo, 'get_current', return_value=media)

    # ── idle polling ───────────────────────────────────────────────────────────

    def test_idle_tick_makes_no_json_rpc_calls(self):
        with mock.patch.object(monitor.MediaInfo, 'get_current') as get_current:
            self.player._send_update()
        get_current.assert_not_called()
        self.client.scrobble.assert_not_called()

    def test_tick_while_a_send_is_in_flight_makes_no_json_rpc_calls(self):
        self.tracker.start_session(_Media())
        self.player._sending = True
        with mock.patch.object(monitor.MediaInfo, 'get_current') as get_current:
            self.player._send_update()
        get_current.assert_not_called()

    # ── sending ────────────────────────────────────────────────────────────────

    def test_update_sends_and_records(self):
        media = _Media(percentage=10)
        self.tracker.start_session(media)
        with self._playing(media):
            self.player._send_update()
        self.client.scrobble.assert_called_once_with({'progressPercent': 10})
        self.assertEqual(self.tracker.snapshot().last_percentage, 10)
        self.assertFalse(self.player._sending)

    def test_failed_send_is_not_recorded_and_clears_the_in_flight_flag(self):
        self.client.scrobble.return_value = False
        media = _Media(percentage=10)
        self.tracker.start_session(media)
        with self._playing(media):
            self.player._send_update()
        self.assertEqual(self.tracker.snapshot().last_percentage, 0.0)
        self.assertFalse(self.player._sending)

    def test_client_exception_still_clears_the_in_flight_flag(self):
        self.client.scrobble.side_effect = RuntimeError('boom')
        media = _Media()
        self.tracker.start_session(media)
        with self._playing(media), self.assertRaises(RuntimeError):
            self.player._send_update()
        self.assertFalse(self.player._sending)

    def test_nothing_playing_sends_nothing(self):
        self.tracker.start_session(_Media())
        with self._playing(None):
            self.player._send_update()
        self.client.scrobble.assert_not_called()

    def test_force_overrides_the_due_check(self):
        media = _Media(percentage=10)
        self.tracker.start_session(media)
        self.tracker.record_scrobble(media, 10**9)   # just scrobbled: not due
        with self._playing(media):
            self.player._send_update()
            self.client.scrobble.assert_not_called()
            self.player._send_update(force=True)
        self.client.scrobble.assert_called_once()

    def test_crossing_the_watched_threshold_notifies_once(self):
        media = _Media(percentage=85)
        self.tracker.start_session(media)
        with self._playing(media):
            self.player._send_update()
        self.dialog.return_value.notification.assert_called_once()

    def test_notifications_can_be_turned_off(self):
        self.addon.getSettingBool.side_effect = lambda sid: sid != 'show_notifications'
        media = _Media(percentage=85)
        self.tracker.start_session(media)
        with self._playing(media):
            self.player._send_update()
        self.dialog.return_value.notification.assert_not_called()

    # ── session start ──────────────────────────────────────────────────────────

    def test_start_ignores_unknown_disabled_and_missing_media(self):
        with self._playing(None):
            self.player._on_start()
        with self._playing(_Media('unknown')):
            self.player._on_start()
        self.addon.getSettingBool.side_effect = lambda sid: sid != 'scrobble_movies'
        with self._playing(_Media('movie')):
            self.player._on_start()
        self.assertFalse(self.tracker.has_session)

    def test_type_gates(self):
        self.addon.getSettingBool.side_effect = lambda sid: sid == 'scrobble_music'
        self.assertTrue(ChroniclePlayer._should_scrobble_type('track'))
        self.assertFalse(ChroniclePlayer._should_scrobble_type('movie'))
        self.assertFalse(ChroniclePlayer._should_scrobble_type('episode'))
        self.assertFalse(ChroniclePlayer._should_scrobble_type('other'))

    def test_both_start_callbacks_start_one_session(self):
        media = _Media()
        started = []
        with self._playing(media), mock.patch.object(self.player, '_start_session_async', side_effect=started.append):
            self.player.onPlayBackStarted()
            self.player.onAVStarted()
            for t in threading.enumerate():
                if t.name == 'ChronicleScrobbler-SessionStart':
                    t.join(2)
        self.assertEqual(len(started), 1)

    # ── resume from Chronicle ──────────────────────────────────────────────────

    def test_resume_seeks_to_the_percent_of_total(self):
        self.client.get_resume_state.return_value = {'resumePositionPercent': 25.0}
        self.assertTrue(self.player._maybe_resume_from_chronicle(_Media(total=1000)))
        self.assertEqual(self.player.seeked_to, 250.0)

    def test_resume_skipped_when_kodi_has_its_own_bookmark_or_no_duration(self):
        self.assertFalse(self.player._maybe_resume_from_chronicle(_Media(resume=30)))
        self.assertFalse(self.player._maybe_resume_from_chronicle(_Media(total=0)))
        self.client.get_resume_state.assert_not_called()

    def test_resume_ignores_bad_server_answers(self):
        for answer in ([], None, {}, {'resumePositionPercent': 0}, {'resumePositionPercent': 'x'}):
            self.client.get_resume_state.return_value = answer
            self.assertFalse(self.player._maybe_resume_from_chronicle(_Media()), answer)
        self.assertIsNone(self.player.seeked_to)

    def test_opening_scrobble_is_skipped_after_a_resume_seek_only(self):
        media = _Media()
        with mock.patch.object(self.player, '_maybe_resume_from_chronicle', return_value=True), \
                mock.patch.object(self.player, '_send_update') as send:
            self.player._start_session_async(media)
        send.assert_not_called()
        with mock.patch.object(self.player, '_maybe_resume_from_chronicle', return_value=False), \
                mock.patch.object(self.player, '_send_update') as send:
            self.player._start_session_async(media)
        send.assert_called_once()

    # ── stop / rating signal ───────────────────────────────────────────────────

    def _signals(self):
        return {k: json.loads(v) for k, v in stubs._FAKE_FILES.items() if k.startswith(monitor._RATING_SIGNAL_DIR)}

    def test_watched_session_writes_a_rating_signal_on_stop(self):
        media = _Media('episode', percentage=90)
        self.tracker.start_session(media)
        self.tracker.record_scrobble(media, 1.0)
        self.player._session_started = True
        self.player.onPlayBackStopped()
        (payload,) = self._signals().values()
        self.assertEqual((payload['title'], payload['displayTitle'], payload['mediaType'], payload['dbId']),
                         ('Show', 'Show S01E02', 'episode', 7))
        self.assertIn('writtenAt', payload)
        self.assertFalse(self.tracker.has_session)
        self.assertFalse(self.player._session_started)

    def test_unwatched_session_writes_no_signal(self):
        media = _Media(percentage=10)
        self.tracker.start_session(media)
        self.tracker.record_scrobble(media, 1.0)
        self.player.onPlayBackStopped()
        self.assertEqual(self._signals(), {})

    def test_signal_write_failure_is_swallowed(self):
        media = _Media(percentage=90)
        self.tracker.start_session(media)
        self.tracker.record_scrobble(media, 1.0)
        with mock.patch.object(monitor.xbmcvfs, 'mkdirs', side_effect=OSError('disk')):
            self.player.onPlayBackStopped()   # must not raise

    def test_ended_sends_a_final_forced_update_then_stops(self):
        order = []
        with mock.patch.object(self.player, '_send_update', side_effect=lambda force=False: order.append(('send', force))), \
                mock.patch.object(self.player, '_on_stop', side_effect=lambda: order.append('stop')):
            self.player.onPlayBackEnded()
        self.assertEqual(order, [('send', True), 'stop'])

    def test_resume_and_seek_callbacks_send_an_update(self):
        with mock.patch.object(self.player, '_send_update') as send:
            self.player.onPlayBackResumed()
            self.player.onPlayBackSeek(1000, 500)
        self.assertEqual(send.call_count, 2)

    def test_error_stops_the_session(self):
        self.tracker.start_session(_Media())
        self.player.onPlayBackError()
        self.assertFalse(self.tracker.has_session)

    def test_display_title(self):
        self.assertEqual(ChroniclePlayer._display_title(_Media('episode')), 'Show S01E02')
        self.assertEqual(ChroniclePlayer._display_title(_Media('movie')), 'Heat')


class TestMonitor(unittest.TestCase):
    def setUp(self):
        for target in ('ChronicleClient', 'ProgressTracker'):
            p = mock.patch.object(monitor, target)
            p.start()
            self.addCleanup(p.stop)

    def test_settings_change_refreshes_the_shared_client_in_place(self):
        m = monitor.ChronicleMonitor()
        client = m._client
        m.onSettingsChanged()
        client.refresh_settings.assert_called_once()
        self.assertIs(m._player._client, client)

    def test_poll_loop_survives_an_error_and_stops_on_request(self):
        m = monitor.ChronicleMonitor()
        calls = []

        def boom(force=False):
            calls.append(1)
            if len(calls) == 2:
                m._stop_event.set()
            raise RuntimeError('rpc down')

        m._player._send_update = boom
        with mock.patch.object(monitor, '_POLL_SLEEP', 0):
            m._poll_loop()
        self.assertEqual(len(calls), 2)

    def test_run_stops_the_poll_thread_on_abort(self):
        m = monitor.ChronicleMonitor()
        m.aborted = True
        with mock.patch.object(monitor, '_POLL_SLEEP', 0):
            m.run()
        self.assertTrue(m._stop_event.is_set())
        self.assertFalse(m._poll_thread.is_alive())


if __name__ == '__main__':
    unittest.main()
