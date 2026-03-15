---
name: check-costs
description: Assess the running costs of the deployed crypto-investment-bot. Queries GCP billing, estimates Gemini API usage from logs, and reports total monthly spend.
---

Assess the running costs of the deployed crypto-investment-bot. Gather real data from GCP billing and bot logs, then compute estimated monthly spend.

## Steps

Run steps 1-3 in parallel:

1. **GCP billing — actual spend this month:**
   ```
   gcloud billing accounts list --format="value(name)" 2>/dev/null | head -1
   ```
   Then with the billing account:
   ```
   gcloud billing projects describe $(gcloud config get-value project 2>/dev/null) --format="json" 2>/dev/null
   ```
   And get the BigQuery billing export or cost breakdown:
   ```
   gcloud compute instances describe crypto-bot-eu --zone=europe-west3-a --format="json(machineType,scheduling.preemptible,disks[].diskSizeGb)" 2>/dev/null
   ```

2. **Gemini API usage from bot logs (last 24h):**
   ```
   gcloud compute ssh crypto-bot-eu --zone=europe-west3-a --tunnel-through-iap -- "sudo docker logs cryptobot --since 24h 2>&1 | grep -c 'Gemini'" 2>/dev/null
   ```
   And count specific call types:
   ```
   gcloud compute ssh crypto-bot-eu --zone=europe-west3-a --tunnel-through-iap -- "sudo docker logs cryptobot --since 24h 2>&1 | grep -cE 'article scoring complete|Gemini (grounded|cache hit|news analysis complete|position analyst)'" 2>/dev/null
   ```

3. **Config-based estimation** — read the local config to calculate theoretical costs:
   - Read `config/settings.yaml` for: `run_interval_minutes`, `use_grounded_search`, `position_analyst.check_interval_minutes`, `cache_ttl_minutes`
   - Read `config/watch_list.yaml` for symbol counts
   - Count RSS feeds in `src/collectors/news_data.py`

## Cost Model

Use these rates for estimation:

### GCE VM
| Instance | Region | Monthly (sustained) |
|----------|--------|---------------------|
| e2-micro | europe-west3 | ~$7.60 |
| Persistent disk (10GB) | europe-west3 | ~$0.40 |
| Network egress | — | ~$0.50 |
| **Subtotal** | | **~$8.50/mo** |

### Gemini API (Vertex AI)
| Model | Input | Output | Notes |
|-------|-------|--------|-------|
| gemini-2.5-flash | $0.15/1M tokens | $0.60/1M tokens | Article scoring |
| gemini-2.5-flash-lite | $0.075/1M tokens | $0.30/1M tokens | News assessment, position analyst |
| Grounded search | $35/1000 queries | — | Only if `use_grounded_search: true` |

Estimate per-call token usage:
- Article scoring batch (50 articles): ~2000 input tokens, ~500 output tokens → ~$0.0006/call
- News impact assessment (all symbols): ~5000 input tokens, ~1000 output tokens → ~$0.0007/call
- Position analyst (per position): ~3000 input tokens, ~500 output tokens → ~$0.0004/call
- Grounded search (if enabled): $0.035/call

### Free Services
- Binance API: $0
- Alpha Vantage (free tier): $0
- RSS feeds: $0
- yfinance: $0

## Calculation

```
cycles_per_day = 1440 / run_interval_minutes  (e.g., 96 for 15-min)

# Article scoring: ~1 batch call per cycle (50 articles), halved by cache
scoring_calls = cycles_per_day / 2
scoring_cost = scoring_calls * $0.0006

# News assessment: 1 call per cycle (batches all symbols)
assessment_calls = cycles_per_day / 2  (cache reduces)
assessment_cost = assessment_calls * $0.0007

# Grounded search (if enabled): 1 call per cycle
grounded_calls = cycles_per_day / 2 if use_grounded_search else 0
grounded_cost = grounded_calls * $0.035

# Position analyst: depends on check_interval and open positions
analyst_calls = (1440 / check_interval_minutes) * avg_open_positions
analyst_cost = analyst_calls * $0.0004

daily_gemini = scoring_cost + assessment_cost + grounded_cost + analyst_cost
monthly_gemini = daily_gemini * 30

total_monthly = $8.50 (VM) + monthly_gemini
```

## Summary Format

Present as:

```
MONTHLY COST ESTIMATE
=====================

GCE Infrastructure
  VM (e2-micro, Frankfurt)      $7.60
  Disk + network                $0.90
  ─────────────────────────────────
  Subtotal                      $8.50

Gemini API (Vertex AI)
  Article scoring     X calls/day   $Y.YY/mo
  News assessment     X calls/day   $Y.YY/mo
  Grounded search     X calls/day   $Y.YY/mo   [or "disabled"]
  Position analyst    X calls/day   $Y.YY/mo
  ─────────────────────────────────
  Subtotal                          $Z.ZZ/mo

Other APIs                          $0.00
  ─────────────────────────────────

TOTAL                              $XX.XX/mo  (~$X.XX/day)

Config that affects cost:
  - Cycle interval: 15 min (96 cycles/day)
  - Gemini cache TTL: 30 min
  - Grounded search: enabled/disabled
  - Position analyst: every 1440 min
  - Watch list: N crypto + M stock symbols
```

If actual GCP billing data is available, show it alongside the estimate for comparison.
