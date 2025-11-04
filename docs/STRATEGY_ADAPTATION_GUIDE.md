# Strategy Adaptation Guide

This document outlines the formal, data-driven process for adapting and tuning the investment logic of the Crypto Investment Bot. Following this guide ensures that all strategy modifications are based on evidence and rigorous testing, not on emotion or short-term market noise.

---

## 🧠 Guiding Principle: Evidence-Based Decisions

The core principle of strategy adaptation is that **all changes must be justified by data.** We avoid making frequent, reactive adjustments based on a small number of recent trades. Instead, we follow a cyclical, deliberate process of analysis, hypothesis, testing, and monitoring.

---

## 🔄 The Adaptation Workflow Cycle

### Phase 1: Data Collection

**Goal:** To gather a statistically significant dataset of closed trades before making any conclusions about the current strategy's performance.

- **Action:** Let the bot run uninterrupted with its current settings for a significant period (weeks or even months). A handful of trades is not enough data to evaluate a strategy.
- **Threshold:** Aim to have at least 50-100 closed trades before conducting a full performance review.

### Phase 2: Performance Analysis

**Goal:** To deeply understand the behavior and performance of the current strategy using the collected trade data.

- **Action:** Connect to the database and export all closed trades into a spreadsheet or data analysis tool (like a Jupyter notebook).
- **Key Analytical Questions:**
    1.  **Stop-Loss Analysis:** How many trades were stopped out? Of those, how many would have eventually become profitable if the stop-loss was wider (e.g., 3% instead of 2%)? Are stop-losses being hit by normal volatility or by genuine trend reversals?
    2.  **Take-Profit Analysis:** How many trades hit the take-profit target? Of those, how much further did the price run? Are we cutting our winners short?
    3.  **Win/Loss Ratio & PnL:** What is the overall win rate? What is the average profit on a winning trade versus the average loss on a losing trade? (A strategy can be profitable with a low win rate if the average win is much larger than the average loss).
    4.  **Asset-Specific Performance:** Does the strategy perform better or worse on specific cryptocurrencies (e.g., BTC vs. a more volatile altcoin)?

### Phase 3: Hypothesis Formation

**Goal:** To translate the insights from your analysis into a clear, testable hypothesis for a strategy improvement.

- **Action:** Formulate a specific, measurable statement.
- **BAD Hypothesis:** "Let's try to make more money."
- **GOOD Hypothesis:** "My analysis shows that 30% of my stop-losses are triggered by minor volatility before the price reverses. **I hypothesize that widening the stop-loss to 3.5% and increasing the take-profit target to 7% to compensate for the increased risk will improve the strategy's overall profitability.**"

### Phase 4: Rigorous Backtesting

**Goal:** To validate your new hypothesis against all available historical data *before* deploying it to the live bot.

- **Action:**
    1.  Modify the backtesting script (`src/analysis/backtest.py`) to use your **new, proposed** set of parameters.
    2.  Run the backtest against the entire history of data collected by the bot.
    3.  Compare the backtest results of the new strategy directly against the results of the old strategy.
- **Evaluation:** Did the new strategy perform better? Did it improve the win rate, the average profit, or the overall PnL? Does it align with your hypothesis?

### Phase 5: Deployment & Monitoring

**Goal:** To implement the new strategy and monitor its live performance to ensure it matches the backtested expectations.

- **Action:**
    1.  If the backtest results are positive, update the relevant parameters in the `config/settings.yaml.example` file.
    2.  Commit the change with a clear message explaining the new strategy (e.g., `feat(strategy): Widen stop-loss to 3.5% based on backtest results`).
    3.  Push the commit to trigger the deployment.
    4.  Closely monitor the bot's performance over the next data collection cycle (Phase 1).

---

## ⚠️ AWarning: Avoid Curve Fitting

**Curve fitting** (or over-optimization) is the single biggest danger in strategy adaptation. It occurs when you tune your parameters so perfectly to past data that they no longer work on future, unseen data.

To avoid this:
- **Use a large dataset.** Never optimize a strategy based on a small number of trades.
- **Keep it simple.** A strategy with a few, robust parameters is often better than a complex one with dozens of finely-tuned settings.
- **Accept imperfection.** No strategy will be perfect. The goal is to find a profitable edge over the long term, not to win every single trade.
