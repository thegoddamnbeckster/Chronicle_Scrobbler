# -*- coding: utf-8 -*-
"""service.chronicle.scrobbler — Background service entry point.

Starts automatically when Kodi launches. Runs the ChronicleMonitor for the
lifetime of the Kodi process, scrobbling playback progress to Chronicle.
"""

from lib.logger import Logger
from lib.monitor import ChronicleMonitor

log = Logger('service')

if __name__ == '__main__':
    log.info('Chronicle Scrobbler service starting')
    monitor = ChronicleMonitor()
    monitor.run()
    log.info('Chronicle Scrobbler service stopped')
