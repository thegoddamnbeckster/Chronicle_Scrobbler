# -*- coding: utf-8 -*-
"""ChroniclePlayer (xbmc.Player) + ChronicleMonitor (xbmc.Monitor).

Two separate classes, not one -- confirmed against Kodi's own docs
(https://xbmc.github.io/docs.kodi.tv/master/kodi-base/dd/d41/group__python___player_c_b.html):
onPlayBackStarted/onAVStarted/onPlayBackPaused/onPlayBackResumed/onPlayBackStopped/
onPlayBackEnded/onPlayBackSeek/onPlayBackError are xbmc.Player callbacks, NOT
xbmc.Monitor callbacks. A single class inheriting only xbmc.Monitor and defining
methods with those names compiles fine and LOOKS like it should work, but Kodi's
core only ever dispatches those specific calls to a registered xbmc.Player
instance -- a Monitor subclass with the same method names never receives them at
all, silently. That was this file's actual shape until now: ChronicleMonitor
defined every onPlayBack*/onAVStarted method on an xbmc.Monitor base, so none of
them were ever invoked, no exception, nothing in kodi.log -- the poll thread's own
_send_update() no-ops via "if not self._tracker.has_session: return" since
start_session() is only ever reached from those same never-firing callbacks.
Confirmed live (2026-08-24): a real Player.Open playback session produced zero
[monitor] log lines, while a *different* addon's own xbmc.Player subclass, played
at the same moment, logged onAVStarted normally. Structurally, no scrobble could
ever have been sent by this addon before this fix, regardless of Chronicle
connectivity or settings -- this predates the resume-position work entirely.

The two classes share the same ChronicleClient/ProgressTracker/Lock (passed in,
not each constructing its own) so pause/resume state and cross-object settings
reloads (onSettingsChanged lives on Monitor; the client it reloads is also what
Player scrobbles through) stay consistent between them. Composition over the
documented alternative of multiple-inheriting both bases into one class -- the
practical examples of real addons doing this favour two separate objects.

Event flow (ChroniclePlayer):
  onAVStarted / onPlayBackStarted  →  _on_start()
  onPlayBackPaused                 →  tracker ignores polls while paused
  onPlayBackResumed / onPlayBackSeek  →  immediate update
  onPlayBackEnded                  →  final update + _on_stop()
  onPlayBackStopped / onPlayBackError →  _on_stop()

Background thread (owned by ChronicleMonitor, alongside its abort-loop):
  Wakes every _POLL_SLEEP seconds, calls ChroniclePlayer._send_update().
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

# Minimum gap between VideoLibrary.Clean runs -- see ChronicleMonitor.onScanFinished().
# Clean is safe and idempotent to re-run, this just avoids back-to-back thrashing when
# multiple scans fire in quick succession (confirmed happens in practice: two scans
# 50s apart from a single content-settings toggle).
_CLEAN_THROTTLE_SECONDS = 120


class ChroniclePlayer(xbmc.Player):
    """Handles every actual playback event and scrobbles progress to Chronicle.
    See this module's own docstring for why this can't just be more methods on
    ChronicleMonitor."""

    def __init__(self, client: ChronicleClient, tracker: ProgressTracker, lock: threading.Lock):
        super().__init__()
        self._client  = client
        self._tracker = tracker
        self._lock    = lock

    # ── xbmc.Player callbacks ──────────────────────────────────────────────────

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

        self._maybe_resume_from_chronicle(media)

        # Immediately send an opening scrobble (progress ≈ 0 %)
        self._send_update()

    def _maybe_resume_from_chronicle(self, media) -> None:
        """The whole cross-device point of this addon: if a DIFFERENT device left
        this item partway through, pick up from there on this one. Only when Kodi
        itself has no local resume bookmark for this item -- if it does, that's
        this exact device's own prior progress, which is always the more trustworthy
        answer for "where was I" than a cross-device value that's necessarily a bit
        stale (only as fresh as the last scrobble Chronicle received), so it wins
        without even asking Chronicle.
        """
        if media.resume_position > 0:
            log.debug('Kodi already has a local resume bookmark for this item — not asking Chronicle')
            return
        if media.total_time <= 0:
            log.debug('No total duration known yet — cannot convert a resume percent to a seek position')
            return

        state = self._client.get_resume_state(media.to_resume_lookup_payload())
        percent = state.get('resumePositionPercent')
        if not percent:
            return

        seconds = (percent / 100.0) * media.total_time
        try:
            self.seekTime(seconds)
            log.info('Resumed from Chronicle: {0:.1f}% ({1:.0f}s of {2:.0f}s)'.format(
                     percent, seconds, media.total_time))
        except Exception as exc:
            log.warning("Couldn't seek to Chronicle's resume position: {0}".format(exc))

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
            else:
                return

        # Send outside the lock to avoid blocking callbacks
        ok = self._client.scrobble(payload)
        if ok:
            with self._lock:
                self._tracker.record_scrobble(media, now)

    @staticmethod
    def _should_scrobble_type(media_type: str) -> bool:
        if media_type == 'movie':
            return ADDON.getSettingBool('scrobble_movies')
        if media_type == 'episode':
            return ADDON.getSettingBool('scrobble_tv')
        if media_type == 'track':
            return ADDON.getSettingBool('scrobble_music')
        return False


class ChronicleMonitor(xbmc.Monitor):
    """System/library-level events (settings changes, scan-finished) plus the
    service's own abort-loop and background poll thread. Constructs and holds
    the ChroniclePlayer that actually receives playback events -- see this
    module's own docstring for why they're separate objects."""

    def __init__(self):
        super().__init__()
        self._client       = ChronicleClient()
        self._tracker      = ProgressTracker()
        self._lock         = threading.Lock()
        self._player       = ChroniclePlayer(self._client, self._tracker, self._lock)
        self._poll_thread  = None
        self._stop_event   = threading.Event()

    # ── xbmc.Monitor callbacks ─────────────────────────────────────────────────

    def onSettingsChanged(self):
        """Reload the (shared) client when the user changes addon settings.
        Refreshes in place via refresh_settings() rather than reassigning
        self._client to a new object -- ChroniclePlayer holds the same
        reference, and swapping it here wouldn't be visible there."""
        log.info('Settings changed — reloading client')
        with self._lock:
            self._client.refresh_settings()

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
                self._player._send_update()
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
