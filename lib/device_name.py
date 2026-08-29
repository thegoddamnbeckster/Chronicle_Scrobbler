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

    Confirmed live (2026-08-28, Shield Android TV box): BOTH socket calls come
    back unusable there -- Android's sandboxed network stack doesn't expose a
    real hostname/FQDN to Python the way desktop OSes do -- so this used to fall
    straight through to 'Kodi — {FriendlyName}', and since FriendlyName itself
    defaults to the literal string "Kodi" until a user explicitly customizes it
    (Settings -> Services -> Control -> Device Name), the Now Playing banner
    showed the genuinely useless "Kodi — Kodi" on every un-customized install --
    indistinguishable from the original bug this function was written to fix.
    Two changes: gethostname() (a plain, non-DNS local call) tried as a second
    rung before FriendlyName, since it can succeed even when getfqdn()'s DNS
    step fails; and the "Kodi — " prefix is now skipped whenever FriendlyName
    is itself still the unmodified default, since prefixing "Kodi — " onto
    "Kodi" is exactly how the redundant name happened -- an honest, visibly-a-
    fallback label is more useful than repeating the one word twice.
    """
    try:
        fqdn = socket.getfqdn().strip()
        if fqdn and fqdn.lower() not in _LOOPBACK_NAMES:
            return fqdn
    except Exception as exc:
        log.debug('DNS name lookup failed: {0}'.format(exc))

    try:
        hostname = socket.gethostname().strip()
        if hostname and hostname.lower() not in _LOOPBACK_NAMES:
            return hostname
    except Exception as exc:
        log.debug('Local hostname lookup failed: {0}'.format(exc))

    friendly_name = (xbmcgui.Window(10000).getProperty('System.FriendlyName') or '').strip()
    if friendly_name and friendly_name.lower() != 'kodi':
        # A real, user-customized label -- trust it as-is, no "Kodi — " prefix
        # needed; the web UI already shows this is a Kodi session separately.
        return friendly_name

    # Every real identifier this function knows how to find either failed or
    # came back as Kodi's own un-customized default. Naming this explicitly
    # as unnamed (rather than quietly repeating "Kodi") is what actually
    # points the user at the fix: Settings -> Services -> Control -> Device
    # Name, on THIS box specifically.
    return 'Kodi (unnamed device)'
