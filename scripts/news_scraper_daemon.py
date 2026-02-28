#!/usr/bin/env python3
"""Entry point for the continuous news scraper daemon.

Reads config from settings.yaml, creates a ScraperDaemon,
and blocks until SIGTERM/SIGINT triggers graceful shutdown.

Usage:
    .venv/bin/python scripts/news_scraper_daemon.py
"""

import os
import signal
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import app_config
from src.collectors.scraper_daemon import ScraperDaemon
from src.logger import log


def main():
    config = app_config.get('settings', {}).get('news_scraper_daemon', {})
    log.info(f"Daemon config: {config}")

    output_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'data', 'scraped-news.json'
    )

    daemon = ScraperDaemon(output_path=output_path, config=config)

    def handle_signal(signum, frame):
        sig_name = signal.Signals(signum).name
        log.info(f"Received {sig_name}, shutting down...")
        daemon.stop()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    daemon.start()


if __name__ == '__main__':
    main()
