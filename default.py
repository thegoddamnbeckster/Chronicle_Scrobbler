# -*- coding: utf-8 -*-
"""service.chronicle.scrobbler — Script entry point.

Shown when the user opens the addon from the Kodi add-on browser.
Presents a simple action menu:
  • Sync Watch History & Ratings Now
  • Reset TV Show Progress
  • Reset Movie Progress
  • Test Connection
  • Connect to Chronicle  (QR device auth)
  • Sync Lists to Kodi    (playlist sync)
  • Open Settings
"""

import sys
import traceback

import xbmcgui
import xbmcaddon

from lib.logger import Logger
from lib.chronicle_client import ChronicleClient
from lib.reset_manager import ResetManager
from lib.device_auth import DeviceAuthManager
from lib.playlist_sync import PlaylistSync
from lib.sync_engine import SyncEngine

ADDON = xbmcaddon.Addon()
log   = Logger('default')


def _get_args():
    """Parse action=... from RunScript(service.chronicle.scrobbler,action=...) calls."""
    args = {}
    for arg in sys.argv[1:]:
        if '=' in arg:
            key, value = arg.split('=', 1)
            args[key] = value
    return args


def _refresh_auth_status():
    """Keep the read-only Settings status field honest before Settings is shown.

    Requires BOTH chronicle_url and api_key. Previously checked api_key alone,
    so it kept showing "Connected" purely because an api_key from a PAST
    successful connection was still saved, even while chronicle_url sat empty
    and every actual Connect attempt was failing outright. Confirmed live
    (2026-08-27, sibling Chronicle_Scraper addons): status showed "Connected"
    immediately after a Connect attempt that never got past "URL not set."
    """
    connected = bool(ADDON.getSetting('chronicle_url')) and bool(ADDON.getSetting('api_key'))
    ADDON.setSetting(
        'auth_status',
        ADDON.getLocalizedString(32081 if connected else 32082),  # "Connected" / "Not connected"
    )


_LOOPBACK_MARKERS = ('localhost', '127.0.0.1', '::1')


def _warn_if_localhost():
    """Catch a URL that will only work if Chronicle runs on this same device --
    Kodi and Chronicle are commonly on separate machines, and a loopback address
    only became reachable at all here because Kodi happened to be on the same
    box as the server during testing. Runs right after Connect saves a new URL.
    """
    url = ADDON.getSetting('chronicle_url').lower()
    if not url or not any(marker in url for marker in _LOOPBACK_MARKERS):
        return

    keep = xbmcgui.Dialog().yesno(
        ADDON.getLocalizedString(32000),   # "Chronicle Scrobbler"
        ADDON.getLocalizedString(32083),   # loopback warning text
    )
    if not keep:
        ADDON.setSetting('chronicle_url', '')


def show_menu():
    """Display the main action menu. No auto-bounce to Settings on first run --
    "Edit Connection" is directly reachable from here even when unconfigured,
    since it's the one reliable place the URL ever gets entered. See
    _connect_to_chronicle()'s own docstring for why the Settings screen no
    longer does that job.
    """
    args = _get_args()
    if args.get('action') == 'auth':
        _connect_to_chronicle()
        return

    _refresh_auth_status()

    options = [
        ADDON.getLocalizedString(32070),  # Sync Watch History & Ratings Now
        ADDON.getLocalizedString(32010),  # Reset TV Show Progress
        ADDON.getLocalizedString(32011),  # Reset Movie Progress
        ADDON.getLocalizedString(32012),  # Test Connection
        ADDON.getLocalizedString(32061),  # Edit Connection
        ADDON.getLocalizedString(32050),  # Sync Lists to Kodi
        ADDON.getLocalizedString(32013),  # Open Settings
    ]

    dialog = xbmcgui.Dialog()
    choice = dialog.select(ADDON.getLocalizedString(32000), options)

    if choice == 0:
        _sync_watch_history()
    elif choice == 1:
        ResetManager().prompt_reset_tvshow()
    elif choice == 2:
        ResetManager().prompt_reset_movie()
    elif choice == 3:
        _test_connection()
    elif choice == 4:
        _connect_to_chronicle()
    elif choice == 5:
        _sync_lists()
    elif choice == 6:
        _refresh_auth_status()
        ADDON.openSettings()


def _test_connection():
    """Test connectivity to Chronicle and display a result dialog."""
    client  = ChronicleClient()
    dialog  = xbmcgui.Dialog()
    ok, msg = client.test_connection()

    if ok:
        dialog.ok(
            ADDON.getLocalizedString(32012),
            ADDON.getLocalizedString(32020),   # Connection successful!
        )
    else:
        dialog.ok(
            ADDON.getLocalizedString(32012),
            '{0}\n{1}'.format(ADDON.getLocalizedString(32021), msg),   # Connection failed: <msg>
        )


def _connect_to_chronicle():
    """"Edit Connection" -- launches the QR device-auth flow to obtain an API key.

    Prompts for the Chronicle URL directly, via a reliable modal dialog, but
    ONLY when there isn't already a saved one. Settings' own chronicle_url
    text field is READ-ONLY (enable="false" in resources/settings.xml) and no
    longer trusted for entry at all: Kodi's on-screen-keyboard edit to that
    field was confirmed live (2026-08-27, sibling Chronicle_Scraper addons)
    to not reliably land in the underlying setting, even after the whole
    Settings dialog was fully closed -- not a timing issue, an
    entry-not-committing one, upstream of anything this addon controls.
    Rather than fight that control (and risk the user retyping the same URL
    twice, once in Settings and again in a fallback prompt), this is now the
    ONE place the URL is ever entered. An already-connected reconnect
    (chronicle_url already set) skips the prompt entirely and goes straight
    to the QR window: a working URL is never asked for twice.
    """
    current = ADDON.getSetting('chronicle_url')
    log.info('_connect_to_chronicle: invoked; chronicle_url on disk = {0!r}'.format(current))

    if not current:
        entered = xbmcgui.Dialog().input(ADDON.getLocalizedString(32002), defaultt='')  # "Chronicle URL"
        entered = (entered or '').strip()
        log.info('_connect_to_chronicle: URL prompt returned {0!r}'.format(entered))
        if not entered:
            log.info('_connect_to_chronicle: cancelled -- aborting')
            return
        ADDON.setSetting('chronicle_url', entered)
        log.info('_connect_to_chronicle: saved new URL {0!r}'.format(entered))
        _warn_if_localhost()

    log.info('_connect_to_chronicle: calling DeviceAuthManager().run()')
    try:
        DeviceAuthManager().run()
    except Exception:
        # RunScript-launched scripts have no visible crash surface -- an unhandled
        # exception here would otherwise look EXACTLY like "the connection window
        # never showed up" to the user, with nothing in the log tying the two
        # together unless this is caught and logged explicitly with a traceback.
        log.error('_connect_to_chronicle: DeviceAuthManager().run() raised:\n{0}'.format(
                  traceback.format_exc()))
        xbmcgui.Dialog().ok(
            ADDON.getLocalizedString(32060),
            'Connect failed unexpectedly -- see kodi.log for details.',
        )
    log.info('_connect_to_chronicle: DeviceAuthManager().run() returned')
    _refresh_auth_status()


def _sync_lists():
    """Sync Chronicle lists to Kodi .m3u playlist files."""
    synced, failed = PlaylistSync().sync_all()
    xbmcgui.Dialog().ok(
        ADDON.getLocalizedString(32050),   # "Sync Lists to Kodi"
        ADDON.getLocalizedString(32055).format(synced, failed),  # "Done! {0} written, {1} failed."
    )


def _sync_watch_history():
    """Bulk Chronicle -> Kodi sync: ratings, art, and playcount/lastplayed reconciliation."""
    result = SyncEngine().sync_all()
    xbmcgui.Dialog().ok(
        ADDON.getLocalizedString(32070),   # "Sync Watch History & Ratings Now"
        ADDON.getLocalizedString(32073).format(result.synced, result.skipped, result.failed),
        # "Done! {0} synced, {1} skipped, {2} failed."
    )


if __name__ == '__main__':
    show_menu()
