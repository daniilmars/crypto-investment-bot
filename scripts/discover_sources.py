#!/usr/bin/env python3
"""Run a source discovery cycle. Can be run weekly via cron or manually.

Usage:
    .venv/bin/python scripts/discover_sources.py
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.database import initialize_database
from src.collectors.source_discovery import run_discovery_cycle


def main():
    print("Initializing database...")
    initialize_database()

    print("Running source discovery cycle...")
    summary = run_discovery_cycle()

    if summary.get('skipped'):
        print(f"Skipped: {summary.get('reason')}")
        return

    print(f"Discovery complete:")
    print(f"  Discovered: {summary.get('discovered', 0)}")
    print(f"  Evaluated:  {summary.get('evaluated', 0)}")
    print(f"  Added:      {summary.get('added', 0)}")

    if summary.get('methods'):
        for method, count in summary['methods'].items():
            print(f"  {method}: {count} candidates")


if __name__ == '__main__':
    main()
