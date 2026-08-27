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

import xbmc
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


def _is_configured():
    """True once a server URL and API key are both present.

    "Connect to Chronicle" lives as an action button on the Settings page
    itself (resources/settings.xml), not in this menu — so requiring the key
    here doesn't create a dead end: an unconfigured click goes straight to
    Settings, where the URL field and the connect button sit side by side.
    """
    return bool(ADDON.getSetting('chronicle_url')) and bool(ADDON.getSetting('api_key'))


def _get_args():
    """Parse action=... from RunScript(service.chronicle.scrobbler,action=...) calls."""
    args = {}
    for arg in sys.argv[1:]:
        if '=' in arg:
            key, value = arg.split('=', 1)
            args[key] = value
    return args


def _refresh_auth_status():
    """Keep the read-only Settings status field honest before Settings is shown."""
    connected = bool(ADDON.getSetting('api_key'))
    ADDON.setSetting(
        'auth_status',
        ADDON.getLocalizedString(32081 if connected else 32082),  # "Connected" / "Not connected"
    )


_LOOPBACK_MARKERS = ('localhost', '127.0.0.1', '::1')


def _warn_if_localhost():
    """Catch a URL that will only work if Chronicle runs on this same device --
    Kodi and Chronicle are commonly on separate machines, and a loopback address
    only became reachable at all here because Kodi happened to be on the same
    box as the server during testing. Runs right after Settings closes rather
    than reactively on every settings change, so it only fires once per visit
    instead of on every unrelated toggle.
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
    """Display the main action menu, or jump straight to Settings on first run."""
    args = _get_args()
    if args.get('action') == 'auth':
        _connect_to_chronicle()
        return

    if not _is_configured():
        _refresh_auth_status()
        xbmcgui.Dialog().notification(
            ADDON.getLocalizedString(32000),   # "Chronicle Scrobbler"
            ADDON.getLocalizedString(32079),   # "Not configured yet — opening settings…"
            xbmcgui.NOTIFICATION_INFO,
            4000,
        )
        ADDON.openSettings()
        _warn_if_localhost()
        return

    options = [
        ADDON.getLocalizedString(32070),  # Sync Watch History & Ratings Now
        ADDON.getLocalizedString(32010),  # Reset TV Show Progress
        ADDON.getLocalizedString(32011),  # Reset Movie Progress
        ADDON.getLocalizedString(32012),  # Test Connection
        ADDON.getLocalizedString(32061),  # Connect to Chronicle
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
        _warn_if_localhost()


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
    """Launch the QR device-auth flow to obtain an API key.

    Closes the Settings dialog first, if it's the one that's actually open.
    Root-caused (2026-08-27, against the sibling Chronicle_Scraper addons) by
    reading settings.xml directly moments after a failed Connect attempt:
    chronicle_url was still sitting at its empty default a full minute-plus
    after the user had typed a URL and could see it in the field. Not a
    timing race -- an ORDERING one. Kodi's addon Settings dialog only writes
    settings.xml to disk when the whole dialog closes, not per-field as focus
    moves between controls. "Connect to Chronicle" is a button INSIDE that
    same still-open dialog (RunScript launches this as a brand-new script
    instance while Settings is still up), so no amount of waiting afterward
    can see a value that was never written in the first place -- an earlier
    fix here (v2.2.4) tried a settle-wait before reading and confirmed
    exactly that: still empty after waiting, because there was nothing to
    wait FOR yet.

    Window.IsActive(addonsettings) gates the Action(Back) so this only ever
    closes something when Settings is confirmed to actually be the active
    window -- reached from the main menu (Settings already closed) this is a
    no-op, never risking closing the wrong window.
    """
    settings_open = xbmc.getCondVisibility('Window.IsActive(addonsettings)')
    log.info('_connect_to_chronicle: invoked; Settings dialog open = {0}'.format(settings_open))
    if settings_open:
        log.info('_connect_to_chronicle: closing Settings (Action(Back)) to force a flush')
        xbmc.executebuiltin('Action(Back)')
        xbmc.sleep(400)   # let the close + settings.xml write actually complete
    log.info('_connect_to_chronicle: chronicle_url on disk = {0!r}'.format(
             ADDON.getSetting('chronicle_url')))

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
