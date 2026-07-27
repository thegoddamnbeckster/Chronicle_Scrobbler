# -*- coding: utf-8 -*-
"""Orchestrates the QR-code device authentication flow for Chronicle.

Same shape as SIMKL_Scrobbler's PIN/QR signup flow, retargeted at Chronicle's
own device-auth contract (confirmed live against Chronicle.API's
DeviceAuthController):

  1. POST /api/v1/auth/device  → get code, display_code, qr_url, verification_url
  2. Download QR PNG from qr_url
  3. Show QR dialog with the QR image, display code and verification URL
  4. Poll GET /api/v1/auth/device/{code}/poll every 5 seconds in background
  5. When status == "approved", save apiKey to settings and close dialog
  6. When denied/expired, show error and close
"""

import threading
import time
import json
import urllib.request
import urllib.error

import xbmcvfs
import xbmcgui
import xbmcaddon

from lib.logger import Logger
from lib.chronicle_client import ChronicleClient
from lib.qr_dialog import QRDialog

ADDON = xbmcaddon.Addon()
log   = Logger('device_auth')

_POLL_INTERVAL = 5      # seconds between polls


class DeviceAuthManager:
    """Drives the full QR-code auth flow."""

    def __init__(self):
        self._client = ChronicleClient()

    def run(self) -> bool:
        """
        Start the device auth flow.
        Returns True if an API key was successfully obtained, False otherwise.
        """
        # ── 1. Initiate ─────────────────────────────────────────────────────
        result = self._initiate()
        if result is None:
            xbmcgui.Dialog().ok(
                ADDON.getLocalizedString(32060),
                ADDON.getLocalizedString(32065),  # Could not contact Chronicle
            )
            return False

        code             = result['code']
        display_code     = result['displayCode']
        qr_url           = result['qrUrl']
        verification_url = result['verificationUrl']
        expires_in       = int(result.get('expiresInSeconds', 300))

        log.info('Device auth initiated — display code: {0}'.format(display_code))

        # ── 2. Download QR image ────────────────────────────────────────────
        # Filename includes the code so every attempt gets a fresh path -- Kodi's
        # texture manager caches loaded images in memory by path for the session,
        # so reusing one fixed filename kept showing whatever was first loaded
        # there even after the file on disk had been overwritten with new bytes.
        qr_path = self._download_qr(qr_url, code)

        # ── 3. Start polling thread ─────────────────────────────────────────
        api_key_holder = [None]   # shared result slot
        stop_event     = threading.Event()
        poll_thread    = threading.Thread(
            target=self._poll_loop,
            args=(code, api_key_holder, stop_event),
            daemon=True,
        )
        poll_thread.start()

        # ── 4. Show QR dialog ───────────────────────────────────────────────
        dialog = QRDialog(
            qr_path          = qr_path or '',
            display_code     = display_code,
            verification_url = verification_url,
            expires_in       = expires_in,
            stop_event       = stop_event,
            api_key_holder   = api_key_holder,
        )
        dialog.doModal()     # Blocks until closed (approved, denied, expired, or cancelled)
        del dialog

        stop_event.set()
        poll_thread.join(timeout=10)

        # ── 5. Save API key if approved ─────────────────────────────────────
        api_key = api_key_holder[0]
        if api_key:
            ADDON.setSetting('api_key', api_key)
            ADDON.setSetting('auth_status', ADDON.getLocalizedString(32081))  # "Connected"
            log.info('API key saved successfully')
            xbmcgui.Dialog().ok(
                ADDON.getLocalizedString(32060),
                ADDON.getLocalizedString(32066),  # Connected to Chronicle!
            )
            return True

        return False

    # ── private ────────────────────────────────────────────────────────────────

    def _initiate(self):
        """POST /api/v1/auth/device — returns parsed JSON data dict or None."""
        base_url = ADDON.getSetting('chronicle_url').rstrip('/')
        if not base_url:
            return None

        device_name = 'Kodi — {0}'.format(xbmcgui.Window(10000).getProperty('System.FriendlyName') or 'Kodi')

        try:
            url     = '{0}/api/v1/auth/device'.format(base_url)
            payload = json.dumps({'deviceName': device_name}).encode('utf-8')
            req     = urllib.request.Request(
                url, data=payload,
                headers={'Content-Type': 'application/json',
                         'User-Agent': 'Kodi/Chronicle-Scrobbler/1.0'},
                method='POST',
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = json.loads(resp.read().decode('utf-8'))
                return body.get('data')
        except Exception as exc:
            log.error('Device auth initiation failed: {0}'.format(exc))
            return None

    def _download_qr(self, qr_url: str, code: str) -> str:
        """Download QR PNG to a temp file unique to this code, return its special:// VFS
        path (or '' on failure) -- NOT the translated real filesystem path.

        translatePath() is needed for Python's own open() below, since it can't write
        through a special:// URL directly. But everything downstream (ControlImage) is
        Kodi's own GUI/texture layer, which is documented to expect special:// notation;
        the addon's own bundled images (panel, buttons) render fine referenced that way,
        while this file -- previously handed to ControlImage as the translated raw OS
        path -- rendered as nothing despite being confirmed valid, unique-per-attempt,
        real-color-type PNG data on disk. Untranslated special:// is the one remaining
        difference between the two.
        """
        vfs_path  = 'special://temp/chronicle_qr_{0}.png'.format(code[:16])
        real_path = xbmcvfs.translatePath(vfs_path)
        try:
            req = urllib.request.Request(
                qr_url,
                headers={'User-Agent': 'Kodi/Chronicle-Scrobbler/1.0'},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = resp.read()

            with open(real_path, 'wb') as f:
                f.write(data)

            log.debug('QR image downloaded to {0}'.format(real_path))
            return vfs_path
        except Exception as exc:
            log.warning('QR download failed: {0}'.format(exc))
            return ''

    def _poll_loop(self, code: str, api_key_holder: list, stop_event: threading.Event):
        """Background thread: poll Chronicle until approved, denied, expired, or cancelled."""
        base_url = ADDON.getSetting('chronicle_url').rstrip('/')
        url      = '{0}/api/v1/auth/device/{1}/poll'.format(base_url, code)

        while not stop_event.is_set():
            stop_event.wait(_POLL_INTERVAL)
            if stop_event.is_set():
                break

            try:
                req = urllib.request.Request(
                    url,
                    headers={'User-Agent': 'Kodi/Chronicle-Scrobbler/1.0'},
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    body   = json.loads(resp.read().decode('utf-8'))
                    data   = body.get('data', {})
                    status = data.get('status', 'pending')
                    api_key = data.get('apiKey')

                log.debug('Poll status: {0}'.format(status))

                if status == 'approved' and api_key:
                    api_key_holder[0] = api_key
                    stop_event.set()
                    break
                elif status in ('denied', 'expired'):
                    stop_event.set()
                    break

            except Exception as exc:
                log.warning('Poll error: {0}'.format(exc))
