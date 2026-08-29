# -*- coding: utf-8 -*-
"""Helpers for querying Kodi's JSON-RPC API and building Chronicle payloads.

KodiJsonRpc  — low-level JSON-RPC caller (wraps xbmc.executeJSONRPC).
MediaInfo    — snapshot of the currently-playing item, including all
               progress metadata that Kodi exposes.
"""

import json
import xbmc

from lib.device_name import get_device_name
from lib.logger import Logger

log = Logger('media_info')

# ── Item properties requested from Player.GetItem ─────────────────────────────
_ITEM_PROPS = [
    'title',
    'year',
    'season',
    'episode',
    'showtitle',
    'imdbnumber',   # IMDB id for movies; sometimes TMDB id
    'uniqueid',     # dict: {'imdb': '...', 'tmdb': '...', 'tvdb': '...'}
    'file',
    'duration',     # duration in seconds (audio)
    'runtime',      # runtime in seconds (video)
    'resume',       # {'position': float, 'total': float}
]

# ── Player properties requested from Player.GetProperties ─────────────────────
_PLAYER_PROPS = [
    'time',         # {'hours':0, 'minutes':0, 'seconds':0, 'milliseconds':0}
    'totaltime',    # same format
    'percentage',   # float 0–100
    'speed',        # int: 0=paused, 1=normal, >1=fast-forward
    'position',     # playlist position
    'playlistid',
]


class KodiJsonRpc:
    """Minimal Kodi JSON-RPC caller."""

    @staticmethod
    def call(method: str, params: dict = None) -> dict:
        """Execute a Kodi JSON-RPC method and return the parsed result."""
        request = {'jsonrpc': '2.0', 'method': method, 'id': 1}
        if params:
            request['params'] = params

        raw    = xbmc.executeJSONRPC(json.dumps(request))
        result = json.loads(raw)

        if 'error' in result:
            log.warning('JSON-RPC error [{0}]: {1}'.format(method, result['error']))
            return {}

        return result.get('result', {})


class MediaInfo:
    """Snapshot of the currently-playing media item and its playback state."""

    def __init__(self, player_id: int, item: dict, props: dict):
        self._player_id = player_id
        self._item      = item
        self._props     = props

    # ── factory ────────────────────────────────────────────────────────────────

    @classmethod
    def get_current(cls):
        """Return a MediaInfo for the active player, or None if nothing is playing."""
        players = KodiJsonRpc.call('Player.GetActivePlayers')
        if not players:
            return None

        player    = players[0]
        player_id = player.get('playerid', 0)
        ptype     = player.get('type', '')   # 'video' | 'audio' | 'picture'

        if ptype == 'picture':
            return None

        item_result = KodiJsonRpc.call('Player.GetItem', {
            'playerid':   player_id,
            'properties': _ITEM_PROPS,
        })
        item = item_result.get('item', {})
        if not item:
            return None

        props = KodiJsonRpc.call('Player.GetProperties', {
            'playerid':   player_id,
            'properties': _PLAYER_PROPS,
        })

        return cls(player_id, item, props)

    # ── accessors ──────────────────────────────────────────────────────────────

    @property
    def player_id(self) -> int:
        return self._player_id

    @property
    def media_type(self) -> str:
        """Returns 'movie', 'episode', 'track', or 'unknown'."""
        t = self._item.get('type', '')
        if t == 'movie':   return 'movie'
        if t == 'episode': return 'episode'
        if t == 'song':    return 'track'
        return 'unknown'

    @property
    def title(self) -> str:
        return self._item.get('title', '')

    @property
    def year(self):
        """The item's release year, or None when Kodi has none to give.

        Confirmed directly (2026-08-29): Kodi's Player.GetItem never populates
        'year' for an 'episode' item -- that field belongs to the show/movie, not
        an individual episode -- so this used to silently return the integer 0
        for every single TV episode, not "no year known". Chronicle's own
        matcher (MediaItemMatcher.FindByTitleYearAsync) treats an explicit 0 as
        a REAL year to filter on (`m.Year == 0`), not as "no filter" the way a
        JSON null / omitted field is -- so an episode scrobble could never
        title-match its own already-correctly-tagged show (Year=2013, say) and
        silently created a brand-new, wrongly-Year=0 duplicate stub instead,
        confirmed live against "Rick and Morty" (S03E04): the correct show
        already existed with the right TMDB id, but this scrobble minted a
        second top-level "Rick and Morty" item because year=0 could never
        match Year=2013 and the episode's own uniqueid (not the show's) never
        matched anything either. None here (not 0) round-trips as JSON null,
        which int? Year on the server correctly reads as "no year given".
        """
        raw = self._item.get('year')
        return int(raw) if raw else None

    @property
    def season(self) -> int:
        return int(self._item.get('season', 0) or 0)

    @property
    def episode(self) -> int:
        return int(self._item.get('episode', 0) or 0)

    @property
    def show_title(self) -> str:
        return self._item.get('showtitle', '')

    @property
    def percentage(self) -> float:
        """Playback position as a percentage (0.0–100.0)."""
        return float(self._props.get('percentage', 0.0))

    @property
    def current_time(self) -> float:
        """Elapsed playback time in seconds."""
        return _hmsm_to_seconds(self._props.get('time', {}))

    @property
    def total_time(self) -> float:
        """Total media duration in seconds."""
        t = _hmsm_to_seconds(self._props.get('totaltime', {}))
        if t > 0:
            return t
        # Fall back to item-level runtime / duration fields
        return float(
            self._item.get('runtime', 0)
            or self._item.get('duration', 0)
            or 0
        )

    @property
    def speed(self) -> int:
        """Playback speed: 0=paused, 1=normal, >1=fast-forward."""
        return int(self._props.get('speed', 1))

    @property
    def is_paused(self) -> bool:
        return self.speed == 0

    @property
    def resume_position(self) -> float:
        """Kodi's stored resume position in seconds (from the item, not live props)."""
        resume = self._item.get('resume', {}) or {}
        return float(resume.get('position', 0.0))

    @property
    def db_id(self) -> int:
        """Kodi library database ID, or -1 if the item is not in the library."""
        return int(self._item.get('id', -1) or -1)

    @property
    def external_ids(self) -> dict:
        """
        Build a dict of available external IDs from all Kodi sources.

        Priority: uniqueid dict > imdbnumber field.
        Returns e.g. {'imdb': 'tt1234567', 'tmdb': '12345', 'tvdb': '78901'}.
        """
        ids = {}

        # uniqueid is populated by scrapers and contains the most reliable data
        unique = self._item.get('uniqueid', {}) or {}
        for key in ('imdb', 'tmdb', 'tvdb', 'musicbrainz'):
            val = str(unique.get(key, '') or '').strip()
            if val:
                ids[key] = val

        # imdbnumber is a legacy field — use it only if uniqueid didn't give us IMDB.
        # Confirmed against a real Kodi 21+ library that this field can actually hold a
        # TMDB numeric id instead of a real "tt..." IMDB id, so it's only trusted when
        # it actually looks like one.
        if 'imdb' not in ids:
            imdb = str(self._item.get('imdbnumber', '') or '').strip()
            if imdb.startswith('tt'):
                ids['imdb'] = imdb
            elif imdb.isdigit() and 'tmdb' not in ids:
                ids['tmdb'] = imdb

        return ids

    # ── payload builder ────────────────────────────────────────────────────────

    def to_scrobble_payload(self) -> dict:
        """Build the Chronicle POST /api/v1/scrobble request body.

        Matches Chronicle.API's real ScrobbleRequestDto exactly: mediaItemId,
        progressPercent, timestamp, deviceName, externalIds, title, year, mediaType.
        No mediaItemId is included here — Chronicle resolves/creates the item from
        title/year/externalIds/mediaType (see ScrobbleService.FindOrCreateMediaItemAsync).
        Season/episode/showTitle are Kodi-side concepts only; Chronicle's scrobble
        contract has no fields for them, so they are intentionally left out rather
        than sent as ignored extras.
        """
        return {
            'mediaType':       self.media_type,
            'title':           self.show_title if self.media_type == 'episode' else self.title,
            'year':            self.year,
            'progressPercent': round(self.percentage, 2),
            'externalIds':     self.external_ids,
            'deviceName':      get_device_name(),
        }

    def to_resume_lookup_payload(self) -> dict:
        """Build the Chronicle POST /api/v1/scrobble/resume request body -- the same
        identifying fields as to_scrobble_payload(), minus progressPercent/deviceName,
        which don't mean anything for a lookup rather than an event."""
        return {
            'mediaType':   self.media_type,
            'title':       self.show_title if self.media_type == 'episode' else self.title,
            'year':        self.year,
            'externalIds': self.external_ids,
        }


# ── helpers ────────────────────────────────────────────────────────────────────

def _hmsm_to_seconds(t: dict) -> float:
    """Convert a Kodi time dict {hours, minutes, seconds, milliseconds} to seconds."""
    if not t:
        return 0.0
    return (
        t.get('hours',        0) * 3600
        + t.get('minutes',    0) * 60
        + t.get('seconds',    0)
        + t.get('milliseconds', 0) / 1000.0
    )
