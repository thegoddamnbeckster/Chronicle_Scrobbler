# -*- coding: utf-8 -*-
"""Resolves a human-meaningful name for this Kodi instance.

Shared by device_auth.py (device-auth pairing request) and media_info.py
(every scrobble payload) so a scrobble's deviceName column in Chronicle's
history/reports actually identifies which box played it (e.g. "Vision" or
a DNS name) instead of the literal, useless "Kodi" every installation used
to send -- indistinguishable from any other Kodi instance scrobbling to the
same Chronicle account.
"""

import socket

import xbmcgui

from lib.logger import Logger

log = Logger('device_name')

_LOOPBACK_NAMES = ('localhost', 'localhost.localdomain')


def get_device_name() -> str:
    """Prefer the machine's actual DNS name over Kodi's own FriendlyName setting --
    FriendlyName is just an arbitrary label the user can set to anything, while a
    real DNS name is a verifiable identifier for the physical device.

    socket.getfqdn() tries a genuine reverse-DNS/hosts lookup and upgrades to a
    dotted name when that succeeds, but on a typical home LAN there's usually no
    such record -- it then falls back to the plain OS hostname, which is still a
    real, meaningful identifier (often mDNS-resolvable as "<hostname>.local") and
    clearly better than an arbitrary Kodi settings label. Only the degenerate
    "localhost" non-answer is treated as "no usable name".
    """
    try:
        fqdn = socket.getfqdn().strip()
        if fqdn and fqdn.lower() not in _LOOPBACK_NAMES:
            return fqdn
    except Exception as exc:
        log.debug('DNS name lookup failed: {0}'.format(exc))

    friendly_name = xbmcgui.Window(10000).getProperty('System.FriendlyName')
    return 'Kodi — {0}'.format(friendly_name or 'Kodi')
