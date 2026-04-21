---
name: check-costs
description: Assess the running costs of the deployed crypto-investment-bot. Queries GCP billing, estimates Gemini API usage from logs, and reports total monthly spend.
---

Assess the running costs of the deployed crypto-investment-bot. Gather real data from GCP billing and bot logs, then compute estimated monthly spend.

## SSH Resilience (MANDATORY)

Before step 2's log-grep SSH, probe the tunnel (10s cap):
```bash
gcloud compute ssh crypto-bot-eu --zone=europe-west3-a --tunnel-through-iap \
  --ssh-flag="-o ConnectTimeout=10" \
  --ssh-flag="-o ServerAliveInterval=5" \
  --ssh-flag="-o ServerAliveCountMax=2" \
  --command="echo READY"
```
On hang / exit 255: `ssh-keygen -R compute.<IP> 2>/dev/null; ssh-keygen -R <IP> 2>/dev/null`, re-probe ONCE, then stop with diagnostic if still failing.

All SSH calls must include: `--ssh-flag="-o ConnectTimeout=15" --ssh-flag="-o ServerAliveInterval=10" --ssh-flag="-o ServerAliveCountMax=3"`

## Steps

Run steps 1-3 in parallel:

1. **GCP VM details:**
   ```
   gcloud compute instances describe crypto-bot-eu --zone=europe-west3-a --format="json(machineType,scheduling.preemptible,disks[].diskSizeGb)" 2>/dev/null
   ```

2. **Gemini API usage from bot logs (last 24h):**
   IMPORTANT: The Docker container name is `crypto-bot` (NOT `cryptobot`).
   Run a single SSH command to gather all metrics at once:
   ```
   gcloud compute ssh crypto-bot-eu --zone=europe-west3-a --tunnel-through-iap -- "
   echo '=== CYCLES ==='
   sudo docker logs crypto-bot --since 24h 2>&1 | grep -c 'Cycle complete'
   echo '=== ARTICLE SCORING ==='
   sudo docker logs crypto-bot --since 24h 2>&1 | grep -c 'article scoring complete'
   echo '=== NEWS ANALYSIS (grounded) ==='
   sudo docker logs crypto-bot --since 24h 2>&1 | grep -c 'grounded news analysis complete'
   echo '=== NEWS ANALYSIS (fallback) ==='
   sudo docker logs crypto-bot --since 24h 2>&1 | grep -c 'Gemini news analysis complete'
   echo '=== POSITION ANALYST ==='
   sudo docker logs crypto-bot --since 24h 2>&1 | grep -ci 'position analyst'
   echo '=== CACHE HITS ==='
   sudo docker logs crypto-bot --since 24h 2>&1 | grep 'score cache' | head -3
   echo '=== GROUNDED SETTING ==='
   sudo docker exec crypto-bot grep use_grounded config/settings.yaml
   echo '=== UPTIME ==='
   sudo docker inspect crypto-bot --format='{{.State.StartedAt}}' 2>/dev/null
   " 2>/dev/null
   ```

3. **Config-based estimation** — read the local config to calculate theoretical costs:
   - Read `config/settings.yaml` for: `run_interval_minutes`, `use_grounded_search`, `position_analyst.check_interval_minutes`, `cache_ttl_minutes`
   - Read `config/watch_list.yaml` for symbol counts (crypto, US stocks, EU stocks, Asia stocks)

## Cost Model

Use these rates for estimation:

### GCE VM
The VM is **PREEMPTIBLE** — use preemptible pricing, not standard.

| Instance | Region | Monthly (preemptible) | Monthly (standard) |
|----------|--------|-----------------------|---------------------|
| e2-micro | europe-west3 | ~$1.83 | ~$7.60 |
| Persistent disk (30GB) | europe-west3 | ~$1.20 | ~$1.20 |
| Network egress | — | ~$0.50 | ~$0.50 |
| **Subtotal** | | **~$3.53/mo** | ~$9.30/mo |

### Gemini API — Two SDK Paths

CRITICAL: The bot uses TWO different SDKs with different pricing:

| Path | SDK | Model | Pricing |
|------|-----|-------|---------|
| Grounded search (`analyze_news_with_search`) | `google.genai` (Gemini API) | gemini-2.5-flash-lite | **FREE** within 1,500 calls/day |
| Fallback news analysis (`analyze_news_impact`) | `vertexai` (Vertex AI) | gemini-2.5-flash-lite | $0.075/$0.30 per 1M tokens |
| Article scoring (`score_articles_batch`) | `vertexai` (Vertex AI) | gemini-2.5-flash | $0.15/$0.60 per 1M tokens |
| Position analyst | `vertexai` (Vertex AI) | gemini-2.5-pro | $1.25/$5.00 per 1M tokens |

**Grounded search is FREE** — it uses the `google-genai` SDK with `GoogleSearch()` tool.
The free tier allows 1,500 grounded queries/day. The bot batches ~349 symbols into
~14 groups of 25, using ~840 calls/day at 60 cycles/day (well within 1,500 limit).

DO NOT report grounded search as costing $35/1000 — that is the Vertex AI grounding price,
not the google-genai SDK free tier price.

### Per-call token cost estimates (Vertex AI path only)
- Article scoring batch (~2 articles avg due to cache): ~800 input, ~200 output → ~$0.00024/call
- Fallback news assessment (all symbols batched): ~5000 input, ~1000 output → ~$0.000675/call
- Position analyst (per position): ~3000 input, ~500 output → ~$0.006/call (gemini-2.5-pro is expensive)

### Free Services
- Binance API: $0
- Alpha Vantage (free tier): $0
- RSS feeds (113 feeds): $0
- yfinance: $0
- Google Search grounding (via google-genai SDK): $0 within 1,500/day

## Calculation

```
# Extrapolate from log sample to 24h
sample_hours = hours since container started (from docker inspect)
cycles_per_day = (cycles_in_sample / sample_hours) * 24

# Article scoring (Vertex AI, paid): only new articles scored, ~99.5% cache hit rate
scoring_calls_per_day = (scoring_calls_in_sample / sample_hours) * 24
scoring_cost = scoring_calls_per_day * $0.00024

# News analysis — check which path is active on deployed VM:
if deployed use_grounded_search == true:
    # Grounded search via google-genai SDK = FREE
    news_cost = $0.00
else:
    # Fallback via Vertex AI = PAID
    # Flag this as suboptimal — free path exists!
    news_calls_per_day = (fallback_calls_in_sample / sample_hours) * 24
    news_cost = news_calls_per_day * $0.000675

# Position analyst (Vertex AI, paid): 1x/day at check_interval_minutes=1440
analyst_cost = 1 * $0.006  # ~$0.18/mo

daily_gemini = scoring_cost + news_cost + analyst_cost
monthly_gemini = daily_gemini * 30

total_monthly = $3.53 (VM) + monthly_gemini
```

## Summary Format

Present as:

```
MONTHLY COST ESTIMATE
=====================

GCE Infrastructure
  VM (e2-micro, Frankfurt, PREEMPTIBLE)   $1.83
  Disk (30GB) + network                   $1.70
  ─────────────────────────────────────────────
  Subtotal                                $3.53

Gemini API
  Article scoring       X calls/day   $Y.YY/mo   (gemini-2.5-flash, Vertex AI)
  News analysis          —            $0.00/mo    (grounded search, google-genai FREE tier)
   or: News analysis    X calls/day   $Y.YY/mo   (⚠ FALLBACK path — enable grounded search!)
  Position analyst      ~1 call/day   $0.18/mo   (gemini-2.5-pro, Vertex AI)
  ─────────────────────────────────────────────
  Subtotal                            $Z.ZZ/mo

  Free tier budget: ~840/1,500 calls/day used (56%)

Other APIs                            $0.00
  ─────────────────────────────────────────────

TOTAL                                $XX.XX/mo   (~$X.XX/day)

Config that affects cost:
  - Cycle interval: 15 min (~60 actual cycles/day)
  - Grounded search: enabled/DISABLED (⚠ if disabled, flag it)
  - Article scoring cache hit rate: ~99.5%
  - Position analyst: every 1440 min (1x/day)
  - Watch list: N crypto + M US + P EU + Q Asia symbols
```

## Important Checks

1. **Grounded search disabled?** If the deployed config has `use_grounded_search: false`,
   prominently flag this: the bot is paying for the Vertex AI fallback path when the
   grounded search path is FREE. Recommend enabling it.

2. **Preemptible?** Verify `scheduling.preemptible: true`. If it changed to standard,
   the VM cost jumps from $1.83 to $7.60/mo.

3. **Local vs deployed config divergence:** Compare `use_grounded_search` between
   `config/settings.yaml` (local) and deployed container. They may differ.

4. **Container name:** Always use `crypto-bot` (with hyphen), NOT `cryptobot`.
