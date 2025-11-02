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

### 2. Standard Signal Analysis (Technical + On-Chain)

If no high-priority signals are generated, the bot proceeds with its standard analysis for each symbol in the `watch_list`. A signal is generated based on the confluence of the following factors:

#### a. Trend Analysis: Simple Moving Average (SMA)

-   **Logic:** The bot compares the asset's `current_price` to its Simple Moving Average (SMA).
    -   `current_price > SMA`: Indicates a bullish trend.
    -   `current_price < SMA`: Indicates a bearish trend.
-   **Configuration:**
    -   `sma_period`: The lookback period (in number of data points) for calculating the SMA. A common value is 20.

#### b. Momentum Analysis: Relative Strength Index (RSI)

-   **Logic:** The RSI is a momentum oscillator that measures the speed and change of price movements.
    -   An RSI value **below** the `rsi_oversold_threshold` indicates the asset may be oversold and due for a price increase (bullish).
    -   An RSI value **above** the `rsi_overbought_threshold` indicates the asset may be overbought and due for a price correction (bearish).
-   **Configuration:**
    -   `rsi_period`: The lookback period for calculating the RSI. A common value is 14.
    -   `rsi_oversold_threshold`: The RSI level below which an asset is considered oversold (e.g., 30).
    -   `rsi_overbought_threshold`: The RSI level above which an asset is considered overbought (e.g., 70).

#### c. Momentum Analysis: Moving Average Convergence Divergence (MACD)

-   **Logic:** MACD is a trend-following momentum indicator that shows the relationship between two moving averages of a security’s price.
    -   A `MACD line` crossing **above** the `Signal line` is a bullish signal.
    -   A `MACD line` crossing **below** the `Signal line` is a bearish signal.
-   **Configuration:**
    -   `macd_fast_period`: The lookback period for the fast Exponential Moving Average (EMA). A common value is 12.
    -   `macd_slow_period`: The lookback period for the slow EMA. A common value is 26.
    -   `macd_signal_period`: The lookback period for the signal line EMA. A common value is 9.

#### d. Volatility Analysis: Bollinger Bands

-   **Logic:** Bollinger Bands are a volatility indicator composed of a middle band (an SMA) and two outer bands.
    -   Prices moving **below** the `Lower Band` may indicate an oversold condition (bullish).
    -   Prices moving **above** the `Upper Band` may indicate an overbought condition (bearish).
-   **Configuration:**
    -   `bollinger_period`: The lookback period for the middle band SMA. A common value is 20.
    -   `bollinger_std_dev`: The number of standard deviations for the outer bands. A common value is 2.

#### e. On-Chain Volume Confirmation

-   **Logic:** The technical signals are validated by looking at the net flow of whale transactions for that specific symbol. Due to limitations in the free Whale Alert API, the bot now fetches all available transactions and filters them locally by symbol.
    -   A bullish technical setup (e.g., Price > SMA, RSI < 30, MACD crossover) is confirmed if there is a net inflow of the asset to private wallets (accumulation).
    -   A bearish technical setup (e.g., Price < SMA, RSI > 70, MACD crossunder) is confirmed if there is a net inflow of the asset to exchange wallets (potential sell-off).

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
