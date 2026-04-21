---
name: check-bot
description: Check the performance and health of the deployed crypto-investment-bot on GCE. Use when monitoring bot operations, checking logs, or verifying deployment status.
---

Check the performance of the deployed crypto-investment-bot on GCE. Run all checks in parallel where possible and present a formatted summary.

## SSH Resilience Pattern

IAP SSH tunnels hang indefinitely when stuck. Never issue a long-running SSH
command without first running a lightweight probe. All SSH calls below must
include the keepalive flags shown in Step 4.

On probe failure (see Step 4 below):
1. Clear stale known_hosts for the VM's IP:
   ```bash
   ssh-keygen -R compute.<IP> 2>/dev/null && ssh-keygen -R <IP> 2>/dev/null
   ```
2. Re-probe ONCE.
3. If still failing, surface the diagnostic and stop — GitHub Actions'
   scheduled `Health Check` workflow is authoritative for bot liveness
   even when IAP is broken (`gh run list --workflow="Health Check" --limit 3`).

## Steps

1. **VM status + IP** — look up the current external IP dynamically (the ephemeral IP rotates across preemptions/restarts):
   ```
   gcloud compute instances describe crypto-bot-eu --zone=europe-west3-a \
     --format="value(status,networkInterfaces[0].accessConfigs[0].natIP)"
   ```
   Status first, IP second. If status is not RUNNING, stop here and offer a restart (see bottom).

2. **Health check** — curl the health endpoint at the IP from step 1:
   ```
   curl -s --max-time 10 http://<IP>:8080/health
   ```
   A single 503 is not a fault — the bot returns 503 during the ~10s active-cycle window every 15 min. Retry once if the first call fails.

3. **GitHub Actions status** — check recent workflow runs:
   ```
   gh run list --limit 5
   ```

4. **SSH probe (MANDATORY before the log dump in Step 5)** — 10s hard cap:
   ```bash
   gcloud compute ssh crypto-bot-eu --zone=europe-west3-a --tunnel-through-iap \
     --ssh-flag="-o ConnectTimeout=10" \
     --ssh-flag="-o ServerAliveInterval=5" \
     --ssh-flag="-o ServerAliveCountMax=2" \
     --command="echo READY"
   ```
   If hangs or returns exit 255: apply ssh-keygen recovery above, re-probe ONCE, then stop with diagnostic if still failing.

5. **Latest cycle logs** — only runs if probe succeeded:
   ```bash
   gcloud compute ssh crypto-bot-eu --zone=europe-west3-a --tunnel-through-iap \
     --ssh-flag="-o ConnectTimeout=15" \
     --ssh-flag="-o ServerAliveInterval=10" \
     --ssh-flag="-o ServerAliveCountMax=3" \
     --command="sudo docker logs crypto-bot --since 30m 2>&1 | tail -200"
   ```
   Worst-case hang: ~45s (connect + keepalive stall), not infinite.

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
