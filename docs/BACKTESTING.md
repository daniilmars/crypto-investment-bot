# Backtesting and Strategy Optimization Framework

This document outlines the process for backtesting the trading bot's strategy against historical data and optimizing its parameters for the best performance.

## Overview

The backtesting framework is designed to systematically test different configurations of the investment bot's variables (e.g., SMA period, RSI thresholds, risk management percentages) to identify the optimal parameter set.

The process involves three main scripts:
1.  `scripts/backfill_historical_data.py`: For populating the database with high-frequency historical data.
2.  `scripts/optimize_strategy.py`: For running the backtester across a grid of different parameter combinations.
3.  `scripts/view_optimization_results.py`: For analyzing the results of the optimization runs.

## The Workflow

### Step 1: Backfill the Database with High-Frequency Data

The accuracy of a backtest is highly dependent on the quality of the historical data. The live bot collects data periodically, which can result in an incomplete dataset. To run a proper backtest, you must first backfill the database with a complete, high-frequency dataset.

**Command:**
```bash
python3 scripts/backfill_historical_data.py
```
This script will:
- Connect to the Binance API (no keys required for public data).
- Fetch the last 90 days of **hourly** candlestick data for all cryptocurrencies in your `config/watch_list.yaml`.
- Save this data to the `market_prices` table in your local SQLite database.

**Important:** Before running the backfill script, you should clear any old, low-frequency data from the `market_prices` table:
```bash
sqlite3 data/crypto_data.db "DELETE FROM market_prices;"
```

### Step 2: Run the Strategy Optimization

Once you have a high-quality dataset, you can run the optimization script. This script will run the backtester for a "grid" of different parameter combinations, allowing you to systematically test how different settings affect performance.

The script uses Python's `multiprocessing` module to run the backtests in parallel, significantly speeding up the process.

**Command:**
```bash
python3 scripts/optimize_strategy.py
```
This script will:
- Define a `param_grid` of parameters to test (e.g., different SMA periods, RSI thresholds).
- Run a full backtest for every possible combination of these parameters.
- Save the results of each run (parameters and final PnL) to the `optimization_results` table in the database.

### Step 3: Analyze the Results

After the optimization is complete, you can use the `view_optimization_results.py` script to view a summary of all the backtesting runs.

**Command:**
```bash
python3 scripts/view_optimization_results.py
```
This will display a table of all the parameter combinations tested and their resulting PnL, allowing you to easily identify the best-performing configuration.

## Conclusion

This framework provides a systematic and efficient way to test and refine your trading strategy. By following this workflow, you can make data-driven decisions to improve the bot's performance.
