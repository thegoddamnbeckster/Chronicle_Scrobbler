# -*- coding: utf-8 -*-
"""HTTP client for the Chronicle REST API.

Uses urllib from the Python standard library — no third-party packages
needed inside Kodi's Python environment.

Authentication: X-Api-Key header (Chronicle scrobbler API key).
"""

import json
import urllib.request
import urllib.error
import xbmcaddon

from lib.logger import Logger

ADDON = xbmcaddon.Addon()
log   = Logger('client')

_USER_AGENT = 'Kodi/Chronicle-Scrobbler/1.0'


class ChronicleClient:
    """Sends scrobble events and health-check requests to a Chronicle server."""

    def __init__(self):
        self._base_url = ADDON.getSetting('chronicle_url').rstrip('/')
        self._api_key  = ADDON.getSetting('api_key')

    def refresh_settings(self):
        """Re-read URL and API key from addon settings.

        Call this after a device-auth flow completes so the client picks up
        the newly saved API key without needing to be reconstructed.
        """
        self._base_url = ADDON.getSetting('chronicle_url').rstrip('/')
        self._api_key  = ADDON.getSetting('api_key')

    # ── scrobbling ────────────────────────────────────────────────────────────

    def scrobble(self, payload: dict) -> bool:
        """POST /api/v1/scrobble — send a playback progress event / new watch.

        payload keys match Chronicle.API's real ScrobbleRequestDto:
            mediaItemId    (int, optional)   already-known Chronicle item id
            title          (str)             required when mediaItemId absent
            year           (int, optional)
            mediaType      (str, optional)   'movie' | 'tv_episode' | 'tv_show' | 'track'
            externalIds    (dict, optional)  {'imdb': '...', 'tmdb': '...', 'tvdb': '...'}
            progressPercent (float)          0.0-100.0
            timestamp      (str, optional)   ISO-8601; server defaults to "now"
            deviceName     (str, optional)

        Returns True on success (HTTP 2xx), False otherwise.
        """
        if not self._base_url or not self._api_key:
            log.warning('Chronicle URL or API key not configured — scrobble skipped')
            return False

        url  = '{0}/api/v1/scrobble'.format(self._base_url)
        data = json.dumps(payload).encode('utf-8')
        req  = self._build_request(url, data=data, method='POST')

        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status in (200, 201, 204):
                    log.debug('Scrobble accepted (HTTP {0})'.format(resp.status))
                    return True
                log.warning('Scrobble returned unexpected HTTP {0}'.format(resp.status))
                return False
        except urllib.error.HTTPError as exc:
            log.error('Scrobble HTTP {0}: {1}'.format(exc.code, exc.reason))
            return False
        except Exception as exc:
            log.error('Scrobble failed: {0}'.format(exc))
            return False

    def get_watch_summary(self, media_item_id: int) -> dict:
        """GET /api/v1/scrobble/summary/{mediaItemId}.

        Returns {'lastWatchedAt': iso-str|None, 'watchedCount': int}, or
        {'lastWatchedAt': None, 'watchedCount': 0} on any error (matches the
        server's own "no watches yet" response shape, so callers don't need
        a separate error branch just to treat it as "nothing known").
        """
        empty = {'lastWatchedAt': None, 'watchedCount': 0}
        if not self._base_url or not self._api_key:
            return empty

        url = '{0}/api/v1/scrobble/summary/{1}'.format(self._base_url, media_item_id)
        req = self._build_request(url)

        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = json.loads(resp.read().decode('utf-8'))
                return body.get('data', empty)
        except Exception as exc:
            log.error('get_watch_summary({0}) failed: {1}'.format(media_item_id, exc))
            return empty

    def get_resume_state(self, payload: dict) -> dict:
        """POST /api/v1/scrobble/resume — the cross-device "resume where I left off"
        check, called on playback start before this device has any local resume
        bookmark of its own for the item.

        payload keys match Chronicle.API's ResumeLookupRequestDto -- the same
        identifying subset scrobble() takes (mediaItemId OR
        title/year/mediaType/externalIds), just without progressPercent/timestamp/
        deviceName, which don't mean anything for a lookup.

        Returns {} when there's nothing to resume (item never seen, already fully
        watched, or the request/lookup failed) -- deliberately the same empty shape
        for "not found" as for "found nothing to resume", matching what the server
        itself does (see ScrobbleController.GetResumeState's own doc): a caller
        checking whether to seek on playback start only needs "is there a position",
        not why not. Otherwise {'mediaItemId': int, 'resumePositionPercent': float,
        'resumeUpdatedAt': iso-str|None}.
        """
        if not self._base_url or not self._api_key:
            return {}

        url  = '{0}/api/v1/scrobble/resume'.format(self._base_url)
        data = json.dumps(payload).encode('utf-8')
        req  = self._build_request(url, data=data, method='POST')

        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = json.loads(resp.read().decode('utf-8'))
                return body.get('data') or {}
        except Exception as exc:
            log.error('get_resume_state failed: {0}'.format(exc))
            return {}

    def get_media(self, media_id: int) -> dict:
        """GET /api/v1/media/{id} — full MediaItemDto, including Ancestors.

        Used by sync_engine.py to resolve an episode's season number (the season's
        own MediaItem.Number) since AncestorDto only carries id+name, not number.
        """
        if not self._base_url or not self._api_key:
            return {}

        url = '{0}/api/v1/media/{1}'.format(self._base_url, media_id)
        req = self._build_request(url)

        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = json.loads(resp.read().decode('utf-8'))
                return body.get('data', {})
        except Exception as exc:
            log.error('get_media({0}) failed: {1}'.format(media_id, exc))
            return {}

    # ── library (ratings, status) ────────────────────────────────────────────

    def get_library(self, status: str, page: int = 1, per_page: int = 100) -> dict:
        """GET /api/v1/library?status=&page=&perPage= — one page of the user's
        library entries for a given status. Returns the raw {'data': [...],
        'pagination': {...}} response dict, or {} on error."""
        if not self._base_url or not self._api_key:
            return {}

        url = '{0}/api/v1/library?status={1}&page={2}&perPage={3}'.format(
            self._base_url, status, page, per_page)
        req = self._build_request(url)

        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode('utf-8'))
        except Exception as exc:
            log.error('get_library(status={0}, page={1}) failed: {2}'.format(status, page, exc))
            return {}

    def iter_library_all_statuses(self):
        """Yield every LibraryEntryDto across all statuses, paginating each.

        Generator so sync_engine.py can update a progress bar per item without
        needing to know the total count ahead of time.
        """
        for status in ('Watching', 'Completed', 'Dropped', 'PlanToWatch', 'OnHold', 'Rewatching'):
            page = 1
            while True:
                body = self.get_library(status, page=page, per_page=100)
                entries = body.get('data') or []
                if not entries:
                    break
                for entry in entries:
                    yield entry
                if len(entries) < 100:
                    break
                page += 1

    def update_library_entry(self, entry_id: int, user_rating=None, status: str = None) -> bool:
        """PATCH /api/v1/library/{id} — used only for user-initiated rating/status
        edits made from Kodi (e.g. a future context-menu "Rate" action). NOT used
        by the bulk Chronicle -> Kodi sync, which only ever reads from Chronicle."""
        if not self._base_url or not self._api_key:
            return False

        payload = {}
        if user_rating is not None:
            payload['userRating'] = user_rating
        if status is not None:
            payload['status'] = status
        if not payload:
            return True

        url  = '{0}/api/v1/library/{1}'.format(self._base_url, entry_id)
        data = json.dumps(payload).encode('utf-8')
        req  = self._build_request(url, data=data, method='PATCH')

        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status in (200, 204)
        except Exception as exc:
            log.error('update_library_entry({0}) failed: {1}'.format(entry_id, exc))
            return False

    # ── lists ─────────────────────────────────────────────────────────────────

    def get_lists(self) -> list:
        """GET /api/v1/lists — return all lists for the authenticated user.

        Returns a list of list-summary dicts (id, name, isOrdered, itemCount,
        description) or an empty list on error.
        """
        if not self._base_url or not self._api_key:
            log.warning('Chronicle URL or API key not configured — get_lists skipped')
            return []

        url = '{0}/api/v1/lists'.format(self._base_url)
        req = self._build_request(url)

        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = json.loads(resp.read().decode('utf-8'))
                return body.get('data', [])
        except Exception as exc:
            log.error('get_lists failed: {0}'.format(exc))
            return []

    def get_list(self, list_id: int) -> dict:
        """GET /api/v1/lists/{id} — return a single list with all its items.

        Returns the data dict (id, name, isOrdered, items[]) or an empty dict
        on error.
        """
        if not self._base_url or not self._api_key:
            log.warning('Chronicle URL or API key not configured — get_list skipped')
            return {}

        url = '{0}/api/v1/lists/{1}'.format(self._base_url, list_id)
        req = self._build_request(url)

        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = json.loads(resp.read().decode('utf-8'))
                return body.get('data', {})
        except Exception as exc:
            log.error('get_list({0}) failed: {1}'.format(list_id, exc))
            return {}

    def test_connection(self):
        """GET /api/health — verify connectivity and API key.

        Returns a (success: bool, message: str) tuple.
        """
        if not self._base_url:
            return False, 'Chronicle URL is not configured.'

        url = '{0}/api/health'.format(self._base_url)
        req = self._build_request(url)

        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status == 200:
                    return True, ''
                return False, 'Unexpected HTTP {0}'.format(resp.status)
        except urllib.error.HTTPError as exc:
            return False, 'HTTP {0}: {1}'.format(exc.code, exc.reason)
        except Exception as exc:
            return False, str(exc)

    # ── private ─────────────────────────────────────────────────────────────────

    def _build_request(self, url: str, data=None, method: str = 'GET') -> urllib.request.Request:
        headers = {
            'Content-Type': 'application/json',
            'X-Api-Key':    self._api_key,
            'User-Agent':   _USER_AGENT,
        }
        return urllib.request.Request(url, data=data, headers=headers, method=method)
