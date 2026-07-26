# -*- coding: utf-8 -*-
"""Tracks playback progress across a single playback session and decides
when to send scrobble events to Chronicle.

Scrobble rules:
  1. Timed interval — fire every poll_interval seconds (user-configurable,
     default 30 s) while the item is playing and not paused.
  2. Seek delta — if the playback position jumps by ≥ 5 % (seek forward/back)
     and at least MIN_INTERVAL seconds have passed since the last scrobble.
  3. Watched threshold — fire once when the percentage first crosses the
     configurable watched threshold (default 80 %).
  4. Never fire while paused.
  5. Never fire more often than MIN_INTERVAL seconds.
"""

import time
from dataclasses import dataclass, field
from typing import Optional

import xbmcaddon

from lib.logger import Logger

ADDON = xbmcaddon.Addon()
log   = Logger('tracker')

# Hard floor: never send two scrobbles closer than this many seconds apart.
MIN_INTERVAL = 15.0


@dataclass
class PlaybackState:
    """Mutable state kept for one playback session."""
    media_type:      str   = ''
    title:           str   = ''
    season:          int   = 0
    episode:         int   = 0
    show_title:      str   = ''
    external_ids:    dict  = field(default_factory=dict)
    total_time:      float = 0.0
    last_percentage: float = 0.0
    watched_sent:    bool  = False


class ProgressTracker:
    """Manages a single playback session and determines when to scrobble."""

    def __init__(self):
        self._state:         Optional[PlaybackState] = None
        self._last_scrobble: float = 0.0   # monotonic timestamp of last scrobble

    # ── session lifecycle ──────────────────────────────────────────────────────

    @property
    def has_session(self) -> bool:
        return self._state is not None

    def start_session(self, media_info) -> None:
        """Called when playback of a new item begins."""
        self._state = PlaybackState(
            media_type=media_info.media_type,
            title=media_info.title,
            season=media_info.season,
            episode=media_info.episode,
            show_title=media_info.show_title,
            external_ids=media_info.external_ids,
            total_time=media_info.total_time,
        )
        self._last_scrobble = 0.0   # force a scrobble on the first update
        log.info('Session started: "{0}" ({1})'.format(media_info.title, media_info.media_type))

    def end_session(self) -> None:
        """Called when playback stops, errors, or the item ends."""
        if self._state:
            log.info('Session ended: "{0}"'.format(self._state.title))
        self._state = None

    # ── scrobble decision ──────────────────────────────────────────────────────

    def should_scrobble(self, media_info, now: float = None) -> bool:
        """Return True if a scrobble should be sent right now."""
        if self._state is None:
            return False
        if media_info.is_paused:
            return False

        if now is None:
            now = time.monotonic()

        elapsed = now - self._last_scrobble

        # Hard floor — never send faster than MIN_INTERVAL
        if elapsed < MIN_INTERVAL:
            return False

        # Rule 1: timed interval
        poll_interval = float(ADDON.getSetting('poll_interval') or 30)
        if elapsed >= poll_interval:
            return True

        # Rule 2: significant seek / position jump
        progress_delta = abs(media_info.percentage - self._state.last_percentage)
        if progress_delta >= 5.0:
            return True

        # Rule 3: watched-threshold crossing (fire once per session)
        threshold = float(ADDON.getSetting('watched_threshold') or 80)
        if not self._state.watched_sent and media_info.percentage >= threshold:
            return True

        return False

    def record_scrobble(self, media_info, now: float = None) -> None:
        """Update internal state after a scrobble has been successfully sent."""
        if self._state is None:
            return
        if now is None:
            now = time.monotonic()

        threshold = float(ADDON.getSetting('watched_threshold') or 80)
        if media_info.percentage >= threshold:
            self._state.watched_sent = True

        self._state.last_percentage = media_info.percentage
        self._last_scrobble         = now
        log.debug('Scrobble recorded @ {0:.1f}%'.format(media_info.percentage))
