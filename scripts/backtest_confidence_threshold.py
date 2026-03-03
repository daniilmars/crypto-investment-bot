#!/usr/bin/env python3
"""Backtest Gemini confidence threshold against historical signals.

Queries the signals table and scraped_articles (gemini_score) to estimate
how many more/fewer signals would be generated at different thresholds.

Usage:
    .venv/bin/python scripts/backtest_confidence_threshold.py [--days 14]
"""

import argparse
import os
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def get_db_path():
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        'data', 'crypto_bot.db')


def analyze_signal_reasons(db_path, days):
    """Parse confidence values from signal reasons."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    # Get all signals with their reasons
    cursor.execute(
        "SELECT symbol, signal_type, reason, price, timestamp "
        "FROM signals WHERE timestamp > ? ORDER BY timestamp",
        (cutoff,)
    )
    signals = cursor.fetchall()

    # Parse confidence from reason strings
    confidence_pattern = re.compile(r'confidence[:\s]+(\d+\.?\d*)', re.IGNORECASE)
    gemini_pattern = re.compile(r'Gemini\s+(bullish|bearish|neutral)\s*\(confidence\s+(\d+\.?\d*)\)', re.IGNORECASE)

    thresholds = [0.3, 0.4, 0.5, 0.6, 0.7, 0.8]
    threshold_counts = {t: {'BUY': 0, 'SELL': 0, 'HOLD': 0} for t in thresholds}

    total_signals = len(signals)
    buy_count = sum(1 for s in signals if s['signal_type'] == 'BUY')
    sell_count = sum(1 for s in signals if s['signal_type'] == 'SELL')
    hold_count = sum(1 for s in signals if s['signal_type'] == 'HOLD')

    parsed_confidences = []

    for sig in signals:
        reason = sig['reason'] or ''
        match = gemini_pattern.search(reason)
        if not match:
            match = confidence_pattern.search(reason)

        if match:
            if hasattr(match, 'group') and match.lastindex and match.lastindex >= 2:
                conf = float(match.group(2))
                direction = match.group(1).lower()
            else:
                conf = float(match.group(1))
                direction = None

            parsed_confidences.append({
                'symbol': sig['symbol'],
                'signal_type': sig['signal_type'],
                'confidence': conf,
                'direction': direction,
                'timestamp': sig['timestamp'],
            })

    conn.close()
    return {
        'total': total_signals,
        'buy': buy_count,
        'sell': sell_count,
        'hold': hold_count,
        'parsed': parsed_confidences,
        'days': days,
    }


def analyze_gemini_scores(db_path, days):
    """Analyze gemini_score distribution from scraped articles."""
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    cursor.execute(
        "SELECT symbol, gemini_score FROM scraped_articles "
        "WHERE gemini_score IS NOT NULL AND collected_at > ?",
        (cutoff,)
    )
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        return None

    scores = [r[1] for r in rows]
    symbol_scores = defaultdict(list)
    for sym, score in rows:
        if sym:
            symbol_scores[sym].append(score)

    thresholds = [0.3, 0.4, 0.5, 0.6, 0.7, 0.8]
    above_counts = {t: sum(1 for s in scores if abs(s) >= t) for t in thresholds}

    return {
        'total_articles': len(rows),
        'avg_score': sum(scores) / len(scores),
        'median_score': sorted(scores)[len(scores) // 2],
        'max_score': max(scores),
        'min_score': min(scores),
        'above_threshold': above_counts,
        'by_symbol_count': {s: len(v) for s, v in sorted(symbol_scores.items(),
                            key=lambda x: -len(x[1]))[:15]},
    }


def main():
    parser = argparse.ArgumentParser(description='Backtest confidence thresholds')
    parser.add_argument('--days', type=int, default=14, help='Days to look back')
    args = parser.parse_args()

    db_path = get_db_path()
    if not os.path.exists(db_path):
        print(f"Database not found at {db_path}")
        print("Run this script from the project root or copy the DB locally.")
        sys.exit(1)

    print(f"=== Confidence Threshold Backtest ({args.days} days) ===\n")

    # 1. Signal distribution
    sig_data = analyze_signal_reasons(db_path, args.days)
    print(f"Signals in last {args.days} days: {sig_data['total']}")
    print(f"  BUY:  {sig_data['buy']}")
    print(f"  SELL: {sig_data['sell']}")
    print(f"  HOLD: {sig_data['hold']}")
    print()

    # 2. Parsed confidence values
    parsed = sig_data['parsed']
    if parsed:
        print(f"Signals with parseable Gemini confidence: {len(parsed)}")
        confs = [p['confidence'] for p in parsed]
        print(f"  Avg confidence: {sum(confs)/len(confs):.3f}")
        print(f"  Median:         {sorted(confs)[len(confs)//2]:.3f}")
        print(f"  Range:          {min(confs):.3f} - {max(confs):.3f}")
        print()

        print("Signals that would pass at each threshold:")
        thresholds = [0.3, 0.4, 0.5, 0.6, 0.7, 0.8]
        for t in thresholds:
            passing = [p for p in parsed if p['confidence'] >= t]
            marker = " <-- current" if t == 0.5 else (" <-- old" if t == 0.7 else "")
            print(f"  >= {t:.1f}: {len(passing):3d} signals"
                  f" ({len(passing)/len(parsed)*100:5.1f}%){marker}")
        print()
    else:
        print("No signals with parseable confidence found in reasons.\n")

    # 3. Article gemini scores
    article_data = analyze_gemini_scores(db_path, args.days)
    if article_data:
        print(f"Gemini-scored articles: {article_data['total_articles']}")
        print(f"  Avg score:  {article_data['avg_score']:+.3f}")
        print(f"  Median:     {article_data['median_score']:+.3f}")
        print(f"  Range:      {article_data['min_score']:+.3f} to {article_data['max_score']:+.3f}")
        print()
        print("Articles with |score| above threshold:")
        for t, count in article_data['above_threshold'].items():
            pct = count / article_data['total_articles'] * 100
            print(f"  >= {t:.1f}: {count:4d} ({pct:5.1f}%)")
        print()
        print("Top symbols by article count:")
        for sym, count in list(article_data['by_symbol_count'].items())[:10]:
            print(f"  {sym:8s}: {count}")
    else:
        print("No Gemini-scored articles found.\n")


if __name__ == '__main__':
    main()
