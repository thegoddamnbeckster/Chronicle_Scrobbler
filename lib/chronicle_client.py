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

from lib.http_core import (
    api_headers, call_with_timeout, find_shared_chronicle_url as _find_shared_url, normalize_base_url, user_agent)
from lib import connection_health
from lib.logger import Logger

ADDON = xbmcaddon.Addon()
log   = Logger('client')
health = connection_health.install(ADDON, log)

_USER_AGENT = user_agent(ADDON.getAddonInfo('id'))


def _read_connection():
    """(base_url, api_key) as saved right now. A fresh xbmcaddon.Addon() every time: an Addon object made when the service started never
    sees a key that the Settings page / device-auth flow saves later (Kodi caches settings per object), and the service would keep sending
    the old key until Kodi was restarted."""
    fresh = xbmcaddon.Addon()
    return normalize_base_url(fresh.getSetting('chronicle_url')), fresh.getSetting('api_key')


def find_shared_chronicle_url():
    """Another installed Chronicle add-on's saved server address, to pre-fill the "Edit Connection" prompt (see http_core)."""
    return _find_shared_url(ADDON.getAddonInfo('id'), log)


class ChronicleClient:
    """Sends scrobble events and health-check requests to a Chronicle server."""

    def __init__(self):
        # Nothing is cached here: _settings() reads the saved URL and key afresh each time (see _read_connection).
        pass

    def refresh_settings(self):
        """Kept for callers that used to need it after device-auth; every request now reads the saved settings itself."""

    def _settings(self):
        """One atomic read of (base_url, api_key); both come from the same Addon object so they always belong together."""
        return _read_connection()

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
        if not health.not_configured(base_url, api_key):
            log.warning('Chronicle URL or API key not configured — scrobble skipped')
            return False

        req = self._build_request(
            base_url, api_key, '/api/v1/scrobble', data=payload, method='POST')

        def _do():
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status in (200, 201, 204):
                    log.debug('Scrobble accepted (HTTP {0})'.format(resp.status))
                    return True
                log.warning('Scrobble returned unexpected HTTP {0}'.format(resp.status))
                return False

        try:
            # call_with_timeout, not a bare urlopen -- see this module's own watchdog
            # doc. Without it, a DNS/connect-phase hang here wedges scrobbling silently
            # for the rest of the Kodi session: this call runs with self._sending held
            # True (see monitor.py's ChroniclePlayer._send_update), and every later poll
            # tick and playback callback sees that flag and no-ops, with nothing in the
            # log to explain why. Confirmed live (2026-09-11).
            return call_with_timeout(_do, timeout=10)
        except urllib.error.HTTPError as exc:
            log.error('Scrobble HTTP {0}: {1}'.format(exc.code, exc.reason))
            return False
        except Exception as exc:
            log.error('Scrobble failed: {0}'.format(exc))
            return False

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
        if not health.not_configured(base_url, api_key):
            return {}

        req = self._build_request(
            base_url, api_key, '/api/v1/scrobble/resume', data=payload, method='POST')

        def _do():
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = json.loads(resp.read().decode('utf-8'))
                return body.get('data') or {}

        try:
            # call_with_timeout -- see scrobble()'s identical comment; this call sits
            # on the same playback-start critical path.
            return call_with_timeout(_do, timeout=10)
        except Exception as exc:
            log.error('get_resume_state failed: {0}'.format(exc))
            return {}

    # ── library (ratings, status) ────────────────────────────────────────────

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
        if not health.not_configured(base_url, api_key):
            return False

        req = self._build_request(
            base_url, api_key, '/api/v1/library/{0}'.format(entry_id), data=payload, method='PATCH')

        def _do():
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status in (200, 204)

        try:
            return call_with_timeout(_do, timeout=10)
        except Exception as exc:
            log.error('update_library_entry({0}) failed: {1}'.format(entry_id, exc))
            return False

    # ── lists ─────────────────────────────────────────────────────────────────

    def get_lists(self):
        """GET /api/v1/lists — return all lists for the authenticated user.

        Returns a list of list-summary dicts (id, name, isOrdered, itemCount,
        description), [] when the user has no lists, or None when the request
        failed (so "nothing to sync" and "could not ask" are not the same answer).
        """
        lists = self._get_json('/api/v1/lists', default=None, warn_unconfigured='get_lists')
        return lists if isinstance(lists, list) else None

    def get_list(self, list_id: int) -> dict:
        """GET /api/v1/lists/{id} — return a single list with all its items.

        Returns the data dict (id, name, isOrdered, items[]) or an empty dict
        on error.
        """
        return self._get_json(
            '/api/v1/lists/{0}'.format(list_id), default={}, warn_unconfigured='get_list'
        ) or {}

    def get_current_user(self):
        """GET /api/v1/users/me -- the identity behind this addon's own API key.
        Accepts the same X-Api-Key auth as every scrobble endpoint (Chronicle's
        default authorization policy takes either a JWT or an API key -- see
        Chronicle.API's Program.cs), so this needs no separate auth path.

        Used only for the connection-status display in default.py's menu
        heading/Settings status field -- never anything scrobbling depends on.
        Short 5s timeout (vs. the 10s default elsewhere in this client): this
        runs every time the addon's menu opens, so a slow/unreachable server
        must not make the menu itself feel stuck; a plain "Connected"
        fallback (see default.py's _refresh_auth_status()) covers the miss.

        Returns {'username', 'displayName', ...} dict, or None on any
        failure (not configured, network error, revoked key)."""
        return self._get_json('/api/v1/users/me', default=None, timeout=5,
                               warn_unconfigured='get_current_user()')

    def test_connection(self):
        """GET /api/health — verify connectivity and API key.

        Returns a (success: bool, message: str) tuple.
        """
        base_url, api_key = self._settings()
        if not base_url:
            return False, 'Chronicle URL is not configured.'

        req = self._build_request(base_url, api_key, '/api/health')

        def _do():
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status == 200:
                    return True, ''
                return False, 'Unexpected HTTP {0}'.format(resp.status)

        try:
            # call_with_timeout -- this is called synchronously from the Settings
            # dialog's "Test Connection" button; without the watchdog, a DNS/connect
            # hang here freezes that dialog instead of surfacing an error.
            return call_with_timeout(_do, timeout=10)
        except urllib.error.HTTPError as exc:
            return False, 'HTTP {0}: {1}'.format(exc.code, exc.reason)
        except Exception as exc:
            return False, str(exc)

    # ── private ─────────────────────────────────────────────────────────────────

    def _get_json(self, path: str, default=None, timeout: int = 10, unwrap: bool = True,
                  warn_unconfigured: str = None):
        """Shared GET-and-parse-JSON helper: builds the request from one atomic
        settings snapshot, sends it, and returns body['data'] (or the whole body
        when unwrap=False, for an endpoint whose caller wants the envelope's other
        fields -- e.g. 'pagination' -- too) -- `default` on any failure, including
        "not configured"."""
        base_url, api_key = self._settings()
        if not health.not_configured(base_url, api_key):
            if warn_unconfigured:
                log.warning('Chronicle URL or API key not configured — {0} skipped'.format(
                    warn_unconfigured))
            return default

        req = self._build_request(base_url, api_key, path)

        def _do():
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = json.loads(resp.read().decode('utf-8'))
                return body if not unwrap else body.get('data', default)

        try:
            return call_with_timeout(_do, timeout=timeout)
        except Exception as exc:
            log.error('{0} failed: {1}'.format(path, exc))
            return default

    def _build_request(self, base_url: str, api_key: str, path: str,
                        data=None, method: str = 'GET') -> urllib.request.Request:
        headers = api_headers(api_key, _USER_AGENT)
        body = json.dumps(data).encode('utf-8') if data is not None else None
        return urllib.request.Request(
            '{0}{1}'.format(base_url, path), data=body, headers=headers, method=method)
