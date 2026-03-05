# Profit Improvement Development Concept

## Philosophy

This document defines a systematic approach to improving trade profitability through local development with Claude Code. The core principle: **every change must be measurable, backtestable, and reversible.** We treat the bot as a quantitative system where each improvement is a hypothesis validated with data.

---

## Current Baseline

From the Feb 2026 backtest (23-day window, $10k capital):
- Return: +30.6%, Win rate: 55.7%, Sharpe: 2.15
- SL: 3.5%, TP: 8%, Trailing: 2%/1.5%, Risk: 3%
- Signal mode: sentiment (Gemini-first)

This is the benchmark. Every proposed change must beat these numbers on the same data window AND survive walk-forward validation on unseen data.

---

## Development Loop

Every improvement iteration follows this cycle:

```
1. MEASURE  → Analyze current trade data, identify weakest link
2. HYPOTHESIZE → Propose a specific, testable change
3. BACKTEST → Run against historical data (walk-forward)
4. IMPLEMENT → Code the change, add tests
5. SHADOW  → Deploy to auto-bot, compare against manual
6. PROMOTE → If shadow outperforms, promote to manual
```

Claude Code's role in each step:
- **MEASURE**: Query SQLite DB, compute signal-to-outcome statistics
- **HYPOTHESIZE**: Analyze patterns in losing trades, propose logic changes
- **BACKTEST**: Run `backtest.py` with modified parameters, compare metrics
- **IMPLEMENT**: Write code, unit tests, update config
- **SHADOW**: Deploy config change to auto-bot section
- **PROMOTE**: Update manual config after validation period

---

## Improvement Areas (Priority Order)

### 1. Signal Quality Analysis (Highest Impact)

**Problem**: We don't systematically track which signals lead to profitable trades.

**What to build**: A signal scorecard that connects generated signals to trade outcomes.

```
scripts/signal_scorecard.py
├── For each closed trade, find the originating signal
├── Compute: signal → entry → exit → PnL chain
├── Break down by:
│   ├── Signal mode (scoring vs sentiment)
│   ├── Gemini confidence bucket (0.7-0.8, 0.8-0.9, 0.9-1.0)
│   ├── RSI zone at entry (oversold/neutral/overbought)
│   ├── SMA trend alignment (with/against)
│   ├── Macro regime at entry (risk_on/caution/risk_off)
│   ├── News velocity at entry (quiet/active/breaking)
│   ├── Time of day / day of week
│   └── Asset type (crypto/stock)
└── Output: which conditions produce the highest edge
```

**Example insight**: "BUY signals with Gemini confidence > 0.85 AND RSI < 50 AND price > SMA win 72% of the time. BUY signals with Gemini confidence 0.7-0.8 AND RSI > 60 win only 41%."

**Action**: Tighten entry filters where win rate is below breakeven.

### 2. Exit Optimization (Second Highest Impact)

**Problem**: SL/TP/trailing parameters are static. Different market conditions need different exits.

**What to build**: Regime-adaptive exit parameters.

```
src/analysis/adaptive_exits.py
├── Input: macro_regime, asset volatility (ATR), position age
├── Output: dynamic SL%, TP%, trailing activation/distance
└── Logic:
    ├── RISK_ON + low volatility  → tight SL (2.5%), wide TP (10%), aggressive trailing
    ├── RISK_ON + high volatility → wider SL (4.5%), wide TP (12%), standard trailing
    ├── CAUTION                   → standard SL (3.5%), standard TP (8%)
    └── RISK_OFF                  → tight SL (2%), tight TP (5%), no trailing
```

**Validation approach**:
1. Run `sltp_sweep.py` separately for each regime period in history
2. Find optimal SL/TP per regime
3. Backtest the adaptive strategy vs fixed parameters
4. Must show improvement in Sortino ratio (we care about downside)

### 3. Entry Timing Refinement

**Problem**: Signals fire on cycle boundaries (every 15 min). Price may have already moved.

**What to build**: Entry quality scoring to avoid chasing.

```
Key metrics to track:
- Slippage from signal price to actual entry: how much did price move?
- Mean reversion after entry: does price often dip right after we buy?
- Signal freshness: Gemini assessment age vs price movement since assessment
```

**Concrete improvements**:
- Add a "signal staleness" check: if Gemini assessment is >15min old and price moved >1% since assessment, re-evaluate before entering
- Add a limit-order simulation in paper trading: instead of market orders at cycle price, simulate a limit order at a slight discount (0.1-0.2%) and track fill rate
- Track "entry efficiency": (best_price_in_first_hour - entry_price) / entry_price

### 4. Position Sizing Calibration

**Problem**: Kelly criterion is theoretically optimal but assumes i.i.d. trades, which crypto isn't.

**What to build**: Conditional Kelly that accounts for regime.

```
Current: kelly = half_kelly * macro_multiplier * event_multiplier
Problem: kelly is computed from ALL historical trades regardless of regime

Better: compute separate win rates per regime
- kelly_risk_on  = half_kelly from trades entered during RISK_ON
- kelly_caution  = half_kelly from trades entered during CAUTION
- kelly_risk_off = half_kelly from trades entered during RISK_OFF

Use the regime-specific kelly for sizing.
```

**Also**: Track optimal position count. Current max is 5. But does performance degrade with more positions? Compute Sharpe per number of concurrent positions.

### 5. Loss Analysis & Pattern Recognition

**Problem**: We don't systematically study losing trades.

**What to build**: A loss autopsy tool.

```
scripts/loss_autopsy.py
├── Extract all losing trades from DB
├── For each loss:
│   ├── What was the signal confidence?
│   ├── What was the macro regime?
│   ├── Was there a news event within 24h?
│   ├── Did the position analyst recommend SELL before SL hit?
│   ├── How close did the trailing stop come to activating?
│   ├── What was the max favorable excursion (MFE)?
│   └── What was the max adverse excursion (MAE)?
├── Cluster losses into categories:
│   ├── "SL too tight" (MFE > TP but SL hit first)
│   ├── "Bad entry" (never profitable)
│   ├── "Event-driven" (news reversal)
│   ├── "Regime shift" (entered RISK_ON, regime changed)
│   └── "Slow bleed" (gradual decline, SL hit after days)
└── Output: actionable patterns with suggested fixes
```

**MFE/MAE analysis is the single most valuable tool for exit optimization.** If we find that 40% of SL exits had MFE > 3% (the trade was profitable before stopping out), the SL is too tight or trailing activation is too high.

### 6. Gemini Prompt Engineering

**Problem**: Position analyst and signal quality depend on Gemini prompt quality. No systematic evaluation.

**What to build**: Prompt A/B testing framework.

```
src/analysis/prompt_variants.py
├── Define prompt variants with IDs (e.g., "analyst_v1", "analyst_v2")
├── For each position check, randomly assign a variant
├── Log: (position_id, prompt_variant, recommendation, confidence, actual_outcome)
├── After N samples, compute accuracy per variant
└── Promote winning variant

Example variants to test:
- More concise prompts (reduce token cost, may improve focus)
- Chain-of-thought prompts (explicit reasoning steps)
- Quantitative-only prompts (remove qualitative language)
- Contrarian prompts ("argue against the current position")
```

### 7. Correlation-Aware Position Management

**Problem**: Multiple crypto positions often move together. 5 correlated positions = 1 concentrated bet.

**What to build**: Dynamic correlation tracking.

```
scripts/correlation_analysis.py
├── Compute rolling 7-day correlation matrix from price history
├── Before opening new position:
│   ├── Calculate portfolio beta to BTC
│   ├── Calculate average pairwise correlation of open positions
│   └── If adding this position raises avg correlation > 0.7, reduce size by 50%
└── Track: did correlation-adjusted sizing reduce drawdowns?
```

---

## Backtest Workflow (How to Validate Changes)

### Standard Validation Protocol

Every parameter or logic change must pass this:

```bash
# Step 1: Baseline (current config)
.venv/bin/python src/analysis/backtest.py --walk-forward --walk-forward-splits 5

# Step 2: Modified config
# Edit settings or code
.venv/bin/python src/analysis/backtest.py --walk-forward --walk-forward-splits 5

# Step 3: Compare
# Improvement must show:
#   - Higher Sharpe ratio (risk-adjusted return)
#   - Lower max drawdown OR same drawdown with higher return
#   - Consistent across all 5 walk-forward folds (not just average)
#   - At least 30 trades in sample (statistical significance)
```

### What "Better" Means (Decision Criteria)

| Metric | Minimum Bar | Ideal |
|--------|------------|-------|
| Sharpe ratio | > baseline | > 2.0 |
| Sortino ratio | > baseline | > 2.5 |
| Max drawdown | < baseline | < -5% |
| Win rate | > 50% | > 55% |
| Profit factor | > 1.5 | > 2.0 |
| Walk-forward fold consistency | 4/5 folds profitable | 5/5 |

**Red flags that indicate overfitting**:
- Sharpe > 5 on any fold (unrealistically good)
- Win rate > 80% with < 20 trades
- Large variance between folds
- Performance only works on one specific time window

### Hold-Out Protocol

Reserve the most recent 30 days of data as a final hold-out set. Never backtest on it during development. Only use it for final validation before promoting to production.

```
Historical data: [---- train (60%) ----][-- walk-forward (30%) --][- hold-out (10%) -]
                                                                   ^^^ never touch
```

---

## Claude Code Development Workflow

### Session Structure

Each development session follows this pattern:

```
1. "What's the current performance?"
   → Claude reads trade DB, computes recent metrics
   → Identifies weakest area (worst losing pattern, lowest win-rate bucket)

2. "Analyze the problem"
   → Claude queries specific trades, signals, conditions
   → Produces hypothesis: "BUY signals in X condition lose Y% of the time"

3. "Propose and backtest a fix"
   → Claude modifies signal logic or parameters
   → Runs backtest, compares to baseline
   → Reports: "Modified version: Sharpe 2.3 vs 2.15 baseline, 4/5 folds better"

4. "Implement if validated"
   → Claude writes code + tests
   → Deploys to auto-bot config for shadow testing
```

### Practical Session Commands

```bash
# Quick performance check
.venv/bin/python scripts/performance_analysis.py

# Signal scorecard (build this first)
.venv/bin/python scripts/signal_scorecard.py

# Loss autopsy
.venv/bin/python scripts/loss_autopsy.py

# Backtest with current config
.venv/bin/python src/analysis/backtest.py --walk-forward

# Parameter sweep
.venv/bin/python scripts/sltp_sweep.py

# Compare strategies
.venv/bin/python scripts/backtest_compare.py
```

### File Conventions

New analysis scripts go in `scripts/`. New trading logic goes in `src/analysis/` or `src/orchestration/`. Tests for everything in `tests/`.

```
scripts/
├── signal_scorecard.py      # Signal quality analysis
├── loss_autopsy.py          # Losing trade patterns
├── correlation_analysis.py  # Position correlation tracking
├── regime_performance.py    # Performance breakdown by regime
└── entry_efficiency.py      # Entry timing quality

src/analysis/
├── adaptive_exits.py        # Regime-aware SL/TP/trailing
└── prompt_variants.py       # Gemini prompt A/B testing

tests/
├── test_adaptive_exits.py
├── test_signal_scorecard.py
└── test_loss_autopsy.py
```

---

## Implementation Roadmap

### Phase A: Measurement (Sessions 1-3)
Build the analysis tools that tell us where we're losing money.
1. Signal scorecard — connect signals to outcomes
2. Loss autopsy — categorize losing trades
3. MFE/MAE analysis — entry/exit quality

### Phase B: Quick Wins (Sessions 4-6)
Low-risk parameter changes validated by backtest.
4. Tighten entry filters based on scorecard data
5. Regime-adaptive exit parameters
6. Conditional Kelly sizing per regime

### Phase C: Structural Improvements (Sessions 7-10)
Logic changes that require more careful testing.
7. Entry timing refinement (staleness check)
8. Correlation-aware position sizing
9. Gemini prompt optimization
10. Walk-forward re-validation of all changes combined

### Phase D: Continuous Improvement
Ongoing process after initial improvements are deployed.
- Weekly: review auto-bot vs manual performance
- Bi-weekly: re-run signal scorecard, check for drift
- Monthly: full walk-forward re-validation
- Quarterly: re-sweep parameters on fresh data

---

## Anti-Patterns to Avoid

1. **Optimizing on in-sample data** — Always use walk-forward. A strategy that only works on the training window is worthless.
2. **Too many parameters** — Every new parameter multiplies overfitting risk. Prefer simple, robust logic.
3. **Chasing win rate** — A 90% win rate with huge losses on the 10% is worse than 55% with controlled losses. Optimize Sharpe and Sortino, not win rate.
4. **Ignoring transaction costs** — Paper PnL includes 0.1% simulated fees. Real slippage on illiquid alts can be 0.5%+. Be conservative.
5. **Recency bias** — Last week's performance doesn't predict next week's. Use multi-month backtests.
6. **Overfitting to BTC** — BTC dominates crypto. A strategy that only works on BTC is fragile. Test on alts too.
7. **Ignoring drawdowns** — A strategy that returns 50% but has 30% max drawdown will get stopped out by the circuit breaker in live trading. Drawdown is the binding constraint.
