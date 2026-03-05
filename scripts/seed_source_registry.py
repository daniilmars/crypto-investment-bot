#!/usr/bin/env python3
"""One-time migration: seed the source_registry table from hardcoded feeds.

Usage:
    .venv/bin/python scripts/seed_source_registry.py
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.database import initialize_database
from src.collectors.source_registry import seed_registry, get_source_count


def main():
    print("Initializing database (creates tables if needed)...")
    initialize_database()

    print("Seeding source registry from hardcoded feeds and scrapers...")
    inserted = seed_registry()

    total = get_source_count()
    print(f"Done. Inserted {inserted} new sources. Total active: {total}")


if __name__ == '__main__':
    main()
