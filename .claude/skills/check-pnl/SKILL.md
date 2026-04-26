---
name: check-pnl
description: Check PnL and trading performance of the deployed crypto-investment-bot. Shows per-strategy performance, open positions with unrealized PnL, and recent trades. Use for quick performance snapshots.
argument-hint: "[detailed]"
---

Check the PnL of the deployed crypto-investment-bot on GCE.

## Arguments

Parse `$ARGUMENTS`:
- `detailed` — include open positions with current prices and unrealized PnL

## SSH Resilience Pattern

**IAP SSH tunnels hang indefinitely when stuck.** Follow this ordered fail-fast
pattern on EVERY skill invocation. Never issue a long-running SSH command
without first proving the tunnel is alive.

### Step A — Pre-flight VM status (2-3s, no SSH)

```bash
gcloud compute instances describe crypto-bot-eu --zone=europe-west3-a \
  --format="value(status,networkInterfaces[0].accessConfigs[0].natIP)"
```

If `status` ≠ `RUNNING` → stop immediately, report status, offer `gcloud compute instances start` command. Do NOT proceed to SSH.

Note the IP — if it changed since last invocation, `~/.ssh/google_compute_known_hosts` is now stale (common cause of silent hangs).

### Step B — Lightweight SSH probe (10s cap)

```bash
gcloud compute ssh crypto-bot-eu --zone=europe-west3-a --tunnel-through-iap \
  --ssh-flag="-o ConnectTimeout=10" \
  --ssh-flag="-o ServerAliveInterval=5" \
  --ssh-flag="-o ServerAliveCountMax=2" \
  --command="echo READY"
```

Expected: `READY` within ~10s. If it hangs or returns exit code 255, the tunnel is broken.

### Step C — On probe failure: one recovery retry

Clear stale known-hosts for the VM's current IP (auto-rotates on preemption), then re-probe once:

```bash
# Pull the IP from Step A, then:
ssh-keygen -R compute.<IP> 2>/dev/null
ssh-keygen -R <IP> 2>/dev/null
# Re-run probe from Step B
```

The `ssh-keygen -R` calls are safe — known_hosts regenerates automatically on next successful connection.

### Step D — If probe STILL fails: surface diagnostics, do not hang

Output to the user:
```
IAP tunnel unreachable after retry. Possible causes:
  • Transient GCP/IAP issue (usually clears in 2-3 min — try again)
  • Local network / VPN interfering
  • Stale gcloud auth → run: gcloud auth login

Bot may still be healthy — GitHub Actions health check is authoritative:
  gh run list --limit 3 --workflow="Health Check"
```

Do NOT retry a third time automatically. Let the user decide.

### Step E — Full command with keepalives (60s hard ceiling)

Only after probe succeeds, run the real script with SSH keepalive flags:

```bash
gcloud compute ssh crypto-bot-eu --zone=europe-west3-a --tunnel-through-iap \
  --ssh-flag="-o ConnectTimeout=15" \
  --ssh-flag="-o ServerAliveInterval=10" \
  --ssh-flag="-o ServerAliveCountMax=3" \
  --command="<script>"
```

Worst-case hang: 15s connect + 30s keepalive stall = ~45s, not infinite.

## Steps

1. **Run SSH Resilience Steps A-B-(C)-E** above. If probe fails, stop at Step D — do NOT attempt the full script.

2. Write a Python script to `/tmp/check_pnl.py` that queries the production database. Base64-encode it, transfer via SSH (using Step E flags), copy into the container, and run from `/app`.

2. The script should query:

### Basic PnL (always shown)

For each strategy (`manual`, `auto`, `momentum`, `conservative`):
- Closed trades: count, wins, losses, win rate, total PnL
- Open positions: count, total locked capital
- Last 5 closed trades: symbol, PnL, exit_reason, timestamp

All closed-trade queries MUST include `AND COALESCE(excluded_from_stats, 0) = 0`
to match the wallet's filtered semantics. Excluded trades are still in the DB
(e.g. HII #134 + LHX #133 marked `pre_PR_C_sector_vibes_bug`) but don't count
toward stats / Kelly / Mini App PnL. Without this filter, /check-pnl numbers
will silently disagree with the wallet, Mini App, and `_get_paper_balance`.

```sql
-- Closed trades
SELECT COUNT(*), SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END), SUM(pnl)
FROM trades WHERE status = 'CLOSED'
  AND COALESCE(excluded_from_stats, 0) = 0
  AND trading_strategy = ?

-- Open positions (no exclusion filter needed — only relevant when CLOSED)
SELECT COUNT(*), COALESCE(SUM(entry_price * quantity), 0)
FROM trades WHERE status = 'OPEN' AND trading_strategy = ?

-- Last 5 closed
SELECT symbol, pnl, exit_reason, exit_timestamp
FROM trades WHERE status = 'CLOSED'
  AND COALESCE(excluded_from_stats, 0) = 0
  AND trading_strategy = ?
ORDER BY exit_timestamp DESC LIMIT 5
```

### Detailed (when `detailed` argument provided)

For each open position, fetch current price and compute unrealized PnL:
- Use `get_current_price(f"{symbol}USDT")` from `src.collectors.binance_data` for crypto (the endpoint expects Binance trading pairs, not bare base symbols)
- Use latest price from `market_prices` table for stocks
- Show: symbol, entry_price, current_price, PnL%, PnL$, age

```sql
SELECT symbol, entry_price, quantity, trading_strategy, asset_type, entry_timestamp
FROM trades WHERE status = 'OPEN' ORDER BY trading_strategy, entry_timestamp
```

### Today's Activity

```sql
-- Trades closed today
SELECT symbol, pnl, exit_reason, trading_strategy, exit_timestamp
FROM trades WHERE status = 'CLOSED'
  AND COALESCE(excluded_from_stats, 0) = 0
  AND exit_timestamp >= date('now')
ORDER BY exit_timestamp

-- Trades opened today
SELECT symbol, entry_price, quantity, trading_strategy, entry_timestamp
FROM trades WHERE status = 'OPEN' AND entry_timestamp >= date('now')
ORDER BY entry_timestamp
```

## FX normalization (matches the Mini App)

All totals are **USD-converted** via `src.analysis.fx.to_usd`. Foreign-currency
trades (`.L`, `.CO`, `.HK`, `.MC`, `.HE`, `.T`, …) have their raw `pnl` field
in *local currency* — summing them as USD requires conversion.

The script imports the helper at run-time:

```python
import sys
sys.path.insert(0, '/app')
from src.analysis.fx import to_usd, currency_for_symbol

# Per trade:
ccy = currency_for_symbol(symbol)
pnl_usd = to_usd(float(pnl or 0), ccy)
```

The script also prints:
- A **`Mini App active`** sub-total (auto + conservative + longterm only) that
  matches the Mini App's headline `realized.all` figure.
- A single **raw mixed-ccy** total line at the bottom for cross-check.

## Summary Format

```
PnL SUMMARY — [DATE]  (USD-normalized)
═══════════════════════════════════════════════════════════════

Strategy       Closed   W/L       WR      PnL (USD)    Open    Locked (USD)
──────────────────────────────────────────────────────────────────────
MANUAL         XX       XW/XL    XX%    +$XX.XX        X       $X,XXX
AUTO           XX       XW/XL    XX%    +$XX.XX        X       $X,XXX
MOMENTUM       XX       XW/XL    XX%    +$XX.XX        X       $X,XXX  [retired]
CONSERVATIVE   XX       XW/XL    XX%    +$XX.XX        X       $X,XXX
LONGTERM       XX       XW/XL    XX%    +$XX.XX        X       $X,XXX
──────────────────────────────────────────────────────────────────────
TOTAL          XX       XW/XL    XX%   +$XXX.XX        X       $X,XXX
Mini App view  (auto + conservative + longterm)  +$XXX.XX      ← matches app

Raw mixed-ccy total (cross-check only): +$XXX.XX
FX adjustment:                          −$XX.XX

TODAY'S ACTIVITY (USD):
  Closed: X trades, PnL +$XX.XX
  Opened: X new positions

RECENT WINS/LOSSES:
  [last 5 trades per strategy, showing USD PnL]
```

If `detailed`, add after the summary:

```
OPEN POSITIONS (unrealized PnL, USD-normalized)
════════════════════════════════

STRATEGY_NAME (X open)
  Symbol     Entry      Current    PnL%    PnL local    PnL (USD)    Age
  XXX        $XX.XX     $XX.XX    +X.X%   +£X.XX       +$X.XX       Xd
  ...
  SUBTOTAL: XW/XL  unrealized=$+XX.XX
```

## Important Notes

- The Docker container name is `crypto-bot` (with hyphen)
- Use base64 encoding to transfer scripts (avoids quoting issues)
- Copy script into container then run from `/app` working directory
- For crypto prices, call `get_current_price(f"{symbol}USDT")` from `src.collectors.binance_data`; the Binance endpoint rejects bare base symbols
- For stock prices, query latest from `market_prices` table
- If SSH fails, check if VM needs restart (memory pressure issue on e2-micro)
- **FX rates** live in the `fx_rates` table (refreshed every 6h by `fx_refresh_loop` in main.py). If that table is empty, `to_usd` falls back to the hard-coded map in `src.analysis.fx._FALLBACK_USD_PER_UNIT`.
- The Mini App's `_KNOWN_STRATEGIES = ("auto", "conservative", "longterm")` — `manual` and `momentum` are excluded from the "Mini App view" total but still shown per-strategy for transparency.
