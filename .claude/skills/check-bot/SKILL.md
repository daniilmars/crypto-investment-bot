---
name: check-bot
description: Check the performance and health of the deployed crypto-investment-bot on GCE. Use when monitoring bot operations, checking logs, or verifying deployment status.
---

Check the performance of the deployed crypto-investment-bot on GCE. Run all checks in parallel where possible and present a formatted summary.

## Steps

1. **Health check** — curl the health endpoint:
   ```
   curl -s --max-time 10 http://35.198.169.161:8080/health
   ```

2. **VM status** — check if the GCE instance is running:
   ```
   gcloud compute instances describe crypto-bot-eu --zone=europe-west3-a --format="value(status)"
   ```

3. **GitHub Actions status** — check recent workflow runs:
   ```
   gh run list --limit 5
   ```

4. **Latest cycle logs** — SSH into the VM and get the last 30 minutes of bot logs:
   ```
   gcloud compute ssh crypto-bot-eu --zone=europe-west3-a --tunnel-through-iap -- "sudo docker logs cryptobot --since 30m 2>&1 | tail -200"
   ```

## Summary Format

Present findings in this structure:

**Health:** [status of each check: bot_loop, database, telegram]

**VM Status:** [RUNNING/TERMINATED/etc]

**Latest Cycle:**
- Macro regime and multiplier
- News pipeline: RSS count + web scrape count
- Deep scraping: enriched/total
- Gemini scoring: cache hits vs new API calls
- Grounded search: symbols assessed

**Trading State:**
- Circuit breaker status
- Signal summary (any BUY/SELL or all HOLD)
- Open positions: auto count, manual count
- Auto trading stats: trades, wins, win rate

**Errors/Warnings:**
- Any recurring errors from logs
- RSS feed failures
- Scraper issues

**GitHub Actions:**
- Last deploy status
- Health check status

If the VM is TERMINATED (spot instance preemption), note this and offer to restart it with:
```
gcloud compute instances start crypto-bot-eu --zone=europe-west3-a
```

If SSH/health check fails, check VM status first — spot instances get preempted regularly.
