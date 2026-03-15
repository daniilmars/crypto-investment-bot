---
name: backtest
description: Run backtesting analysis on the crypto-investment-bot. Syncs production data from GCE, then runs exit parameter sweeps, signal quality analysis, and MFE/MAE analysis. Use when the user wants to analyze trading performance, optimize exit parameters, or find the best confidence thresholds.
argument-hint: "[sync|sweep|quality|mfe|full] [auto|manual] [crypto|stock]"
---

Run backtesting analysis on the crypto-investment-bot's historical trades. This uses only saved DB data — no Gemini API calls.

## Arguments

Parse `$ARGUMENTS` for these optional flags (in any order):
- **Mode:** `sync`, `sweep`, `quality`, `mfe`, `full` (default: `full` = all analyses)
- **Strategy filter:** `auto` or `manual` (default: both)
- **Asset filter:** `crypto` or `stock` (default: both)

## Step 1: Sync production data

The production database runs on GCE (`crypto-bot-eu`). Local SQLite only has test data. Before any analysis, check if a recent sync exists:

```bash
ls -la data/backtest.db 2>/dev/null
```

If the file doesn't exist, is older than 24 hours, or the user specified `sync`:

1. Export the 3 required tables from the deployed PostgreSQL via SSH. The quoting is tricky — use escaped single quotes inside double-quoted Python strings:
   ```bash
   gcloud compute ssh crypto-bot-eu --zone=europe-west3-a --tunnel-through-iap --ssh-flag="-o ServerAliveInterval=30" -- "sudo docker exec crypto-bot python -c \"
   import json
   from src.database import get_db_connection, _cursor
   import psycopg2
   conn = get_db_connection()
   tables = {
       'trades': \\\"SELECT symbol, order_id, side, entry_price, exit_price, quantity, status, pnl, entry_timestamp, exit_timestamp, exit_reason, dynamic_sl_pct, dynamic_tp_pct, asset_type, trading_strategy, trading_mode FROM trades WHERE status = 'CLOSED'\\\",
       'market_prices': \\\"SELECT symbol, price, timestamp FROM market_prices\\\",
       'signal_attribution': \\\"SELECT * FROM signal_attribution WHERE resolved_at IS NOT NULL\\\",
   }
   result = {}
   for name, query in tables.items():
       cur = conn.cursor()
       cur.execute(query)
       cols = [d[0] for d in cur.description]
       rows = [dict(zip(cols, row)) for row in cur.fetchall()]
       result[name] = {'columns': cols, 'rows': rows}
       cur.close()
   conn.close()
   print(json.dumps(result, default=str))
   \"" > /tmp/backtest_export.json 2>/tmp/backtest_ssh_err.txt
   ```

2. The export file may contain log lines before the JSON. Strip them before importing:
   ```bash
   # Extract only the JSON line (starts with '{')
   grep '^{' /tmp/backtest_export.json > /tmp/backtest_clean.json
   ```

3. Import into local SQLite:
   ```bash
   .venv/bin/python -c "
   import json, sqlite3, os
   with open('/tmp/backtest_clean.json') as f:
       data = json.load(f)
   os.makedirs('data', exist_ok=True)
   conn = sqlite3.connect('data/backtest.db')
   for table_name, table_data in data.items():
       cols = table_data['columns']
       rows = table_data['rows']
       if not rows:
           continue
       placeholders = ', '.join(['?'] * len(cols))
       col_names = ', '.join(cols)
       conn.execute(f'DROP TABLE IF EXISTS {table_name}')
       # Infer types
       type_map = {}
       for col in cols:
           sample = rows[0].get(col)
           if isinstance(sample, (int, bool)):
               type_map[col] = 'INTEGER'
           elif isinstance(sample, float):
               type_map[col] = 'REAL'
           else:
               type_map[col] = 'TEXT'
       col_defs = ', '.join(f'{c} {type_map[c]}' for c in cols)
       conn.execute(f'CREATE TABLE {table_name} ({col_defs})')
       for row in rows:
           values = [row.get(c) for c in cols]
           conn.execute(f'INSERT INTO {table_name} ({col_names}) VALUES ({placeholders})', values)
   conn.commit()
   trades_count = conn.execute('SELECT COUNT(*) FROM trades').fetchone()[0]
   prices_count = conn.execute('SELECT COUNT(*) FROM market_prices').fetchone()[0]
   attr_count = conn.execute('SELECT COUNT(*) FROM signal_attribution').fetchone()[0]
   conn.close()
   print(f'Synced: {trades_count} trades, {prices_count} prices, {attr_count} attributions')
   "
   ```

4. Report the sync result (row counts per table).

If SSH fails (VM might be stopped — it's a spot instance), warn the user and suggest:
```
gcloud compute instances start crypto-bot-eu --zone=europe-west3-a
```
Then offer to proceed with existing local data if `data/backtest.db` exists.

## Step 2: Run analyses

Set the `DATABASE_URL` environment variable to point at the local backtest DB, then run the appropriate analyses.

### Exit Parameter Sweep (mode: `sweep` or `full`)

```bash
DATABASE_URL="" .venv/bin/python -c "
import os, sqlite3
os.environ.pop('DATABASE_URL', None)

# Monkey-patch get_db_connection to use backtest.db
import src.database as db
_original_get_db = db.get_db_connection
def _backtest_conn(db_url=None):
    conn = sqlite3.connect('data/backtest.db')
    conn.row_factory = sqlite3.Row
    return conn
db.get_db_connection = _backtest_conn
db.release_db_connection = lambda conn: conn.close() if conn else None

from src.analysis.trade_replay import run_exit_sweep, format_sweep_report
sweep = run_exit_sweep(
    trading_strategy=$STRATEGY_FILTER,
    asset_type=$ASSET_FILTER,
)
print(format_sweep_report(sweep))
"
```

Replace `$STRATEGY_FILTER` and `$ASSET_FILTER` with the parsed argument values (as Python string literals like `'auto'` or `None`).

### Signal Quality Analysis (mode: `quality` or `full`)

```bash
DATABASE_URL="" .venv/bin/python -c "
import os, sqlite3
os.environ.pop('DATABASE_URL', None)
import src.database as db
_original_get_db = db.get_db_connection
def _backtest_conn(db_url=None):
    conn = sqlite3.connect('data/backtest.db')
    conn.row_factory = sqlite3.Row
    return conn
db.get_db_connection = _backtest_conn
db.release_db_connection = lambda conn: conn.close() if conn else None

from src.analysis.trade_replay import analyze_signal_quality, format_quality_report
quality = analyze_signal_quality(trading_strategy=$STRATEGY_FILTER)
print(format_quality_report(quality))
"
```

### MFE/MAE Analysis (mode: `mfe` or `full`)

```bash
DATABASE_URL="" .venv/bin/python -c "
import os, sqlite3
os.environ.pop('DATABASE_URL', None)
import src.database as db
def _backtest_conn(db_url=None):
    conn = sqlite3.connect('data/backtest.db')
    conn.row_factory = sqlite3.Row
    return conn
db.get_db_connection = _backtest_conn
db.release_db_connection = lambda conn: conn.close() if conn else None

from src.analysis.trade_replay import analyze_mfe_mae
mfe = analyze_mfe_mae(trading_strategy=$STRATEGY_FILTER, asset_type=$ASSET_FILTER)
print(f\"Trades analyzed: {mfe.get('trade_count', 0)}\")
for reason, data in mfe.get('by_exit_reason', {}).items():
    print(f\"  {reason} ({data['count']} trades):\")
    print(f\"    Avg MFE: {data['avg_mfe_pct']:+.2f}%  (median: {data['median_mfe_pct']:+.2f}%)\")
    print(f\"    Avg MAE: {data['avg_mae_pct']:+.2f}%  (median: {data['median_mae_pct']:+.2f}%)\")
    print(f\"    Was profitable before exit: {data['was_profitable_count']}/{data['count']}\")
    print(f\"    Avg PnL: \${data['avg_pnl']:.2f}\")
"
```

## Step 3: Present results

Summarize findings in this structure:

**Data:** X trades, Y price points, Z attributions (synced at [time])

**Exit Sweep:**
- Current performance: PnL, win rate, profit factor
- Best parameter set found: SL/TP/trailing values
- Improvement vs current: +$X (+Y%)
- Top 3 parameter sets

**Signal Quality:**
- Best catalyst types (by win rate and total PnL)
- Confidence threshold analysis — current vs optimal
- Recommendation: adjust threshold to X.XX?

**MFE/MAE Insights:**
- How many SL exits were profitable at some point before being stopped out
- Average profit left on the table (MFE at exit vs MFE peak)
- Actionable: "X% of stop-loss exits had reached +Y% profit — consider widening trailing stop"

**Recommendations:**
Based on the data, list 2-3 concrete parameter changes with expected impact. Be conservative — only recommend changes supported by sufficient sample size (>20 trades).
