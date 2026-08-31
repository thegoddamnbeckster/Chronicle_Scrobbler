# -*- coding: utf-8 -*-
"""Matches a Chronicle media item against Kodi's video library.

Factored out of playlist_sync.py so both playlist_sync.py and sync_engine.py
share one matching implementation instead of two. ID-priority chain confirmed
against a real Kodi 21+ library:

  movies:   uniqueid.imdb -> uniqueid.tmdb -> imdbnumber (legacy, sometimes a
            TMDB numeric id instead of a real IMDB id — only trusted when it
            actually starts with 'tt') -> title+year
  tvshows:  uniqueid.tvdb -> uniqueid.tmdb -> uniqueid.imdb -> title
  episodes: matched via the show's own match (tvshowid), then season+episode
"""

from lib.logger import Logger
from lib.media_info import KodiJsonRpc

log = Logger('kodi_matcher')

# 'file' is included for every entity so playlist_sync.py can build .m3u paths
# from the same lookups sync_engine.py uses for rating/playcount/art reconciliation
# — one matching implementation, every caller gets what it needs from one call.
_MOVIE_PROPS   = ['title', 'year', 'imdbnumber', 'uniqueid', 'userrating', 'playcount', 'lastplayed', 'art', 'file']
_SHOW_PROPS    = ['title', 'year', 'imdbnumber', 'uniqueid', 'userrating']
_EPISODE_PROPS = ['title', 'season', 'episode', 'tvshowid', 'userrating', 'playcount', 'lastplayed', 'art', 'file']


def find_movie(external_ids: dict, title: str, year=None) -> dict:
    """Return the matching Kodi movie dict, or None."""
    if 'imdb' in external_ids:
        m = _first_match('VideoLibrary.GetMovies', 'movies', 'imdbnumber', external_ids['imdb'], _MOVIE_PROPS)
        if m:
            return m
        m = _first_match_uniqueid('VideoLibrary.GetMovies', 'movies', 'imdb', external_ids['imdb'], _MOVIE_PROPS)
        if m:
            return m

    if 'tmdb' in external_ids:
        m = _first_match_uniqueid('VideoLibrary.GetMovies', 'movies', 'tmdb', external_ids['tmdb'], _MOVIE_PROPS)
        if m:
            return m

    if not title:
        return None

    result = KodiJsonRpc.call('VideoLibrary.GetMovies', {
        'filter':     {'field': 'title', 'operator': 'is', 'value': title},
        'properties': _MOVIE_PROPS,
    })
    movies = result.get('movies', [])
    if year:
        for m in movies:
            if str(m.get('year', '')) == str(year):
                return m
    return movies[0] if movies else None


def find_movie_set(title: str) -> dict:
    """Return the matching Kodi Movie Set (collection) dict, or None.

    Chronicle models a movie collection as a plain MediaItem container (HierarchyLevel
    0, movies-type, with movie children) — it has no external IDs of its own, no
    watch/rating concept the way a real movie does. Kodi's native counterpart is a
    Movie Set (VideoLibrary.GetMovieSets / SetMovieSetDetails), which only takes a
    title, plot, and art — no userrating/playcount. Matched by title only; used as a
    fallback in sync_engine.py when find_movie() finds nothing for a root item (i.e.
    it's a collection container, not an individual watchable movie).
    """
    if not title:
        return None
    result = KodiJsonRpc.call('VideoLibrary.GetMovieSets', {
        'properties': ['title', 'art'],
    })
    sets = result.get('sets', [])
    for s in sets:
        if s.get('title', '').strip().lower() == title.strip().lower():
            return s
    return None


def find_tvshow(external_ids: dict, title: str) -> dict:
    """Return the matching Kodi tvshow dict, or None."""
    for source in ('tvdb', 'tmdb', 'imdb'):
        if source in external_ids:
            m = _first_match_uniqueid('VideoLibrary.GetTVShows', 'tvshows', source, external_ids[source], _SHOW_PROPS)
            if m:
                return m

    if not title:
        return None
    result = KodiJsonRpc.call('VideoLibrary.GetTVShows', {
        'filter':     {'field': 'title', 'operator': 'is', 'value': title},
        'properties': _SHOW_PROPS,
    })
    shows = result.get('tvshows', [])
    return shows[0] if shows else None


def find_episode(tvshow_id: int, season: int, episode: int) -> dict:
    """Return the matching Kodi episode dict for a known show, or None."""
    if tvshow_id is None:
        return None
    result = KodiJsonRpc.call('VideoLibrary.GetEpisodes', {
        'tvshowid': tvshow_id,
        'filter': {'and': [
            {'field': 'season',  'operator': 'is', 'value': str(season)},
            {'field': 'episode', 'operator': 'is', 'value': str(episode)},
        ]},
        'properties': _EPISODE_PROPS,
    })
    episodes = result.get('episodes', [])
    return episodes[0] if episodes else None


# ── helpers ────────────────────────────────────────────────────────────────────

def _first_match(method: str, result_key: str, field: str, value: str, props: list) -> dict:
    result = KodiJsonRpc.call(method, {
        'filter':     {'field': field, 'operator': 'is', 'value': str(value)},
        'properties': props,
    })
    items = result.get(result_key, [])
    return items[0] if items else None


def _first_match_uniqueid(method: str, result_key: str, source: str, value: str, props: list) -> dict:
    result = KodiJsonRpc.call(method, {
        'filter':     {'field': 'uniqueid', 'operator': 'is', 'value': '{0}:{1}'.format(source, value)},
        'properties': props,
    })
    items = result.get(result_key, [])
    return items[0] if items else None
