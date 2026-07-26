# -*- coding: utf-8 -*-
"""Syncs Chronicle lists to Kodi as persistent .m3u playlist files.

For each Chronicle list, this module:
  1. Fetches the list and its items from Chronicle.
  2. For each item, looks up the file path in Kodi's video library via
     lib/kodi_matcher.py (IMDB -> TMDB -> title+year for movies; TVDB ->
     TMDB -> IMDB -> title for shows, then season+episode for episodes).
  3. Writes an .m3u playlist file to Kodi's special://profile/playlists/video/
     directory.

Only ordered lists create numbered playlists; unordered lists are also written
but without positional ordering guarantees.
"""

import xbmcvfs
import xbmcgui
import xbmcaddon

from lib.logger import Logger
from lib.chronicle_client import ChronicleClient
from lib import kodi_matcher

ADDON = xbmcaddon.Addon()
log   = Logger('playlist_sync')

# Where Kodi stores persistent video playlists
_PLAYLIST_DIR = 'special://profile/playlists/video/'


class PlaylistSync:
    """Fetches Chronicle lists and writes them as Kodi .m3u playlist files."""

    def __init__(self):
        self._client = ChronicleClient()

    # ── public ─────────────────────────────────────────────────────────────────

    def sync_all(self) -> tuple:
        """Sync all Chronicle lists to Kodi playlists.

        Returns (lists_synced, lists_failed).
        """
        lists = self._client.get_lists()
        if lists is None:
            log.error('Failed to fetch lists from Chronicle')
            return 0, 0

        synced = 0
        failed = 0
        total  = len(lists)

        pbar = xbmcgui.DialogProgress()
        pbar.create(
            ADDON.getLocalizedString(32050),  # Sync Lists to Kodi
            ADDON.getLocalizedString(32051),  # Fetching lists…
        )

        for i, lst in enumerate(lists):
            if pbar.iscanceled():
                break

            pbar.update(
                int(i / max(total, 1) * 100),
                ADDON.getLocalizedString(32052).format(lst.get('name', '?')),
            )

            detail = self._client.get_list(lst['id'])
            if not detail:
                failed += 1
                continue

            ok = self._write_playlist(detail)
            if ok:
                synced += 1
            else:
                failed += 1

        pbar.close()
        return synced, failed

    # ── private — playlist creation ────────────────────────────────────────────

    def _write_playlist(self, lst: dict) -> bool:
        """Write a single Chronicle list as an .m3u file in Kodi's playlist directory."""
        name    = lst.get('name', 'Untitled List')
        items   = lst.get('items', [])
        ordered = lst.get('isOrdered', True)

        if not items:
            log.debug('Skipping empty list: {0}'.format(name))
            return True

        # Sort by position for ordered lists
        if ordered:
            items = sorted(items, key=lambda i: i.get('position', 0))

        lines = ['#EXTM3U']
        matched = 0

        for entry in items:
            media   = entry.get('mediaItem', {})
            mtype   = media.get('mediaTypeName', '').lower()
            ext_ids = {e['source']: e['externalId'] for e in media.get('externalIds', [])}
            title   = media.get('name', '')
            year    = media.get('year')
            runtime = (media.get('runtimeMinutes') or 0) * 60  # seconds

            filepath = self._resolve_filepath(mtype, ext_ids, title, year)
            if not filepath:
                log.warning('Could not resolve path for: {0}'.format(title))
                lines.append('# NOT FOUND: {0}'.format(title))
                continue

            lines.append('#EXTINF:{0},{1}'.format(int(runtime), title))
            lines.append(filepath)
            matched += 1

        if matched == 0:
            log.warning('No Kodi matches for list "{0}" — playlist not written'.format(name))
            return False

        # Sanitize name for filesystem
        safe_name = ''.join(c if c.isalnum() or c in ' _-.' else '_' for c in name).strip()
        filename  = 'chronicle_{0}.m3u'.format(safe_name.replace(' ', '_'))
        path      = xbmcvfs.translatePath(_PLAYLIST_DIR + filename)

        try:
            xbmcvfs.mkdirs(xbmcvfs.translatePath(_PLAYLIST_DIR))
            with open(path, 'w', encoding='utf-8') as f:
                f.write('\n'.join(lines))
            log.info('Written playlist: {0} ({1} items)'.format(filename, matched))
            return True
        except Exception as exc:
            log.error('Failed to write playlist "{0}": {1}'.format(filename, exc))
            return False

    # ── private — Kodi library lookup ──────────────────────────────────────────

    def _resolve_filepath(self, media_type: str, ext_ids: dict, title: str, year) -> str:
        """Return the file path for a media item from the Kodi library, or ''."""
        if 'episode' in media_type or 'tv' in media_type:
            # Chronicle list items don't carry season/episode context here — this
            # path realistically only resolves shows/movies. Fall through to movie
            # lookup for anything not clearly an episode.
            show = kodi_matcher.find_tvshow(ext_ids, title)
            if show:
                return show.get('file', '')

        movie = kodi_matcher.find_movie(ext_ids, title, year)
        return movie.get('file', '') if movie else ''
