---
name: assess-quality
description: Comprehensive quality assessment of the crypto-investment-bot. Scores 6 categories, compares to previous assessment, and saves results. Use when evaluating bot quality, after major changes, or periodically (monthly).
argument-hint: "[save]"
---

Run a comprehensive quality assessment of the crypto-investment-bot. This assessment reads the actual codebase, checks deployed state, and scores against a fixed framework.

## Arguments

Parse `$ARGUMENTS`:
- `save` — save the assessment to `memory/quality-assessment.md` after scoring

## Step 1: Gather Evidence

Before scoring, gather real data. Run these in parallel where possible:

1. **Test coverage**: `.venv/bin/python -m pytest tests/ -q 2>&1 | tail -3`
2. **Code stats**: Count files, lines, functions in `src/`
3. **Deployed state** (if VM is running):
   - Strategy configs: how many active?
   - Open positions per strategy
   - Trade stats (total trades, win rate, PnL per strategy)
   - Grounded search success rate (last 24h)
   - Gemini assessments table row count
   - Strategy scores table row count
   - Weekly review config
4. **Codebase inspection** (read key files):
   - `config/settings.yaml` — strategies section, feedback configs
   - `src/analysis/gemini_news_analyzer.py` — prompt construction (check for feedback sections)
   - `src/orchestration/cycle_runner.py` — strategy loop, compute_effective_strength wiring
   - `src/execution/circuit_breaker.py` — per-strategy support
   - `src/analysis/weekly_self_review.py` — exists?
   - `src/analysis/strategy_weights.py` — exists and wired?
5. **Previous assessment**: Read `memory/quality-assessment.md` for comparison

## Step 2: Score Each Category

Score each category 1-10 based on the evidence gathered. Be honest and specific — cite actual code/data.

### Category 1: Architecture & Code Quality (15%)

Score based on:
- Separation of concerns (cycle_runner, trade_executor, signal_engine, news_pipeline)
- Test count and pass rate
- Error handling patterns (try/except with logging, not silent)
- Lint cleanliness (flake8 violations)
- Code duplication (legacy wrappers vs clean abstractions)

### Category 2: Signal Intelligence (25%)

Score based on:
- RSS feed count and diversity (crypto, stocks, regulatory, KOL, Asia)
- Web scraper coverage
- Gemini prompt quality (enriched fields: catalyst_count, hype_vs_fundamental, risk_factors)
- Feedback layers in prompt (trade outcomes, source reliability, symbol memory, regime trajectory)
- Strategy weights engine (compute_effective_strength wired and tested?)
- Article scoring cache hit rate

### Category 3: Risk Management (20%)

Score based on:
- Macro regime detection (5 indicators, weighted scoring, trajectory)
- Circuit breaker (per-strategy, deadlock-proof, correct capital base)
- Dynamic SL/TP per strategy (ATR-based, configurable per strategy)
- RISK_OFF behavior (suppress buys, exit acceleration)
- Position limits per strategy
- Sector correlation limits

### Category 4: Trading Execution (15%)

Score based on:
- Number of active strategies (auto, momentum, conservative)
- Strategy isolation (separate capital, positions, cooldowns, peaks)
- Auto-execution for non-manual strategies (is_auto != 'manual')
- Signal mutation protection (original_signal copy before manual path)
- Per-strategy signal strength via compute_effective_strength
- Order execution (paper fill with slippage simulation)

### Category 5: Operational Reliability (10%)

Score based on:
- Push-to-deploy (GitHub Actions CI/CD)
- Health checks (frequency, what they check)
- Telegram error forwarding (with ignore patterns for known noise)
- Database backups
- Grounded search reliability (success rate, sequential batching)
- Cost efficiency (monthly spend, free tier usage)
- Preemptible VM resilience

### Category 6: Continuous Improvement (15%)

Score based on:
- Trade outcome feedback in Gemini prompt (deployed?)
- Source reliability scoring in prompt (deployed?)
- Symbol win rate memory in prompt (deployed?)
- Regime trajectory in prompt (deployed?)
- Gemini assessments persistence table (exists? has data?)
- Strategy scores persistence table (exists? has data?)
- Weekly self-review automation (scheduled? sent first review?)
- Backtesting skill (exists? includes intelligence analysis?)
- Auto-tuner infrastructure (exists in config?)

## Step 3: Calculate Weighted Score

```
Category                    Weight    Score    Weighted
──────────────────────────────────────────────────────
Architecture & Code          15%      X/10     X.XX
Signal Intelligence          25%      X/10     X.XX
Risk Management              20%      X/10     X.XX
Trading Execution            15%      X/10     X.XX
Operational Reliability      10%      X/10     X.XX
Continuous Improvement       15%      X/10     X.XX
──────────────────────────────────────────────────────
WEIGHTED TOTAL                                 X.XX/10
```

## Step 4: Compare to Previous Assessment

Read `memory/quality-assessment.md` and highlight:
- Categories that improved (with specific reasons)
- Categories that regressed (with specific reasons)
- New capabilities since last assessment
- Remaining gaps and priority improvements

## Step 5: Present Results

Format as:

```
QUALITY ASSESSMENT — [DATE]
============================

Overall: X.X/10 (previous: Y.Y/10, change: +/-Z.Z)

[Category scores table]

IMPROVEMENTS SINCE LAST ASSESSMENT:
- [bullet points]

REMAINING GAPS:
- [bullet points with priority: HIGH/MEDIUM/LOW]

TOP 3 PRIORITIES:
1. [most impactful improvement]
2. [second]
3. [third]
```

## Step 6: Save (if `save` argument provided)

Update `memory/quality-assessment.md` with the new assessment. Keep the same frontmatter format. Include the date, scores, and key findings.
