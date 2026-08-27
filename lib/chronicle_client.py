# -*- coding: utf-8 -*-
"""HTTP client for the Chronicle REST API.

Uses urllib from the Python standard library — no third-party packages
needed inside Kodi's Python environment.

Authentication: X-Api-Key header (Chronicle scrobbler API key).
"""

import json
import threading
import urllib.request
import urllib.error
import xbmcaddon

from lib.logger import Logger

ADDON = xbmcaddon.Addon()
log   = Logger('client')

_USER_AGENT = 'Kodi/Chronicle-Scrobbler/1.0'

# urlopen(timeout=N) only bounds the socket once it exists -- the DNS lookup
# (getaddrinfo) that happens before that is NOT covered by that timeout on
# any platform. A dead/unreachable DNS server or a stale hostname can hang a
# "timed" call forever, well past whatever timeout= was passed in. Confirmed
# live against the sibling Chronicle_Scraper addons (2026-08-27), whose own
# chronicle_client.py carries this exact same watchdog for this exact same
# reason -- this addon's device_auth.py never adopted it. call_with_timeout()
# is the backstop: it runs the call on a daemon thread and gives up after
# timeout + _WATCHDOG_GRACE_SECONDS even if that thread never returns,
# leaving the runaway thread to die on its own (daemon=True means it can't
# block Kodi from exiting).
_WATCHDOG_GRACE_SECONDS = 5


def call_with_timeout(fn, timeout):
    result = {}

    def _target():
        try:
            result['value'] = fn()
        except BaseException as exc:
            result['error'] = exc

    t = threading.Thread(target=_target, daemon=True)
    t.start()
    t.join(timeout + _WATCHDOG_GRACE_SECONDS)
    if t.is_alive():
        raise TimeoutError('no response within {0}s (DNS/network hang)'.format(
            timeout + _WATCHDOG_GRACE_SECONDS))
    if 'error' in result:
        raise result['error']
    return result.get('value')


class ChronicleClient:
    """Sends scrobble events and health-check requests to a Chronicle server."""

    def __init__(self):
        # A single tuple, always replaced as one atomic assignment (never mutated
        # field-by-field) -- every request-sending method takes ONE snapshot of this
        # via _settings() and uses both values from that same snapshot throughout,
        # so a concurrent refresh_settings() (e.g. right after device-auth completes)
        # can never hand one method a mix of the old URL and the new key or vice versa.
        self._settings_tuple = (
            ADDON.getSetting('chronicle_url').rstrip('/'),
            ADDON.getSetting('api_key'),
        )

    def refresh_settings(self):
        """Re-read URL and API key from addon settings.

        Call this after a device-auth flow completes so the client picks up
        the newly saved API key without needing to be reconstructed.
        """
        self._settings_tuple = (
            ADDON.getSetting('chronicle_url').rstrip('/'),
            ADDON.getSetting('api_key'),
        )

    def _settings(self):
        """One atomic read of (base_url, api_key) -- see __init__."""
        return self._settings_tuple

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
        base_url, api_key = self._settings()
        if not base_url or not api_key:
            log.warning('Chronicle URL or API key not configured — scrobble skipped')
            return False

        req = self._build_request(
            base_url, api_key, '/api/v1/scrobble', data=payload, method='POST')
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
        return self._get_json(
            '/api/v1/scrobble/summary/{0}'.format(media_item_id), default=empty
        ) or empty

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
        base_url, api_key = self._settings()
        if not base_url or not api_key:
            return {}

        req = self._build_request(
            base_url, api_key, '/api/v1/scrobble/resume', data=payload, method='POST')
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
        own MediaItem.Number) since AncestorDto only carries id+number.
        """
        return self._get_json('/api/v1/media/{0}'.format(media_id), default={}) or {}

    # ── library (ratings, status) ────────────────────────────────────────────

    def get_library(self, status: str, page: int = 1, per_page: int = 100) -> dict:
        """GET /api/v1/library?status=&page=&perPage= — one page of the user's
        library entries for a given status. Returns the raw {'data': [...],
        'pagination': {...}} response dict, or {} on error."""
        return self._get_json(
            '/api/v1/library?status={0}&page={1}&perPage={2}'.format(status, page, per_page),
            default={}, timeout=15, unwrap=False,
        ) or {}

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
        payload = {}
        if user_rating is not None:
            payload['userRating'] = user_rating
        if status is not None:
            payload['status'] = status
        if not payload:
            return True

        base_url, api_key = self._settings()
        if not base_url or not api_key:
            return False

        req = self._build_request(
            base_url, api_key, '/api/v1/library/{0}'.format(entry_id), data=payload, method='PATCH')
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
        return self._get_json('/api/v1/lists', default=[], warn_unconfigured='get_lists') or []

    def get_list(self, list_id: int) -> dict:
        """GET /api/v1/lists/{id} — return a single list with all its items.

        Returns the data dict (id, name, isOrdered, items[]) or an empty dict
        on error.
        """
        return self._get_json(
            '/api/v1/lists/{0}'.format(list_id), default={}, warn_unconfigured='get_list'
        ) or {}

    def test_connection(self):
        """GET /api/health — verify connectivity and API key.

        Returns a (success: bool, message: str) tuple.
        """
        base_url, api_key = self._settings()
        if not base_url:
            return False, 'Chronicle URL is not configured.'

        req = self._build_request(base_url, api_key, '/api/health')
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

    def _get_json(self, path: str, default=None, timeout: int = 10, unwrap: bool = True,
                  warn_unconfigured: str = None):
        """Shared GET-and-parse-JSON helper: builds the request from one atomic
        settings snapshot, sends it, and returns body['data'] (or the whole body
        when unwrap=False, for endpoints like get_library() whose caller wants
        'pagination' too) -- `default` on any failure, including "not configured"."""
        base_url, api_key = self._settings()
        if not base_url or not api_key:
            if warn_unconfigured:
                log.warning('Chronicle URL or API key not configured — {0} skipped'.format(
                    warn_unconfigured))
            return default

        req = self._build_request(base_url, api_key, path)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = json.loads(resp.read().decode('utf-8'))
                return body if not unwrap else body.get('data', default)
        except Exception as exc:
            log.error('{0} failed: {1}'.format(path, exc))
            return default

    def _build_request(self, base_url: str, api_key: str, path: str,
                        data=None, method: str = 'GET') -> urllib.request.Request:
        headers = {
            'Content-Type': 'application/json',
            'X-Api-Key':    api_key,
            'User-Agent':   _USER_AGENT,
        }
        body = json.dumps(data).encode('utf-8') if data is not None else None
        return urllib.request.Request(
            '{0}{1}'.format(base_url, path), data=body, headers=headers, method=method)
