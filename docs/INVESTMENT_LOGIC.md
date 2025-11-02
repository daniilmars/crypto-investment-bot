# Investment Logic & Configuration

This document details the investment and trading strategy employed by the bot, and explains how to configure its behavior using the `config/settings.yaml` file.

---

## 🧠 Core Investment Strategy

The bot's strategy is based on a multi-factor model that combines **technical analysis** with **on-chain data analysis** to generate trading signals. The goal is to identify assets that show strong technical momentum, which is then confirmed by the actions of major market participants (i.e., "whales").

The signal generation process is hierarchical, with high-priority, event-driven signals taking precedence over the standard technical analysis model.

---

## ιε Signal Generation Hierarchy

### 1. High-Priority Event-Driven Signals (Overrides)

These signals are designed to react immediately to significant, unambiguous on-chain events. If any of these conditions are met, a `BUY` or `SELL` signal is generated, and the standard analysis is bypassed for that cycle.

#### a. High-Interest Wallet Activity

-   **Logic:** The bot monitors for any transactions involving wallets specified in the `high_interest_wallets` list.
    -   A transfer **from** a high-interest wallet **to** an exchange is interpreted as a strong bearish signal (potential sell-off).
    -   A transfer **from** an exchange **to** a high-interest wallet is interpreted as a strong bullish signal (accumulation).
-   **Configuration:**
    -   `high_interest_wallets`: A list of known wallet owner names to monitor (e.g., `"Grayscale"`, `"US Government"`).

#### b. Large-Scale Stablecoin Inflows

-   **Logic:** The bot monitors the aggregate flow of major stablecoins to all exchange wallets. A significant influx of stablecoins ("dry powder") onto exchanges is a leading indicator of market-wide buying pressure.
-   **Configuration:**
    -   `stablecoins_to_monitor`: A list of stablecoin symbols to include in the analysis (e.g., `usdt`, `usdc`).
    -   `stablecoin_inflow_threshold_usd`: The total USD value of stablecoin inflows that must be exceeded within a single cycle to trigger a market-wide `BUY` signal.

### 2. Standard Signal Analysis (Scoring-Based)

If no high-priority signals are generated, the bot proceeds with its standard analysis for each symbol in the `watch_list`. Instead of requiring all factors to align perfectly, the bot now uses a scoring system. Each of the following factors contributes a point to either a "buy" or "sell" score.

#### a. Trend Analysis: Simple Moving Average (SMA)

-   **Logic:** Compares the asset's `current_price` to its Simple Moving Average (SMA).
    -   `current_price > SMA`: Adds 1 to the `buy_score`.
    -   `current_price < SMA`: Adds 1 to the `sell_score`.
-   **Configuration:**
    -   `sma_period`: The lookback period for the SMA calculation.

#### b. Momentum Analysis: Relative Strength Index (RSI)

-   **Logic:** The RSI measures the speed and change of price movements.
    -   An RSI value **below** `rsi_oversold_threshold`: Adds 1 to the `buy_score`.
    -   An RSI value **above** `rsi_overbought_threshold`: Adds 1 to the `sell_score`.
-   **Configuration:**
    -   `rsi_oversold_threshold`: The RSI level below which an asset is considered oversold.
    -   `rsi_overbought_threshold`: The RSI level above which an asset is considered overbought.

#### c. On-Chain Volume Confirmation

-   **Logic:** The net flow of whale transactions for the specific symbol is analyzed.
    -   A net outflow from exchanges (more leaving than entering) suggests accumulation: Adds 1 to the `buy_score`.
    -   A net inflow to exchanges (more entering than leaving) suggests potential selling pressure: Adds 1 to the `sell_score`.

#### Signal Generation

-   A `BUY` signal is generated if the final `buy_score` is **2 or greater**.
-   A `SELL` signal is generated if the final `sell_score` is **2 or greater**.
-   If neither threshold is met, a `HOLD` signal is issued.

This scoring system is more flexible and robust than a rigid confluence model, as it can generate signals even if not all indicators are in perfect alignment, reflecting more realistic market conditions.

### 3. Risk Management Filter: Transaction Velocity

-   **Logic:** As a final check, the bot analyzes the frequency of recent whale transactions. If the number of transactions in the last hour is anomalously high compared to a historical baseline, it indicates extreme market volatility. In such cases, the bot will issue a `VOLATILITY_WARNING` and will not enter a new trade, even if the technical and on-chain factors align. This is a capital preservation measure.
-   **Configuration:**
    -   `transaction_velocity_baseline_hours`: The number of hours to use for the historical average transaction frequency.
    -   `transaction_velocity_multiplier`: The multiplier by which the current frequency must exceed the average to trigger a warning (e.g., `2.0` means 2x the normal rate).

---

## 📈 Paper Trading & Risk Management

When `paper_trading` is enabled, the bot will simulate trades based on the generated signals and a set of risk management rules.

-   **Position Sizing:** For each `BUY` signal, the bot calculates the position size as a percentage of the total available capital. This ensures that no single trade exposes the portfolio to excessive risk.
-   **Stop-Loss and Take-Profit:** All open positions are monitored on every cycle.
    -   If a position's value drops by the `stop_loss_percentage`, a simulated `SELL` order is executed to limit losses.
    -   If a position's value increases by the `take_profit_percentage`, a simulated `SELL` order is executed to secure gains.
-   **Concurrency Limit:** The bot will not open new positions if the number of currently open trades meets or exceeds the `max_concurrent_positions` limit.

### Configuration Variables

-   `paper_trading`: `true` to enable paper trading, `false` to disable.
-   `paper_trading_initial_capital`: The starting USD balance for the simulation.
-   `trade_risk_percentage`: The percentage of total capital to risk on a single trade (e.g., `0.01` for 1%).
-   `stop_loss_percentage`: The percentage drop from the entry price that will trigger a stop-loss (e.g., `0.02` for 2%).
-   `take_profit_percentage`: The percentage gain from the entry price that will trigger a take-profit (e.g., `0.05` for 5%).
-   `max_concurrent_positions`: The maximum number of trades that can be open at the same time.
