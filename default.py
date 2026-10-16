# -*- coding: utf-8 -*-
"""service.chronicle.scrobbler — Script entry point.

Launching the addon from Kodi's Add-on browser goes straight to Settings --
per-user correction (2026-08-29): "I only ever want them to open the regular
settings window, not whatever [the old action-list menu] is." The other
actions this used to offer as a Dialog().select() menu (Sync Watch History &
Ratings Now, Reset TV/Movie Progress, Test Connection, Sync Lists to Kodi)
are still reachable -- each now has its own action button inside the
relevant Settings category, using the same RunScript(...,action=...)
mechanism Edit Connection/Change Chronicle URL already used.
"""

import sys

import xbmcgui
import xbmcaddon

from lib.logger import Logger
from lib.chronicle_client import ChronicleClient
from lib.connection_ui import ConnectionUi, parse_script_args
from lib.reset_manager import ResetManager
from lib.device_auth import DeviceAuthManager
from lib.playlist_sync import PlaylistSync

ADDON = xbmcaddon.Addon()
log   = Logger('default')

_on_connected = None

_ui = ConnectionUi(ADDON, log, ChronicleClient, lambda base_url: DeviceAuthManager(base_url=base_url), _on_connected)


def _get_args():
    return parse_script_args(sys.argv)


_refresh_auth_status = _ui.refresh_auth_status
_test_connection = _ui.test_connection
_connect_to_chronicle = _ui.connect
_change_chronicle_url = _ui.change_url


def show_menu():
    """Entry point for both a plain addon-browser launch (no action= arg --
    goes straight to Settings) and every RunScript(...,action=X) call a
    Settings action button makes (dispatched below, each returning before
    Settings would otherwise open). "Edit Connection" works even when
    unconfigured, since it's the one reliable place the URL ever gets
    entered -- see _connect_to_chronicle()'s own docstring for why the
    Settings screen no longer does that job itself.
    """
    args = _get_args()
    if args.get('action') == 'auth':
        _connect_to_chronicle()
        return
    if args.get('action') == 'change_url':
        _change_chronicle_url()
        return
    if args.get('action') == 'test_connection':
        _test_connection()
        return
    if args.get('action') == 'reset_tv':
        ResetManager().prompt_reset_tvshow()
        return
    if args.get('action') == 'reset_movie':
        ResetManager().prompt_reset_movie()
        return
    if args.get('action') == 'sync_lists':
        _sync_lists()
        return

    _refresh_auth_status()
    ADDON.openSettings()


def _sync_lists():
    """Sync Chronicle lists to Kodi .m3u playlist files."""
    result = PlaylistSync().sync_all()
    title = ADDON.getLocalizedString(32050)   # "Sync Lists to Kodi"
    if result is None:
        xbmcgui.Dialog().ok(title, ADDON.getLocalizedString(32056))   # "Sync failed: Chronicle is not connected or not reachable."
    elif result == (0, 0):
        xbmcgui.Dialog().ok(title, ADDON.getLocalizedString(32054))   # "No lists found on Chronicle."
    else:
        xbmcgui.Dialog().ok(title, ADDON.getLocalizedString(32055).format(*result))  # "Done! {0} written, {1} failed."


if __name__ == '__main__':
    show_menu()
