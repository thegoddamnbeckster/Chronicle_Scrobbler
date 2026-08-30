# -*- coding: utf-8 -*-
"""Chronicle -> Kodi bulk sync: ratings, art, and watch-count reconciliation.

Chronicle is the source of truth (user decision) — but the two reconciliation
rules differ by field type:

  Rating + art:          unconditional overwrite. Whatever Chronicle has for a
                          field replaces Kodi's local value, no exceptions —
                          there's no "which side is newer" question for a
                          single rating value.

  Playcount/lastplayed:  genuine two-way reconciliation. Either side can have
                          a watch the other doesn't know about (a watch on a
                          different Chronicle-tracked source Kodi never saw,
                          or a Kodi watch from before this addon existed).
                          Whichever side's last-played timestamp is more
                          recent is "ahead"; the other side's watch count is
                          brought up to match it.

Covers three levels, since a Chronicle library entry can be any of them:
  - Movies (HierarchyLevel 0, movies/fanedits-type) — full rating+art+playcount.
  - Movie collections (a movies-type root container with no independent movie
    of its own — Chronicle models these as a plain parent MediaItem, no
    external IDs, no watch/rating concept) — matched as a fallback against
    Kodi's native Movie Set when find_movie() finds nothing; art+overview only,
    since Kodi Movie Sets have no userrating/playcount field.
  - TV shows (HierarchyLevel 0, tv/anime-type) — rating+art only (no
    reconciled playcount at the show level — Kodi tracks watch state per
    episode, not as an independent show-level count).
  - TV episodes (HierarchyLevel 2, tv/anime-type) — full rating+art+playcount,
    resolved against Kodi via the show's own match + season/episode number.
    Season number is fetched from the season's own MediaItem (AncestorDto only
    carries id+name, not the season's Number) — one extra API call per episode
    entry actually present in the user's library.

NOT built: a bulk Kodi -> Chronicle export of ratings/playcount. Kodi still
feeds Chronicle, but only through live scrobble events (lib/monitor.py) —
new *watches* are additive facts; Chronicle already owns the reconciled
count once this sync has run.
"""

import xbmcgui
import xbmcaddon

from lib.logger import Logger
from lib.chronicle_client import ChronicleClient
from lib.device_name import get_device_name
from lib import kodi_matcher

ADDON = xbmcaddon.Addon()
log   = Logger('sync_engine')

_MOVIE_TYPES = ('movies', 'movie', 'fanedits')
_TV_TYPES    = ('tv', 'anime')

# Chronicle ResolvedMetadataDto field -> Kodi 'art' dict key.
# Movies, TV shows, and movie sets all use the same poster/fanart/etc. keys.
_ART_FIELD_MAP = {
    'posterUrl':   'poster',
    'backdropUrl': 'fanart',
    'logoUrl':     'clearlogo',
    'bannerUrl':   'banner',
    'clearartUrl': 'clearart',
    'discUrl':     'discart',
}

# Episodes are a different Kodi art model — confirmed live against a real Kodi 21+
# episode: they have their own 'thumb' (still/screenshot), not poster/fanart (those
# only exist inherited under 'tvshow.poster'/'tvshow.fanart', which belong to the
# show, not the episode, and are not meaningfully overwritable per-episode).
_EPISODE_ART_FIELD_MAP = {
    'posterUrl': 'thumb',
}


class SyncResult:
    def __init__(self):
        self.synced  = 0
        self.skipped = 0
        self.failed  = 0


class SyncEngine:
    """Bulk Chronicle -> Kodi push, run on demand from the addon menu."""

    def __init__(self):
        self._client = ChronicleClient()

    # ── public ─────────────────────────────────────────────────────────────────

    def sync_all(self) -> SyncResult:
        result = SyncResult()

        pbar = xbmcgui.DialogProgress()
        pbar.create(
            ADDON.getLocalizedString(32070),   # "Sync Watch History & Ratings Now"
            ADDON.getLocalizedString(32071),   # "Fetching your Chronicle library…"
        )

        try:
            for i, entry in enumerate(self._client.iter_library_all_statuses()):
                if pbar.iscanceled():
                    log.info('Sync cancelled by user after {0} entries'.format(i))
                    break

                media = entry.get('mediaItem') or {}
                name  = media.get('name', '?')
                pbar.update(0, ADDON.getLocalizedString(32072).format(name))  # "Syncing: {0}"

                try:
                    handled = self._sync_entry(entry, media)
                    if handled:
                        result.synced += 1
                    else:
                        result.skipped += 1
                except Exception as exc:
                    log.error('Sync failed for "{0}": {1}'.format(name, exc))
                    result.failed += 1
        finally:
            pbar.close()

        log.info('Sync complete: {0} synced, {1} skipped, {2} failed'.format(
            result.synced, result.skipped, result.failed))
        return result

    # ── per-entry dispatch ────────────────────────────────────────────────────

    def _sync_entry(self, entry: dict, media: dict) -> bool:
        media_type = (media.get('mediaTypeInternalName') or '').lower()
        level      = media.get('hierarchyLevel', 0)

        if level == 2 and media_type in _TV_TYPES:
            return self._sync_episode(entry, media)
        if level == 0 and media_type in _TV_TYPES:
            return self._sync_tvshow(entry, media)
        if level == 0 and media_type in _MOVIE_TYPES:
            return self._sync_movie_or_collection(entry, media)

        # Season-level (level 1) entries and unsupported types have no direct
        # Kodi counterpart to push to — skip, not a failure.
        return False

    # ── movies (+ collection fallback) ───────────────────────────────────────

    def _sync_movie_or_collection(self, entry: dict, media: dict) -> bool:
        ext_ids = _external_ids(media)
        title   = media.get('name')
        year    = media.get('year')

        kodi_movie = kodi_matcher.find_movie(ext_ids, title, year)
        if kodi_movie:
            self._push_rating_and_art(
                'VideoLibrary.SetMovieDetails', {'movieid': kodi_movie['movieid']}, entry, media)
            self._reconcile_watch_count(
                media.get('id'), kodi_movie, 'VideoLibrary.SetMovieDetails', 'movieid')
            return True

        # No individual movie matched — this may be a movie-collection container
        # (Chronicle models collections as a plain parent MediaItem with no
        # external IDs of its own). Fall back to Kodi's native Movie Set, art+
        # overview only (Movie Sets have no userrating/playcount in Kodi).
        kodi_set = kodi_matcher.find_movie_set(title)
        if kodi_set:
            self._push_art_only(
                'VideoLibrary.SetMovieSetDetails', {'setid': kodi_set['setid']}, media)
            return True

        return False

    # ── TV shows ──────────────────────────────────────────────────────────────

    def _sync_tvshow(self, entry: dict, media: dict) -> bool:
        ext_ids = _external_ids(media)
        kodi_show = kodi_matcher.find_tvshow(ext_ids, media.get('name'))
        if not kodi_show:
            return False

        # No playcount reconciliation at the show level — Kodi tracks watch
        # state per episode, not as an independent show-level count.
        self._push_rating_and_art(
            'VideoLibrary.SetTVShowDetails', {'tvshowid': kodi_show['tvshowid']}, entry, media)
        return True

    # ── TV episodes ───────────────────────────────────────────────────────────

    def _sync_episode(self, entry: dict, media: dict) -> bool:
        ancestors = media.get('ancestors') or []
        if len(ancestors) < 2:
            log.debug('Episode "{0}" has no season/show ancestors — skipping'.format(
                media.get('name')))
            return False

        show_ancestor   = ancestors[0]
        season_ancestor = ancestors[1]

        kodi_show = kodi_matcher.find_tvshow({}, show_ancestor.get('name'))
        if not kodi_show:
            return False

        season_media = self._client.get_media(season_ancestor.get('id'))
        season_number = season_media.get('number')
        episode_number = media.get('number')
        if season_number is None or episode_number is None:
            log.debug('Could not resolve season/episode number for "{0}"'.format(
                media.get('name')))
            return False

        kodi_episode = kodi_matcher.find_episode(
            kodi_show['tvshowid'], season_number, episode_number)
        if not kodi_episode:
            return False

        self._push_rating_and_art(
            'VideoLibrary.SetEpisodeDetails', {'episodeid': kodi_episode['episodeid']}, entry, media,
            art_map=_EPISODE_ART_FIELD_MAP)
        self._reconcile_watch_count(
            media.get('id'), kodi_episode, 'VideoLibrary.SetEpisodeDetails', 'episodeid')
        return True

    # ── shared push helpers ───────────────────────────────────────────────────

    def _push_rating_and_art(self, method: str, id_params: dict, entry: dict, media: dict,
                              art_map: dict = None) -> None:
        """Unconditional overwrite of userrating + art. Never touches playcount/lastplayed
        — that's _reconcile_watch_count's job, since it needs the two-way comparison."""
        params = dict(id_params)

        if ADDON.getSettingBool('sync_ratings'):
            user_rating = entry.get('userRating')
            if user_rating is not None:
                params['userrating'] = user_rating

        if ADDON.getSettingBool('sync_art'):
            art = self._build_art_dict(media, art_map)
            if art:
                params['art'] = art

        if len(params) == len(id_params):
            return  # nothing to push

        kodi_matcher.KodiJsonRpc.call(method, params)

    def _push_art_only(self, method: str, id_params: dict, media: dict) -> None:
        if not ADDON.getSettingBool('sync_art'):
            return
        params = dict(id_params)
        art = self._build_art_dict(media)
        if art:
            params['art'] = art
        overview = (media.get('resolvedMetadata') or {}).get('overview') or media.get('overview')
        if overview:
            params['plot'] = overview
        if len(params) == len(id_params):
            return
        kodi_matcher.KodiJsonRpc.call(method, params)

    def _reconcile_watch_count(self, media_item_id, kodi_item: dict, set_method: str, id_key: str) -> None:
        """Two-way playcount/lastplayed reconciliation by recency (see module docstring)."""
        if not ADDON.getSettingBool('sync_playcount'):
            return
        if media_item_id is None:
            return

        summary = self._client.get_watch_summary(media_item_id)
        chronicle_count = summary.get('watchedCount', 0)
        chronicle_last  = summary.get('lastWatchedAt')  # ISO string or None

        kodi_playcount = kodi_item.get('playcount', 0) or 0
        kodi_lastplayed = kodi_item.get('lastplayed') or None  # 'YYYY-MM-DD HH:MM:SS' or ''

        if chronicle_count == 0 and kodi_playcount == 0:
            return  # nothing watched anywhere yet

        kodi_is_newer = _kodi_lastplayed_is_newer(kodi_lastplayed, chronicle_last)

        if kodi_is_newer and kodi_playcount > chronicle_count:
            # Kodi has watch(es) Chronicle doesn't know about — bring Chronicle's
            # count up to match by submitting synthetic "already watched" events.
            gap = kodi_playcount - chronicle_count
            for _ in range(gap):
                self._client.scrobble({
                    'mediaItemId':     media_item_id,
                    'progressPercent': 100,
                    'timestamp':       _kodi_lastplayed_to_iso(kodi_lastplayed),
                    'deviceName':      '{0} (reconciled from local play count)'.format(get_device_name()),
                })
        elif not kodi_is_newer and chronicle_count > kodi_playcount:
            # Chronicle has watch(es) Kodi doesn't know about — push Kodi's
            # playcount/lastplayed up to match Chronicle's.
            params = {id_key: kodi_item[id_key], 'playcount': chronicle_count}
            if chronicle_last:
                params['lastplayed'] = _iso_to_kodi_lastplayed(chronicle_last)
            kodi_matcher.KodiJsonRpc.call(set_method, params)

    # ── silent ratings+progress sync (background, periodic) ──────────────────

    def sync_ratings_silent(self) -> SyncResult:
        """Per-user request (2026-08-30): "any ratings that are saved in
        chronicle need to be synchronized back into whatever Kodi is running a
        chronicle sync/scrape" -- and the 2026-08-30 follow-up: "get the
        rating and progress from trakt and Simkl and make it sync into Kodi.
        the most recent status wins." sync_all() above is the deliberate,
        on-demand, foreground action (progress dialog, notifications) for a
        full rating+art+playcount pass; this is its silent counterpart --
        rating + resume position only, no UI at all -- meant to be called
        periodically from the background service (see monitor.py) so a
        rating or an in-progress position made anywhere (this device, a
        different Kodi, the web UI, a Trakt/Simkl sync) reaches every device
        running this addon within one sync interval. "Most recent wins" is
        already resolved server-side before this ever runs (Chronicle is the
        single merged source of truth by the time GET /library returns), so
        this pushes whatever Chronicle currently has, unconditionally.
        Each field respects its own setting (sync_ratings / sync_progress)
        independently -- a no-op for a given item only when BOTH are off.
        """
        result = SyncResult()
        sync_ratings  = ADDON.getSettingBool('sync_ratings')
        sync_progress = ADDON.getSettingBool('sync_progress')
        if not sync_ratings and not sync_progress:
            return result

        for entry in self._client.iter_library_all_statuses():
            media = entry.get('mediaItem') or {}
            try:
                if self._push_rating_and_progress_silent(entry, media, sync_ratings, sync_progress):
                    result.synced += 1
                else:
                    result.skipped += 1
            except Exception as exc:
                log.error('Silent rating/progress sync failed for "{0}": {1}'.format(
                    media.get('name', '?'), exc))
                result.failed += 1

        log.info('Silent rating/progress sync complete: {0} synced, {1} skipped, {2} failed'.format(
            result.synced, result.skipped, result.failed))
        return result

    def _push_rating_and_progress_silent(
            self, entry: dict, media: dict, sync_ratings: bool, sync_progress: bool) -> bool:
        user_rating = entry.get('userRating') if sync_ratings else None
        # Deliberately NOT skipping here just because Chronicle itself has neither a rating
        # nor a resume position -- progress is bidirectional now (per-user request,
        # 2026-08-30), so Kodi having a local-only position Chronicle doesn't know about yet
        # is exactly the case this whole method needs to still look the item up for, not
        # short-circuit past. sync_progress off entirely is the only reason to skip that side.
        if user_rating is None and not sync_progress:
            return False

        media_type = (media.get('mediaTypeInternalName') or '').lower()
        level      = media.get('hierarchyLevel', 0)
        ext_ids    = _external_ids(media)

        # Whole-show entries have no single Kodi resume field (Kodi tracks resume per
        # movie/episode, not per show) -- rating is still meaningful there, progress isn't.
        if level == 0 and media_type in _TV_TYPES:
            kodi_item = kodi_matcher.find_tvshow(ext_ids, media.get('name'))
            if not kodi_item:
                return False
            params = {'tvshowid': kodi_item['tvshowid']}
            if user_rating is not None:
                params['userrating'] = user_rating
            if len(params) == 1:
                return False
            kodi_matcher.KodiJsonRpc.call('VideoLibrary.SetTVShowDetails', params)
            return True

        if level == 0 and media_type in _MOVIE_TYPES:
            kodi_item = kodi_matcher.find_movie(ext_ids, media.get('name'), media.get('year'))
            if not kodi_item:
                return False
            return self._reconcile_movie_or_episode(
                entry, media, kodi_item, sync_progress, user_rating,
                id_key='movieid', set_method='VideoLibrary.SetMovieDetails')

        if level == 2 and media_type in _TV_TYPES:
            ancestors = media.get('ancestors') or []
            if len(ancestors) < 2:
                return False
            kodi_show = kodi_matcher.find_tvshow({}, ancestors[0].get('name'))
            if not kodi_show:
                return False
            season_media = self._client.get_media(ancestors[1].get('id'))
            season_number  = season_media.get('number')
            episode_number = media.get('number')
            if season_number is None or episode_number is None:
                return False
            kodi_episode = kodi_matcher.find_episode(
                kodi_show['tvshowid'], season_number, episode_number)
            if not kodi_episode:
                return False
            return self._reconcile_movie_or_episode(
                entry, media, kodi_episode, sync_progress, user_rating,
                id_key='episodeid', set_method='VideoLibrary.SetEpisodeDetails')

        # Season-level entries, movie collections, and unsupported types have
        # no direct Kodi rating/resume field to push to -- skip, not a failure.
        return False

    def _reconcile_movie_or_episode(
            self, entry: dict, media: dict, kodi_item: dict, sync_progress: bool,
            user_rating, id_key: str, set_method: str) -> bool:
        """Rating (if any) always pushes Chronicle -> Kodi -- there's no meaningful
        "Kodi's own rating" to compare against, Chronicle is the only side with a concept
        of a synced rating at all. Progress is genuinely bidirectional (per-user request,
        2026-08-30): whichever side was touched more recently wins, and its value
        propagates to the other -- see _resolve_progress_direction's own doc.
        """
        acted = False
        params = {id_key: kodi_item[id_key]}

        if sync_progress:
            direction, pct = _resolve_progress_direction(entry, kodi_item)
            if direction == 'pull':
                if self._pull_progress_to_chronicle(media.get('id'), pct, kodi_item.get('lastplayed')):
                    acted = True
            elif direction == 'push':
                _add_resume(params, pct, media.get('runtimeMinutes'))

        if user_rating is not None:
            params['userrating'] = user_rating

        # A pull already fully handled progress on its own (a separate call, to Chronicle
        # rather than Kodi) -- only push to Kodi here if there's a rating and/or a resume
        # value queued up (len > 1: the id key alone never counts).
        if len(params) > 1:
            kodi_matcher.KodiJsonRpc.call(set_method, params)
            acted = True

        return acted

    def _pull_progress_to_chronicle(self, media_item_id, kodi_pct: float, kodi_lastplayed) -> bool:
        """Imports Kodi's own (more recent) progress into Chronicle via the same scrobble
        endpoint live playback already uses -- ScrobbleService.ScrobbleAsync already applies
        its own "only if at least as recent" guard against ResumeUpdatedAt server-side, and
        already correctly handles the >=80% "mark watched, clear resume" transition, so this
        doesn't need to duplicate either check here.
        """
        if not media_item_id:
            return False
        return self._client.scrobble({
            'mediaItemId':      media_item_id,
            'progressPercent':  kodi_pct,
            'timestamp':        _kodi_lastplayed_to_iso(kodi_lastplayed),
            'deviceName':       '{0} (reconciled from local Kodi playback)'.format(get_device_name()),
        })

    def _build_art_dict(self, media: dict, art_map: dict = None) -> dict:
        resolved = media.get('resolvedMetadata') or {}
        field_map = art_map or _ART_FIELD_MAP
        art = {}
        for chronicle_key, kodi_key in field_map.items():
            val = resolved.get(chronicle_key)
            if val:
                art[kodi_key] = val
        return art


# ── module-level helpers ────────────────────────────────────────────────────────

def _external_ids(media: dict) -> dict:
    return {e['source']: e['externalId'] for e in (media.get('externalIds') or [])}


# A position this close to the end is treated as "effectively finished" -- pushing a
# near-100% resume point is pointless (Kodi would just re-prompt to resume 2 minutes from
# the end) and a value of exactly 100 has no reliable meaning as a *resumable* position.
_RESUME_SKIP_THRESHOLD_PERCENT = 98


def _add_resume(params: dict, resume_pct, runtime_minutes) -> None:
    """Adds 'resume' to params in place when Chronicle's side won the direction comparison
    (_resolve_progress_direction returned 'push') -- called only in that case, so unlike
    before this never needs its own "is there even a value" guard against Chronicle simply
    having nothing on file; that ambiguity is now resolved once, up front, by
    _resolve_progress_direction itself.
    """
    if runtime_minutes is None or runtime_minutes <= 0:
        return
    if resume_pct <= 0 or resume_pct >= _RESUME_SKIP_THRESHOLD_PERCENT:
        return

    total_seconds = int(runtime_minutes) * 60
    params['resume'] = {
        'position': int(total_seconds * resume_pct / 100),
        'total':    total_seconds,
    }


def _resolve_progress_direction(entry: dict, kodi_item: dict):
    """Per-user request (2026-08-30): "if Chronicle's last watched status is older than
    Kodi's status, Kodi should win. Kodi's data should be added into Chronicle. If
    Chronicle's data is more current than Kodi's, Chronicle's data should sync into Kodi."

    Returns ('push', chronicle_pct) to send Chronicle's value to Kodi, ('pull', kodi_pct) to
    import Kodi's value into Chronicle instead, or (None, None) when neither side has
    anything meaningful to reconcile. When only one side has data at all, that side always
    wins outright -- the timestamp comparison only matters once both sides have something to
    disagree about.
    """
    chronicle_pct = entry.get('resumePositionPercent')
    chronicle_ts  = entry.get('resumeUpdatedAt')

    kodi_lastplayed = kodi_item.get('lastplayed')
    kodi_playcount  = kodi_item.get('playcount') or 0
    resume          = kodi_item.get('resume') or {}
    kodi_position   = resume.get('position') or 0
    kodi_total      = resume.get('total') or 0

    if kodi_playcount > 0:
        kodi_pct = 100.0
    elif kodi_position > 0 and kodi_total > 0:
        kodi_pct = (kodi_position / kodi_total) * 100.0
    else:
        kodi_pct = None
    # A percent with no timestamp at all (Kodi has literally never played this) isn't a
    # real signal to reconcile -- lastplayed is Kodi's empty-string/epoch default here.
    has_kodi = kodi_pct is not None and bool(kodi_lastplayed)
    has_chronicle = chronicle_pct is not None

    if not has_kodi and not has_chronicle:
        return None, None
    if has_kodi and not has_chronicle:
        return 'pull', kodi_pct
    if has_chronicle and not has_kodi:
        return 'push', chronicle_pct

    if _kodi_lastplayed_is_newer(kodi_lastplayed, chronicle_ts):
        return 'pull', kodi_pct
    return 'push', chronicle_pct


def _kodi_lastplayed_is_newer(kodi_lastplayed, chronicle_last_iso) -> bool:
    """True if Kodi's lastplayed is strictly more recent than Chronicle's.
    A missing value on either side counts as "epoch" (always older)."""
    from datetime import datetime

    def _parse_kodi(s):
        if not s:
            return datetime.min
        try:
            return datetime.strptime(s, '%Y-%m-%d %H:%M:%S')
        except ValueError:
            return datetime.min

    def _parse_iso(s):
        if not s:
            return datetime.min
        try:
            return datetime.strptime(s[:19], '%Y-%m-%dT%H:%M:%S')
        except ValueError:
            return datetime.min

    return _parse_kodi(kodi_lastplayed) > _parse_iso(chronicle_last_iso)


def _kodi_lastplayed_to_iso(kodi_lastplayed) -> str:
    """'YYYY-MM-DD HH:MM:SS' -> ISO-8601 'YYYY-MM-DDTHH:MM:SS' for the scrobble payload."""
    if not kodi_lastplayed:
        return None
    return kodi_lastplayed.replace(' ', 'T')


def _iso_to_kodi_lastplayed(iso_str: str) -> str:
    """ISO-8601 -> Kodi's 'YYYY-MM-DD HH:MM:SS' format for SetDetails calls."""
    return iso_str[:19].replace('T', ' ')
