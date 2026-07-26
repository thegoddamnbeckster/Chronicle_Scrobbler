# -*- coding: utf-8 -*-
"""Resets Kodi watch progress for TV shows or movies.

"Reset" means:
  • playcount  = 0
  • lastplayed = ''  (empty string clears the field in Kodi)
  • resume     = {position: 0, total: 0}

Chronicle is NOT notified — Chronicle is the source of truth and its own
history is untouched by a purely Kodi-local reset. If the user later
re-syncs, Chronicle's own playcount/rating for the item will simply be
pushed back down again on the next sync.
"""

import xbmcgui
import xbmcaddon

from lib.logger import Logger
from lib.media_info import KodiJsonRpc

ADDON = xbmcaddon.Addon()
log   = Logger('reset')

_CLEAR_RESUME = {'position': 0, 'total': 0}


class ResetManager:
    """Interactive flows for resetting TV show and movie watch progress in Kodi."""

    # ── public ─────────────────────────────────────────────────────────────────

    def prompt_reset_tvshow(self) -> None:
        """Let the user pick a TV show, confirm, then reset all episode progress."""
        shows = self._get_tvshows()
        if not shows:
            xbmcgui.Dialog().ok(
                ADDON.getLocalizedString(32010),
                ADDON.getLocalizedString(32030),   # No TV shows found
            )
            return

        dialog = xbmcgui.Dialog()
        idx    = dialog.select(
            ADDON.getLocalizedString(32031),       # Select TV show to reset
            [s['label'] for s in shows],
        )
        if idx < 0:
            return

        show      = shows[idx]
        tvshow_id = show['tvshowid']
        title     = show['label']

        if not dialog.yesno(
            ADDON.getLocalizedString(32010),
            ADDON.getLocalizedString(32032).format(title),
        ):
            return

        self._reset_tvshow(tvshow_id, title)

    def prompt_reset_movie(self) -> None:
        """Let the user pick a movie, confirm, then reset its watch progress."""
        movies = self._get_movies()
        if not movies:
            xbmcgui.Dialog().ok(
                ADDON.getLocalizedString(32011),
                ADDON.getLocalizedString(32040),   # No movies found
            )
            return

        dialog = xbmcgui.Dialog()
        idx    = dialog.select(
            ADDON.getLocalizedString(32041),       # Select movie to reset
            [m['label'] for m in movies],
        )
        if idx < 0:
            return

        movie    = movies[idx]
        movie_id = movie['movieid']
        title    = movie['label']

        if not dialog.yesno(
            ADDON.getLocalizedString(32011),
            ADDON.getLocalizedString(32042).format(title),
        ):
            return

        self._reset_movie(movie_id, title)

    # ── TV show internals ──────────────────────────────────────────────────────

    def _get_tvshows(self) -> list:
        result = KodiJsonRpc.call('VideoLibrary.GetTVShows', {
            'properties': ['title'],
            'sort':       {'order': 'ascending', 'method': 'title'},
        })
        return result.get('tvshows', [])

    def _get_episodes(self, tvshow_id: int) -> list:
        result = KodiJsonRpc.call('VideoLibrary.GetEpisodes', {
            'tvshowid':   tvshow_id,
            'properties': ['title', 'season', 'episode', 'playcount', 'lastplayed', 'resume'],
        })
        return result.get('episodes', [])

    def _reset_tvshow(self, tvshow_id: int, title: str) -> None:
        episodes = self._get_episodes(tvshow_id)
        if not episodes:
            log.warning('No episodes found for tvshowid={0}'.format(tvshow_id))
            return

        total  = len(episodes)
        errors = 0

        pbar = xbmcgui.DialogProgress()
        pbar.create(
            ADDON.getLocalizedString(32010),
            ADDON.getLocalizedString(32033).format(title),
        )

        for i, ep in enumerate(episodes):
            if pbar.iscanceled():
                log.info('Reset cancelled by user at episode {0}/{1}'.format(i, total))
                break

            pbar.update(
                int(i / total * 100),
                '{0}  S{1:02d}E{2:02d}'.format(title, ep.get('season', 0), ep.get('episode', 0)),
            )

            if not self._set_episode_watched(ep['episodeid'], watched=False):
                errors += 1

        pbar.close()

        dialog = xbmcgui.Dialog()
        if errors == 0:
            dialog.ok(
                ADDON.getLocalizedString(32010),
                ADDON.getLocalizedString(32034).format(title, total),
            )
        else:
            dialog.ok(
                ADDON.getLocalizedString(32010),
                ADDON.getLocalizedString(32035).format(errors, total),
            )

        log.info('TV show reset "{0}" — {1} episodes, {2} errors'.format(title, total, errors))

    def _set_episode_watched(self, episode_id: int, watched: bool) -> bool:
        result = KodiJsonRpc.call('VideoLibrary.SetEpisodeDetails', {
            'episodeid': episode_id,
            'playcount': 1 if watched else 0,
            'lastplayed': '' if not watched else None,
            'resume':    _CLEAR_RESUME,
        })
        return result == 'OK'

    # ── movie internals ────────────────────────────────────────────────────────

    def _get_movies(self) -> list:
        result = KodiJsonRpc.call('VideoLibrary.GetMovies', {
            'properties': ['title'],
            'sort':       {'order': 'ascending', 'method': 'title'},
        })
        return result.get('movies', [])

    def _reset_movie(self, movie_id: int, title: str) -> None:
        result = KodiJsonRpc.call('VideoLibrary.SetMovieDetails', {
            'movieid':    movie_id,
            'playcount':  0,
            'lastplayed': '',
            'resume':     _CLEAR_RESUME,
        })
        ok = (result == 'OK')

        dialog = xbmcgui.Dialog()
        if ok:
            dialog.ok(
                ADDON.getLocalizedString(32011),
                ADDON.getLocalizedString(32043).format(title),
            )
            log.info('Movie reset "{0}"'.format(title))
        else:
            dialog.ok(
                ADDON.getLocalizedString(32011),
                ADDON.getLocalizedString(32044).format(title),
            )
            log.error('Failed to reset movie "{0}"'.format(title))
