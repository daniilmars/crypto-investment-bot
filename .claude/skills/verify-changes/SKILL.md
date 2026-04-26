---
name: verify-changes
description: Verify that recently shipped changes are behaving as designed. 10 specific checks covering attribution, time_stop, exit_reasoning, rotation, calibration, gate logging, auto activity, and cross-strategy concentration. Run 2-3 days after every PR cluster. Lighter than /assess-quality (no scoring), tighter than /check-news (no 9-layer rubric).
argument-hint: "[save]"
---

Audit whether the last 7 days of shipped changes are still working. Each check
returns PASS / WARN / FAIL with a one-line evidence string. Output is a table
suitable for a 2-min skim. Saves to `memory/verify-changes-history.md` when
`save` is passed so trajectory is visible run-over-run.

## When to use

- 2-3 days after shipping a PR cluster
- When `/check-pnl` or `/check-bot` shows something unexpected and you want
  to know which recent change is responsible
- Before doing `/assess-quality` (cheaper warm-up that surfaces the
  measurable regressions before the scoring exercise)

## Arguments

Parse `$ARGUMENTS`:
- `save` — append the run to `memory/verify-changes-history.md` and update
  MEMORY.md index. Includes timestamp + each check's PASS/WARN/FAIL.

## SSH Resilience (MANDATORY)

Before any SSH calls, probe the tunnel (10s cap):
```bash
gcloud compute ssh crypto-bot-eu --zone=europe-west3-a --tunnel-through-iap \
  --ssh-flag="-o ConnectTimeout=10" \
  --ssh-flag="-o ServerAliveInterval=5" \
  --ssh-flag="-o ServerAliveCountMax=2" \
  --command="echo READY"
```
On hang / exit 255: clear stale known_hosts, re-probe ONCE, then stop with
diagnostic if still failing.
```bash
ssh-keygen -R compute.<IP> 2>/dev/null; ssh-keygen -R <IP> 2>/dev/null
```
All SSH/SCP calls below must include keepalives:
`--ssh-flag="-o ConnectTimeout=15" --ssh-flag="-o ServerAliveInterval=10" --ssh-flag="-o ServerAliveCountMax=3"`

## What gets checked (10 items)

| # | Check | PASS | WARN | FAIL |
|---|---|---|---|---|
| 1 | source_additions | ≥10 of 14 new sources producing ≥1 article/7d | 6-9 producing | <6 |
| 2 | time_stop | ≥1 fire AND 0 winners-in-disguise (≥+5% post-close) | fires happening with 1 winner-in-disguise | ≥2 winners-in-disguise OR no fires for 7d |
| 3 | exit_reasoning | ≥95% of new closes (7d) have exit_reasoning | 70-95% | <70% |
| 4 | rotation_attribution | 100% of rotation entries (7d) have SL/TP AND attribution | partial | rotation entries with NULL SL/TP |
| 5 | attribution_trajectory | ≥5 daily snapshots in last 7d in `attribution_coverage_history` | 1-4 | 0 (loop dead) |
| 6 | calibration_loop | latest `gemini_calibration` snapshot < 30h old | 30-72h | >72h (loop dead) |
| 7 | gate_reject_logging | ≥100 `[GATE_REJECT]` lines / 24h | 10-100 | <10 (instrumentation broken) |
| 8 | auto_activity | ≥3 auto trades opened in 7d | 1-2 (slow) | 0 (dormant) |
| 9 | cross_strategy_concentration | 0 symbols in ≥2 strategies | 1-2 symbols doubled | ≥3 doubled OR any tripled |
| 10 | signal_decisions_writer | wrote within last 7d | last 7-14d | dead >14d |

## Steps

### Step 1 — SSH probe (Resilience pattern above)

### Step 2 — Build the audit script

Write `/tmp/verify_changes.py` with the script in the **Audit Script**
section below. Base64-encode it; SCP via SSH; copy into container; run
inside container with `python3 -w /app /tmp/verify_changes.py`.

### Step 3 — Parse the script's stdout (already structured) and render

The script emits one line per check in this format:
```
CHECK_ID|PASS|evidence string
CHECK_ID|WARN|evidence string
CHECK_ID|FAIL|evidence string
```

Render as a table (see Summary Format).

### Step 4 — If `save` argument: append to memory

Update `~/.claude/projects/-Users-daniil-Projects-crypto-investment-bot/memory/verify-changes-history.md`:
- If file doesn't exist, create with frontmatter (template in Save Format).
- Append a new dated section to the top of the body — keep last 10 runs.
- Update MEMORY.md index entry: `verify-changes-history.md — last run YYYY-MM-DD: P/W/F: X/Y/Z`.

## Audit Script

The script (write to `/tmp/verify_changes.py` and SCP+exec inside container):

```python
"""verify-changes audit script — 10 checks. Outputs structured PASS/WARN/FAIL."""
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, '/app')

DB = '/app/data/crypto_data.db'
NEW_SOURCES = [
    'Defense News', 'Breaking Defense', 'Power Magazine', 'Utility Dive',
    'Splash247', 'The Defiant',
    'Handelsblatt Finanzen', 'Dagens Industri',
    'Investing.com France', 'Investing.com Italy',
    'La Repubblica Economia', 'Investing.com Netherlands', 'NRC',
    'DigiTimes Asia',
]

c = sqlite3.connect(DB)
c.row_factory = sqlite3.Row

def out(check_id, level, evidence):
    print(f"{check_id}|{level}|{evidence}")

# 1. source_additions — count sources producing ≥1 article/7d.
#    Stored under feed-self-reported title, so look up via source_registry.
producing = 0
for name in NEW_SOURCES:
    r = c.execute(
        "SELECT last_article_at FROM source_registry WHERE source_name=?",
        (name,)).fetchone()
    if r and r['last_article_at']:
        # last_article_at within 7 days
        try:
            last = datetime.fromisoformat(str(r['last_article_at']).replace('Z',''))
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) - last < timedelta(days=7):
                producing += 1
        except Exception:
            pass
if producing >= 10:
    out("source_additions", "PASS", f"{producing}/14 new sources active in 7d")
elif producing >= 6:
    out("source_additions", "WARN", f"{producing}/14 new sources — some silent")
else:
    out("source_additions", "FAIL", f"only {producing}/14 sources producing")

# 2. time_stop — count fires + winners-in-disguise
ts_fires = list(c.execute("""
    SELECT symbol, exit_timestamp, exit_price, trading_strategy
    FROM trades WHERE exit_reason='time_stop'
      AND exit_timestamp > datetime('now','-7 days')
"""))
winners_in_disguise = 0
for tr in ts_fires:
    sym = tr['symbol']
    exit_p = float(tr['exit_price'] or 0)
    if exit_p == 0:
        continue
    r = c.execute("""
        SELECT MAX(price) AS max_p FROM market_prices
        WHERE symbol=? AND timestamp > ?
    """, (sym, tr['exit_timestamp'])).fetchone()
    max_p = float(r['max_p']) if r and r['max_p'] else None
    if max_p and (max_p - exit_p) / exit_p >= 0.05:
        winners_in_disguise += 1
n_fires = len(ts_fires)
if n_fires >= 1 and winners_in_disguise == 0:
    out("time_stop", "PASS", f"{n_fires} fires, 0 winners-in-disguise")
elif n_fires == 0:
    out("time_stop", "WARN", "no time_stop fires in 7d")
elif winners_in_disguise == 1:
    out("time_stop", "WARN", f"{n_fires} fires, 1 winner-in-disguise")
else:
    out("time_stop", "FAIL", f"{winners_in_disguise} winners-in-disguise")

# 3. exit_reasoning coverage — filter to closes AFTER the column shipped
#    (column added 2026-04-20 21:00 UTC in commit d41ad4b). Pre-PR closes
#    can't have the column populated and shouldn't count against coverage.
COL_SHIPPED = '2026-04-20 21:00:00'
r = c.execute("""
    SELECT COUNT(*) AS total,
           SUM(CASE WHEN exit_reasoning IS NOT NULL AND exit_reasoning!='' THEN 1 ELSE 0 END) AS with_r
    FROM trades WHERE status='CLOSED'
      AND exit_timestamp > datetime('now','-7 days')
      AND exit_timestamp > ?
""", (COL_SHIPPED,)).fetchone()
total = r['total']
with_r = r['with_r'] or 0
pct = (with_r / total * 100) if total else 0
if total == 0:
    out("exit_reasoning", "WARN", "no post-PR closes in 7d to evaluate")
elif pct >= 95:
    out("exit_reasoning", "PASS", f"{pct:.0f}% ({with_r}/{total} post-PR)")
elif pct >= 70:
    out("exit_reasoning", "WARN", f"{pct:.0f}% ({with_r}/{total}) — gap")
else:
    out("exit_reasoning", "FAIL", f"only {pct:.0f}% ({with_r}/{total})")

# 4. rotation_attribution
r = c.execute("""
    SELECT t.id, t.symbol, t.dynamic_sl_pct, t.dynamic_tp_pct,
           sa.id AS attr_id
    FROM trades t
    LEFT JOIN signal_attribution sa ON sa.trade_order_id = t.order_id
    WHERE t.trade_reason LIKE 'rotation_from_%'
      AND t.entry_timestamp > datetime('now','-7 days')
""").fetchall()
n_rot = len(r)
n_complete = sum(1 for x in r
                 if x['dynamic_sl_pct'] is not None
                 and x['dynamic_tp_pct'] is not None
                 and x['attr_id'] is not None)
if n_rot == 0:
    out("rotation_attribution", "PASS", "no rotation entries in 7d (n/a)")
elif n_complete == n_rot:
    out("rotation_attribution", "PASS", f"{n_complete}/{n_rot} rotations have SL/TP+attrib")
else:
    out("rotation_attribution", "FAIL",
        f"only {n_complete}/{n_rot} have SL/TP+attrib — {n_rot-n_complete} naked")

# 5. attribution_trajectory — daily snapshot count vs. days the loop has
#    been live (loop shipped 2026-04-21 21:00 UTC in commit 852d027).
#    Expected = min(7, days_since_loop_shipped).
LOOP_SHIPPED = datetime(2026, 4, 21, 21, 0, tzinfo=timezone.utc)
days_since_ship = max(1, min(7, int(
    (datetime.now(timezone.utc) - LOOP_SHIPPED).total_seconds() / 86400)))
r = c.execute("""
    SELECT COUNT(DISTINCT date(computed_at)) AS days
    FROM attribution_coverage_history
    WHERE computed_at > datetime('now','-7 days')
""").fetchone()
days = r['days']
if days >= days_since_ship:
    out("attribution_trajectory", "PASS",
        f"{days} daily snapshots (expected ≥{days_since_ship})")
elif days >= max(1, days_since_ship - 1):
    out("attribution_trajectory", "WARN",
        f"{days} snapshots, expected ≥{days_since_ship} — 1-day gap")
else:
    out("attribution_trajectory", "FAIL",
        f"only {days} snapshots, expected ≥{days_since_ship} — loop gaps")

# 6. calibration_loop snapshot age
r = c.execute("""
    SELECT MAX(computed_at) AS latest FROM gemini_calibration
""").fetchone()
if r['latest']:
    try:
        latest = datetime.fromisoformat(str(r['latest']).replace('Z',''))
        if latest.tzinfo is None:
            latest = latest.replace(tzinfo=timezone.utc)
        age_h = (datetime.now(timezone.utc) - latest).total_seconds() / 3600
        if age_h < 30:
            out("calibration_loop", "PASS", f"snapshot {age_h:.0f}h old")
        elif age_h < 72:
            out("calibration_loop", "WARN", f"snapshot {age_h:.0f}h old — should be <30h")
        else:
            out("calibration_loop", "FAIL", f"snapshot {age_h:.0f}h old — loop dead")
    except Exception as e:
        out("calibration_loop", "FAIL", f"timestamp parse error: {e}")
else:
    out("calibration_loop", "FAIL", "no calibration snapshots ever")

# 7. gate_reject_logging — count [GATE_REJECT] lines in last 24h docker logs
try:
    proc = subprocess.run(
        ['docker', 'logs', 'crypto-bot', '--since', '24h'],
        capture_output=True, text=True, timeout=30, check=False)
    n_gates = sum(1 for line in proc.stdout.splitlines() if '[GATE_REJECT]' in line)
    n_gates += sum(1 for line in proc.stderr.splitlines() if '[GATE_REJECT]' in line)
    if n_gates >= 100:
        out("gate_reject_logging", "PASS", f"{n_gates} lines / 24h")
    elif n_gates >= 10:
        out("gate_reject_logging", "WARN", f"{n_gates} lines / 24h — low")
    else:
        out("gate_reject_logging", "FAIL", f"only {n_gates} lines — instrumentation broken?")
except Exception as e:
    # Inside container we can't run docker, so this check is skipped
    out("gate_reject_logging", "WARN", f"check skipped (run-context: {str(e)[:40]})")

# 8. auto activity 7d
r = c.execute("""
    SELECT COUNT(*) AS n FROM trades
    WHERE trading_strategy='auto' AND entry_timestamp > datetime('now','-7 days')
""").fetchone()
n_auto = r['n']
if n_auto >= 3:
    out("auto_activity", "PASS", f"{n_auto} new trades in 7d")
elif n_auto >= 1:
    out("auto_activity", "WARN", f"{n_auto} trades — slow")
else:
    out("auto_activity", "FAIL", "0 trades — dormant")

# 9. cross_strategy_concentration
doubled = list(c.execute("""
    SELECT symbol, COUNT(DISTINCT trading_strategy) AS n_strats,
           GROUP_CONCAT(trading_strategy) AS strats
    FROM trades
    WHERE status='OPEN' AND trading_strategy != 'manual'
    GROUP BY symbol
    HAVING n_strats >= 2
"""))
n_doubled = len(doubled)
n_tripled = sum(1 for d in doubled if d['n_strats'] >= 3)
if n_doubled == 0:
    out("cross_strategy_concentration", "PASS", "0 symbols in 2+ strategies")
elif n_tripled > 0 or n_doubled >= 3:
    syms = [d['symbol'] for d in doubled]
    out("cross_strategy_concentration", "FAIL",
        f"{n_doubled} doubled ({n_tripled} tripled): {','.join(syms[:5])}")
else:
    syms = [d['symbol'] for d in doubled]
    out("cross_strategy_concentration", "WARN",
        f"{n_doubled} symbols doubled: {','.join(syms[:3])}")

# 10. signal_decisions writer
r = c.execute("""
    SELECT MAX(decided_at) AS latest FROM signal_decisions
""").fetchone()
if r['latest']:
    try:
        latest = datetime.fromisoformat(str(r['latest']).replace('Z',''))
        if latest.tzinfo is None:
            latest = latest.replace(tzinfo=timezone.utc)
        age_d = (datetime.now(timezone.utc) - latest).total_seconds() / 86400
        if age_d < 7:
            out("signal_decisions_writer", "PASS", f"last write {age_d:.1f}d ago")
        elif age_d < 14:
            out("signal_decisions_writer", "WARN", f"last write {age_d:.1f}d ago")
        else:
            out("signal_decisions_writer", "FAIL", f"last write {age_d:.1f}d ago — dead")
    except Exception as e:
        out("signal_decisions_writer", "FAIL", f"parse error: {e}")
else:
    out("signal_decisions_writer", "FAIL", "table empty")

c.close()
```

**Note on check 7:** the script runs inside the docker container, which can't
run `docker logs` itself. Run that one check via SSH directly (outside the
container) and merge into the output:
```bash
gcloud compute ssh ... --command='sudo docker logs crypto-bot --since 24h 2>&1 | grep -c "\[GATE_REJECT\]"'
```

## Summary Format

```
═══════════════════════════════════════════════════════════════════
VERIFY-CHANGES — YYYY-MM-DD HH:MM UTC
═══════════════════════════════════════════════════════════════════

| # | Check                       | Status | Evidence                          |
|---|-----------------------------|--------|-----------------------------------|
| 1 | source_additions            | ✅ PASS | 13/14 sources active in 7d        |
| 2 | time_stop                   | ✅ PASS | 4 fires, 0 winners-in-disguise   |
| 3 | exit_reasoning              | ✅ PASS | 100% (5/5)                        |
| 4 | rotation_attribution        | ✅ PASS | 1/1 rotation has SL/TP+attrib    |
| 5 | attribution_trajectory      | ✅ PASS | 5 daily snapshots                 |
| 6 | calibration_loop            | ✅ PASS | snapshot 14h old                  |
| 7 | gate_reject_logging         | ✅ PASS | 2,847 lines / 24h                 |
| 8 | auto_activity               | ⚠️  WARN | 1 trade — slow                    |
| 9 | cross_strategy_concentration| ⚠️  WARN | ETH,SLB doubled                   |
|10 | signal_decisions_writer     | ❌ FAIL | last write 9.0d ago — dead       |

Tally: 7 PASS · 2 WARN · 1 FAIL

REMAINING CONCERNS (in order of priority):
1. signal_decisions_writer dead — fix the writer (~1h)
2. auto dormancy — per-asset-type threshold (queued, see commit 783144b)
3. cross-strategy concentration — accept for now (Apr 23 decision)

NEXT VERIFY: run after next PR cluster, OR if /check-pnl shows
something unexpected.
```

## Save Format (when `save` arg is passed)

File: `~/.claude/projects/-Users-daniil-Projects-crypto-investment-bot/memory/verify-changes-history.md`

If file doesn't exist, create with this frontmatter:

```yaml
---
name: Verify-changes audit history
description: Trail of /verify-changes runs — PASS/WARN/FAIL per check, run-over-run trajectory.
type: project
---

# Verify-Changes History
```

Append a new section to the **top** of the body (newest first):

```markdown
## YYYY-MM-DD HH:MM — P:7 W:2 F:1

| Check                       | Status | Evidence |
|-----------------------------|--------|----------|
| source_additions            | PASS   | 13/14 active in 7d |
| time_stop                   | PASS   | 4 fires, 0 WID |
| ... etc ...                 |        |          |

**Concerns:** signal_decisions writer dead (9d), auto dormancy persists.

---
```

Keep only the most recent **10 runs** to prevent drift. If the file has more
than 10 dated sections, prune the oldest.

Then update `~/.claude/projects/-Users-daniil-Projects-crypto-investment-bot/memory/MEMORY.md`:
- Find the `## Memory Files` section.
- If a `verify-changes-history.md` line already exists, replace it (update
  the date + tally).
- Otherwise append a single line:
  ```
  - [verify-changes-history.md](verify-changes-history.md) — last verify-changes Apr 26: P:7 W:2 F:1
  ```

## Notes

- All checks bounded by index queries; total wall-clock ≈ SSH round-trip
  (~3s) + ~200ms of queries + 1 docker logs grep (~1s for 24h).
- Check 7 (gate_reject_logging) requires a separate SSH command outside the
  container (docker exec inside container can't see docker logs). The skill
  output should merge it back into the table.
- `signal_decisions_writer` failure has been in the FAIL state since Apr 23
  baseline — it's a pre-existing bug, not a regression. Don't escalate
  unless it transitions PASS → FAIL.
- Don't ship code from this skill — it's read-only diagnosis. Findings
  feed into separate PR conversations.
- If the Bot is mid-deploy or the container was just reset, gate_reject_logging
  may show low counts until the next cycle accumulates events. Re-run after
  one full cycle (~15 min) before treating as a real regression.
