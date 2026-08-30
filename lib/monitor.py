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
import xbmcgui
import xbmcvfs

from lib.logger import Logger
from lib.chronicle_client import ChronicleClient
from lib.media_info import MediaInfo
from lib.progress_tracker import ProgressTracker
from lib.sync_engine import SyncEngine

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

# How often the background service silently re-pushes ratings to this device's
# Kodi library (SyncEngine.sync_ratings_silent()) -- per-user request
# (2026-08-30): "any ratings that are saved in chronicle need to be
# synchronized back into whatever Kodi is running a chronicle sync/scrape."
# A rating made on THIS device already pushes immediately (see
# Chronicle_Rating's rate_flow.py); this periodic pass is what brings a
# rating made anywhere ELSE (a different Kodi, the web UI, an import) to
# this device too, without requiring the manual "Sync Watch History &
# Ratings Now" action. 30 minutes -- frequent enough that a rating shows up
# the same viewing session on another device, not so frequent it's hammering
# Chronicle with a full library fetch every few minutes.
_RATING_SYNC_INTERVAL_SECONDS = 1800

# Cross-addon signal directory: this addon (Chronicle_Scrobbler) writes one file here
# per watched session, and the separate script.chronicle.rating addon's own background
# service watches it and prompts for a rating. special://temp/ (not addon_data/) so it's
# reachable from both addons' own processes regardless of addon id -- same pattern
# Chronicle_Scraper already uses for its own scraper<->service coordination.
_RATING_SIGNAL_DIR = 'special://temp/chronicle_scrobbler/'


class ChroniclePlayer(xbmc.Player):
    """Handles every actual playback event and scrobbles progress to Chronicle.
    See this module's own docstring for why this can't just be more methods on
    ChronicleMonitor."""

    def __init__(self, client: ChronicleClient, tracker: ProgressTracker, lock: threading.Lock):
        super().__init__()
        self._client  = client
        self._tracker = tracker
        self._lock    = lock
        # Kodi fires BOTH onPlayBackStarted and onAVStarted for video (only the former
        # fires for audio) -- guards _on_start() so a video session doesn't start twice,
        # double-send the opening scrobble, and double-query the resume endpoint.
        self._session_started = False
        # True while a scrobble POST is in flight -- prevents the poll thread and a
        # Kodi callback thread from both sending a scrobble for the same moment.
        self._sending = False

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
            if self._session_started:
                # onPlayBackStarted and onAVStarted both fire for video -- the first
                # one through already started this session.
                return
            self._session_started = True
            self._tracker.start_session(media)

        self._notify(ADDON.getLocalizedString(32088).format(self._display_title(media)))

        # The resume lookup is a real network call (see _maybe_resume_from_chronicle) --
        # run it on a background thread so a slow/unreachable Chronicle server never
        # stalls Kodi's player-callback dispatch thread. Mirrors the pattern already
        # established elsewhere in this addon for backgrounding blocking network calls
        # (lib/device_auth.py's poll_thread, lib/qr_dialog.py's _monitor_thread).
        threading.Thread(
            target=self._start_session_async, args=(media,),
            name='ChronicleScrobbler-SessionStart', daemon=True,
        ).start()

    def _start_session_async(self, media) -> None:
        resumed = self._maybe_resume_from_chronicle(media)
        # Only send the immediate "opening ≈0%" scrobble when nothing was just resumed --
        # xbmc.Player.seekTime() isn't guaranteed synchronous, so sending right after a
        # resume-seek risks reporting Chronicle a stale near-0% position that overwrites
        # the very resume state it just told this device about. The next poll tick
        # (within _POLL_SLEEP seconds) reports the real, post-seek position instead.
        if not resumed:
            self._send_update()

    def _maybe_resume_from_chronicle(self, media) -> bool:
        """The whole cross-device point of this addon: if a DIFFERENT device left
        this item partway through, pick up from there on this one. Only when Kodi
        itself has no local resume bookmark for this item -- if it does, that's
        this exact device's own prior progress, which is always the more trustworthy
        answer for "where was I" than a cross-device value that's necessarily a bit
        stale (only as fresh as the last scrobble Chronicle received), so it wins
        without even asking Chronicle.

        Returns True if a resume seek was actually performed.
        """
        if media.resume_position > 0:
            log.debug('Kodi already has a local resume bookmark for this item — not asking Chronicle')
            return False
        if media.total_time <= 0:
            log.debug('No total duration known yet — cannot convert a resume percent to a seek position')
            return False

        state = self._client.get_resume_state(media.to_resume_lookup_payload())
        if not isinstance(state, dict):
            log.warning('Unexpected resume-state response shape: {0!r}'.format(state))
            return False

        percent = state.get('resumePositionPercent')
        if not isinstance(percent, (int, float)) or percent <= 0:
            return False

        seconds = (percent / 100.0) * media.total_time
        try:
            self.seekTime(seconds)
            log.info('Resumed from Chronicle: {0:.1f}% ({1:.0f}s of {2:.0f}s)'.format(
                     percent, seconds, media.total_time))
            return True
        except Exception as exc:
            log.warning("Couldn't seek to Chronicle's resume position: {0}".format(exc))
            return False

    def _on_stop(self) -> None:
        """End the current scrobble session."""
        with self._lock:
            self._session_started = False
            state = self._tracker.snapshot()   # before end_session() clears it
            self._tracker.end_session()

        if state is not None and state.watched_sent:
            self._write_rating_signal(state)

    def _write_rating_signal(self, state) -> None:
        """Drops a small JSON file for the separate Chronicle Rating add-on's own
        background service to pick up and prompt from -- see that addon's service.py.
        Written only when this session actually crossed the watched threshold (state.
        watched_sent), never for a session abandoned a few minutes in.

        One uniquely-named file per session, not a single fixed path -- back-to-back
        sessions (binge-watching several episodes in a row) would otherwise race to
        overwrite the same file before the rating add-on's poll loop got to the
        previous one, silently dropping a prompt. The rating add-on deletes each file
        once it's shown that prompt (or decided not to), so this directory only ever
        holds genuinely pending items.
        """
        try:
            xbmcvfs.mkdirs(_RATING_SIGNAL_DIR)
            written_at = int(time.time() * 1000)
            payload = {
                'title':        state.show_title if state.media_type == 'episode' else state.title,
                'displayTitle': self._display_title(state),
                'mediaType':    state.media_type,
                'season':       state.season,
                'episode':      state.episode,
                'externalIds':  state.external_ids,
                # Kodi's own local library id for this item -- per-user request
                # (2026-08-30): "when a rating is completed... the media item needs
                # to be saved to the Kodi my rating". -1 (not in the library) means
                # Chronicle Rating has nothing to push a rating back into locally.
                'dbId':         state.db_id,
                # Epoch ms this signal was written -- lets Chronicle Rating's service.py
                # tell a genuinely-just-finished session apart from one that's been
                # sitting unconsumed since before Kodi was last shut down (e.g. closed
                # right after playback ended, before the rating service's next poll),
                # and skip prompting for the latter instead of ambushing the user with
                # a rating dialog the moment Kodi starts back up. See that file's own
                # _is_stale() for the age cutoff.
                'writtenAt':    written_at,
            }
            path = '{0}rating_{1}_{2}.json'.format(
                _RATING_SIGNAL_DIR, written_at, threading.get_ident())
            f = xbmcvfs.File(path, 'w')
            try:
                f.write(bytearray(json.dumps(payload), 'utf-8'))
            finally:
                f.close()
            log.info('Wrote rating signal for "{0}" -> {1}'.format(payload['displayTitle'], path))
        except Exception as exc:
            log.warning('Could not write rating signal: {0}'.format(exc))

    def _send_update(self, force: bool = False) -> None:
        """Send a scrobble if the tracker decides one is due."""
        media = MediaInfo.get_current()
        if media is None:
            return

        now = time.monotonic()
        with self._lock:
            # should_scrobble()/record_scrobble() are split across the unlocked network
            # call below -- without this guard, the poll thread (every _POLL_SLEEP
            # seconds) and a Kodi callback thread (e.g. onPlayBackSeek) can both pass
            # the gate before either records, sending duplicate scrobbles for the same
            # moment. Only one send is ever in flight per session.
            if not self._tracker.has_session or self._sending:
                return
            if not (force or self._tracker.should_scrobble(media, now)):
                return
            payload = media.to_scrobble_payload()
            self._sending = True

        try:
            # Send outside the lock to avoid blocking callbacks
            ok = self._client.scrobble(payload)
        finally:
            with self._lock:
                self._sending = False
        if ok:
            with self._lock:
                just_watched = self._tracker.record_scrobble(media, now)
            if just_watched:
                self._notify(ADDON.getLocalizedString(32089).format(self._display_title(media)))

    @staticmethod
    def _display_title(media) -> str:
        if media.media_type == 'episode' and media.show_title:
            return '{0} S{1:02d}E{2:02d}'.format(media.show_title, media.season, media.episode)
        return media.title or media.show_title or ''

    @staticmethod
    def _notify(message: str) -> None:
        """On-screen toast for scrobble start/watched events -- gated by its own
        setting (default on) since some users would rather scrobbling stay silent."""
        if not ADDON.getSettingBool('show_notifications'):
            return
        xbmcgui.Dialog().notification(
            ADDON.getLocalizedString(32000),   # "Chronicle Scrobbler"
            message,
            xbmcgui.NOTIFICATION_INFO,
            4000,
        )

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
        self._rating_sync_thread = None
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
        self._start_rating_sync_thread()

        while not self.abortRequested():
            self.waitForAbort(10)

        log.info('Abort requested — shutting down poll thread')
        self._stop_event.set()
        if self._poll_thread and self._poll_thread.is_alive():
            self._poll_thread.join(timeout=20)
        if self._rating_sync_thread and self._rating_sync_thread.is_alive():
            self._rating_sync_thread.join(timeout=5)

    # ── private helpers ────────────────────────────────────────────────────────

    def _start_rating_sync_thread(self) -> None:
        """Own thread, own (longer) interval, not the tight playback poll loop --
        SyncEngine.sync_ratings_silent() fetches the user's whole Chronicle
        library and can take a real amount of time for a large one; running it
        on the playback poll thread would delay scrobbling, and running it on
        the outer abort-check loop (waitForAbort(10) in run() above) would
        delay Kodi shutdown responsiveness for however long a sync in progress
        takes. Waits out a full interval before its FIRST run too -- nothing
        this addon does needs a rating sync within seconds of Kodi starting,
        and every other startup-timing lesson in this codebase (see the
        rating add-on's own _flush_stale_startup_signals) points the same way:
        don't front-load background work into the moment Kodi just opened.
        """
        thread = threading.Thread(
            target=self._rating_sync_loop,
            name='ChronicleScrobbler-RatingSync',
            daemon=True,
        )
        thread.start()
        self._rating_sync_thread = thread

    def _rating_sync_loop(self) -> None:
        log.debug('Rating sync loop started')
        while not self._stop_event.wait(_RATING_SYNC_INTERVAL_SECONDS):
            try:
                SyncEngine().sync_ratings_silent()
            except Exception as exc:
                log.error('Silent rating sync error: {0}'.format(exc))
        log.debug('Rating sync loop stopped')

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
