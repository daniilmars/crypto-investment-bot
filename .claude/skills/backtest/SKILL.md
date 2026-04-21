---
name: backtest
description: Run backtesting analysis on the crypto-investment-bot. Syncs production data from GCE, then runs exit parameter sweeps, signal quality analysis, MFE/MAE analysis, and news/sentiment intelligence analysis. Use when the user wants to analyze trading performance, optimize exit parameters, evaluate news source quality, or find the best confidence thresholds.
argument-hint: "[sync|sweep|quality|mfe|intelligence|full] [auto|manual] [crypto|stock]"
---

Run backtesting analysis on the crypto-investment-bot's historical trades. This uses only saved DB data — no Gemini API calls.

## SSH Resilience (MANDATORY before sync)

Before the first `gcloud compute ssh` or `gcloud compute scp`, run a 10s-cap probe:

```bash
gcloud compute ssh crypto-bot-eu --zone=europe-west3-a --tunnel-through-iap \
  --ssh-flag="-o ConnectTimeout=10" \
  --ssh-flag="-o ServerAliveInterval=5" \
  --ssh-flag="-o ServerAliveCountMax=2" \
  --command="echo READY"
```

If probe hangs or returns exit 255: clear stale known_hosts and retry ONCE.
```bash
# Fetch IP via gcloud compute instances describe ... --format="value(networkInterfaces[0].accessConfigs[0].natIP)"
ssh-keygen -R compute.<IP> 2>/dev/null; ssh-keygen -R <IP> 2>/dev/null
```
If still failing, stop — do NOT hang on the long scp.

All SSH/SCP calls must use keepalive flags:
`--ssh-flag="-o ConnectTimeout=15" --ssh-flag="-o ServerAliveInterval=10" --ssh-flag="-o ServerAliveCountMax=3"`

## Arguments

Parse `$ARGUMENTS` for these optional flags (in any order):
- **Mode:** `sync`, `sweep`, `quality`, `mfe`, `intelligence`, `full` (default: `full` = all analyses)
- **Strategy filter:** `auto` or `manual` (default: both)
- **Asset filter:** `crypto` or `stock` (default: both)

## Step 1: Sync production data

The production database runs on GCE (`crypto-bot-eu`). Local SQLite only has test data. Before any analysis, check if a recent sync exists:

```bash
ls -la data/backtest.db 2>/dev/null
```

If the file doesn't exist, is older than 24 hours, or the user specified `sync`:

1. Export all required tables via SSH. Write a Python script to `/tmp/export_backtest.py`, base64-encode it, transfer to VM, copy into container, and run from `/app`. This avoids quoting hell with inline Python.

   The script should export these tables:
   ```python
   tables = {
       'trades': "SELECT symbol, order_id, side, entry_price, exit_price, quantity, status, pnl, entry_timestamp, exit_timestamp, exit_reason, dynamic_sl_pct, dynamic_tp_pct, asset_type, trading_strategy, trading_mode FROM trades WHERE status = 'CLOSED'",
       'market_prices': "SELECT symbol, price, timestamp FROM market_prices",
       'signal_attribution': "SELECT * FROM signal_attribution WHERE resolved_at IS NOT NULL",
       'signals_buy': "SELECT symbol, signal_type, reason, price, timestamp FROM signals WHERE signal_type = 'BUY' ORDER BY timestamp DESC LIMIT 5000",
       'signal_decisions': "SELECT symbol, signal_type, asset_type, decision, signal_strength, gemini_confidence, catalyst_freshness, reason, price, decided_at FROM signal_decisions ORDER BY decided_at",
       'scraped_articles': "SELECT symbol, title, source, gemini_score, category, collected_at FROM scraped_articles WHERE gemini_score IS NOT NULL ORDER BY collected_at DESC LIMIT 5000",
   }
   ```

   Use this pattern to transfer and run scripts on the container:
   ```bash
   # Write script locally, base64 encode, transfer via SSH
   SCRIPT_B64=$(cat /tmp/export_backtest.py | base64)
   gcloud compute ssh crypto-bot-eu --zone=europe-west3-a --tunnel-through-iap -- "
   echo '$SCRIPT_B64' | base64 -d > /tmp/export_backtest.py
   sudo docker cp /tmp/export_backtest.py crypto-bot:/app/export_backtest.py
   sudo docker exec -w /app crypto-bot python3 export_backtest.py
   " > /tmp/backtest_export.json 2>/dev/null
   ```

2. The export file may contain log lines before the JSON. Strip them:
   ```bash
   grep '^{' /tmp/backtest_export.json > /tmp/backtest_clean.json
   ```

3. Import into local SQLite (`data/backtest.db`). Handle empty tables gracefully (skip if no rows). Tables with 0 rows should be noted but not cause errors.

4. Report the sync result (row counts per table).

If SSH fails (VM might be stopped — it's a spot instance), warn the user and suggest:
```
gcloud compute instances start crypto-bot-eu --zone=europe-west3-a
```
Then offer to proceed with existing local data if `data/backtest.db` exists.

## Step 2: Run analyses

Set the `DATABASE_URL` environment variable to point at the local backtest DB, then run the appropriate analyses.

### DB Connection Monkey-Patch (use in all analysis scripts)

```python
import os, sqlite3
os.environ.pop('DATABASE_URL', None)
import src.database as db
def _backtest_conn(db_url=None):
    conn = sqlite3.connect('data/backtest.db')
    conn.row_factory = sqlite3.Row
    return conn
db.get_db_connection = _backtest_conn
db.release_db_connection = lambda conn: conn.close() if conn else None
```

### Exit Parameter Sweep (mode: `sweep` or `full`)

```python
from src.analysis.trade_replay import run_exit_sweep, format_sweep_report
sweep = run_exit_sweep(trading_strategy=$STRATEGY_FILTER, asset_type=$ASSET_FILTER)
print(format_sweep_report(sweep))
```

Replace `$STRATEGY_FILTER` and `$ASSET_FILTER` with parsed argument values (Python string literals like `'auto'` or `None`).

### Signal Quality Analysis (mode: `quality` or `full`)

```python
from src.analysis.trade_replay import analyze_signal_quality, format_quality_report
quality = analyze_signal_quality(trading_strategy=$STRATEGY_FILTER)
print(format_quality_report(quality))
```

### MFE/MAE Analysis (mode: `mfe` or `full`)

```python
from src.analysis.trade_replay import analyze_mfe_mae
mfe = analyze_mfe_mae(trading_strategy=$STRATEGY_FILTER, asset_type=$ASSET_FILTER)
print(f"Trades analyzed: {mfe.get('trade_count', 0)}")
for reason, data in mfe.get('by_exit_reason', {}).items():
    print(f"  {reason} ({data['count']} trades):")
    print(f"    Avg MFE: {data['avg_mfe_pct']:+.2f}%  (median: {data['median_mfe_pct']:+.2f}%)")
    print(f"    Avg MAE: {data['avg_mae_pct']:+.2f}%  (median: {data['median_mae_pct']:+.2f}%)")
    print(f"    Was profitable before exit: {data['was_profitable_count']}/{data['count']}")
    print(f"    Avg PnL: ${data['avg_pnl']:.2f}")
```

### News & Sentiment Intelligence Analysis (mode: `intelligence` or `full`)

This is the core intelligence analysis. Run it as a single Python script against backtest.db:

```python
import sqlite3, json
from collections import defaultdict

conn = sqlite3.connect('data/backtest.db')
conn.row_factory = None

# Load data
trades = conn.execute("SELECT symbol, side, entry_price, exit_price, pnl, entry_timestamp, exit_timestamp, exit_reason, asset_type, trading_strategy FROM trades").fetchall()
trade_cols = ['symbol','side','entry_price','exit_price','pnl','entry_timestamp','exit_timestamp','exit_reason','asset_type','trading_strategy']
trades = [dict(zip(trade_cols, r)) for r in trades]

# --- 1. Parse Gemini confidence & freshness from BUY signals ---
try:
    buy_signals = conn.execute("SELECT symbol, signal_type, reason, price, timestamp FROM signals_buy").fetchall()
    sig_cols = ['symbol','signal_type','reason','price','timestamp']
    buy_signals = [dict(zip(sig_cols, r)) for r in buy_signals]
except:
    buy_signals = []

# Match each trade to its originating BUY signal
for t in trades:
    t['gemini_conf'] = None
    t['freshness'] = None
    t['catalyst'] = None
    for s in buy_signals:
        if s['symbol'] == t['symbol'] and s['signal_type'] == 'BUY':
            reason = s['reason'] or ''
            if 'Gemini bullish' in reason:
                try:
                    t['gemini_conf'] = float(reason.split('(')[1].split(',')[0])
                    t['freshness'] = reason.split('freshness=')[1].split(')')[0]
                    t['catalyst'] = reason.split('): ')[1][:80] if '): ' in reason else ''
                    break
                except:
                    pass

# --- 2. Trade outcomes by Gemini confidence ---
print("=== TRADE OUTCOMES BY GEMINI CONFIDENCE ===")
buckets = defaultdict(lambda: {'wins': 0, 'losses': 0, 'pnl': 0})
for t in trades:
    conf = t['gemini_conf']
    bucket = '0.80+' if conf and conf >= 0.8 else '0.70-0.79' if conf and conf >= 0.7 else '0.60-0.69' if conf and conf >= 0.6 else '<0.60' if conf else 'unknown'
    pnl = t['pnl'] or 0
    if pnl > 0: buckets[bucket]['wins'] += 1
    else: buckets[bucket]['losses'] += 1
    buckets[bucket]['pnl'] += pnl

print(f"{'Confidence':<14} {'Trades':>7} {'Wins':>5} {'WR':>6} {'PnL':>10}")
print('-' * 45)
for b in ['0.80+', '0.70-0.79', '0.60-0.69', '<0.60', 'unknown']:
    if b in buckets:
        d = buckets[b]
        total = d['wins'] + d['losses']
        wr = d['wins'] / total * 100 if total else 0
        print(f"{b:<14} {total:>7} {d['wins']:>5} {wr:>5.0f}% ${d['pnl']:>8.2f}")

# --- 3. Trade outcomes by catalyst freshness ---
print(f"\n=== TRADE OUTCOMES BY CATALYST FRESHNESS ===")
fresh_b = defaultdict(lambda: {'wins': 0, 'losses': 0, 'pnl': 0})
for t in trades:
    fresh = t['freshness'] or 'unknown'
    pnl = t['pnl'] or 0
    if pnl > 0: fresh_b[fresh]['wins'] += 1
    else: fresh_b[fresh]['losses'] += 1
    fresh_b[fresh]['pnl'] += pnl

print(f"{'Freshness':<14} {'Trades':>7} {'Wins':>5} {'WR':>6} {'PnL':>10}")
print('-' * 45)
for f in sorted(fresh_b.keys()):
    d = fresh_b[f]
    total = d['wins'] + d['losses']
    wr = d['wins'] / total * 100 if total else 0
    print(f"{f:<14} {total:>7} {d['wins']:>5} {wr:>5.0f}% ${d['pnl']:>8.2f}")

# --- 4. Signal decisions (confirmed vs expired/rejected) ---
print(f"\n=== SIGNAL DECISIONS ===")
try:
    decisions = conn.execute("SELECT decision, signal_type, gemini_confidence FROM signal_decisions").fetchall()
    dec_stats = defaultdict(lambda: {'count': 0, 'by_conf': defaultdict(int)})
    for dec, sig_type, conf in decisions:
        dec_stats[dec or 'unknown']['count'] += 1
        if conf is not None:
            bucket = '0.80+' if conf >= 0.8 else '0.70-0.79' if conf >= 0.7 else '0.60-0.69' if conf >= 0.6 else '<0.60'
            dec_stats[dec or 'unknown']['by_conf'][bucket] += 1
    for dec, stats in sorted(dec_stats.items()):
        print(f"  {dec}: {stats['count']} signals")
        for cb, cnt in sorted(stats['by_conf'].items()):
            print(f"    {cb}: {cnt}")
except Exception as e:
    print(f"  (signal_decisions table not available: {e})")

# --- 5. Article scoring by news source ---
print(f"\n=== ARTICLE SCORING BY SOURCE (top 15) ===")
try:
    articles = conn.execute("SELECT source, gemini_score FROM scraped_articles WHERE gemini_score IS NOT NULL").fetchall()
    src_stats = defaultdict(lambda: {'count': 0, 'total': 0, 'bullish': 0, 'bearish': 0})
    for src, score in articles:
        s = src_stats[src or 'unknown']
        s['count'] += 1
        s['total'] += score
        if score > 0.1: s['bullish'] += 1
        elif score < -0.1: s['bearish'] += 1
    print(f"{'Source':<30} {'Articles':>8} {'Avg Score':>10} {'Bull%':>6} {'Bear%':>6}")
    print('-' * 65)
    for src, s in sorted(src_stats.items(), key=lambda x: -x[1]['count'])[:15]:
        avg = s['total'] / s['count'] if s['count'] else 0
        bull = s['bullish'] / s['count'] * 100 if s['count'] else 0
        bear = s['bearish'] / s['count'] * 100 if s['count'] else 0
        print(f"{src[:30]:<30} {s['count']:>8} {avg:>+10.3f} {bull:>5.0f}% {bear:>5.0f}%")
except Exception as e:
    print(f"  (scraped_articles table not available: {e})")

# --- 6. Trade details with catalyst info ---
print(f"\n=== TRADE DETAILS (sorted by PnL) ===")
print(f"{'Symbol':<8} {'Strategy':<10} {'PnL':>8} {'Exit':>22} {'Conf':>5} {'Fresh':>10} {'Catalyst':<50}")
print('-' * 120)
for t in sorted(trades, key=lambda x: -(x['pnl'] or 0)):
    pnl = t['pnl'] or 0
    marker = 'W' if pnl > 0 else 'L'
    print(f"{marker} {t['symbol']:<6} {(t['trading_strategy'] or '?'):<10} ${pnl:>7.2f} {(t['exit_reason'] or 'unknown'):>22} {(t['gemini_conf'] or 0):>5.2f} {(t['freshness'] or '?'):>10} {(t['catalyst'] or '')[:50]}")

conn.close()
```

## Step 3: Present results

Summarize findings in this structure:

**Data:** X trades, Y price points, Z articles scored, W signal decisions (synced at [time])

**Exit Sweep:**
- Current performance: PnL, win rate, profit factor
- Best parameter set found: SL/TP/trailing values
- Improvement vs current: +$X (+Y%)
- Top 3 parameter sets

**Signal Quality:**
- Best catalyst types (by win rate and total PnL)
- Confidence threshold analysis — current vs optimal
- Recommendation: adjust threshold to X.XX?

**News Intelligence:**
- Trade outcomes by Gemini confidence bucket (which confidence levels produce winners?)
- Catalyst freshness analysis (does "breaking" or "recent" news lead to better trades?)
- Signal decision funnel (how many signals are approved vs expired vs rejected?)
- Top news sources by article volume, average sentiment score, and bullish/bearish ratio
- Individual trade details showing the catalyst that triggered each trade

**MFE/MAE Insights:**
- How many SL exits were profitable at some point before being stopped out
- Average profit left on the table (MFE at exit vs MFE peak)
- Actionable: "X% of stop-loss exits had reached +Y% profit — consider widening trailing stop"

**Recommendations:**
Based on ALL the data (exit params, intelligence, and MFE/MAE), list 2-3 concrete changes with expected impact:
- Parameter changes (SL/TP/trailing) supported by sweep data
- Confidence threshold adjustments supported by intelligence data
- News source or freshness weighting changes supported by article analysis
- Be conservative — only recommend changes supported by sufficient sample size (>20 trades)
- Note if sample size is too small and suggest continuing to collect data
