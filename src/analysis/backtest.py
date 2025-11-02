import pandas as pd
import sys
import os
from datetime import timedelta

# Add the project root to the Python path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..')))

from src.config import app_config
from src.database import get_db_connection
from src.analysis.signal_engine import generate_signal
from src.analysis.technical_indicators import calculate_rsi, calculate_sma
from src.logger import log

# --- Backtesting Configuration ---
INITIAL_CAPITAL = app_config.get('settings', {}).get('paper_trading_initial_capital', 10000.0)
TRADE_RISK_PERCENTAGE = app_config.get('settings', {}).get('trade_risk_percentage', 0.01)
STOP_LOSS_PERCENTAGE = app_config.get('settings', {}).get('stop_loss_percentage', 0.02)
TAKE_PROFIT_PERCENTAGE = app_config.get('settings', {}).get('take_profit_percentage', 0.05)
MAX_CONCURRENT_POSITIONS = app_config.get('settings', {}).get('max_concurrent_positions', 3)
SMA_PERIOD = app_config.get('settings', {}).get('sma_period', 20)
RSI_PERIOD = app_config.get('settings', {}).get('rsi_period', 14)

def load_historical_data():
    """Loads all historical price and whale data from the database."""
    log.info("Loading all historical data...")
    conn = get_db_connection()
    
    prices_df = pd.read_sql_query("SELECT * FROM market_prices ORDER BY timestamp ASC", conn)
    whales_df = pd.read_sql_query("SELECT * FROM whale_transactions ORDER BY timestamp ASC", conn)
    
    conn.close()

    prices_df['timestamp'] = pd.to_datetime(prices_df['timestamp'])
    whales_df['timestamp'] = pd.to_datetime(whales_df['timestamp'], unit='s')

    return prices_df, whales_df

class MockBinanceTrader:
    """A mock trader to simulate portfolio management during the backtest."""
    def __init__(self, initial_capital):
        self.capital = initial_capital
        self.positions = {}
        self.trade_history = []

    def get_open_positions(self):
        return list(self.positions.values())

    def get_account_balance(self):
        total_value = self.capital
        for symbol, pos in self.positions.items():
            if 'current_price' in pos:
                total_value += pos['quantity'] * pos['current_price']
        return {'total_usd': total_value}

    def place_order(self, symbol, side, quantity, price):
        if side == 'BUY':
            cost = quantity * price
            if self.capital >= cost:
                self.capital -= cost
                if symbol in self.positions:
                    existing_pos = self.positions[symbol]
                    new_quantity = existing_pos['quantity'] + quantity
                    new_entry_price = ((existing_pos['entry_price'] * existing_pos['quantity']) + cost) / new_quantity
                    existing_pos['quantity'] = new_quantity
                    existing_pos['entry_price'] = new_entry_price
                else:
                    self.positions[symbol] = {'symbol': symbol, 'quantity': quantity, 'entry_price': price}
                log.info(f"EXECUTE BUY: {quantity:.4f} {symbol} at ${price:,.2f}")
        
        elif side == 'SELL':
            if symbol in self.positions:
                pos = self.positions.pop(symbol)
                revenue = pos['quantity'] * price
                pnl = (price - pos['entry_price']) * pos['quantity']
                self.capital += revenue
                self.trade_history.append({'symbol': symbol, 'pnl': pnl})
                log.info(f"EXECUTE SELL: {pos['quantity']:.4f} {symbol} at ${price:,.2f}, PnL: ${pnl:,.2f}")

    def update_positions_price(self, symbol, price):
        if symbol in self.positions:
            self.positions[symbol]['current_price'] = price

def run_backtest(watch_list, prices_df, whales_df):
    """Runs the backtesting simulation."""
    log.info("\n--- Starting Backtest Simulation ---")
    trader = MockBinanceTrader(INITIAL_CAPITAL)

    for symbol_short in watch_list:
        symbol = symbol_short if symbol_short.endswith('USDT') else f"{symbol_short}USDT"
        symbol_prices = prices_df[prices_df['symbol'] == symbol].copy()
        if len(symbol_prices) < max(SMA_PERIOD, RSI_PERIOD):
            log.warning(f"Not enough price data for {symbol} to generate signals. Skipping.")
            continue

        # Pre-calculate indicators for the whole series
        symbol_prices['sma'] = symbol_prices['price'].rolling(window=SMA_PERIOD).mean()
        
        # RSI calculation needs a list, so we'll do it iteratively
        price_list = symbol_prices['price'].tolist()
        rsi_values = [calculate_rsi(price_list[:i+1], RSI_PERIOD) for i in range(len(price_list))]
        symbol_prices['rsi'] = rsi_values

        symbol_prices = symbol_prices.dropna()

        for i, row in symbol_prices.iterrows():
            current_price = row['price']
            trader.update_positions_price(symbol, current_price)

            # --- Stop-Loss / Take-Profit ---
            open_positions = trader.get_open_positions()
            for pos in open_positions:
                if pos['symbol'] == symbol:
                    pnl_percentage = (current_price - pos['entry_price']) / pos['entry_price']
                    if pnl_percentage <= -STOP_LOSS_PERCENTAGE:
                        log.info(f"STOP-LOSS triggered for {symbol}")
                        trader.place_order(symbol, 'SELL', pos['quantity'], current_price)
                    elif pnl_percentage >= TAKE_PROFIT_PERCENTAGE:
                        log.info(f"TAKE-PROFIT triggered for {symbol}")
                        trader.place_order(symbol, 'SELL', pos['quantity'], current_price)

            # --- Generate Signal ---
            market_data = {'current_price': current_price, 'sma': row['sma'], 'rsi': row['rsi']}
            
            start_time = row['timestamp'] - timedelta(minutes=15)
            end_time = row['timestamp']
            relevant_whales = whales_df[(whales_df['timestamp'] >= start_time) & (whales_df['timestamp'] <= end_time)]
            whale_transactions = relevant_whales.to_dict('records') if not relevant_whales.empty else []
            
            signal_data = generate_signal(symbol, whale_transactions, market_data)
            signal = signal_data.get('signal')

            # --- Trading Logic ---
            if signal == 'BUY' and len(trader.get_open_positions()) < MAX_CONCURRENT_POSITIONS and symbol not in trader.positions:
                capital_to_risk = trader.get_account_balance()['total_usd'] * TRADE_RISK_PERCENTAGE
                quantity_to_buy = capital_to_risk / current_price
                trader.place_order(symbol, 'BUY', quantity_to_buy, current_price)
            
            elif signal == 'SELL' and symbol in trader.positions:
                trader.place_order(symbol, 'SELL', trader.positions[symbol]['quantity'], current_price)

    print_results(trader)

def print_results(trader):
    """Prints the final results of the backtest."""
    final_value = trader.get_account_balance()['total_usd']
    total_pnl = final_value - INITIAL_CAPITAL
    total_pnl_percent = (total_pnl / INITIAL_CAPITAL) * 100
    
    num_trades = len(trader.trade_history)
    wins = sum(1 for trade in trader.trade_history if trade['pnl'] > 0)
    losses = num_trades - wins
    win_rate = (wins / num_trades * 100) if num_trades > 0 else 0

    log.info("\n--- Backtest Results ---")
    log.info(f"Initial Capital: ${INITIAL_CAPITAL:,.2f}")
    log.info(f"Final Portfolio Value: ${final_value:,.2f}")
    log.info(f"Total Profit/Loss: ${total_pnl:,.2f} ({total_pnl_percent:.2f}%)")
    log.info(f"Total Trades: {num_trades}")
    log.info(f"Wins: {wins}, Losses: {losses}")
    log.info(f"Win Rate: {win_rate:.2f}%")
    log.info("------------------------")

if __name__ == '__main__':
    prices_df, whales_df = load_historical_data()
    if prices_df.empty:
        log.info("No data found in market_prices table. Exiting backtest.")
    else:
        watch_list = prices_df['symbol'].unique().tolist()
        run_backtest(watch_list, prices_df, whales_df)
