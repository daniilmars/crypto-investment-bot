import argparse
import pandas as pd
import sys
import os

# Add the project root to the Python path
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from src.config import app_config
from src.database import get_db_connection
from src.analysis.signal_engine import generate_signal
from src.analysis.technical_indicators import calculate_rsi
from src.logger import log

# --- Constants ---
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
            prices_df['timestamp'] = prices_df['timestamp'].dt.tz_localize('UTC')
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
    def __init__(self, params):
        self.params = params

    def generate_signals(self, symbol, historical_prices, whale_transactions, current_price, stablecoin_data, velocity_data):
        if len(historical_prices) < max(self.params.sma_period, self.params.rsi_period):
            log.debug(f"[{symbol}] HOLD: Not enough historical price data ({len(historical_prices)} points).")
            return {'signal': 'HOLD'}
            
        price_list = historical_prices['price'].tolist()
        sma = historical_prices['price'].rolling(window=self.params.sma_period).mean().iloc[-1]
        rsi = calculate_rsi(price_list, period=self.params.rsi_period)
        market_data = {'current_price': current_price, 'sma': sma, 'rsi': rsi}

        return generate_signal(
            symbol=symbol,
            whale_transactions=whale_transactions,
            market_data=market_data,
            high_interest_wallets=self.params.high_interest_wallets,
            stablecoin_data=stablecoin_data,
            stablecoin_threshold=self.params.stablecoin_inflow_threshold_usd,
            velocity_data=velocity_data,
            velocity_threshold_multiplier=self.params.transaction_velocity_threshold_multiplier,
            rsi_overbought_threshold=self.params.rsi_overbought_threshold,
            rsi_oversold_threshold=self.params.rsi_oversold_threshold
        )

class Backtester:
    """Orchestrates the backtesting simulation."""
    def __init__(self, watch_list, prices_df, whales_df, params):
        self.watch_list = watch_list
        self.prices_df = prices_df
        self.whales_df = whales_df
        self.params = params
        self.portfolio = Portfolio(params.initial_capital)
        self.strategy = Strategy(params)

    def run(self):
        log.info("\n--- Starting Backtest Simulation ---")
        # Ensure whale timestamps are timezone-aware for proper comparison
        self.whales_df['timestamp'] = self.whales_df['timestamp'].dt.tz_localize('UTC')
        
        all_prices = self.prices_df.pivot(index='timestamp', columns='symbol', values='price').ffill()
        
        for timestamp, prices in all_prices.iterrows():
            current_prices = prices.to_dict()
            self.portfolio.record_equity(timestamp, current_prices)
            self.check_for_exits(current_prices, timestamp)
            
            if len(self.portfolio.positions) < self.params.max_concurrent_positions:
                self.check_for_entries(current_prices, timestamp)
        self.print_results()

    def check_for_exits(self, current_prices, timestamp):
        for symbol in list(self.portfolio.positions.keys()):
            pos = self.portfolio.positions[symbol]
            current_price = current_prices.get(symbol)
            if current_price is None: continue
            pnl_percentage = (current_price - pos['entry_price']) / pos['entry_price']
            if pnl_percentage <= -self.params.stop_loss_percentage or pnl_percentage >= self.params.take_profit_percentage:
                log.info(f"[{timestamp}] EXIT '{symbol}': PnL% {pnl_percentage:.2%} triggered exit.")
                self.portfolio.place_order(symbol, 'SELL', pos['quantity'], current_price, timestamp)

    def check_for_entries(self, current_prices, timestamp):
        # --- Calculate point-in-time on-chain metrics ---
        # Consider whale transactions from the last hour
        one_hour_ago = timestamp - pd.Timedelta(hours=1)
        recent_whales_df = self.whales_df[self.whales_df['timestamp'].between(one_hour_ago, timestamp)]
        recent_whales = recent_whales_df.to_dict('records')

        # Calculate stablecoin flows from these recent transactions
        stablecoin_inflow = sum(
            tx['amount_usd'] for tx in recent_whales 
            if tx['symbol'] in self.params.stablecoins_to_monitor and tx['to_owner_type'] == 'exchange'
        )
        stablecoin_data = {'stablecoin_inflow_usd': stablecoin_inflow}

        for symbol in self.watch_list:
            if symbol in self.portfolio.positions: continue
            current_price = current_prices.get(symbol)
            if pd.isna(current_price): continue

            # --- Calculate Transaction Velocity ---
            baseline_start = timestamp - pd.Timedelta(hours=self.params.transaction_velocity_baseline_hours)
            baseline_whales_df = self.whales_df[self.whales_df['timestamp'].between(baseline_start, timestamp)]
            
            current_count = len(recent_whales_df[recent_whales_df['symbol'] == symbol.lower()])
            baseline_count = len(baseline_whales_df[baseline_whales_df['symbol'] == symbol.lower()])
            baseline_avg = baseline_count / self.params.transaction_velocity_baseline_hours if self.params.transaction_velocity_baseline_hours > 0 else 0
            velocity_data = {'current_count': current_count, 'baseline_avg': baseline_avg}

            # --- Generate Signal ---
            historical_prices = self.prices_df[(self.prices_df['symbol'] == symbol) & (self.prices_df['timestamp'] <= timestamp)]
            
            signal_data = self.strategy.generate_signals(symbol, historical_prices, recent_whales, current_price, stablecoin_data, velocity_data)
            
            if signal_data.get('signal') == 'BUY':
                log.info(f"[{timestamp}] ENTRY '{symbol}': BUY signal received. Reason: {signal_data.get('reason')}")
                capital_to_risk = self.portfolio.cash * self.params.trade_risk_percentage
                quantity_to_buy = capital_to_risk / current_price
                self.portfolio.place_order(symbol, 'BUY', quantity_to_buy, current_price, timestamp)
            elif signal_data.get('signal') not in ['HOLD', 'VOLATILITY_WARNING']:
                 log.debug(f"[{timestamp}] '{symbol}': {signal_data.get('signal')} signal received. Reason: {signal_data.get('reason')}")

    def print_results(self):
        log.info("\n--- Backtest Results ---")
        final_value = self.portfolio.equity_curve[-1]['value']
        total_pnl = final_value - self.params.initial_capital
        total_pnl_percent = (total_pnl / self.params.initial_capital) * 100
        num_trades = len(self.portfolio.trade_history)
        if num_trades == 0:
            log.info("No trades were executed.")
            print(f"Final PnL: 0.00")
            return
        wins = sum(1 for t in self.portfolio.trade_history if t['pnl'] > 0)
        win_rate = (wins / num_trades * 100) if num_trades > 0 else 0
        log.info(f"Final Portfolio Value: ${final_value:,.2f}")
        log.info(f"Total PnL: ${total_pnl:,.2f} ({total_pnl_percent:.2f}%)")
        log.info(f"Total Trades: {num_trades}")
        log.info(f"Win Rate: {win_rate:.2f}%")
        # Standardized output for parsing
        print(f"Final PnL: {total_pnl:.2f}")

def main():
    parser = argparse.ArgumentParser(description="Run a backtest of the crypto trading bot.")
    # Portfolio & Risk
    parser.add_argument('--initial-capital', type=float, default=app_config.get('settings', {}).get('paper_trading_initial_capital', 10000.0))
    parser.add_argument('--trade-risk-percentage', type=float, default=app_config.get('settings', {}).get('trade_risk_percentage', 0.01))
    parser.add_argument('--stop-loss-percentage', type=float, default=app_config.get('settings', {}).get('stop_loss_percentage', 0.02))
    parser.add_argument('--take-profit-percentage', type=float, default=app_config.get('settings', {}).get('take_profit_percentage', 0.05))
    parser.add_argument('--max-concurrent-positions', type=int, default=app_config.get('settings', {}).get('max_concurrent_positions', 3))
    
    # Technical Indicators
    parser.add_argument('--sma-period', type=int, default=app_config.get('settings', {}).get('sma_period', 20))
    parser.add_argument('--rsi-period', type=int, default=app_config.get('settings', {}).get('rsi_period', 14))
    parser.add_argument('--rsi-overbought-threshold', type=int, default=app_config.get('settings', {}).get('rsi_overbought_threshold', 70))
    parser.add_argument('--rsi-oversold-threshold', type=int, default=app_config.get('settings', {}).get('rsi_oversold_threshold', 30))

    # On-Chain & Anomaly Detection
    parser.add_argument('--stablecoin-inflow-threshold-usd', type=float, default=app_config.get('settings', {}).get('stablecoin_inflow_threshold_usd', 100000000))
    parser.add_argument('--transaction-velocity-baseline-hours', type=int, default=app_config.get('settings', {}).get('transaction_velocity_baseline_hours', 24))
    parser.add_argument('--transaction-velocity-threshold-multiplier', type=float, default=app_config.get('settings', {}).get('transaction_velocity_threshold_multiplier', 5.0))
    
    # Watch Lists (as comma-separated strings)
    parser.add_argument('--high-interest-wallets', type=str, default=",".join(app_config.get('settings', {}).get('high_interest_wallets', [])))
    parser.add_argument('--stablecoins-to-monitor', type=str, default=",".join(app_config.get('settings', {}).get('stablecoins_to_monitor', [])))

    args = parser.parse_args()
    
    # Convert comma-separated strings to lists
    args.high_interest_wallets = args.high_interest_wallets.split(',') if args.high_interest_wallets else []
    args.stablecoins_to_monitor = args.stablecoins_to_monitor.split(',') if args.stablecoins_to_monitor else []

    prices, whales = DataLoader.load_historical_data()
    if prices.empty:
        log.info("No data found. Exiting backtest.")
    else:
        watchlist = prices['symbol'].unique().tolist()
        backtester = Backtester(watchlist, prices, whales, args)
        backtester.run()

if __name__ == '__main__':
    main()
