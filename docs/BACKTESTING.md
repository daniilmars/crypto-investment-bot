# Backtesting and Strategy Optimization Framework

This document outlines the process for backtesting the trading bot's strategy against historical data and optimizing its parameters for the best performance.

## Overview

The backtesting framework simulates the bot's full trading logic — including trailing stops, market regime detection, multi-timeframe confirmation, and Kelly-based position sizing — against historical price and whale transaction data.

The process involves three main scripts:
1.  `scripts/backfill_historical_data.py`: For populating the database with high-frequency historical data.
2.  `scripts/optimize_strategy.py`: For running the backtester across a grid of different parameter combinations.
3.  `scripts/view_optimization_results.py`: For analyzing the results of the optimization runs.

## Features

| Feature | Description |
|---------|-------------|
| **Risk Metrics** | Sharpe ratio, Sortino ratio, max drawdown, profit factor, Calmar ratio |
| **Trailing Stop** | Tracks peak price, activates after configurable gain threshold |
| **Market Regime** | ATR + ADX-based detection (trending/ranging/volatile) adjusts risk |
| **Multi-Timeframe** | Confirms signals across short/medium/long windows, filters false entries |
| **Kelly Sizing** | Dynamic position sizing based on running win rate after 10+ trades |
| **Slippage** | Configurable basis-point slippage on all fills (default 5 bps) |
| **Warm-up Period** | Skips first N bars to ensure indicators have valid data |
| **Walk-Forward** | Out-of-sample validation across multiple time folds |

## The Workflow

### Step 1: Backfill the Database with High-Frequency Data

```bash
python3 scripts/backfill_historical_data.py
```
This fetches the last 90 days of **hourly** candlestick data from Binance.

**Important:** Clear old data first:
```bash
sqlite3 data/crypto_data.db "DELETE FROM market_prices;"
```

### Step 2: Run a Single Backtest

```bash
python3 src/analysis/backtest.py
```

Override parameters via CLI flags:
```bash
python3 src/analysis/backtest.py \
    --stop-loss-percentage 0.03 \
    --take-profit-percentage 0.08 \
    --slippage-bps 10 \
    --trailing-stop-activation 0.03 \
    --trailing-stop-distance 0.02
```

### Step 3: Run Walk-Forward Validation

Walk-forward splits the data into non-overlapping windows and tests strategy consistency out-of-sample. This is the most reliable way to validate a strategy.

```bash
python3 src/analysis/backtest.py --walk-forward --walk-forward-splits 3
```

Key output metric: **Fold Consistency** — the % of time windows where the strategy was profitable. If this is below 66%, the strategy may be overfit.

### Step 4: Run the Strategy Optimization

```bash
python3 scripts/optimize_strategy.py
```

The grid now tests SMA period, RSI thresholds, stop-loss, and take-profit in parallel.

### Step 5: Analyze the Results

```bash
python3 scripts/view_optimization_results.py
```

## Output Metrics

| Metric | What it tells you |
|--------|-------------------|
| **Sharpe Ratio** | Risk-adjusted return (> 1.0 is good, > 2.0 is excellent) |
| **Sortino Ratio** | Like Sharpe but only penalizes downside vol (higher = better) |
| **Max Drawdown** | Worst peak-to-trough decline (smaller = better) |
| **Profit Factor** | Gross profit / gross loss (> 1.5 is good) |
| **Calmar Ratio** | Return / max drawdown (higher = better risk-adjusted) |
| **Win Rate** | % of profitable trades |
| **Avg Win / Avg Loss** | The payoff ratio per trade |

## Interpreting Walk-Forward Results

- **Fold Consistency > 66%**: Strategy likely has an edge
- **Avg Sharpe > 1.0 across folds**: Robust risk-adjusted performance
- **Similar metrics across folds**: Strategy is stable, not overfit
- **High variance between folds**: Strategy may be regime-dependent
