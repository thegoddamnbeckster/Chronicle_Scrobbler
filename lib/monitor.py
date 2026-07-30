# -*- coding: utf-8 -*-
"""ChronicleMonitor — xbmc.Monitor subclass.

Handles all Kodi player lifecycle events and drives a background polling
thread that sends periodic scrobble updates to Chronicle.

Event flow:
  onAVStarted / onPlayBackStarted  →  _on_start()
  onPlayBackPaused                 →  tracker ignores polls while paused
  onPlayBackResumed / onPlayBackSeek  →  immediate update
  onPlayBackEnded                  →  final update + _on_stop()
  onPlayBackStopped / onPlayBackError →  _on_stop()

Background thread:
  Wakes every _POLL_SLEEP seconds, calls _send_update().
  ProgressTracker decides whether a scrobble is due based on:
    • configured poll interval (default 30 s)
    • significant progress delta (≥ 5 % jump from a seek)
    • watched-threshold crossing (one-shot per session)
"""

import json
import threading
import time

import xbmc
import xbmcaddon
import xbmcvfs

from lib.logger import Logger
from lib.chronicle_client import ChronicleClient
from lib.media_info import MediaInfo
from lib.progress_tracker import ProgressTracker

ADDON = xbmcaddon.Addon()
log   = Logger('monitor')

# Seconds the poll thread sleeps between iterations.
# Keep this shorter than MIN_INTERVAL so we never miss a threshold crossing.
_POLL_SLEEP = 5

# Minimum gap between VideoLibrary.Clean runs -- see onScanFinished(). Clean is
# safe and idempotent to re-run, this just avoids back-to-back thrashing when
# multiple scans fire in quick succession (confirmed happens in practice: two
# scans 50s apart from a single content-settings toggle).
_CLEAN_THROTTLE_SECONDS = 120


class ChronicleMonitor(xbmc.Monitor):
    """Monitors Kodi playback and scrobbles progress to Chronicle."""

    def __init__(self):
        super().__init__()
        self._client       = ChronicleClient()
        self._tracker      = ProgressTracker()
        self._lock         = threading.Lock()
        self._poll_thread  = None
        self._stop_event   = threading.Event()

    # ── xbmc.Monitor callbacks ─────────────────────────────────────────────────

    def onPlayBackStarted(self):
        # onPlayBackStarted fires early — metadata may not be populated yet.
        # onAVStarted is preferred; this is a fallback for audio items.
        log.debug('onPlayBackStarted')
        self._on_start()

    def onAVStarted(self):
        """Fires once audio/video is actually playing (after buffering)."""
        log.info('onAVStarted — new playback session')
        self._on_start()

    def onPlayBackPaused(self):
        log.info('Playback paused — scrobbling suspended')
        # No action needed: ProgressTracker.should_scrobble() returns False while paused.

    def onPlayBackResumed(self):
        log.info('Playback resumed')
        self._send_update()

    def onPlayBackStopped(self):
        log.info('Playback stopped')
        self._on_stop()

    def onPlayBackEnded(self):
        log.info('Playback ended — sending final update')
        self._send_update(force=True)
        self._on_stop()

    def onPlayBackSeek(self, time_ms, seek_offset):
        log.debug('Seek — position {0} ms, offset {1} ms'.format(time_ms, seek_offset))
        self._send_update()

    def onPlayBackError(self):
        log.warning('Playback error')
        self._on_stop()

    def onSettingsChanged(self):
        """Reload client when the user changes addon settings."""
        log.info('Settings changed — reloading client')
        with self._lock:
            self._client = ChronicleClient()

    def onScanFinished(self, library):
        """Fires whenever ANY video library scan completes -- confirmed via direct
        testing that this is the one hook common to every trigger path: a manual
        "Set Content" toggle refresh, Kodi's own automatic startup scan, and a
        scheduled scan all reach it identically, since Kodi's C++ core fires this
        the same way regardless of what asked for the scan. That's exactly why this
        is where library cleanup belongs, instead of trying to special-case each
        trigger individually (which isn't reliably distinguishable from the addon
        side at all -- there's no per-trigger marker in Kodi's own logging either).

        Runs VideoLibrary.Clean so items whose files were moved or removed (e.g.
        after a NAS storage rebalance) get purged from the library instead of
        leaving stale paths behind -- a stale path makes Kodi's own scanner skip
        that item on every future scan without ever handing it to our metadata
        scraper again, which is what silently broke art/collection syncing for
        titles that had genuinely moved to a different share.
        """
        if library != 'video':
            return
        log.info('onScanFinished(video) -- checking whether to run VideoLibrary.Clean')
        self._maybe_clean_video_library()

    # ── service entry point ────────────────────────────────────────────────────

    def run(self) -> None:
        """Block until Kodi requests an abort. Called from service.py."""
        log.info('Poll thread starting')
        self._start_poll_thread()

        while not self.abortRequested():
            self.waitForAbort(10)

        log.info('Abort requested — shutting down poll thread')
        self._stop_event.set()
        if self._poll_thread and self._poll_thread.is_alive():
            self._poll_thread.join(timeout=20)

    # ── private helpers ────────────────────────────────────────────────────────

    def _on_start(self) -> None:
        """Begin a new scrobble session for the currently-playing item."""
        media = MediaInfo.get_current()
        if media is None:
            return
        if media.media_type == 'unknown':
            log.debug('Unknown media type — not scrobbling')
            return
        if not self._should_scrobble_type(media.media_type):
            log.info('Scrobbling disabled for type "{0}"'.format(media.media_type))
            return

        with self._lock:
            self._tracker.start_session(media)

        # Immediately send an opening scrobble (progress ≈ 0 %)
        self._send_update()

    def _on_stop(self) -> None:
        """End the current scrobble session."""
        with self._lock:
            self._tracker.end_session()

    def _send_update(self, force: bool = False) -> None:
        """Send a scrobble if the tracker decides one is due."""
        media = MediaInfo.get_current()
        if media is None:
            return

        now = time.monotonic()
        with self._lock:
            if not self._tracker.has_session:
                return
            if force or self._tracker.should_scrobble(media, now):
                payload = media.to_scrobble_payload()

        # Send outside the lock to avoid blocking callbacks
        ok = self._client.scrobble(payload)
        if ok:
            with self._lock:
                self._tracker.record_scrobble(media, now)

    def _start_poll_thread(self) -> None:
        self._stop_event.clear()
        self._poll_thread = threading.Thread(
            target=self._poll_loop,
            name='ChronicleScrobbler-Poller',
            daemon=True,
        )
        self._poll_thread.start()

    def _poll_loop(self) -> None:
        """Background thread: wake every _POLL_SLEEP seconds and maybe scrobble."""
        log.debug('Poll loop started')
        while not self._stop_event.is_set():
            try:
                self._send_update()
            except Exception as exc:
                log.error('Poll error: {0}'.format(exc))
            self._stop_event.wait(_POLL_SLEEP)
        log.debug('Poll loop stopped')

    @staticmethod
    def _clean_marker_path() -> str:
        return xbmcvfs.translatePath(ADDON.getAddonInfo('profile') + 'last_video_clean.txt')

    def _maybe_clean_video_library(self) -> None:
        """Runs VideoLibrary.Clean, throttled -- see onScanFinished() and
        _CLEAN_THROTTLE_SECONDS for why this exists and why the throttle is safe."""
        marker = self._clean_marker_path()
        try:
            if xbmcvfs.exists(marker):
                f = xbmcvfs.File(marker, 'r')
                try:
                    last_run = float(f.read() or '0')
                finally:
                    f.close()
                if time.time() - last_run < _CLEAN_THROTTLE_SECONDS:
                    log.debug('Skipping VideoLibrary.Clean -- ran recently')
                    return
        except Exception as exc:
            log.warning("Couldn't read Clean Library throttle marker: {0}".format(exc))

        try:
            response = xbmc.executeJSONRPC(json.dumps({
                'jsonrpc': '2.0',
                'id': 1,
                'method': 'VideoLibrary.Clean',
                'params': {'showdialog': False},
            }))
            log.info('VideoLibrary.Clean triggered: {0}'.format(response))
        except Exception as exc:
            log.error("Couldn't trigger VideoLibrary.Clean: {0}".format(exc))
            return

        try:
            f = xbmcvfs.File(marker, 'w')
            try:
                f.write(bytearray(str(time.time()), 'utf-8'))
            finally:
                f.close()
        except Exception as exc:
            log.warning("Couldn't write Clean Library throttle marker: {0}".format(exc))

    @staticmethod
    def _should_scrobble_type(media_type: str) -> bool:
        if media_type == 'movie':
            return ADDON.getSettingBool('scrobble_movies')
        if media_type == 'episode':
            return ADDON.getSettingBool('scrobble_tv')
        if media_type == 'track':
            return ADDON.getSettingBool('scrobble_music')
        return False
