import pandas as pd
import numpy as np
from src.config import app_config
from src.database import get_db_connection
from src.analysis.signal_engine import generate_signal
from src.analysis.technical_indicators import calculate_rsi
from src.logger import log

# --- Configuration ---
INITIAL_CAPITAL = app_config.get('settings', {}).get('paper_trading_initial_capital', 10000.0)
TRADE_RISK_PERCENTAGE = app_config.get('settings', {}).get('trade_risk_percentage', 0.01)
STOP_LOSS_PERCENTAGE = app_config.get('settings', {}).get('stop_loss_percentage', 0.02)
TAKE_PROFIT_PERCENTAGE = app_config.get('settings', {}).get('take_profit_percentage', 0.05)
MAX_CONCURRENT_POSITIONS = app_config.get('settings', {}).get('max_concurrent_positions', 3)
SMA_PERIOD = app_config.get('settings', {}).get('sma_period', 20)
RSI_PERIOD = app_config.get('settings', {}).get('rsi_period', 14)
FEE_RATE = 0.001

class DataLoader:
    """Handles loading of historical data."""
    @staticmethod
    def load_historical_data():
        log.info("Loading historical data...")
        conn = get_db_connection()
        prices_df = pd.read_sql_query("SELECT * FROM market_prices ORDER BY timestamp ASC", conn)
        whales_df = pd.read_sql_query("SELECT * FROM whale_transactions ORDER BY timestamp ASC", conn)
        conn.close()
        if not prices_df.empty:
            prices_df['timestamp'] = pd.to_datetime(prices_df['timestamp'])
        if not whales_df.empty:
            whales_df['timestamp'] = pd.to_datetime(whales_df['timestamp'], unit='s')
        log.info(f"Loaded {len(prices_df)} price records and {len(whales_df)} whale transactions.")
        return prices_df, whales_df

class Portfolio:
    """Manages portfolio state and performance tracking."""
    def __init__(self, initial_capital):
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
        if side == 'BUY' and self.cash >= quantity * price + fee:
            self.cash -= (quantity * price + fee)
            self.positions[symbol] = {'quantity': quantity, 'entry_price': price, 'entry_timestamp': timestamp}
        elif side == 'SELL' and symbol in self.positions:
            pos = self.positions.pop(symbol)
            revenue = pos['quantity'] * price
            pnl = (price - pos['entry_price']) * pos['quantity'] - fee
            self.cash += (revenue - fee)
            self.trade_history.append({'symbol': symbol, 'pnl': pnl})

    def record_equity(self, timestamp, current_prices):
        self.equity_curve.append({'timestamp': timestamp, 'value': self.get_total_value(current_prices)})

class Strategy:
    """Generates trading signals based on market data."""
    @staticmethod
    def generate_signals(symbol, historical_prices, whale_transactions, current_price):
        if len(historical_prices) < max(SMA_PERIOD, RSI_PERIOD):
            return {'signal': 'HOLD'}
        price_list = historical_prices['price'].tolist()
        sma = historical_prices['price'].rolling(window=SMA_PERIOD).mean().iloc[-1]
        rsi = calculate_rsi(price_list, period=RSI_PERIOD)
        market_data = {'current_price': current_price, 'sma': sma, 'rsi': rsi}
        return generate_signal(symbol, whale_transactions, market_data)

class Backtester:
    """Orchestrates the backtesting simulation."""
    def __init__(self, watch_list, prices_df, whales_df):
        self.watch_list = watch_list
        self.prices_df = prices_df
        self.whales_df = whales_df
        self.portfolio = Portfolio(INITIAL_CAPITAL)

    def run(self):
        log.info("\n--- Starting Backtest Simulation ---")
        all_prices = self.prices_df.pivot(index='timestamp', columns='symbol', values='price').ffill()
        for timestamp, prices in all_prices.iterrows():
            current_prices = prices.to_dict()
            self.portfolio.record_equity(timestamp, current_prices)
            self.check_for_exits(current_prices, timestamp)
            if len(self.portfolio.positions) < MAX_CONCURRENT_POSITIONS:
                self.check_for_entries(current_prices, timestamp)
        self.print_results()

    def check_for_exits(self, current_prices, timestamp):
        for symbol in list(self.portfolio.positions.keys()):
            pos = self.portfolio.positions[symbol]
            current_price = current_prices.get(symbol)
            if current_price is None: continue
            pnl_percentage = (current_price - pos['entry_price']) / pos['entry_price']
            if pnl_percentage <= -STOP_LOSS_PERCENTAGE or pnl_percentage >= TAKE_PROFIT_PERCENTAGE:
                self.portfolio.place_order(symbol, 'SELL', pos['quantity'], current_price, timestamp)

    def check_for_entries(self, current_prices, timestamp):
        for symbol in self.watch_list:
            if symbol in self.portfolio.positions: continue
            current_price = current_prices.get(symbol)
            if pd.isna(current_price): continue
            historical_prices = self.prices_df[(self.prices_df['symbol'] == symbol) & (self.prices_df['timestamp'] <= timestamp)]
            relevant_whales = self.whales_df[self.whales_df['timestamp'] <= timestamp]
            signal_data = Strategy.generate_signals(symbol, historical_prices, relevant_whales.to_dict('records'), current_price)
            if signal_data.get('signal') == 'BUY':
                capital_to_risk = self.portfolio.cash * TRADE_RISK_PERCENTAGE
                quantity_to_buy = capital_to_risk / current_price
                self.portfolio.place_order(symbol, 'BUY', quantity_to_buy, current_price, timestamp)

    def print_results(self):
        log.info("\n--- Backtest Results ---")
        final_value = self.portfolio.equity_curve[-1]['value']
        total_pnl = final_value - INITIAL_CAPITAL
        total_pnl_percent = (total_pnl / INITIAL_CAPITAL) * 100
        num_trades = len(self.portfolio.trade_history)
        if num_trades == 0:
            log.info("No trades were executed.")
            return
        wins = sum(1 for t in self.portfolio.trade_history if t['pnl'] > 0)
        win_rate = (wins / num_trades * 100) if num_trades > 0 else 0
        log.info(f"Final Portfolio Value: ${final_value:,.2f}")
        log.info(f"Total PnL: ${total_pnl:,.2f} ({total_pnl_percent:.2f}%)")
        log.info(f"Total Trades: {num_trades}")
        log.info(f"Win Rate: {win_rate:.2f}%")

if __name__ == '__main__':
    prices, whales = DataLoader.load_historical_data()
    if prices.empty:
        log.info("No data found. Exiting backtest.")
    else:
        watchlist = prices['symbol'].unique().tolist()
        backtester = Backtester(watchlist, prices, whales)
        backtester.run()
