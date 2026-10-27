# -*- coding: utf-8 -*-
"""service.chronicle.scrobbler — Background service entry point.

Starts automatically when Kodi launches. Runs the ChronicleMonitor for the
lifetime of the Kodi process, scrobbling playback progress to Chronicle.
"""

import threading

from lib.chronicle_client import ChronicleClient
from lib.logger import Logger
from lib.monitor import ChronicleMonitor

log = Logger('service')

if __name__ == '__main__':
    log.info('Chronicle Scrobbler service starting')
    # Ask Chronicle who this key belongs to: a missing or rejected key raises the connection warning now, not at the next scrobble.
    threading.Thread(target=ChronicleClient().get_current_user, name='chronicle-connection-check', daemon=True).start()
    monitor = ChronicleMonitor()
    monitor.run()
    log.info('Chronicle Scrobbler service stopped')
