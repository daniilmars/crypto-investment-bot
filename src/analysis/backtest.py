import pandas as pd
import numpy as np
import sys
import os
from datetime import timedelta

# Add the project root to the Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from src.config import app_config
from src.database import get_db_connection
from src.analysis.signal_engine import generate_signal
from src.analysis.technical_indicators import calculate_rsi, calculate_macd, calculate_bollinger_bands
from src.logger import log

# --- Backtesting Configuration ---
INITIAL_CAPITAL = app_config.get('settings', {}).get('paper_trading_initial_capital', 10000.0)
TRADE_RISK_PERCENTAGE = app_config.get('settings', {}).get('trade_risk_percentage', 0.01)
STOP_LOSS_PERCENTAGE = app_config.get('settings', {}).get('stop_loss_percentage', 0.02)
TAKE_PROFIT_PERCENTAGE = app_config.get('settings', {}).get('take_profit_percentage', 0.05)
MAX_CONCURRENT_POSITIONS = app_config.get('settings', {}).get('max_concurrent_positions', 3)
SMA_PERIOD = app_config.get('settings', {}).get('sma_period', 20)
RSI_PERIOD = app_config.get('settings', {}).get('rsi_period', 14)
FEE_RATE = 0.001  # Standard 0.1% trading fee

def load_historical_data():
    """Loads all historical price and whale data from the database."""
    log.info("Loading all historical data for backtest...")
    conn = get_db_connection()
    
    prices_df = pd.read_sql_query("SELECT * FROM market_prices ORDER BY timestamp ASC", conn)
    
    if prices_df.empty:
        conn.close()
        return pd.DataFrame(), pd.DataFrame()

    prices_df['timestamp'] = pd.to_datetime(prices_df['timestamp'])

    # Extract unique symbols from prices_df to filter whale transactions
    unique_symbols = prices_df['symbol'].unique().tolist()
    
    # Fetch and save relevant whale transactions using the updated get_whale_transactions
    from src.collectors.whale_alert import get_whale_transactions
    get_whale_transactions(symbols=unique_symbols)

    whales_df = pd.read_sql_query("SELECT * FROM whale_transactions ORDER BY timestamp ASC", conn)
    conn.close()

    whales_df['timestamp'] = pd.to_datetime(whales_df['timestamp'], unit='s')

    log.info(f"Loaded {len(prices_df)} price records and {len(whales_df)} whale transactions.")
    return prices_df, whales_df

class Portfolio:
    """Manages the state of the portfolio, including capital, positions, and performance tracking."""
    def __init__(self, initial_capital):
        self.initial_capital = initial_capital
        self.cash = initial_capital
        self.positions = {}
        self.trade_history = []
        self.equity_curve = []

    def get_total_value(self, current_prices):
        total_value = self.cash
        for symbol, pos in self.positions.items():
            total_value += pos['quantity'] * current_prices.get(symbol, pos['entry_price'])
        return total_value

    def place_order(self, symbol, side, quantity, price, timestamp):
        fee = quantity * price * FEE_RATE
        if side == 'BUY':
            cost = quantity * price
            if self.cash >= cost + fee:
                self.cash -= (cost + fee)
                if symbol in self.positions:
                    # Averaging down is not implemented to keep logic simple
                    log.warning(f"Attempted to buy {symbol} which is already in portfolio. This is not supported.")
                    return
                self.positions[symbol] = {'quantity': quantity, 'entry_price': price, 'entry_timestamp': timestamp}
                log.info(f"{timestamp} | BUY: {quantity:.4f} {symbol} at ${price:,.2f} | Cost: ${cost:,.2f} | Fee: ${fee:,.2f}")
        
        elif side == 'SELL':
            if symbol in self.positions:
                pos = self.positions.pop(symbol)
                revenue = pos['quantity'] * price
                pnl = (price - pos['entry_price']) * pos['quantity'] - fee
                self.cash += (revenue - fee)
                self.trade_history.append({
                    'symbol': symbol, 'pnl': pnl, 'entry_price': pos['entry_price'], 'exit_price': price,
                    'entry_time': pos['entry_timestamp'], 'exit_time': timestamp
                })
                log.info(f"{timestamp} | SELL: {pos['quantity']:.4f} {symbol} at ${price:,.2f} | PnL: ${pnl:,.2f} | Fee: ${fee:,.2f}")

    def record_equity(self, timestamp, current_prices):
        self.equity_curve.append({
            'timestamp': timestamp,
            'value': self.get_total_value(current_prices)
        })

class Backtester:
    """Orchestrates the backtesting simulation."""
    def __init__(self, watch_list, prices_df, whales_df):
        self.watch_list = watch_list
        self.prices_df = prices_df
        self.whales_df = whales_df
        self.portfolio = Portfolio(INITIAL_CAPITAL)

    def run(self):
        log.info("\n--- Starting Backtest Simulation ---")
        
        # Combine all price data into a single timeline
        all_prices = self.prices_df.pivot(index='timestamp', columns='symbol', values='price')
        all_prices = all_prices.ffill() # Forward-fill missing values
        
        for timestamp, prices in all_prices.iterrows():
            current_prices = prices.to_dict()
            
            # --- 1. Update Portfolio Value and Check for Exits ---
            self.portfolio.record_equity(timestamp, current_prices)
            self.check_for_exits(current_prices, timestamp)

            # --- 2. Generate Signals and Check for Entries ---
            if len(self.portfolio.positions) < MAX_CONCURRENT_POSITIONS:
                self.check_for_entries(current_prices, timestamp)

        self.print_results()

    def check_for_exits(self, current_prices, timestamp):
        for symbol in list(self.portfolio.positions.keys()):
            pos = self.portfolio.positions[symbol]
            current_price = current_prices.get(symbol)
            if current_price is None: continue

            pnl_percentage = (current_price - pos['entry_price']) / pos['entry_price']
            
            if pnl_percentage <= -STOP_LOSS_PERCENTAGE:
                log.info(f"STOP-LOSS triggered for {symbol} at {pnl_percentage:.2%}")
                self.portfolio.place_order(symbol, 'SELL', pos['quantity'], current_price, timestamp)
            elif pnl_percentage >= TAKE_PROFIT_PERCENTAGE:
                log.info(f"TAKE-PROFIT triggered for {symbol} at {pnl_percentage:.2%}")
                self.portfolio.place_order(symbol, 'SELL', pos['quantity'], current_price, timestamp)

    def check_for_entries(self, current_prices, timestamp):
        for symbol in self.watch_list:
            if symbol in self.portfolio.positions: continue

            current_price = current_prices.get(symbol)
            if pd.isna(current_price): continue

            # Prepare data for signal generation
            historical_prices_df = self.prices_df[(self.prices_df['symbol'] == symbol) & (self.prices_df['timestamp'] <= timestamp)]
            if len(historical_prices_df) < max(SMA_PERIOD, RSI_PERIOD):
                continue

            # Filter whale transactions relevant to the current timestamp
            current_unix_timestamp = int(timestamp.timestamp())
            current_timestamp_dt = pd.to_datetime(current_unix_timestamp, unit='s')
            relevant_whales = self.whales_df[self.whales_df['timestamp'] <= current_timestamp_dt]
            whale_transactions = relevant_whales.to_dict('records')

            # Calculate technical indicators
            price_list = historical_prices_df['price'].tolist()
            sma = historical_prices_df['price'].rolling(window=SMA_PERIOD).mean().iloc[-1]
            rsi = calculate_rsi(price_list, period=RSI_PERIOD)
            macd = calculate_macd(price_list)
            bollinger_bands = calculate_bollinger_bands(price_list)

            market_data = {
                'current_price': current_price,
                'sma': sma,
                'rsi': rsi,
                'macd': macd,
                'bollinger_bands': bollinger_bands
            }

            signal_data = generate_signal(symbol, whale_transactions, market_data)
            
            if signal_data.get('signal') == 'BUY':
                log.info(f"[{timestamp}] BUY signal generated for {symbol}. Reason: {signal_data.get('reason')}")
                capital_to_risk = self.portfolio.cash * TRADE_RISK_PERCENTAGE
                quantity_to_buy = capital_to_risk / current_price
                self.portfolio.place_order(symbol, 'BUY', quantity_to_buy, current_price, timestamp)
            # else:
                # log.debug(f"[{timestamp}] HOLD signal for {symbol}. Reason: {signal_data.get('reason')}")

    def print_results(self):
        log.info("\n--- Backtest Results ---")
        
        final_value = self.portfolio.equity_curve[-1]['value']
        total_pnl = final_value - self.portfolio.initial_capital
        total_pnl_percent = (total_pnl / self.portfolio.initial_capital) * 100
        
        num_trades = len(self.portfolio.trade_history)
        if num_trades == 0:
            log.info("No trades were executed.")
            return

        # --- Performance Metrics ---
        wins = sum(1 for t in self.portfolio.trade_history if t['pnl'] > 0)
        losses = num_trades - wins
        win_rate = (wins / num_trades * 100) if num_trades > 0 else 0
        
        gross_profit = sum(t['pnl'] for t in self.portfolio.trade_history if t['pnl'] > 0)
        gross_loss = abs(sum(t['pnl'] for t in self.portfolio.trade_history if t['pnl'] < 0))
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf')

        equity_df = pd.DataFrame(self.portfolio.equity_curve).set_index('timestamp')
        equity_df['returns'] = equity_df['value'].pct_change().fillna(0)
        
        # Sharpe Ratio (assuming 0 risk-free rate)
        sharpe_ratio = (equity_df['returns'].mean() / equity_df['returns'].std()) * np.sqrt(252) # Annualized
        
        # Max Drawdown
        rolling_max = equity_df['value'].cummax()
        drawdown = (equity_df['value'] - rolling_max) / rolling_max
        max_drawdown = abs(drawdown.min()) * 100

        log.info(f"Initial Capital:      ${self.portfolio.initial_capital:,.2f}")
        log.info(f"Final Portfolio Value:  ${final_value:,.2f}")
        log.info(f"Total PnL:              ${total_pnl:,.2f} ({total_pnl_percent:.2f}%)")
        log.info("-" * 30)
        log.info(f"Total Trades:           {num_trades}")
        log.info(f"Win Rate:               {win_rate:.2f}%")
        log.info(f"Profit Factor:          {profit_factor:.2f}")
        log.info(f"Sharpe Ratio (Ann.):    {sharpe_ratio:.2f}")
        log.info(f"Max Drawdown:           {max_drawdown:.2f}%")
        log.info("-" * 30)

if __name__ == '__main__':
    prices_df, whales_df = load_historical_data()
    if prices_df.empty:
        log.info("No data found in market_prices table. Exiting backtest.")
    else:
        watch_list = prices_df['symbol'].unique().tolist()
        backtester = Backtester(watch_list, prices_df, whales_df)
        backtester.run()
